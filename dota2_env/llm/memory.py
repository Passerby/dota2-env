"""What an LLM team remembers: facts read off every world state frame, the blackboard its agents share, and the
lessons one match leaves for the next.

The tracker has to see every frame, oldest first (the env's info['world_states']): deaths and kills are counters
it compares frame to frame, and Roshan and couriers are per-frame events. Everything here is written by the
runner's main thread only.
"""

import math
import os
from dataclasses import dataclass, field
from typing import Literal

import yaml

from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT, UNIT_TYPE_HERO, UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import hero_names
from dota2_env.observation import team_player_ids

EventKind = Literal['death', 'tower', 'roshan', 'courier']
TriggerKind = Literal['start', 'timer', 'death', 'teammate_death', 'tower', 'roshan', 'call', 'ask', 'goal']

NEAR_GOAL = 800.0  # game seconds spent this close to the goal count as following the plan


@dataclass(frozen=True, kw_only=True)
class Event:
    """Something the team's world state showed happening.

    team is the side it happened to: the dead hero's, the fallen tower's, the courier's owner, or Roshan's killer's.
    """

    kind: EventKind
    dota_time: float
    team: int
    name: str = ''  # unit name of the dead hero or the fallen tower
    player_id: int | None = None  # the dead hero's player
    killer: str | None = None  # unit name of the hero credited with the kill, when exactly one was
    position: tuple[float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class Sighting:
    """An enemy hero the last time the team saw it."""

    dota_time: float
    name: str
    level: int
    health: float  # fraction of its maximum
    position: tuple[float, float]


@dataclass(kw_only=True)
class Progress:
    """How a hero has done since its long think last asked for a plan: the follow-through the next one reads."""

    since: float
    last_hits: int
    gold: int
    deaths: int = 0
    near_goal: float = 0.0  # game seconds within NEAR_GOAL of the goal
    closest: float | None = None  # the nearest it came to the goal
    moved: float = 0.0  # distance walked, which tells a hero stuck on a wall from one that is busy
    position: tuple[float, float] | None = None
    dota_time: float | None = None  # of the last frame counted


@dataclass(frozen=True, kw_only=True)
class Directive:
    """What a long think told its hero to do."""

    decided_at: float  # dota_time of the frame the long think saw
    plan: str
    goal: tuple[float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class Memo:
    """A note a long think keeps for itself, shown to it again every time until it forgets it."""

    dota_time: float
    text: str


@dataclass(frozen=True, kw_only=True)
class Call:
    """A shout on the blackboard: row called target, or the whole team when target is None."""

    row: int
    target: int | None
    text: str
    dota_time: float
    expires: float


@dataclass(frozen=True, kw_only=True)
class Trigger:
    """Why a long think was asked for: the event behind it, the row that called or died, what a model wrote."""

    kind: TriggerKind
    dota_time: float
    event: Event | None = None
    row: int | None = None
    text: str = ''


@dataclass(kw_only=True)
class Blackboard:
    """What the agents of one team share, row by row: each one's plan and intent, and the calls still standing."""

    directives: list[Directive | None]
    intents: list[tuple[float, str] | None]
    calls: list[Call] = field(default_factory=list)

    def post(self, call: Call) -> bool:
        """Put call up in place of the caller's last one to the same target; False when it only repeats that one."""
        repeated = any(
            standing.row == call.row and standing.target == call.target and standing.text == call.text
            for standing in self.calls
            if standing.expires > call.dota_time
        )
        self.calls = [
            standing
            for standing in self.calls
            if standing.expires > call.dota_time and (standing.row, standing.target) != (call.row, call.target)
        ] + [call]
        return not repeated

    def calls_for(self, row: int, dota_time: float) -> list[Call]:
        """The standing calls from the others to row or to the whole team."""
        return [
            call for call in self.calls if call.expires > dota_time and call.row != row and call.target in (None, row)
        ]


class MemoryTracker:
    """Facts about the match read off every world state frame of one team."""

    def __init__(self, team_id: int, team_size: int):
        self.team_id = team_id
        self.enemy = TEAM_DIRE if team_id == TEAM_RADIANT else TEAM_RADIANT
        self.team_size = team_size
        self.events: list[Event] = []
        self.ours: list[int] = []  # the team's player ids by row, as of the last frame
        self.seen: dict[int, Sighting] = {}  # enemy player id: its last sighting
        self.progress: list[Progress | None] = [None] * team_size
        self.names: dict[int, str] = {}  # player id: hero unit name
        self.positions: dict[int, tuple[float, float]] = {}  # player id: where its hero was last seen
        self.counts: dict[int, tuple[int, int]] = {}  # player id: kills and deaths at the last frame
        self.towers: dict[int, tuple[str, int, tuple[float, float]]] = {}  # handle: name, team, place of a standing one

    def update(self, world_state: CMsgBotWorldState, goals: list[tuple[float, float] | None]) -> list[Event]:
        """Read one frame, the one after the last; goals are the rows' current goals. Returns the frame's events."""
        dota_time = world_state.dota_time
        self.ours = ours = team_player_ids(world_state, self.team_id)[: self.team_size]
        # Note (ruidu): -fill_with_bots parks idle wisps behind the lane players of a 1v1, so both sides are cut to
        # the team size; the first ids are the ones playing.
        players = set(ours) | set(team_player_ids(world_state, self.enemy)[: self.team_size])
        found = {}
        for unit in world_state.units:
            if unit.unit_type == UNIT_TYPE_HERO and not unit.is_illusion and unit.player_id in players:
                found[unit.player_id] = unit
        for player in world_state.players:
            if player.player_id in players and player.hero_id in hero_names():
                self.names.setdefault(player.player_id, hero_names()[player.hero_id])
        for player_id, unit in found.items():
            self.names[player_id] = unit.name
            self.positions[player_id] = (unit.location.x, unit.location.y)
            if unit.team_id == self.enemy and unit.is_alive:
                self.seen[player_id] = Sighting(
                    dota_time=dota_time,
                    name=unit.name,
                    level=unit.level,
                    health=unit.health / max(unit.health_max, 1),
                    position=(unit.location.x, unit.location.y),
                )

        events = self.deaths(world_state, players, ours) + self.fallen_towers(world_state)
        teams = {player.player_id: player.team_id for player in world_state.players}
        for killed in world_state.roshan_killed_events:
            killer = killed.killer_player_id
            events.append(
                Event(kind='roshan', dota_time=dota_time, team=teams.get(killer, 0), killer=self.names.get(killer))
            )
        for killed in world_state.courier_killed_events:
            events.append(
                Event(
                    kind='courier',
                    dota_time=dota_time,
                    team=killed.team_id,
                    killer=self.names.get(killed.killer_player_id),
                )
            )

        for row, player_id in enumerate(ours):
            progress = self.progress[row]
            unit = found.get(player_id)
            if progress is None:
                continue
            # Note (ruidu): a dead or unreported hero (docs/VERSION_DIFF.md) next shows up in the fountain, which is
            # no walk, so the count starts over from there.
            if unit is None or not unit.is_alive:
                progress.position = progress.dota_time = None
                continue
            here = (unit.location.x, unit.location.y)
            if progress.position is not None:
                progress.moved += math.dist(progress.position, here)
            goal = goals[row] if row < len(goals) else None
            if goal is not None:
                distance = math.dist(here, goal)
                progress.closest = distance if progress.closest is None else min(progress.closest, distance)
                if distance <= NEAR_GOAL and progress.dota_time is not None:
                    progress.near_goal += dota_time - progress.dota_time
            progress.position, progress.dota_time = here, dota_time
        for event in events:
            if event.kind == 'death' and event.player_id in ours:
                progress = self.progress[ours.index(event.player_id)]
                if progress is not None:
                    progress.deaths += 1
        self.events += events
        return events

    def deaths(self, world_state: CMsgBotWorldState, players: set[int], ours: list[int]) -> list[Event]:
        """A death is a player's death counter going up; the killer is the one hero whose kills went up with it."""
        counts = {
            player.player_id: (player.kills, player.deaths)
            for player in world_state.players
            if player.player_id in players
        }
        killers = [
            player_id for player_id, (kills, _) in counts.items() if kills > self.counts.get(player_id, (kills, 0))[0]
        ]
        events = []
        for player_id, (_, deaths) in counts.items():
            if deaths > self.counts.get(player_id, (0, deaths))[1]:
                events.append(
                    Event(
                        kind='death',
                        dota_time=world_state.dota_time,
                        team=self.team_id if player_id in ours else self.enemy,
                        name=self.names.get(player_id, ''),
                        player_id=player_id,
                        killer=self.names.get(killers[0]) if len(killers) == 1 else None,
                        position=self.positions.get(player_id),
                    )
                )
        self.counts.update(counts)
        return events

    def fallen_towers(self, world_state: CMsgBotWorldState) -> list[Event]:
        """Destroyed buildings stop being reported, so a tower missing from a frame that reports towers has fallen."""
        towers = {unit.handle: unit for unit in world_state.units if unit.unit_type == UNIT_TYPE_TOWER}
        if not towers:
            return []
        events = []
        for handle, (name, team, place) in list(self.towers.items()):
            if handle not in towers or not towers[handle].is_alive:
                events.append(
                    Event(kind='tower', dota_time=world_state.dota_time, team=team, name=name, position=place)
                )
                del self.towers[handle]
        for handle, unit in towers.items():
            if unit.is_alive:
                self.towers.setdefault(handle, (unit.name, unit.team_id, (unit.location.x, unit.location.y)))
        return events

    def restart(self, row: int, hero: CMsgBotWorldState.Unit | None, dota_time: float) -> Progress | None:
        """Start counting row's follow-through afresh as its long think is asked again; returns the finished count."""
        finished = self.progress[row]
        self.progress[row] = Progress(
            since=dota_time,
            last_hits=hero.last_hits if hero is not None else 0,
            gold=hero.reliable_gold + hero.unreliable_gold if hero is not None else 0,
            position=(hero.location.x, hero.location.y) if hero is not None else None,
            dota_time=dota_time,
        )
        return finished


def lessons_path(memory_dir: str, hero: str) -> str:
    return os.path.join(memory_dir, 'heroes', f'{hero}.yaml')


def read_memory(path: str) -> dict[str, object]:
    """A memory file as it is on disk, keys a person added included; empty before anything wrote it."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_lessons(memory_dir: str, hero: str, limit: int) -> list[dict[str, str]]:
    """The newest lessons earlier matches left for hero, oldest first."""
    return list(read_memory(lessons_path(memory_dir, hero)).get('lessons') or [])[-limit:]


def save_lessons(memory_dir: str, hero: str, lessons: list[dict[str, str]]) -> None:
    """Append lessons to hero's file, read again first because a person may have edited it since."""
    path = lessons_path(memory_dir, hero)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = read_memory(path)
    data['lessons'] = list(data.get('lessons') or []) + lessons
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
