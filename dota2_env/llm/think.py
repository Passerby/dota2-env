"""The long thinks of an LLM team: what each one reads, when it is asked, and what its reply changes.

A long think sees the whole match every minute or so, and whenever something big happens, and writes the plan its
hero's per-second channel works to (runner.py, agent.py); the plan lands on the team's blackboard (memory.py). The
wording lives in prompts/think_*.jinja; like agent.py, this module only hands the templates data.
"""

import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from dota2_env.bridge.constants import UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.llm import agent
from dota2_env.llm.channel import Channel, Reading, Request, reply_record
from dota2_env.llm.config import AgentConfig, MatchConfig, TeamConfig
from dota2_env.llm.gateway import Gateway
from dota2_env.llm.memory import (
    NEAR_GOAL,
    Blackboard,
    Directive,
    Event,
    Memo,
    MemoryTracker,
    Progress,
    Sighting,
    Trigger,
    load_lessons,
)
from dota2_env.map_features import GameMode, upcoming
from dota2_env.observation import N_ABILITIES, find_hero, team_player_ids
from dota2_env.text import LANES, RE_TOWER, TEAMS, creep_waves

REMARKS = 8  # the per-second channel's last intents and reasons a long think reads
GOAL_REACHED = 400.0  # a hero this close to its goal has got there, which asks for the next plan
WAITING_TRIGGERS = 6  # the most a long think still in flight is told about when it is asked again

CastSlots = dict[int, dict[int, tuple[str, tuple[str, ...]]]]


@dataclass(frozen=True, kw_only=True)
class Mate:
    """One hero of the team as its long thinks see it."""

    config: AgentConfig
    player: CMsgBotWorldState.Player | None
    hero: CMsgBotWorldState.Unit | None
    items: list[str]
    directive: Directive | None
    intent: tuple[float, str] | None


@dataclass(frozen=True, kw_only=True)
class Foe:
    """An enemy hero: in sight on this frame, or where the team saw it last."""

    name: str  # unit name, empty until the team has seen the hero or its player's hero id
    player: CMsgBotWorldState.Player | None
    hero: CMsgBotWorldState.Unit | None
    seen: Sighting | None


def think_system_prompt(
    config: AgentConfig, match: MatchConfig, team: TeamConfig, lessons: list[dict[str, str]]
) -> str:
    """Fixed for the whole match like the per-second one, the lessons of earlier matches included."""
    return (
        agent.PROMPTS.get_template('think_system.jinja')
        .render(
            **agent.hero_context(config, match, team.team_id),
            team=team.agents,
            lessons=lessons,
            plan_limit=agent.PLAN_LIMIT,
            note_limit=agent.NOTE_LIMIT,
            call_limit=agent.CALL_LIMIT,
            notes_limit=match.notes_limit,
        )
        .removesuffix('\n')
    )


def think_user_prompt(
    world_state: CMsgBotWorldState,
    team: TeamConfig,
    row: int,
    player_ids: list[int],
    items: dict[int, list[str]],
    tracker: MemoryTracker,
    board: Blackboard,
    triggers: list[Trigger],
    since: float,
    events: list[Event],
    progress: Progress | None,
    remarks: Sequence[tuple[float, str, str]],
    memos: list[Memo],
    mode: GameMode,
) -> str:
    """One long think's message: why it was asked, the whole match, what happened since the last one at since,
    how the hero did with the last plan, and what it noted down.

    items are the inventory names by player id; remarks are the per-second channel's (dota_time, 'intent' or
    'reason', text) since the last long think, oldest first.
    """
    team_id = team.team_id
    players = {player.player_id: player for player in world_state.players}
    nicknames = [config.nickname for config in team.agents]
    mates = [
        Mate(
            config=config,
            player=players.get(player_id),
            hero=find_hero(world_state, team_id, player_id),
            items=items.get(player_id, []),
            directive=board.directives[index],
            intent=board.intents[index],
        )
        for index, (config, player_id) in enumerate(zip(team.agents, player_ids, strict=True))
    ]
    foes = []
    for player_id in team_player_ids(world_state, tracker.enemy)[: len(team.agents)]:
        hero = find_hero(world_state, tracker.enemy, player_id)
        foes.append(
            Foe(
                name=tracker.names.get(player_id, ''),
                player=players.get(player_id),
                hero=hero if hero is not None and hero.is_alive else None,
                seen=tracker.seen.get(player_id),
            )
        )
    towers: dict[int, list[tuple[tuple[int, str], str, float]]] = {team_id: [], tracker.enemy: []}
    for unit in world_state.units:
        match = RE_TOWER.fullmatch(unit.name)
        if unit.unit_type == UNIT_TYPE_TOWER and unit.is_alive and match is not None and unit.team_id in towers:
            place = (('top', 'mid', 'bot', None).index(match.group(2)), match.group(1))
            towers[unit.team_id].append((place, unit.name, unit.health / max(unit.health_max, 1)))
    sides = (team_id, tracker.enemy)
    ours, theirs = ([(name, health) for _, name, health in sorted(towers[side])] for side in sides)
    return (
        agent.PROMPTS.get_template('think_user.jinja')
        .render(
            team_id=team_id,
            row=row,
            nicknames=nicknames,
            triggers=triggers,
            since=since,
            now=world_state.dota_time,
            side=TEAMS[team_id]['zh'],
            score=[sum(player.kills for player in world_state.players if player.team_id == side) for side in sides],
            mates=mates,
            foes=foes,
            ours=ours,
            theirs=theirs,
            lanes={lane: names['zh'] for lane, names in LANES.items()},
            waves=creep_waves(world_state, team_id),
            coming=upcoming(mode, world_state.dota_time),
            events=events,
            calls=[(nicknames[call.row], call) for call in board.calls_for(row, world_state.dota_time)],
            previous=board.directives[row],
            progress=progress,
            near_goal=NEAR_GOAL,
            remarks=remarks,
            memos=memos,
        )
        .removesuffix('\n')
    )


