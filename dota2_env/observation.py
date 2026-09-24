"""CMsgBotWorldState -> fixed-size numpy observation.

The observation is hero-centric: one feature vector for our hero, one row per ability slot, and a
table of the `MAX_UNITS` nearest units. Row `i` of the unit table is what `target = i` refers to
in the action space; `Observation.unit_handles[i]` keeps the matching Dota entity handle.

build_team_observation stacks TEAM_SIZE of those, one per controlled hero, and adds a
team vector of the map-wide state that a single hero-centric view cannot show.

The map part (local_map, runes, landmarks) comes from dota2_env.map_features, which also defines
its feature order; it needs the TreeTable of the match and stays zero without one.
"""

import math
from dataclasses import dataclass, field

import numpy as np
from gymnasium import spaces

from dota2_env.bridge.constants import (
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_COURIER,
    UNIT_TYPE_CREEP_HERO,
    UNIT_TYPE_FORT,
    UNIT_TYPE_HERO,
    UNIT_TYPE_JUNGLE_CREEP,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_TOWER,
)
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import load_records
from dota2_env.map_features import (
    LANDMARK_FEATURES,
    LANDMARKS,
    MAP_FEATURES,
    MAP_RADIUS,
    MAP_SCALE,
    RUNE_FEATURES,
    RUNE_SPOTS,
    TreeTable,
    map_arrays,
)

MAX_UNITS = 32
TEAM_SIZE = 5
N_TOWERS = 11  # three per lane plus the two guarding the ancient
N_ABILITIES = 6
N_ITEM_SLOTS = 6  # the inventory; items in the backpack or the stash cannot be used
TP_SLOT = 15  # the Town Portal Scroll has an item slot of its own
STASH_SLOTS = range(9, 15)  # what is bought away from the shop waits here for the courier
N_TALENTS = 8
# Note (ruidu): talents 2k and 2k + 1 form tier k, and taking one closes the other (measured on 6937,
# docs/VERSION_DIFF.md 3.2); the client's CanAbilityBeUpgraded says yes to all eight from level 10 on.
TALENT_LEVELS = (10, 15, 20, 25)
UNIT_RADIUS = 1600.0
MAP_SIDE = 2 * MAP_RADIUS + 1
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
    'tp_charges',
    'tp_cooldown',
    'stash_items',
)
ABILITY_FEATURES = ('level', 'cooldown', 'castable')
ITEM_FEATURES = ('item_id', 'charges', 'cooldown', 'castable')
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
        'items': spaces.Box(-np.inf, np.inf, (N_ITEM_SLOTS, len(ITEM_FEATURES)), np.float32),
        'talents': spaces.Box(-np.inf, np.inf, (N_TALENTS,), np.float32),
        'units': spaces.Box(-np.inf, np.inf, (MAX_UNITS, len(UNIT_FEATURES)), np.float32),
        'unit_mask': spaces.MultiBinary(MAX_UNITS),
        'local_map': spaces.Box(-np.inf, np.inf, (len(MAP_FEATURES), MAP_SIDE, MAP_SIDE), np.float32),
        'runes': spaces.Box(-np.inf, np.inf, (len(RUNE_SPOTS), len(RUNE_FEATURES)), np.float32),
        'landmarks': spaces.Box(-np.inf, np.inf, (len(LANDMARKS), len(LANDMARK_FEATURES)), np.float32),
    }
)


@dataclass
class Observation:
    arrays: dict
    hero: CMsgBotWorldState.Unit | None = None  # None while the client does not report our hero
    origin: tuple[float, float] | None = None  # (x, y) that MOVE actions start from; None when the hero cannot move
    unit_handles: list = field(default_factory=list)
    units: list = field(default_factory=list)  # CMsgBotWorldState.Unit for every row of the unit table
    courier: CMsgBotWorldState.Unit | None = None  # the hero's own, which COURIER sends


def team_player_ids(world_state, team_id):
    """Player ids of team_id, ascending. Row i of a team observation is the i-th of them, and in
    the 1v1 setup the first one is the lane player (every other slot holds an idle filler hero)."""
    return sorted(player.player_id for player in world_state.players if player.team_id == team_id)


def lane_players(world_state: CMsgBotWorldState) -> set[int]:
    """The two 1v1 lane players, the first of each team; -fill_with_bots parks idle wisps in both fountains."""
    both = (team_player_ids(world_state, TEAM_RADIANT), team_player_ids(world_state, TEAM_DIRE))
    return {ids[0] for ids in both if ids}


def find_hero(world_state, team_id, player_id=None):
    """The hero unit of `player_id`, by default of the team's lane player. None while it is not reported."""
    if player_id is None:
        team_players = team_player_ids(world_state, team_id)
        player_id = team_players[0] if team_players else None
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


