"""The post-match review: each thinking agent looks back over the finished match and leaves lessons for the next.

The lessons go to match.memory_dir (memory.save_lessons), where a person can read and edit them, and the long
think's system prompt of the next match with the same hero carries the newest (think.think_system_prompt). The
wording lives in prompts/review_*.jinja.
"""

import logging
import time
from typing import Literal

from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.llm import agent
from dota2_env.llm.channel import Request, reply_record
from dota2_env.llm.config import AgentConfig, MatchConfig
from dota2_env.llm.memory import Directive, Event, Memo, save_lessons
from dota2_env.llm.think import LongThinks
from dota2_env.observation import find_hero

logger = logging.getLogger('dota2_env')

LESSONS_PER_REVIEW = 5
LESSON_KIND_LIMIT = 8
POLL_SECONDS = 0.05  # wall seconds between looks at the replies


def review_system_prompt(config: AgentConfig, match: MatchConfig, team_id: int) -> str:
    return (
        agent.PROMPTS.get_template('review_system.jinja')
        .render(
            **agent.hero_context(config, match, team_id),
            lesson_limit=agent.LESSON_LIMIT,
            lessons_per_review=LESSONS_PER_REVIEW,
        )
        .removesuffix('\n')
    )


def review_user_prompt(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_id: int,
    result: Literal['won', 'lost', 'undecided'],
    plans: list[Directive],
    events: list[Event],
    memos: list[Memo],
    lessons: list[dict[str, str]],
) -> str:
    """The finished match: its result, the hero's plans in order, what happened, and the lessons it already has."""
    return (
        agent.PROMPTS.get_template('review_user.jinja')
        .render(
            team_id=team_id,
            result=result,
            now=world_state.dota_time,
            player=next((player for player in world_state.players if player.player_id == player_id), None),
            hero=find_hero(world_state, team_id, player_id),
            plans=plans,
            events=events,
            memos=memos,
            lessons=lessons,
        )
        .removesuffix('\n')
    )


def read_lesson(text: str) -> tuple[str, str] | None:
    """The kind and the lesson of a LESSON line, "出装, 对面有斧王时先出黑皇杖"; None without a lesson."""
    kind, lesson = [*agent.RE_COMMA.split(text, maxsplit=1), ''][:2]
    if not lesson.strip():
        return None
    return kind.strip()[:LESSON_KIND_LIMIT], lesson.strip()[: agent.LESSON_LIMIT]


def review(
    thinks: LongThinks,
    world_state: CMsgBotWorldState,
    player_ids: list[int],
    winner: int | None,
    match_name: str,
    timeout: float,
) -> None:
    """Ask every thinking agent of the team what the match taught it; its lessons go to match.memory_dir.

    Waits up to timeout wall seconds in all: for the long thinks still in flight, then for the reviews. match_name
    is the evidence each lesson carries, the transcript's file name being the one to give.
    """
    match, team_id = thinks.match, thinks.team.team_id
    waiting = {row: thinker for row, thinker in enumerate(thinks.thinkers) if thinker is not None}
    if match.memory_dir is None or not waiting or not player_ids:
        return
    deadline = time.time() + timeout
    while any(thinker.channel.busy.is_set() for thinker in waiting.values()) and time.time() < deadline:
        time.sleep(POLL_SECONDS)
    thinks.take(world_state.dota_time)
    result = 'undecided' if winner is None else 'won' if winner == team_id else 'lost'
    for row, thinker in list(waiting.items()):
        if thinker.channel.busy.is_set():
            del waiting[row]  # its gateway is stuck, and a review would wait behind it
            continue
        config = thinks.team.agents[row]
        prompt = review_user_prompt(
            world_state,
            team_id,
            player_ids[row],
            result,
            thinker.plans,
            thinks.tracker.events,
            thinker.memos,
            thinker.lessons,
        )
        messages = [
            {'role': 'system', 'content': review_system_prompt(config, match, team_id)},
            {'role': 'user', 'content': prompt},
        ]
        thinker.channel.submit(
            Request(messages=messages, params=dict(thinker.channel.params), dota_time=world_state.dota_time)
        )
    while waiting and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        for row, thinker in list(waiting.items()):
            config = thinks.team.agents[row]
            for reading, event in thinker.channel.events():
                if isinstance(event, str):
                    kind, text = agent.read_line(event)
                    lesson = read_lesson(text) if kind == 'lesson' else None
                    if lesson is not None and len(reading.lessons) < LESSONS_PER_REVIEW:
                        kind_name, lesson_text = lesson
                        reading.lessons.append(
                            {
                                'kind': kind_name,
                                'text': lesson_text,
                                'position': config.position,
                                'match': match_name,
                                'source': 'review',
                            }
                        )
                    elif kind == 'reason':
                        reading.reason = text
                    continue
                thinks.record(
                    'review',
                    nickname=config.nickname,
                    dota_time=world_state.dota_time,
                    **reply_record(reading, event, match.log_prompts),
                    reply=event.text,
                    lessons=reading.lessons,
                    reason=reading.reason,
                    error=event.error,
                )
                if reading.lessons:
                    save_lessons(match.memory_dir, config.hero, reading.lessons)
                del waiting[row]
    for row in waiting:
        logger.warning(f'{thinks.team.agents[row].nickname} did not finish its review within {timeout:.0f}s')
