"""Discrete hero action space, its legality masks, and the translation to bridge action dicts."""

import json
import math
from enum import IntEnum

import numpy as np
from gymnasium import spaces

from dota2_env.bridge.constants import UNIT_TYPE_HERO, UNIT_TYPE_TOWER
from dota2_env.observation import (
    MAX_UNITS,
    N_ABILITIES,
    N_ITEM_SLOTS,
    TEAM_SIZE,
    Observation,
    TeamObservation,
    hero_abilities,
    hero_items,
)

N_MOVE_DIRECTIONS = 16
MOVE_DISTANCE = 300.0
DENY_HEALTH_FRACTION = 0.5
N_CAST_SLOTS = N_ABILITIES + N_ITEM_SLOTS  # ability is a skill slot, or N_ABILITIES + an inventory slot


class ActionType(IntEnum):
    NOOP = 0
    MOVE = 1  # uses move: direction index, 0 = east, counter-clockwise
    ATTACK = 2  # uses target: row of the observation's unit table
    CAST = 3  # uses ability: no target, or our own hero; an item aimed at the ground goes at its feet
    CAST_TARGET = 4  # uses ability and target; a skill aimed at the ground lands where the target stands
    STOP = 5
    CAST_DIRECTION = 6  # uses ability and move: aimed at the ground, cast_range away in that direction


CAST_TYPES = (ActionType.CAST, ActionType.CAST_TARGET, ActionType.CAST_DIRECTION)
NOOP_ACTION = {'type': int(ActionType.NOOP), 'move': 0, 'target': 0, 'ability': 0}

action_space = spaces.Dict(
    {
        'type': spaces.Discrete(len(ActionType)),
        'move': spaces.Discrete(N_MOVE_DIRECTIONS),
        'target': spaces.Discrete(MAX_UNITS),
        'ability': spaces.Discrete(N_CAST_SLOTS),
    }
)

action_mask_space = spaces.Dict(
    {
        'type': spaces.MultiBinary(len(ActionType)),
        'attack_target': spaces.MultiBinary(MAX_UNITS),
        'cast_target': spaces.MultiBinary(MAX_UNITS),
        'ability': spaces.MultiBinary([len(ActionType), N_CAST_SLOTS]),  # row t: what action type t can use
    }
)


team_action_space = spaces.Dict(
    {
        'type': spaces.MultiDiscrete([len(ActionType)] * TEAM_SIZE),
        'move': spaces.MultiDiscrete([N_MOVE_DIRECTIONS] * TEAM_SIZE),
        'target': spaces.MultiDiscrete([MAX_UNITS] * TEAM_SIZE),
        'ability': spaces.MultiDiscrete([N_CAST_SLOTS] * TEAM_SIZE),
    }
)

team_action_mask_space = spaces.Dict(
    {
        'type': spaces.MultiBinary([TEAM_SIZE, len(ActionType)]),
        'attack_target': spaces.MultiBinary([TEAM_SIZE, MAX_UNITS]),
        'cast_target': spaces.MultiBinary([TEAM_SIZE, MAX_UNITS]),
        'ability': spaces.MultiBinary([TEAM_SIZE, len(ActionType), N_CAST_SLOTS]),
    }
)


def build_action_mask(
    observation: Observation, team_id: int, cast_slots: dict[int, tuple[str, tuple[str, ...]]] | None = None
) -> dict[str, np.ndarray]:
    """Which sub-actions are legal right now. Lives in info["action_mask"].

    cast_slots: {ability index: (name, kinds)} as lua reported them; a slot it has not reported cannot be cast.
    """
    mask = {key: np.zeros(space.shape, space.dtype) for key, space in action_mask_space.spaces.items()}
    mask['type'][ActionType.NOOP] = 1
    hero = observation.hero
    if hero is None or not hero.is_alive:
        # alive but not reported by the client (fresh respawn): walking is all we can do
        mask['type'][ActionType.MOVE] = hero is None and observation.origin is not None
        return mask

    for row, unit in enumerate(observation.units):
        if unit.team_id != team_id:
            mask['attack_target'][row] = not unit.is_attack_immune and not unit.is_invulnerable
            mask['cast_target'][row] = not unit.is_magic_immune and not unit.is_invulnerable
        elif unit.unit_type not in (UNIT_TYPE_HERO, UNIT_TYPE_TOWER):
            mask['attack_target'][row] = unit.health < unit.health_max * DENY_HEALTH_FRACTION

    slots = cast_slots or {}
    ready = [
        ability is not None and ability.level > 0 and ability.is_fully_castable and not hero.is_silenced
        for ability in hero_abilities(hero)
    ] + [item is not None and item.is_fully_castable and not hero.is_muted for item in hero_items(hero)]
    for index, is_ready in enumerate(ready):
        kinds = set(slots[index][1]) if is_ready and index in slots else set()
        is_item = index >= N_ABILITIES
        mask['ability'][ActionType.CAST][index] = bool(kinds & {'no_target', 'self'}) or (is_item and 'point' in kinds)
        mask['ability'][ActionType.CAST_TARGET][index] = bool(kinds & {'enemy', 'point'})
        mask['ability'][ActionType.CAST_DIRECTION][index] = 'point' in kinds

    can_act = not hero.is_stunned and not hero.is_hexed and not hero.is_nightmared
    mask['type'][ActionType.MOVE] = can_act and not hero.is_rooted
    mask['type'][ActionType.STOP] = can_act
    mask['type'][ActionType.ATTACK] = can_act and not hero.is_disarmed and mask['attack_target'].any()
    mask['type'][ActionType.CAST] = can_act and mask['ability'][ActionType.CAST].any()
    mask['type'][ActionType.CAST_TARGET] = (
        can_act and mask['ability'][ActionType.CAST_TARGET].any() and mask['cast_target'].any()
    )
    mask['type'][ActionType.CAST_DIRECTION] = can_act and mask['ability'][ActionType.CAST_DIRECTION].any()
    return mask


