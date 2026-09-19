"""Discrete hero action space, its legality masks, and the translation to bridge action dicts."""
import math
from enum import IntEnum

import numpy as np
from gymnasium import spaces

from dota2_env.bridge.constants import UNIT_TYPE_HERO, UNIT_TYPE_TOWER
from dota2_env.observation import MAX_UNITS, N_ABILITIES, hero_abilities

N_MOVE_DIRECTIONS = 16
MOVE_DISTANCE = 300.0
DENY_HEALTH_FRACTION = 0.5


class ActionType(IntEnum):
    NOOP = 0
    MOVE = 1         # uses `move`: direction index, 0 = east, counter-clockwise
    ATTACK = 2       # uses `target`: row of the observation's unit table
    CAST = 3         # uses `ability`: no-target cast of that ability slot
    CAST_TARGET = 4  # uses `ability` and `target`
    STOP = 5


action_space = spaces.Dict({
    'type': spaces.Discrete(len(ActionType)),
    'move': spaces.Discrete(N_MOVE_DIRECTIONS),
    'target': spaces.Discrete(MAX_UNITS),
    'ability': spaces.Discrete(N_ABILITIES),
})

action_mask_space = spaces.Dict({
    'type': spaces.MultiBinary(len(ActionType)),
    'attack_target': spaces.MultiBinary(MAX_UNITS),
    'cast_target': spaces.MultiBinary(MAX_UNITS),
    'ability': spaces.MultiBinary(N_ABILITIES),
})


def build_action_mask(observation, team_id):
    """Which sub-actions are legal right now. Lives in `info["action_mask"]`."""
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

    for slot, ability in enumerate(hero_abilities(hero)):
        mask['ability'][slot] = ability is not None and ability.level > 0 and ability.is_fully_castable

    can_act = not hero.is_stunned and not hero.is_hexed and not hero.is_nightmared
    can_cast = can_act and not hero.is_silenced and mask['ability'].any()
    mask['type'][ActionType.MOVE] = can_act and not hero.is_rooted
    mask['type'][ActionType.STOP] = can_act
    mask['type'][ActionType.ATTACK] = can_act and not hero.is_disarmed and mask['attack_target'].any()
    mask['type'][ActionType.CAST] = can_cast
    mask['type'][ActionType.CAST_TARGET] = can_cast and mask['cast_target'].any()
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
        'ability': pick(mask['ability']),
    }


def to_bridge_action(action, observation, player):
    """One entry for the bridge's `actions` list, or None when the action cannot be carried out."""
    action_type = ActionType(int(action['type']))
    target = int(action['target'])
    target_handle = observation.unit_handles[target] if target < len(observation.unit_handles) else None

    if action_type == ActionType.NOOP or observation.origin is None:
        return None
    if observation.hero is None and action_type != ActionType.MOVE:
        return None
    if action_type == ActionType.STOP:
        return {'actionType': 'DOTA_UNIT_ORDER_STOP', 'player': player}
    if action_type == ActionType.MOVE:
        angle = 2 * math.pi * int(action['move']) / N_MOVE_DIRECTIONS
        return {'actionType': 'DOTA_UNIT_ORDER_MOVE_DIRECTLY', 'player': player, 'moveDirectly': {'location': {
            'x': observation.origin[0] + MOVE_DISTANCE * math.cos(angle),
            'y': observation.origin[1] + MOVE_DISTANCE * math.sin(angle),
            'z': 0.0,
        }}}
    if action_type == ActionType.CAST:
        return {'actionType': 'DOTA_UNIT_ORDER_CAST_NO_TARGET', 'player': player,
                'cast': {'abilitySlot': int(action['ability'])}}
    if target_handle is None:
        return None
    if action_type == ActionType.ATTACK:
        return {'actionType': 'DOTA_UNIT_ORDER_ATTACK_TARGET', 'player': player,
                'attackTarget': {'target': target_handle, 'once': True}}
    return {'actionType': 'DOTA_UNIT_ORDER_CAST_TARGET', 'player': player,
            'castTarget': {'abilitySlot': int(action['ability']), 'target': target_handle}}


def train_ability(player, ability):
    """`ability` is an ability name or "slot:N"."""
    return {'actionType': 'DOTA_UNIT_ORDER_TRAIN_ABILITY', 'player': player, 'trainAbility': {'ability': ability}}


def purchase_item(player, item_name):
    return {'actionType': 'DOTA_UNIT_ORDER_PURCHASE_ITEM', 'player': player, 'purchaseItem': {'itemName': item_name}}