def hero_items(hero):
    """Item per inventory slot, None where the slot is empty."""
    by_slot = {item.slot: item for item in hero.items}
    return [by_slot.get(slot) for slot in range(N_ITEM_SLOTS)]


def hero_talents(hero: CMsgBotWorldState.Unit) -> list[tuple[str, bool]]:
    """(name, learned) of each of the hero's talents, tier by tier; empty for a hero the game data lacks.

    The order is heroes.json's, which is the client's slot order, and the action's talent index.
    """
    record = load_records('heroes').get(hero.name)
    if record is None:
        return []
    abilities = load_records('abilities')
    learned = {ability.ability_id for ability in hero.abilities if ability.level > 0}
    return [(name, abilities[name]['id'] in learned) for name in record['talents'][:N_TALENTS]]


def open_talent_tiers(talents: list[tuple[str, bool]], level: int) -> list[int]:
    """The tiers a hero of this level has reached without taking either of their two talents."""
    return [
        tier
        for tier, unlock in enumerate(TALENT_LEVELS)
        if level >= unlock
        and talents[2 * tier : 2 * tier + 2]
        and not any(has for _, has in talents[2 * tier : 2 * tier + 2])
    ]


def hero_vector(world_state, hero):
    facing = math.radians(hero.facing)
    items = {item.slot: item for item in hero.items}
    tp = items.get(TP_SLOT)
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
            tp.charges / 10.0 if tp is not None else 0.0,
            min(tp.cooldown_remaining, 100.0) / 10.0 if tp is not None else 0.0,
            float(sum(slot in items for slot in STASH_SLOTS)),
        ],
        dtype=np.float32,
    )


def build_observation(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_id: int | None = None,
    hero_players: set[int] | None = None,
    trees: TreeTable | None = None,
) -> Observation:
    """One hero-centric observation.

    hero_players: player ids whose heroes may show up in the unit table; the default keeps only the
                  two 1v1 lane players, because -fill_with_bots parks idle wisps in both fountains.
    trees:        the match's tree table; without it the map part of the observation stays zero.
    """
    arrays = {key: np.zeros(space.shape, space.dtype) for key, space in observation_space.spaces.items()}
    hero = find_hero(world_state, team_id, player_id)
    if hero is None:
        # Current clients stop reporting a hero that respawned until it has left the fountain
        # (see docs/VERSION_DIFF.md). Report it as standing on its spawn point so it can walk out.
        if player_id is None:
            team_players = team_player_ids(world_state, team_id)
            player_id = team_players[0] if team_players else None
        if any(p.player_id == player_id and p.is_alive for p in world_state.players):
            origin = RESPAWN_LOCATION[team_id]
            arrays['hero'][HERO_FEATURES.index('x')] = origin[0] / MAP_SCALE
            arrays['hero'][HERO_FEATURES.index('y')] = origin[1] / MAP_SCALE
            arrays['hero'][HERO_FEATURES.index('is_alive')] = 1.0
            arrays['hero'][HERO_FEATURES.index('dota_time')] = world_state.dota_time / 600.0
            if trees is not None:
                arrays.update(map_arrays(trees, world_state, team_id, *origin))
            return Observation(arrays=arrays, origin=origin)
        return Observation(arrays=arrays)

    arrays['hero'] = hero_vector(world_state, hero)
    for slot, ability in enumerate(hero_abilities(hero)):
        if ability is not None:
            arrays['abilities'][slot] = (
                ability.level / 4.0,
                min(ability.cooldown_remaining, 100.0) / 10.0,
                float(ability.is_fully_castable),
            )
    for slot, item in enumerate(hero_items(hero)):
        if item is not None:
            arrays['items'][slot] = (
                item.ability_id,
                item.charges / 10.0,
                min(item.cooldown_remaining, 100.0) / 10.0,
                float(item.is_fully_castable),
            )
    for index, (_, learned) in enumerate(hero_talents(hero)):
        arrays['talents'][index] = float(learned)
    if trees is not None:
        arrays.update(map_arrays(trees, world_state, team_id, hero.location.x, hero.location.y))

    nearby = []
    if hero_players is None:
        hero_players = lane_players(world_state)
    for unit in world_state.units:
        if unit.handle == hero.handle or not unit.is_alive or unit.unit_type not in OBSERVED_UNIT_TYPES:
            continue
        if unit.unit_type == UNIT_TYPE_HERO and unit.player_id not in hero_players:
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
        courier=next(
            (
                unit
                for unit in world_state.units
                if unit.unit_type == UNIT_TYPE_COURIER and unit.team_id == team_id and unit.player_id == hero.player_id
            ),
            None,
        ),
    )


