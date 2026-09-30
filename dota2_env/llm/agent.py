"""What the per-second channel of an LLM agent reads and what its reply means: prompt text in, one action per line out.

Pure functions, no threads and no HTTP, so a test can drive them against a hand-built world state.
The wording lives in the Jinja templates in prompts/ (act_* here, think_* and review_* in think.py, around the
partials every system prompt shares); this module only hands them data. A reply of any channel is read a line at a
time while it streams in (read_line), and each step is resolved on the frame it goes out on (resolve), because
legality and unit rows belong to that frame, not to the one the model saw.
"""

import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import jinja2
import numpy as np

from dota2_env import actions
from dota2_env.bridge.constants import RUNE_BOUNTY, RUNE_WATER, TEAM_RADIANT
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import display_name, hero_text, item_text, load_records
from dota2_env.llm.config import AgentConfig, MatchConfig, Position
from dota2_env.llm.memory import Call, Directive
from dota2_env.map_features import MapEvent, load_events, read_events
from dota2_env.observation import N_TALENTS, TALENT_LEVELS
from dota2_env.text import LANES, RUNE_TYPES, TEAMS, clock, event_name, map_reference, spot_name, unit_name

CHAT_LIMIT = 80  # all-chat is a taunt channel, not a place to think out loud
REASON_LIMIT = 200
INTENT_LIMIT = 40  # an intent is shown in every turn after it, to its own channel and to the teammates'
CALL_LIMIT = 40
PLAN_LIMIT = 150
NOTE_LIMIT = 60
LESSON_LIMIT = 80
TICKS_PER_GAME_SECOND = 30
WHOLE_TEAM = ('全队', 'ALL')  # what a CALL names instead of a nickname to reach every teammate

LineKind = Literal['step', 'reason', 'say', 'intent', 'call', 'ask', 'plan', 'goal', 'note', 'forget', 'lesson']
# Note (ruidu): every tag a reply line of any channel can start with, and how much of what follows it is kept; the
# runner only acts on the tags of the channel the reply came from. Anything else is a step.
TAGS: dict[str, int] = {
    'REASON': REASON_LIMIT,
    'SAY': CHAT_LIMIT,
    'INTENT': INTENT_LIMIT,
    'CALL': CALL_LIMIT + 20,  # the teammate's nickname comes first
    'ASK': REASON_LIMIT,
    'PLAN': PLAN_LIMIT,
    'GOAL': 40,
    'NOTE': NOTE_LIMIT,
    'FORGET': 10,
    'LESSON': LESSON_LIMIT + 10,  # the kind comes first
}

# Note (ruidu): the letters are the ones act_system.jinja writes the actions with. MOVE and CAST_DIRECTION tell a
# point from a direction only by how many numbers follow, so every form an action takes is listed; a MOVE with a
# point goes out as the env's MOVE_TO, which lets the client plan the whole way, and TP keeps its point as it is.
FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    'MOVE': (('x', 'y'), ('D',)),
    'ATTACK': (('N',),),
    'CAST': (('S',),),
    'CAST_TARGET': (('S', 'N'),),
    'CAST_DIRECTION': (('S', 'x', 'y'), ('S', 'D')),
    'NOOP': ((),),
    'STOP': ((),),
    'PICKUP_RUNE': (('R',),),
    'TP': (('x', 'y'),),
    'TALENT': (('T',),),
    'COURIER': ((),),
}
# x, y are the point of MOVE_TO and TP, or CAST_DIRECTION's aim
FIELD_KEYS = {'D': 'move', 'N': 'target', 'S': 'ability', 'R': 'rune', 'T': 'talent'}
RE_KIND_END = re.compile(r'[^A-Za-z_]')
RE_NUMBER = re.compile(r'[+-]?\d{1,9}(\.\d+)?')  # nine digits cover any coordinate and cannot overflow a float
RE_COMMA = re.compile('[,，]')  # a model writing Chinese often puts a full-width comma after a field

# Note (ruidu): the names are the client's own (DOTA_LaneSelection* in dota_english.txt and dota_schinese.txt).
POSITION_NAME: dict[Position, str] = {
    'safe': '1号位（优势路）',
    'mid': '2号位（中路）',
    'offlane': '3号位（劣势路）',
    'support': '4号位（辅助）',
    'hard_support': '5号位（纯辅助）',
}

