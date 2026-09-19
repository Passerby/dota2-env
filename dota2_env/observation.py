"""CMsgBotWorldState -> fixed-size numpy observation.

The observation is hero-centric: one feature vector for our hero, one row per ability slot, and a
table of the `MAX_UNITS` nearest units. Row `i` of the unit table is what `target = i` refers to
in the action space; `Observation.unit_handles[i]` keeps the matching Dota entity handle.
"""

import math
from dataclasses import dataclass, field

import numpy as np
from gymnasium import spaces

from dota2_env.bridge.constants import (
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_CREEP_HERO,
    UNIT_TYPE_HERO,
    UNIT_TYPE_JUNGLE_CREEP,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_TOWER,
)

MAX_UNITS = 32
N_ABILITIES = 6
UNIT_RADIUS = 1600.0
MAP_SCALE = 8192.0
RESPAWN_LOCATION = {TEAM_RADIANT: (-6700.0, -6700.0), TEAM_DIRE: (6900.0, 6650.0)}

OBSERVED_UNIT_TYPES = (
    UNIT_TYPE_HERO,
    UNIT_TYPE_CREEP_HERO,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_JUNGLE_CREEP,
    UNIT_TYPE_TOWER,
)

HERO_FEATURES = (
    'x',
    'y',
    'facing_sin',
    'facing_cos',
    'is_alive',
    'health_frac',
    'health',
    'health_regen',
    'mana_frac',
    'mana',
    'level',
    'attack_damage',
    'attack_range',
    'attacks_per_second',
    'movement_speed',
    'armor',
    'gold',
    'last_hits',
    'denies',
    'ability_points',
    'is_stunned',
    'is_silenced',
    'is_attacking',
    'dota_time',
    'time_of_day',
)
ABILITY_FEATURES = ('level', 'cooldown', 'castable')
UNIT_FEATURES = (
    'rel_x',
    'rel_y',
    'distance',
    'in_attack_range',
    'is_enemy',
    'is_hero',
    'is_lane_creep',
    'is_tower',
    'health_frac',
    'health',
    'attack_damage',
    'attack_range',
    'hits_to_kill',
    'is_attacking_me',
    'facing_sin',
    'facing_cos',
)

observation_space = spaces.Dict(
    {
        'hero': spaces.Box(-np.inf, np.inf, (len(HERO_FEATURES),), np.float32),
        'abilities': spaces.Box(-np.inf, np.inf, (N_ABILITIES, len(ABILITY_FEATURES)), np.float32),
        'units': spaces.Box(-np.inf, np.inf, (MAX_UNITS, len(UNIT_FEATURES)), np.float32),
        'unit_mask': spaces.MultiBinary(MAX_UNITS),
    }
)


@dataclass
class Observation:
    arrays: dict
    hero: object = None  # CMsgBotWorldState.Unit, None while the client does not report our hero
    origin: tuple = None  # (x, y) that MOVE actions are relative to; None when the hero cannot move
    unit_handles: list = field(default_factory=list)
    units: list = field(default_factory=list)  # CMsgBotWorldState.Unit for every row of the unit table


def lane_player_ids(world_state):
    """{team_id: first player of the team}; in the 1v1 setup everyone else is an idle filler hero."""
    first = {}
    for player in world_state.players:
        if player.team_id not in first or player.player_id < first[player.team_id]:
            first[player.team_id] = player.player_id
    return first


def find_hero(world_state, team_id, player_id=None):
    """The hero unit of `player_id`, by default of the team's lane player. None while it is not reported."""
    if player_id is None:
        player_id = lane_player_ids(world_state).get(team_id)
    for unit in world_state.units:
        if (
            unit.unit_type == UNIT_TYPE_HERO
            and unit.team_id == team_id
            and not unit.is_illusion
            and (player_id is None or unit.player_id == player_id)
        ):
            return unit
    return None


def distance(a, b):
    return math.hypot(a.location.x - b.location.x, a.location.y - b.location.y)


def hero_abilities(hero):
    """Ability per slot for the first N_ABILITIES slots (the trailing slot-0 generic abilities are dropped)."""
    by_slot = {}
    for ability in hero.abilities:
        if ability.slot not in by_slot:
            by_slot[ability.slot] = ability
    return [by_slot.get(slot) for slot in range(N_ABILITIES)]