def sample_masked_action(mask, rng):
    """Uniformly random legal action; handy for smoke tests and as a baseline."""

    def pick(bits):
        legal = np.flatnonzero(bits)
        return int(rng.choice(legal)) if len(legal) else 0

    action_type = pick(mask['type'])
    return {
        'type': action_type,
        'move': int(rng.integers(N_MOVE_DIRECTIONS)),
        'target': pick(mask['cast_target'] if action_type == ActionType.CAST_TARGET else mask['attack_target']),
        'ability': pick(mask['ability'][action_type]),
    }


def build_team_action_mask(
    observation: TeamObservation,
    team_id: int,
    cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]] | None = None,
) -> dict[str, np.ndarray]:
    """Per-hero masks stacked; row i belongs to observation.heroes[i]. cast_slots is keyed by player id."""
    slots = cast_slots or {}
    masks = [
        build_action_mask(hero, team_id, slots.get(player_id))
        for hero, player_id in zip(observation.heroes, observation.player_ids, strict=True)
    ]
    return {key: np.stack([mask[key] for mask in masks]) for key in team_action_mask_space.spaces}


def hero_action(action, row):
    """Row row of a team action as the single-hero action dict to_bridge_action takes."""
    return {key: int(values[row]) for key, values in action.items()}


def sample_masked_team_action(mask, rng):
    """Uniformly random legal action for every hero of the team."""
    picks = [sample_masked_action({key: values[row] for key, values in mask.items()}, rng) for row in range(TEAM_SIZE)]
    return {key: np.array([pick[key] for pick in picks], np.int64) for key in team_action_space.spaces}