# Note (ruidu): StrictUndefined makes a misspelt name an error instead of a sentence silently missing from the
# prompt. trim_blocks and lstrip_blocks drop the lines that hold only a tag, which leaves the newline ending a
# template's last line in the text; the prompts do not end in one, hence the removesuffix after each render.
PROMPTS = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
PROMPTS.filters['display_name'] = display_name
PROMPTS.filters['clock'] = clock
PROMPTS.filters['spot_name'] = spot_name
PROMPTS.filters['event_name'] = event_name
PROMPTS.filters['unit_name'] = unit_name
PROMPTS.globals['positions'] = POSITION_NAME
# Note (ruidu): bound once so an edit to act_user.jinja while a match runs cannot reach decide(), where a half-written
# template would end the match; the system templates are looked up per call instead, see hero_context().
USER = PROMPTS.get_template('act_user.jinja')


@dataclass(frozen=True, kw_only=True)
class Note:
    """One line of the history a user message ends with: a step that went out, or a reply that brought none."""

    kind: Literal['issued', 'rejected', 'undelivered', 'unusable']
    dota_time: float
    position: tuple[float, float] | None = None  # where the hero stood as the step went out
    step: str = ''  # the step, the line as the model wrote it
    error: str | None = None


def hero_context(agent: AgentConfig, match: MatchConfig, team_id: int) -> dict[str, object]:
    """What the partials every system prompt includes need: the agent, its side, the map and the hero's skills."""
    radiant = team_id == TEAM_RADIANT
    events: dict[str, list[MapEvent]] = {}
    for event in load_events(match.mode):
        events.setdefault(event.kind, []).append(event)
    return {
        'agent': agent,
        'match': match,
        'side': TEAMS[team_id]['zh'],
        'safe_lane': LANES['bottom' if radiant else 'top']['zh'],
        'off_lane': LANES['top' if radiant else 'bottom']['zh'],
        'map_reference': map_reference(),
        'skills': hero_text(agent.hero),
        'talents': [display_name(name) for name in load_records('heroes')[agent.hero]['talents'][:N_TALENTS]],
        'talent_levels': TALENT_LEVELS,
        'events': events,
        'event_values': read_events()['values'],
        'power_runes': [names['zh'] for rune, names in RUNE_TYPES.items() if rune not in (RUNE_BOUNTY, RUNE_WATER)],
    }


def act_system_prompt(agent: AgentConfig, match: MatchConfig, team_id: int) -> str:
    """Fixed for the whole match, so gateways that cache prompt prefixes actually get a hit."""
    # Note (ruidu): looked up on every call, not once at import, so scripts/prompt_debugger.py renders an edited
    # template without a restart; jinja re-reads a file only when its mtime changed, and a match calls this once per
    # agent at start.
    return (
        PROMPTS.get_template('act_system.jinja')
        .render(
            **hero_context(agent, match, team_id),
            tp=load_records('items')[actions.TP_SCROLL]['values'],
            frame=match.ticks_per_observation / TICKS_PER_GAME_SECOND,
            chat_limit=CHAT_LIMIT,
            intent_limit=INTENT_LIMIT,
            call_limit=CALL_LIMIT,
        )
        .removesuffix('\n')
    )


def act_user_prompt(
    state: str,
    items: list[str],
    teammates: list[tuple[AgentConfig, CMsgBotWorldState.Unit | None, tuple[float, str] | None]],
    history: Sequence[Note],
    directive: Directive | None = None,
    dota_time: float = 0.0,
    goal_distance: float | None = None,
    calls: Sequence[tuple[str, Call]] = (),
    intent: tuple[float, str] | None = None,
) -> str:
    """One turn's message: what the held items do, the long think's plan, the hero's state and how far it is from
    the plan's goal, its teammates and their intents, the calls to it, its own intent and its last commands.

    Items the data does not know (a newer client) are left out; history is oldest first, empty until a reply.
    teammates carry each one's INTENT and since when; calls carry the caller's nickname.
    """
    known = load_records('items')
    return USER.render(
        items=[item_text(name) for name in dict.fromkeys(items) if name in known],
        directive=directive,
        state=state,
        dota_time=dota_time,
        goal_distance=goal_distance,
        teammates=teammates,
        calls=calls,
        intent=intent,
        history=history,
    ).removesuffix('\n')


