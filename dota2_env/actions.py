"""Discrete hero action space, its legality masks, and the translation to bridge action dicts."""

import json
import math
from enum import IntEnum
from typing import TypedDict

import numpy as np
from gymnasium import spaces

from dota2_env.bridge.constants import UNIT_TYPE_HERO, UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import load_records
from dota2_env.map_features import RUNE_FEATURES, RUNE_SPOTS, load_map
from dota2_env.observation import (
    MAX_UNITS,
    N_ABILITIES,
    N_ITEM_SLOTS,
    N_TALENTS,
    STASH_SLOTS,
    TEAM_SIZE,
    TP_SLOT,
    Observation,
    TeamObservation,
    hero_abilities,
    hero_items,
    hero_talents,
    open_talent_tiers,
)

N_MOVE_DIRECTIONS = 16
MOVE_DISTANCE = 300.0
DENY_HEALTH_FRACTION = 0.5
N_CAST_SLOTS = N_ABILITIES + N_ITEM_SLOTS  # ability is a skill slot, or N_ABILITIES + an inventory slot
TP_SCROLL = 'item_tpscroll'
LABEL_BYTES = 255  # m_CustomHealthLabel is a 256-byte networked string on 6938, its closing NUL included


class ActionType(IntEnum):
    NOOP = 0
    MOVE = 1  # uses move: direction index, 0 = east, counter-clockwise; MOVE_DISTANCE along it
    ATTACK = 2  # uses target: row of the observation's unit table
    CAST = 3  # uses ability: no target, or our own hero; an item aimed at the ground goes at its feet
    CAST_TARGET = 4  # uses ability and target; a skill aimed at the ground lands where the target stands
    STOP = 5
    CAST_DIRECTION = 6  # uses ability and move: aimed at the ground, cast_range away in that direction
    MOVE_TO = 7  # uses point: walk to that spot of the map, on the path the client plans around trees and cliffs
    PICKUP_RUNE = 8  # uses rune: row of the observation's runes table; the hero walks there and picks it up
    TP = 9  # uses point: channel the Town Portal Scroll towards that spot of the map
    TALENT = 10  # uses talent: index into the hero's talents, two per tier; the hero's order carries on
    COURIER = 11  # the hero's courier fetches the stash and carries it to the hero; the hero's order carries on


CAST_TYPES = (ActionType.CAST, ActionType.CAST_TARGET, ActionType.CAST_DIRECTION)


class Action(TypedDict):
    """One hero's action: a value for every key of action_space."""

    type: int
    move: int
    target: int
    ability: int
    point: tuple[float, float] | np.ndarray
    rune: int
    talent: int


NOOP_ACTION: Action = {
    'type': int(ActionType.NOOP),
    'move': 0,
    'target': 0,
    'ability': 0,
    'point': (0.0, 0.0),
    'rune': 0,
    'talent': 0,
}

action_space = spaces.Dict(
    {
        'type': spaces.Discrete(len(ActionType)),
        'move': spaces.Discrete(N_MOVE_DIRECTIONS),
        'target': spaces.Discrete(MAX_UNITS),
        'ability': spaces.Discrete(N_CAST_SLOTS),
        'point': spaces.Box(-np.inf, np.inf, (2,), np.float32),  # world x, y; MOVE_TO and TP bring it onto the map
        'rune': spaces.Discrete(len(RUNE_SPOTS)),
        'talent': spaces.Discrete(N_TALENTS),
    }
)

action_mask_space = spaces.Dict(
    {
        'type': spaces.MultiBinary(len(ActionType)),
        'attack_target': spaces.MultiBinary(MAX_UNITS),
        'cast_target': spaces.MultiBinary(MAX_UNITS),
        'ability': spaces.MultiBinary([len(ActionType), N_CAST_SLOTS]),  # row t: what action type t can use
        'rune': spaces.MultiBinary(len(RUNE_SPOTS)),
        'talent': spaces.MultiBinary(N_TALENTS),
    }
)


