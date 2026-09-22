"""Plain-text rendering of a world state, for render_mode="ansi" and for prompting LLM agents.

Unit rows are numbered exactly like the observation's unit table, so "target 3" in the text is target = 3
in the action space; skill and item rows carry their ability index and the cast types that can use them.
"""

from dota2_env.actions import CAST_TYPES, ActionType, build_action_mask
from dota2_env.bridge.constants import UNIT_TYPE_HERO, UNIT_TYPE_LANE_CREEP, UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.observation import N_ABILITIES, build_observation, hero_abilities, hero_items

UNIT_KIND = {UNIT_TYPE_HERO: 'hero', UNIT_TYPE_TOWER: 'tower', UNIT_TYPE_LANE_CREEP: 'creep'}


def describe(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_id: int | None = None,
    cast_slots: dict[int, tuple[str, tuple[str, ...]]] | None = None,
    hero_players: set[int] | None = None,
) -> str:
    observation = build_observation(world_state, team_id, player_id, hero_players)
    dota_time = world_state.dota_time
    sign = '-' if dota_time < 0 else ''
    lines = [f'time {sign}{int(abs(dota_time)) // 60:d}:{int(abs(dota_time)) % 60:02d}']
    hero = observation.hero
    if hero is None:
        return lines[0] + '\nhero not spawned'

    lines.append(
        '{} lvl {} {} hp {}/{} mana {:.0f}/{:.0f} gold {} lh/dn {}/{} pos ({:.0f}, {:.0f}) dmg {} range {}'.format(
            hero.name.replace('npc_dota_hero_', ''),
            hero.level,
            'alive' if hero.is_alive else 'DEAD',
            hero.health,
            hero.health_max,
            hero.mana,
            hero.mana_max,
            hero.reliable_gold + hero.unreliable_gold,
            hero.last_hits,
            hero.denies,
            hero.location.x,
            hero.location.y,
            hero.attack_damage,
            hero.attack_range,
        )
    )

    slots = cast_slots or {}
    mask = build_action_mask(observation, team_id, slots)

    def cast_state(index: int, cooldown: float, learned: bool) -> str:
        usable = [action_type.name for action_type in CAST_TYPES if mask['ability'][action_type][index]]
        if usable:
            # Note (ruidu): a vector skill's second point follows the cast (actions.to_bridge_action), so say
            # where the swing or the curve will go.
            vector = ' (vector: runs on past the target / along the direction)' if 'vector' in slots[index][1] else ''
            return 'ready: ' + ' '.join(usable) + vector
        if not learned:
            return 'not learned'
        if cooldown > 0:
            return f'cd {cooldown:.1f}s'
        if index in slots and not slots[index][1]:
            return 'passive'
        return 'unavailable'

    lines.append('abilities:')
    for slot, ability in enumerate(hero_abilities(hero)):
        if ability is None:
            continue
        name = slots.get(slot, (f'ability_{ability.ability_id}',))[0]
        state = cast_state(slot, ability.cooldown_remaining, ability.level > 0)
        lines.append(f'  [{slot}] {name} lvl {ability.level} {state}')

    lines.append('items:')
    for slot, item in enumerate(hero_items(hero)):
        if item is None:
            continue
        index = N_ABILITIES + slot
        name = slots.get(index, (f'item_{item.ability_id}',))[0]
        charges = f' charges {item.charges}' if item.charges else ''
        lines.append(f'  [{index}] {name}{charges} {cast_state(index, item.cooldown_remaining, True)}')

    lines.append('units within 1600 (row: side kind hp distance dx,dy flags):')
    for row, unit in enumerate(observation.units):
        features = observation.arrays['units'][row]
        flags = []
        if mask['attack_target'][row]:
            flags.append('attackable')
        if features[3]:
            flags.append('in_range')
        if features[13]:
            flags.append('attacking_me')
        if unit.health <= hero.attack_damage:
            flags.append('one_hit')
        lines.append(
            '  [{}] {} {} hp {}/{} dist {:.0f} ({:+.0f},{:+.0f}) {}'.format(
                row,
                'enemy' if unit.team_id != team_id else 'ally',
                UNIT_KIND.get(unit.unit_type, 'neutral'),
                unit.health,
                unit.health_max,
                features[2] * 1600,
                unit.location.x - hero.location.x,
                unit.location.y - hero.location.y,
                ' '.join(flags),
            )
        )

    lines.append('legal action types: ' + ', '.join(t.name for t in ActionType if mask['type'][t]))
    return '\n'.join(lines)


def hero_blocks(
    world_state,
    team_id: int,
    player_ids: list[int],
    cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]] | None = None,
) -> list[str]:
    """One describe() block per controlled hero, in team observation row order.

    cast_slots is keyed by player id here, one level deeper than describe() takes it.
    """
    hero_players = {player.player_id for player in world_state.players}
    slots = cast_slots or {}
    return [describe(world_state, team_id, player_id, slots.get(player_id), hero_players) for player_id in player_ids]


def describe_team(world_state, team_id, player_ids, cast_slots=None):
    """One describe() block per controlled hero, numbered the way the team observation rows are."""
    blocks = hero_blocks(world_state, team_id, player_ids, cast_slots)
    return '\n\n'.join(
        f'=== hero {row} (player {player_id}) ===\n{block}'
        for row, (player_id, block) in enumerate(zip(player_ids, blocks, strict=True))
    )