def _hero_vector(world_state, hero):
    facing = math.radians(hero.facing)
    return np.array(
        [
            hero.location.x / MAP_SCALE,
            hero.location.y / MAP_SCALE,
            math.sin(facing),
            math.cos(facing),
            float(hero.is_alive),
            hero.health / max(hero.health_max, 1),
            hero.health / 1000.0,
            hero.health_regen / 10.0,
            hero.mana / max(hero.mana_max, 1.0),
            hero.mana / 1000.0,
            hero.level / 30.0,
            hero.attack_damage / 100.0,
            hero.attack_range / 1000.0,
            hero.attacks_per_second,
            hero.current_movement_speed / 500.0,
            hero.armor / 10.0,
            (hero.reliable_gold + hero.unreliable_gold) / 1000.0,
            hero.last_hits / 100.0,
            hero.denies / 100.0,
            float(hero.ability_points),
            float(hero.is_stunned),
            float(hero.is_silenced),
            float(hero.HasField('attack_target_handle') and hero.attack_target_handle != 0xFFFFFFFF),
            world_state.dota_time / 600.0,
            world_state.time_of_day,
        ],
        dtype=np.float32,
    )


def build_observation(world_state, team_id, player_id=None):
    arrays = {key: np.zeros(space.shape, space.dtype) for key, space in observation_space.spaces.items()}
    hero = find_hero(world_state, team_id, player_id)
    if hero is None:
        # Current clients stop reporting a hero that respawned until it has left the fountain
        # (see docs/VERSION_DIFF.md). Report it as standing on its spawn point so it can walk out.
        if player_id is None:
            player_id = lane_player_ids(world_state).get(team_id)
        if any(p.player_id == player_id and p.is_alive for p in world_state.players):
            origin = RESPAWN_LOCATION[team_id]
            arrays['hero'][HERO_FEATURES.index('x')] = origin[0] / MAP_SCALE
            arrays['hero'][HERO_FEATURES.index('y')] = origin[1] / MAP_SCALE
            arrays['hero'][HERO_FEATURES.index('is_alive')] = 1.0
            arrays['hero'][HERO_FEATURES.index('dota_time')] = world_state.dota_time / 600.0
            return Observation(arrays=arrays, origin=origin)
        return Observation(arrays=arrays)

    arrays['hero'] = _hero_vector(world_state, hero)
    for slot, ability in enumerate(hero_abilities(hero)):
        if ability is not None:
            arrays['abilities'][slot] = (
                ability.level / 4.0,
                min(ability.cooldown_remaining, 100.0) / 10.0,
                float(ability.is_fully_castable),
            )

    nearby = []
    players = set(lane_player_ids(world_state).values())
    for unit in world_state.units:
        if unit.handle == hero.handle or not unit.is_alive or unit.unit_type not in OBSERVED_UNIT_TYPES:
            continue
        if unit.unit_type == UNIT_TYPE_HERO and unit.player_id not in players:
            continue
        d = distance(hero, unit)
        if d <= UNIT_RADIUS:
            nearby.append((d, unit))
    nearby.sort(key=lambda pair: pair[0])
    nearby = nearby[:MAX_UNITS]

    for row, (d, unit) in enumerate(nearby):
        facing = math.radians(unit.facing)
        # attack_damage already includes bonus damage; armor reduction is ignored for the estimate.
        hits_to_kill = unit.health / max(hero.attack_damage, 1.0)
        arrays['units'][row] = (
            (unit.location.x - hero.location.x) / UNIT_RADIUS,
            (unit.location.y - hero.location.y) / UNIT_RADIUS,
            d / UNIT_RADIUS,
            float(d <= hero.attack_range + hero.bounding_radius + unit.bounding_radius),
            float(unit.team_id != team_id),
            float(unit.unit_type == UNIT_TYPE_HERO),
            float(unit.unit_type == UNIT_TYPE_LANE_CREEP),
            float(unit.unit_type == UNIT_TYPE_TOWER),
            unit.health / max(unit.health_max, 1),
            unit.health / 1000.0,
            unit.attack_damage / 100.0,
            unit.attack_range / 1000.0,
            min(hits_to_kill, 20.0) / 20.0,
            float(unit.HasField('attack_target_handle') and unit.attack_target_handle == hero.handle),
            math.sin(facing),
            math.cos(facing),
        )
        arrays['unit_mask'][row] = 1

    return Observation(
        arrays=arrays,
        hero=hero,
        origin=(hero.location.x, hero.location.y) if hero.is_alive else None,
        unit_handles=[u.handle for _, u in nearby],
        units=[u for _, u in nearby],
    )