team_action_space = spaces.Dict(
    {
        'type': spaces.MultiDiscrete([len(ActionType)] * TEAM_SIZE),
        'move': spaces.MultiDiscrete([N_MOVE_DIRECTIONS] * TEAM_SIZE),
        'target': spaces.MultiDiscrete([MAX_UNITS] * TEAM_SIZE),
        'ability': spaces.MultiDiscrete([N_CAST_SLOTS] * TEAM_SIZE),
        'point': spaces.Box(-np.inf, np.inf, (TEAM_SIZE, 2), np.float32),
        'rune': spaces.MultiDiscrete([len(RUNE_SPOTS)] * TEAM_SIZE),
        'talent': spaces.MultiDiscrete([N_TALENTS] * TEAM_SIZE),
    }
)

team_action_mask_space = spaces.Dict(
    {
        'type': spaces.MultiBinary([TEAM_SIZE, len(ActionType)]),
        'attack_target': spaces.MultiBinary([TEAM_SIZE, MAX_UNITS]),
        'cast_target': spaces.MultiBinary([TEAM_SIZE, MAX_UNITS]),
        'ability': spaces.MultiBinary([TEAM_SIZE, len(ActionType), N_CAST_SLOTS]),
        'rune': spaces.MultiBinary([TEAM_SIZE, len(RUNE_SPOTS)]),
        'talent': spaces.MultiBinary([TEAM_SIZE, N_TALENTS]),
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
    if hero is not None:
        # a skill point can be spent while dead, stunned or silenced
        for tier in open_talent_tiers(hero_talents(hero), hero.level):
            mask['talent'][2 * tier : 2 * tier + 2] = hero.ability_points > 0
        mask['type'][ActionType.TALENT] = mask['talent'].any()
    if hero is None or not hero.is_alive:
        # alive but not reported by the client (fresh respawn): walking is all we can do
        mask['type'][[ActionType.MOVE, ActionType.MOVE_TO]] = hero is None and observation.origin is not None
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

    mask['rune'] = observation.arrays['runes'][:, RUNE_FEATURES.index('available')] > 0
    tp = next((item for item in hero.items if item.slot == TP_SLOT), None)
    courier = observation.courier

    can_act = not hero.is_stunned and not hero.is_hexed and not hero.is_nightmared
    mask['type'][[ActionType.MOVE, ActionType.MOVE_TO]] = can_act and not hero.is_rooted
    mask['type'][ActionType.STOP] = can_act
    mask['type'][ActionType.ATTACK] = can_act and not hero.is_disarmed and mask['attack_target'].any()
    mask['type'][ActionType.CAST] = can_act and mask['ability'][ActionType.CAST].any()
    mask['type'][ActionType.CAST_TARGET] = (
        can_act and mask['ability'][ActionType.CAST_TARGET].any() and mask['cast_target'].any()
    )
    mask['type'][ActionType.CAST_DIRECTION] = can_act and mask['ability'][ActionType.CAST_DIRECTION].any()
    mask['type'][ActionType.PICKUP_RUNE] = can_act and not hero.is_rooted and mask['rune'].any()
    # a root stops a teleport as well as a walk (the scroll's own notes in items.json)
    mask['type'][ActionType.TP] = (
        can_act and not hero.is_rooted and not hero.is_muted and tp is not None and tp.is_fully_castable
    )
    mask['type'][ActionType.COURIER] = (
        any(item.slot in STASH_SLOTS for item in hero.items) and courier is not None and courier.is_alive
    )
    return mask


def sample_masked_action(mask, rng):
    """Uniformly random legal action; handy for smoke tests and as a baseline."""

    def pick(bits):
        legal = np.flatnonzero(bits)
        return int(rng.choice(legal)) if len(legal) else 0

    action_type = pick(mask['type'])
    min_x, min_y, max_x, max_y = load_map().world_bounds
    return {
        'type': action_type,
        'move': int(rng.integers(N_MOVE_DIRECTIONS)),
        'target': pick(mask['cast_target'] if action_type == ActionType.CAST_TARGET else mask['attack_target']),
        'ability': pick(mask['ability'][action_type]),
        'point': rng.uniform((min_x, min_y), (max_x, max_y)).astype(np.float32),
        'rune': pick(mask['rune']),
        'talent': pick(mask['talent']),
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
    return {key: values[row] if key == 'point' else int(values[row]) for key, values in action.items()}


def sample_masked_team_action(mask, rng):
    """Uniformly random legal action for every hero of the team."""
    picks = [sample_masked_action({key: values[row] for key, values in mask.items()}, rng) for row in range(TEAM_SIZE)]
    spaces_by_key = team_action_space.spaces.items()
    return {key: np.array([pick[key] for pick in picks], space.dtype) for key, space in spaces_by_key}


def to_bridge_action(
    action: Action,
    observation: Observation,
    player: int,
    cast_slots: dict[int, tuple[str, tuple[str, ...]]] | None = None,
) -> dict[str, object] | None:
    """One entry for the bridge's actions list, or None when the action cannot be carried out.

    cast_slots is what lua reported for this hero ({ability index: (name, kinds)}); a slot of the vector kind goes
    out as DOTA_UNIT_ORDER_CAST_VECTOR, which bridge/lua/server_actions.lua issues with its second point.
    A hero that is alive but not reported is walked towards the map centre unless the action is a MOVE or MOVE_TO.
    TALENT and COURIER go out as orders that leave whatever the hero is doing alone.
    """
    if int(action['type']) == ActionType.TALENT and observation.hero is not None:
        talents = hero_talents(observation.hero)
        talent = int(action['talent'])
        return train_ability(player, talents[talent][0]) if 0 <= talent < len(talents) else None
    if observation.origin is None:
        return None
    if observation.hero is None and int(action['type']) not in (ActionType.MOVE, ActionType.MOVE_TO):
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
        # Note (ruidu): MOVE_TO_POSITION walks the client's own path around trees and cliffs, MOVE_DIRECTLY
        # walks into them and stops there (the two lua handlers spell the difference out). One step is short,
        # so its path cannot detour far: a wall across the direction holds the hero, which is what MOVE_TO is for.
        return {
            'actionType': 'DOTA_UNIT_ORDER_MOVE_TO_POSITION',
            'player': player,
            'moveToLocation': {
                'location': {
                    'x': observation.origin[0] + MOVE_DISTANCE * math.cos(angle),
                    'y': observation.origin[1] + MOVE_DISTANCE * math.sin(angle),
                    'z': 0.0,
                }
            },
        }
    if action_type in (ActionType.MOVE_TO, ActionType.TP):
        # the client plans the whole way to the point itself; the space leaves point unbounded, the map does not
        min_x, min_y, max_x, max_y = load_map().world_bounds
        x, y = action['point']
        location = {'x': min(max(float(x), min_x), max_x), 'y': min(max(float(y), min_y), max_y), 'z': 0.0}
        if action_type == ActionType.MOVE_TO:
            return {
                'actionType': 'DOTA_UNIT_ORDER_MOVE_TO_POSITION',
                'player': player,
                'moveToLocation': {'location': location},
            }
        return {
            'actionType': 'DOTA_UNIT_ORDER_CAST_POSITION',
            'player': player,
            'castLocation': {'abilitySlot': -1 - TP_SLOT, 'location': location},
        }
    if action_type == ActionType.PICKUP_RUNE:
        rune = int(action['rune'])
        if not 0 <= rune < len(RUNE_SPOTS):
            return None
        # the server VM picks up the rune lying there; Action_PickUpRune(RUNE_POWERUP_2) goes astray (VERSION_DIFF 3.2)
        x, y = load_map().runes[rune]
        return {
            'actionType': 'DOTA_UNIT_ORDER_PICKUP_RUNE',
            'player': player,
            'pickUpRune': {'location': {'x': float(x), 'y': float(y)}},
        }
    if action_type == ActionType.COURIER:
        return {
            'actionType': 'ACTION_COURIER',
            'player': player,
            'courier': {'action': 'COURIER_ACTION_TAKE_AND_TRANSFER_ITEMS'},
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


def parse_action(action, mask) -> tuple[Action, str | None]:
    """A JSON string or mapping from an LLM to an action dict, plus why it was rejected.

    Never raises: anything unparseable or illegal comes back as NOOP with a message to show the model.
    For a team observation, slice the mask to one row first.
    """
    noop = NOOP_ACTION.copy()
    try:
        data = json.loads(action) if isinstance(action, str) else dict(action)
        action_type = ActionType[str(data['type']).upper()]
        x, y = (float(number) for number in data.get('point', (0.0, 0.0)))
        parsed: Action = {
            'type': int(action_type),
            'move': int(data.get('move', 0)) % N_MOVE_DIRECTIONS,
            'target': int(data.get('target', 0)),
            'ability': int(data.get('ability', 0)),
            'point': (x, y),
            'rune': int(data.get('rune', 0)),
            'talent': int(data.get('talent', 0)),
        }
    except (ValueError, KeyError, TypeError) as e:
        return noop, f'cannot parse action: {e!r}'

    # the action file is JSON, which has no NaN, and the client has no point at infinity
    if not (math.isfinite(x) and math.isfinite(y)):
        return noop, f'point ({x}, {y}) is not on the map'
    if not mask['type'][action_type]:
        return noop, f'{action_type.name} is not legal right now'
    if action_type in CAST_TYPES and not (
        0 <= parsed['ability'] < N_CAST_SLOTS and mask['ability'][action_type][parsed['ability']]
    ):
        return noop, f'ability {parsed["ability"]} is not castable with {action_type.name}'
    # the field an action type picks one of, and the mask of the ones it may pick
    choices = {
        ActionType.ATTACK: ('target', 'attack_target'),
        ActionType.CAST_TARGET: ('target', 'cast_target'),
        ActionType.PICKUP_RUNE: ('rune', 'rune'),
        ActionType.TALENT: ('talent', 'talent'),
    }
    if action_type in choices:
        key, legal = choices[action_type]
        if not (0 <= parsed[key] < len(mask[legal]) and mask[legal][parsed[key]]):
            return noop, f'{key} {parsed[key]} is not valid for {action_type.name}'
    return parsed, None


def upkeep(
    player: int,
    hero: CMsgBotWorldState.Unit,
    courier: CMsgBotWorldState.Unit | None,
    ability_priority: tuple[int, ...],
    restock_tp: bool,
) -> list[dict[str, object]]:
    """The extra actions an env sends along with a hero's action: its skill points and a spare TP scroll.

    ability_priority slots are tried in order, keeping back one point for each talent tier the hero has reached
    without choosing, which TALENT spends. A scroll is bought once the hero has none left, not in its TP slot,
    its stash or on its courier: the shop puts it in the TP slot, anywhere else it waits in the stash.
    """
    extras = []
    kept = len(open_talent_tiers(hero_talents(hero), hero.level))
    if hero.ability_points > kept:
        extras += [train_ability(player, f'slot:{slot}', kept) for slot in ability_priority]
    scroll = load_records('items')[TP_SCROLL]
    carried = [*hero.items, *(courier.items if courier is not None else ())]
    gold = hero.reliable_gold + hero.unreliable_gold
    if restock_tp and gold >= scroll['cost'] and not any(item.ability_id == scroll['id'] for item in carried):
        extras.append(purchase_item(player, TP_SCROLL))
    return extras


def train_ability(player: int, ability: str, keep: int = 0) -> dict[str, object]:
    """ability is an ability name or "slot:N"; lua levels it only while the hero has more than keep points."""
    return {
        'actionType': 'DOTA_UNIT_ORDER_TRAIN_ABILITY',
        'player': player,
        'trainAbility': {'ability': ability, 'keep': keep},
    }


def purchase_item(player, item_name):
    return {'actionType': 'DOTA_UNIT_ORDER_PURCHASE_ITEM', 'player': player, 'purchaseItem': {'itemName': item_name}}


def chat(player: int, message: str, all_chat: bool = True) -> dict[str, object]:
    """Belongs in extra_actions, which lua runs all of, so talking never costs a hero its main action."""
    return {'actionType': 'ACTION_CHAT', 'player': player, 'chat': {'message': message, 'toAllchat': all_chat}}


def label(player: int, text: str) -> dict[str, object]:
    """An extra action the bots skip: the server VM makes text the hero's health bar label until the next one.

    The label is a networked string of LABEL_BYTES, so text is cut to fit between two characters.
    """
    fitted = text.encode('utf-8')[:LABEL_BYTES].decode('utf-8', errors='ignore')
    return {'actionType': 'ACTION_LABEL', 'player': player, 'label': {'text': fitted}}