def read_goal(text: str) -> tuple[float, float] | None:
    """The point of a GOAL line, "1180, -1216" or "(1180, -1216)"; None when it is not two numbers."""
    fields = [field.strip(' ()（）') for field in text.replace('，', ',').split(',')]
    if len(fields) != 2 or not all(agent.RE_NUMBER.fullmatch(field) for field in fields):
        return None
    return float(fields[0]), float(fields[1])


@dataclass(kw_only=True)
class Thinker:
    """One agent's long think: its channel and what only it remembers. Written by the main thread only."""

    channel: Channel
    lessons: list[dict[str, str]]  # what earlier matches left it; its system prompt carries them
    triggers: list[Trigger] = field(default_factory=list)  # what the next long think is asked about
    memos: list[Memo] = field(default_factory=list)  # its notes, oldest first
    remarks: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=REMARKS))
    plans: list[Directive] = field(default_factory=list)  # every plan of the match, for the review
    events_seen: int = 0  # how many of the tracker's events the last long think was shown
    goal_reached: bool = False  # the current plan's goal has asked for a long think already


class LongThinks:
    """The long thinks of one team, row by row: None for an agent whose config has no think.

    record writes a transcript line, call puts a CALL on the blackboard; both are the runner's.
    """

    def __init__(
        self,
        match: MatchConfig,
        team: TeamConfig,
        gateways: dict[str, Gateway],
        board: Blackboard,
        tracker: MemoryTracker,
        record: Callable[..., None],
        call: Callable[[int, str, float], None],
    ):
        self.match = match
        self.team = team
        self.board = board
        self.tracker = tracker
        self.record = record
        self.call = call
        self.thinkers: list[Thinker | None] = []
        for config in team.agents:
            if config.think is None:
                self.thinkers.append(None)
                continue
            memory_dir = match.memory_dir
            lessons = [] if memory_dir is None else load_lessons(memory_dir, config.hero, match.lessons_limit)
            channel = Channel(
                name=f'{config.nickname} (long think)',
                gateway=gateways[config.think.gateway],
                system=think_system_prompt(config, match, team, lessons),
                params=dict(config.think.params),
                interval=config.think.interval,
            )
            self.thinkers.append(Thinker(channel=channel, lessons=lessons))

    @property
    def channels(self) -> list[Channel]:
        return [thinker.channel for thinker in self.thinkers if thinker is not None]

    def wake(self, row: int, trigger: Trigger) -> None:
        """Ask row's long think to think again as soon as it is not already thinking."""
        thinker = self.thinkers[row]
        if thinker is not None:
            thinker.triggers = [*thinker.triggers, trigger][-WAITING_TRIGGERS:]

    def remark(self, row: int, dota_time: float, kind: str, text: str) -> None:
        """Keep what row's per-second channel wrote for its long think, once per change."""
        thinker = self.thinkers[row]
        if thinker is not None and (not thinker.remarks or thinker.remarks[-1][1:] != (kind, text)):
            thinker.remarks.append((dota_time, kind, text))

    def check_goals(self, dota_time: float) -> None:
        """Wake the long think of every hero that got to its plan's goal, once per goal."""
        for row, (thinker, directive) in enumerate(zip(self.thinkers, self.board.directives, strict=True)):
            if thinker is None or thinker.goal_reached or directive is None or directive.goal is None:
                continue
            spot = self.tracker.positions.get(self.tracker.ours[row]) if row < len(self.tracker.ours) else None
            if spot is not None and math.dist(spot, directive.goal) <= GOAL_REACHED:
                thinker.goal_reached = True
                self.wake(row, Trigger(kind='goal', dota_time=dota_time))

    def take(self, dota_time: float) -> None:
        """Take in what every long think has written since the last frame; a PLAN is the plan the moment it arrives."""
        for row, thinker in enumerate(self.thinkers):
            if thinker is None:
                continue
            for reading, event in thinker.channel.events():
                if isinstance(event, str):
                    self.read(row, thinker, reading, event, dota_time)
                    continue
                error = event.error
                if error is None and reading.directive is None:
                    error = 'reply carried no plan'
                directive = reading.directive
                self.record(
                    'think',
                    nickname=self.team.agents[row].nickname,
                    dota_time=dota_time,
                    triggers=[trigger.kind for trigger in reading.request.triggers],
                    **reply_record(reading, event, self.match.log_prompts),
                    reply=event.text,
                    plan=None if directive is None else directive.plan,
                    goal=None if directive is None else directive.goal,
                    notes=reading.notes,
                    forgotten=reading.forgotten,
                    calls=reading.calls,
                    reason=reading.reason,
                    problems=reading.problems,
                    error=error,
                )

    def read(self, row: int, thinker: Thinker, reading: Reading, line: str, dota_time: float) -> None:
        """Act on one line of a long think's reply as it streams in."""
        request = reading.request
        kind, text = agent.read_line(line)
        if kind == 'plan' and text and reading.directive is None:
            reading.directive = Directive(decided_at=request.dota_time, plan=text, goal=reading.goal)
            thinker.plans.append(reading.directive)
            self.direct(row, thinker, reading.directive)
        elif kind == 'goal':
            goal = read_goal(text)
            if goal is None:
                reading.problems.append(f'GOAL {text!r} is not a point x, y')
            elif reading.directive is None:
                reading.goal = goal
            else:
                reading.directive = thinker.plans[-1] = replace(reading.directive, goal=goal)
                self.direct(row, thinker, reading.directive)
        elif kind == 'note' and text and text not in [memo.text for memo in thinker.memos]:
            reading.notes.append(text)
            thinker.memos = [*thinker.memos, Memo(dota_time=request.dota_time, text=text)][-self.match.notes_limit :]
        elif kind == 'forget':
            number = int(text) if text.strip().isdigit() else 0
            if 1 <= number <= len(request.memos):
                memo = request.memos[number - 1]
                thinker.memos = [kept for kept in thinker.memos if kept is not memo]
                reading.forgotten.append(memo.text)
            else:
                reading.problems.append(f'FORGET {text!r} names none of the notes shown')
        elif kind == 'call' and text:
            reading.calls.append(text)
            self.call(row, text, dota_time)
        elif kind == 'reason':
            reading.reason = text

    def direct(self, row: int, thinker: Thinker, directive: Directive) -> None:
        """Make directive row's plan on the blackboard.

        Reaching its goal wakes the long think only for a hero that walks in from outside, not for one already there.
        """
        self.board.directives[row] = directive
        spot = self.tracker.positions.get(self.tracker.ours[row]) if row < len(self.tracker.ours) else None
        thinker.goal_reached = (
            directive.goal is not None and spot is not None and math.dist(spot, directive.goal) <= GOAL_REACHED
        )

    def ask(self, world_state: CMsgBotWorldState, player_ids: list[int], cast_slots: CastSlots) -> None:
        """Start the long think of every agent that is free and due: first thing, when woken, or on its interval."""
        dota_time = world_state.dota_time
        for row, thinker in enumerate(self.thinkers):
            if thinker is None or not thinker.channel.free(dota_time):
                continue
            channel = thinker.channel
            if channel.requested_dota_time == float('-inf'):
                triggers = [Trigger(kind='start', dota_time=dota_time)]
            elif thinker.triggers:
                triggers = thinker.triggers
            elif dota_time - channel.requested_dota_time >= channel.interval:
                triggers = [Trigger(kind='timer', dota_time=dota_time)]
            else:
                continue
            player_id = player_ids[row]
            progress = self.tracker.restart(row, find_hero(world_state, self.team.team_id, player_id), dota_time)
            items = {
                player: [
                    name for index, (name, _) in sorted(cast_slots.get(player, {}).items()) if index >= N_ABILITIES
                ]
                for player in player_ids
            }
            prompt = think_user_prompt(
                world_state,
                self.team,
                row,
                player_ids,
                items,
                self.tracker,
                self.board,
                triggers,
                channel.requested_dota_time if math.isfinite(channel.requested_dota_time) else dota_time,
                self.tracker.events[thinker.events_seen :],
                progress,
                list(thinker.remarks),
                thinker.memos,
                self.match.mode,
            )
            thinker.events_seen = len(self.tracker.events)
            thinker.triggers = []
            thinker.remarks.clear()
            channel.submit(
                Request(
                    messages=[{'role': 'system', 'content': channel.system}, {'role': 'user', 'content': prompt}],
                    params=dict(channel.params),
                    dota_time=dota_time,
                    triggers=triggers,
                    memos=list(thinker.memos),
                )
            )