def to_bridge_action(
    action: dict[str, int],
    observation: Observation,
    player: int,
    cast_slots: dict[int, tuple[str, tuple[str, ...]]] | None = None,
) -> dict[str, object] | None:
    """One entry for the bridge's actions list, or None when the action cannot be carried out.

    cast_slots is what lua reported for this hero ({ability index: (name, kinds)}); a slot of the vector kind goes
    out as DOTA_UNIT_ORDER_CAST_VECTOR, which bridge/lua/server_actions.lua issues with its second point.
    A hero that is alive but not reported is walked towards the map centre unless the action is a MOVE.
    """
    if observation.origin is None:
        return None
    if observation.hero is None and int(action['type']) != ActionType.MOVE:
        # the client reports a respawned hero again only once it has moved (docs/VERSION_DIFF.md)
        x, y = observation.origin
        action_type = ActionType.MOVE
        move = round(math.atan2(-y, -x) / (2 * math.pi) * N_MOVE_DIRECTIONS) % N_MOVE_DIRECTIONS
    else:
        action_type = ActionType(int(action['type']))
        move = int(action['move'])
    target = int(action['target'])
    target_handle = observation.unit_handles[target] if target < len(observation.unit_handles) else None
    ability = int(action['ability'])
    # lua reads a negative slot s as inventory slot -s - 1
    cast_slot = ability if ability < N_ABILITIES else N_ABILITIES - 1 - ability
    is_vector = cast_slots is not None and ability in cast_slots and 'vector' in cast_slots[ability][1]

    if action_type == ActionType.NOOP:
        return None
    if action_type == ActionType.STOP:
        return {'actionType': 'DOTA_UNIT_ORDER_STOP', 'player': player}
    if action_type == ActionType.MOVE:
        angle = 2 * math.pi * move / N_MOVE_DIRECTIONS
        return {
            'actionType': 'DOTA_UNIT_ORDER_MOVE_DIRECTLY',
            'player': player,
            'moveDirectly': {
                'location': {
                    'x': observation.origin[0] + MOVE_DISTANCE * math.cos(angle),
                    'y': observation.origin[1] + MOVE_DISTANCE * math.sin(angle),
                    'z': 0.0,
                }
            },
        }
    if action_type == ActionType.CAST:
        return {
            'actionType': 'DOTA_UNIT_ORDER_CAST_NO_TARGET',
            'player': player,
            'cast': {'abilitySlot': cast_slot},
        }
    if action_type == ActionType.CAST_DIRECTION:
        angle = 2 * math.pi * move / N_MOVE_DIRECTIONS
        aimed = (hero_abilities(observation.hero) + hero_items(observation.hero))[ability]
        # the client reports no cast range for a few ground skills and for an empty slot
        reach = aimed.cast_range if aimed is not None and aimed.cast_range > 0 else MOVE_DISTANCE
        location = {
            'x': observation.origin[0] + reach * math.cos(angle),
            'y': observation.origin[1] + reach * math.sin(angle),
            'z': observation.hero.location.z,
        }
        if is_vector:
            direction = {'x': math.cos(angle), 'y': math.sin(angle)}
            return {
                'actionType': 'DOTA_UNIT_ORDER_CAST_VECTOR',
                'player': player,
                'castVector': {'abilitySlot': cast_slot, 'location': location, 'direction': direction},
            }
        return {
            'actionType': 'DOTA_UNIT_ORDER_CAST_POSITION',
            'player': player,
            'castLocation': {'abilitySlot': cast_slot, 'location': location},
        }
    if target_handle is None:
        return None
    if action_type == ActionType.ATTACK:
        return {
            'actionType': 'DOTA_UNIT_ORDER_ATTACK_TARGET',
            'player': player,
            'attackTarget': {'target': target_handle, 'once': True},
        }
    if is_vector:
        # Note (ruidu): the vector runs from our hero on through the target, so a dash lands on it and a wall
        # or a curve carries on past it; a target standing on us takes the hero's facing instead.
        unit = observation.units[target]
        dx, dy = unit.location.x - observation.hero.location.x, unit.location.y - observation.hero.location.y
        if (dx, dy) == (0.0, 0.0):
            dx, dy = math.cos(math.radians(observation.hero.facing)), math.sin(math.radians(observation.hero.facing))
        length = math.hypot(dx, dy)
        return {
            'actionType': 'DOTA_UNIT_ORDER_CAST_VECTOR',
            'player': player,
            'castVector': {
                'abilitySlot': cast_slot,
                'location': {'x': unit.location.x, 'y': unit.location.y, 'z': unit.location.z},
                'direction': {'x': dx / length, 'y': dy / length},
            },
        }
    return {
        'actionType': 'DOTA_UNIT_ORDER_CAST_TARGET',
        'player': player,
        'castTarget': {'abilitySlot': cast_slot, 'target': target_handle},
    }


def parse_action(action, mask):
    """A JSON string or mapping from an LLM to an action dict, plus why it was rejected.

    Never raises: anything unparseable or illegal comes back as NOOP with a message to show the model.
    For a team observation, slice the mask to one row first.
    """
    noop = dict(NOOP_ACTION)
    try:
        data = json.loads(action) if isinstance(action, str) else dict(action)
        action_type = ActionType[str(data['type']).upper()]
        parsed = dict(
            noop,
            type=int(action_type),
            move=int(data.get('move', 0)) % N_MOVE_DIRECTIONS,
            target=int(data.get('target', 0)),
            ability=int(data.get('ability', 0)),
        )
    except (ValueError, KeyError, TypeError) as e:
        return noop, f'cannot parse action: {e!r}'

    if not mask['type'][action_type]:
        return noop, f'{action_type.name} is not legal right now'
    if action_type in CAST_TYPES and not (
        0 <= parsed['ability'] < N_CAST_SLOTS and mask['ability'][action_type][parsed['ability']]
    ):
        return noop, f'ability {parsed["ability"]} is not castable with {action_type.name}'
    target_mask = {ActionType.ATTACK: mask['attack_target'], ActionType.CAST_TARGET: mask['cast_target']}
    if action_type in target_mask and not (
        0 <= parsed['target'] < MAX_UNITS and target_mask[action_type][parsed['target']]
    ):
        return noop, f'target {parsed["target"]} is not valid for {action_type.name}'
    return parsed, None


def train_ability(player, ability):
    """`ability` is an ability name or "slot:N"."""
    return {'actionType': 'DOTA_UNIT_ORDER_TRAIN_ABILITY', 'player': player, 'trainAbility': {'ability': ability}}


def purchase_item(player, item_name):
    return {'actionType': 'DOTA_UNIT_ORDER_PURCHASE_ITEM', 'player': player, 'purchaseItem': {'itemName': item_name}}


def chat(player: int, message: str, all_chat: bool = True) -> dict[str, object]:
    """Belongs in extra_actions, which lua runs all of, so talking never costs a hero its main action."""
    return {'actionType': 'ACTION_CHAT', 'player': player, 'chat': {'message': message, 'toAllchat': all_chat}}
