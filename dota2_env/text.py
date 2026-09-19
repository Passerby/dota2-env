"""Plain-text rendering of a world state, for `render_mode="ansi"` and for prompting LLM agents.

Unit rows are numbered exactly like the observation's unit table, so "target 3" in the text is
`target = 3` in the action space.
"""

from dota2_env.actions import ActionType, build_action_mask
from dota2_env.bridge.constants import UNIT_TYPE_HERO, UNIT_TYPE_LANE_CREEP, UNIT_TYPE_TOWER
from dota2_env.observation import build_observation, hero_abilities

UNIT_KIND = {UNIT_TYPE_HERO: 'hero', UNIT_TYPE_TOWER: 'tower', UNIT_TYPE_LANE_CREEP: 'creep'}


def _clock(dota_time):
    sign = '-' if dota_time < 0 else ''
    return f'{sign}{int(abs(dota_time)) // 60:d}:{int(abs(dota_time)) % 60:02d}'


def describe(world_state, team_id, player_id=None, ability_names=None):
    observation = build_observation(world_state, team_id, player_id)
    lines = [f'time {_clock(world_state.dota_time)}']
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

    mask = build_action_mask(observation, team_id)
    lines.append('abilities:')
    for slot, ability in enumerate(hero_abilities(hero)):
        if ability is None:
            continue
        name = (ability_names or {}).get(slot, f'ability_{ability.ability_id}')
        state = (
            'ready'
            if mask['ability'][slot]
            else (
                'not learned'
                if ability.level == 0
                else f'cd {ability.cooldown_remaining:.1f}s'
                if ability.cooldown_remaining > 0
                else 'unavailable'
            )
        )
        lines.append(f'  [{slot}] {name} lvl {ability.level} {state}')

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