def read_line(line: str) -> tuple[LineKind | None, str]:
    """What one line of a reply is: a step (the line itself), or a tag of TAGS and the text after it.

    None for a blank line or a markdown fence. Nothing is validated here: a step is only checked
    against the frame it goes out on, by resolve().
    """
    line = line.strip()
    if not line or line.startswith('```'):
        return None, ''
    kind = RE_KIND_END.split(line, maxsplit=1)[0]
    limit = TAGS.get(kind.upper())
    if limit is None:
        return 'step', line
    # Note (ruidu): what follows a tag is mostly Chinese, so the comma after it often comes out full width.
    return kind.lower(), line[len(kind) :].lstrip(' ,，:：')[:limit]


def read_call(text: str, nicknames: list[str]) -> tuple[int | None, str]:
    """The row a CALL's text names first and the message after it; None for the whole team.

    A first field that names nobody is taken as part of a message to the whole team.
    """
    head, rest = [*RE_COMMA.split(text, maxsplit=1), ''][:2]
    head = head.strip()
    lowered = [nickname.lower() for nickname in nicknames]
    if head.lower() in lowered:
        return lowered.index(head.lower()), rest.strip()[:CALL_LIMIT]
    if head.upper() in WHOLE_TEAM:
        return None, rest.strip()[:CALL_LIMIT]
    return None, text.strip()[:CALL_LIMIT]


def read_step(step: str) -> tuple[str, dict[str, float]]:
    """The action type and named numbers of one step, ('CAST_TARGET', {'S': 1.0, 'N': 3.0}) for "CAST_TARGET, 1, 3".

    Raises ValueError, saying why, when the step is written like none of the FORMS.
    """
    head, *fields = step.split(',')
    kind = head.strip().upper()
    if kind not in FORMS:
        raise ValueError(f'{head.strip()!r} is not an action type')
    # Note (ruidu): a model copying a point off the map table brings the brackets along.
    fields = [field.strip(' ()') for field in fields]
    names = next((names for names in FORMS[kind] if len(names) == len(fields)), None)
    if names is None:
        raise ValueError(f'{kind} is written ' + ' or '.join(', '.join((kind, *names)) for names in FORMS[kind]))
    wrong = [field for field in fields if not RE_NUMBER.fullmatch(field)]
    if wrong:
        raise ValueError(f'{wrong[0]!r} is not a number')
    return kind, {name: float(field) for name, field in zip(names, fields, strict=True)}


def resolve(
    step: str,
    mask: dict[str, np.ndarray],
    prompt_handles: list[int],
    unit_handles: list[int],
    position: tuple[float, float] | None = None,
) -> tuple[actions.Action, str | None]:
    """One step of a plan, such as "ATTACK, 3", against the frame it is actually going out on.

    prompt_handles are the unit handles of the frame the model saw, unit_handles those of this frame.
    The table is re-sorted every frame, so a row number only survives the wait as a handle. MOVE, x, y hands
    the point to the client, which plans the way there; CAST_DIRECTION, S, x, y aims from where the hero
    stands on this frame (position), not where it stood.
    """
    noop = actions.NOOP_ACTION.copy()
    try:
        kind, values = read_step(step)
    except ValueError as e:
        return noop, str(e)
    if kind in ('MOVE', 'TP') and 'x' in values:
        point_type = 'MOVE_TO' if kind == 'MOVE' else kind
        return actions.parse_action({'type': point_type, 'point': (values['x'], values['y'])}, mask)
    numbers = {FIELD_KEYS[name]: int(value) for name, value in values.items() if name in FIELD_KEYS}
    if 'x' in values:
        if position is None:
            return noop, 'your hero is not on the map on this frame'
        angle = math.atan2(values['y'] - position[1], values['x'] - position[0]) / (2 * math.pi)
        numbers['move'] = round(angle * actions.N_MOVE_DIRECTIONS) % actions.N_MOVE_DIRECTIONS
    if 'target' in numbers:
        row = numbers['target']
        if not 0 <= row < len(prompt_handles):
            return noop, f'target {row} was not in the unit table'
        handle = prompt_handles[row]
        if handle not in unit_handles:
            return noop, f'target {row} is gone from the unit table'
        numbers['target'] = unit_handles.index(handle)
    return actions.parse_action({'type': kind, **numbers}, mask)