TEAM_FEATURES = (
    'dota_time',
    'time_of_day',
    'heroes_alive',
    'enemy_heroes_visible',
    'gold',
    'towers',
    'enemy_towers',
    'ancient_health_frac',
    'enemy_ancient_health_frac',
    'kills',
    'deaths',
)

team_observation_space = spaces.Dict(
    {
        'heroes': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, len(HERO_FEATURES)), np.float32),
        'abilities': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, N_ABILITIES, len(ABILITY_FEATURES)), np.float32),
        'items': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, N_ITEM_SLOTS, len(ITEM_FEATURES)), np.float32),
        'talents': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, N_TALENTS), np.float32),
        'units': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, MAX_UNITS, len(UNIT_FEATURES)), np.float32),
        'unit_mask': spaces.MultiBinary((TEAM_SIZE, MAX_UNITS)),
        'local_map': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, len(MAP_FEATURES), MAP_SIDE, MAP_SIDE), np.float32),
        'runes': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, len(RUNE_SPOTS), len(RUNE_FEATURES)), np.float32),
        'landmarks': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, len(LANDMARKS), len(LANDMARK_FEATURES)), np.float32),
        'team': spaces.Box(-np.inf, np.inf, (len(TEAM_FEATURES),), np.float32),
    }
)


@dataclass
class TeamObservation:
    arrays: dict
    heroes: list = field(default_factory=list)  # one Observation per controlled hero, in player_ids order
    player_ids: list = field(default_factory=list)


def build_team_observation(
    world_state: CMsgBotWorldState, team_id: int, player_ids: list[int], trees: TreeTable | None = None
) -> TeamObservation:
    """Row i of every array belongs to the hero of player_ids[i]."""
    hero_players = {player.player_id for player in world_state.players}
    heroes = [build_observation(world_state, team_id, player_id, hero_players, trees) for player_id in player_ids]
    arrays = {key: np.zeros(space.shape, space.dtype) for key, space in team_observation_space.spaces.items()}
    for row, observation in enumerate(heroes):
        arrays['heroes'][row] = observation.arrays['hero']
        arrays['abilities'][row] = observation.arrays['abilities']
        arrays['items'][row] = observation.arrays['items']
        arrays['talents'][row] = observation.arrays['talents']
        arrays['units'][row] = observation.arrays['units']
        arrays['unit_mask'][row] = observation.arrays['unit_mask']
        arrays['local_map'][row] = observation.arrays['local_map']
        arrays['runes'][row] = observation.arrays['runes']
        arrays['landmarks'][row] = observation.arrays['landmarks']

    enemy_team = TEAM_DIRE if team_id == TEAM_RADIANT else TEAM_RADIANT
    # Note (ruidu): both sides have exactly N_TOWERS towers and one fort, and nothing else in the
    # world state carries those unit types, so indexing by team id cannot miss (verified on a
    # recorded frame: 11 towers and 1 fort per team, outposts are UNIT_TYPE_BUILDING).
    towers = {TEAM_RADIANT: 0, TEAM_DIRE: 0}
    ancient = {TEAM_RADIANT: 0.0, TEAM_DIRE: 0.0}
    enemy_heroes = 0
    for unit in world_state.units:
        if unit.unit_type == UNIT_TYPE_TOWER and unit.is_alive:
            towers[unit.team_id] += 1
        elif unit.unit_type == UNIT_TYPE_FORT:
            ancient[unit.team_id] = unit.health / max(unit.health_max, 1)
        elif unit.unit_type == UNIT_TYPE_HERO and unit.team_id == enemy_team and unit.is_alive and not unit.is_illusion:
            enemy_heroes += 1
    arrays['team'] = np.array(
        [
            world_state.dota_time / 600.0,
            world_state.time_of_day,
            sum(player.is_alive for player in world_state.players if player.team_id == team_id) / TEAM_SIZE,
            enemy_heroes / TEAM_SIZE,
            sum(h.hero.reliable_gold + h.hero.unreliable_gold for h in heroes if h.hero is not None) / 10000.0,
            towers[team_id] / N_TOWERS,
            towers[enemy_team] / N_TOWERS,
            ancient[team_id],
            ancient[enemy_team],
            sum(player.kills for player in world_state.players if player.team_id == team_id) / 50.0,
            sum(player.deaths for player in world_state.players if player.team_id == team_id) / 50.0,
        ],
        dtype=np.float32,
    )
    return TeamObservation(arrays=arrays, heroes=heroes, player_ids=list(player_ids))
