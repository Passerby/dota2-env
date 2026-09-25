"""Plain-text rendering of a world state, for render_mode="ansi" and for prompting LLM agents.

Unit rows are numbered exactly like the observation's unit table, so "target 3" in the text is target = 3
in the action space; skill and item rows carry their ability index and the cast types that can use them.
Heroes, skills, items and map spots go by the client's own names (dota2_env.game_text); the map lines
need the match's TreeTable and are left out without one, and the line of what comes next (events.json)
needs the game mode.
"""

import math
import re

import numpy as np

from dota2_env.actions import CAST_TYPES, MOVE_DISTANCE, N_MOVE_DIRECTIONS, ActionType, build_action_mask
from dota2_env.bridge.constants import (
    RUNE_WATER,
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_HERO,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_TOWER,
)
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import Language, display_name, item_names
from dota2_env.map_features import (
    LANDMARKS,
    MAP_SCALE,
    RUNE_SPOTS,
    RUNE_STATUS_AVAILABLE,
    EventKind,
    GameMode,
    TreeTable,
    load_map,
    upcoming,
)
from dota2_env.observation import (
    N_ABILITIES,
    STASH_SLOTS,
    TP_SLOT,
    build_observation,
    hero_abilities,
    hero_items,
    hero_talents,
    lane_players,
)

UNIT_KIND = {UNIT_TYPE_HERO: 'hero', UNIT_TYPE_TOWER: 'tower', UNIT_TYPE_LANE_CREEP: 'creep'}
TANGO_TREE_RANGE = 700  # how far lua looks for the tree a tango cast without a target eats (actions/use_ability.lua)
RAY_STEP = 32  # half a gridnav cell
LOWEST_LEVEL = 4  # GetHeightLevel of the river; map_features stores the level negated
WAVE_GAP = 800.0  # creeps of one side closer than this belong to one wave
# Note (ruidu): the client's own names (DOTA_Location_Name_*, DOTA_OutpostName_Default in dota_schinese.txt /
# dota_english.txt on 6934). Its 上路 / 下路 (Top / Bottom) are the halves of the map that map.json calls top and
# bottom, as in its own 上路强化神符 for the top power rune.
TEAMS: dict[int, dict[Language, str]] = {
    TEAM_RADIANT: {'zh': '天辉', 'en': 'Radiant'},
    TEAM_DIRE: {'zh': '夜魇', 'en': 'Dire'},
}
LANES: dict[str, dict[Language, str]] = {
    'top': {'zh': '上路', 'en': 'Top '},
    'mid': {'zh': '中路', 'en': 'Mid '},
    'bottom': {'zh': '下路', 'en': 'Bottom '},
}
TOWER_TIERS: dict[str, dict[Language, str]] = {
    '1': {'zh': '一塔', 'en': 'Tier 1 Tower'},
    '2': {'zh': '二塔', 'en': 'Tier 2 Tower'},
    '3': {'zh': '三塔', 'en': 'Tier 3 Tower'},
    '4': {'zh': '四塔', 'en': 'Tier 4 Tower'},
}
ANCIENT: dict[Language, str] = {'zh': '遗迹', 'en': 'Ancient'}
SPOT_KINDS: dict[str, dict[Language, str]] = {
    'power': {'zh': '强化神符', 'en': 'Power Rune'},
    'bounty': {'zh': '赏金神符', 'en': 'Bounty Rune'},
    'roshan': {'zh': '肉山巢穴', 'en': "Roshan's Pit"},
    'tormentor': {'zh': '痛苦魔方', 'en': 'Tormentor'},
    'wisdom': {'zh': '智慧神龛', 'en': 'Shrine of Wisdom'},
    'lotus': {'zh': '莲花池', 'en': 'Lotus Pool'},
    'outpost': {'zh': '前哨', 'en': 'Outpost'},
}
# Note (ruidu): the world state's rune type is the bot API's RUNE_* value (the bot VM's globals on 6934); the names
# are the client's DOTA_HUD_Rune_* on 6938. 8 is RUNE_XP, which no longer comes to a rune spot since 7.38.
RUNE_TYPES: dict[int, dict[Language, str]] = {
    0: {'zh': '增伤神符', 'en': 'Amplify Damage rune'},
    1: {'zh': '极速神符', 'en': 'Haste rune'},
    2: {'zh': '幻象神符', 'en': 'Illusion rune'},
    3: {'zh': '隐身神符', 'en': 'Invisibility rune'},
    4: {'zh': '恢复神符', 'en': 'Regeneration rune'},
    5: {'zh': '赏金神符', 'en': 'Bounty rune'},
    6: {'zh': '奥术神符', 'en': 'Arcane rune'},
    7: {'zh': '圣水神符', 'en': 'Water rune'},
    9: {'zh': '护盾神符', 'en': 'Shield rune'},
}
SPOT_GROUPS: dict[str, dict[Language, str]] = {
    'rune spots': {'zh': '神符点', 'en': 'Rune spots'},
    'landmarks': {'zh': '地标', 'en': 'Landmarks'},
}
RE_TOWER = re.compile(r'npc_dota_(?:good|bad)guys_tower([1-4])(?:_(top|mid|bot))?')


def clock(dota_time: float) -> str:
    """Game time the way the client's clock shows it: -1:30 before the horn, 12:05 after."""
    sign = '-' if dota_time < 0 else ''
    return f'{sign}{int(abs(dota_time)) // 60:d}:{int(abs(dota_time)) % 60:02d}'


def relative(dx: float, dy: float) -> str:
    """Where something is from the hero, the way unit rows put it: dist 2516 (-140,+2512)."""
    return f'dist {math.hypot(dx, dy):.0f} ({round(dx):+d},{round(dy):+d})'


def tower_name(match: re.Match[str], language: Language) -> str:
    """The client's name of the tower whose unit name RE_TOWER matched: 下路一塔, or 四塔 in the base."""
    tier, lane = match.groups()
    return (LANES[lane.replace('bot', 'bottom')][language] if lane else '') + TOWER_TIERS[tier][language]


def spot_name(spot: str, language: Language = 'zh') -> str:
    """The client's name of a RUNE_SPOTS or LANDMARKS entry: power_top is 上路强化神符."""
    kind, side = spot.rsplit('_', 1)
    return LANES[side][language] + SPOT_KINDS[kind][language]


def event_name(kind: EventKind, language: Language = 'zh') -> str:
    """The client's name of what an events.json entry brings: 赏金神符, 圣水神符, 疗伤莲花."""
    if kind == 'lotus':
        return display_name('item_famango', language)
    if kind == 'water':
        return RUNE_TYPES[RUNE_WATER][language]
    return SPOT_KINDS[kind][language]


def map_reference(language: Language = 'zh') -> list[str]:
    """Where both ancients and all towers, the rune spots and the landmarks stand, one line per group.

    In world coordinates, the ones pos is given in: x grows to the right of the map and y upwards.
    """
    static = load_map()
    lines = []
    for team in (TEAM_RADIANT, TEAM_DIRE):
        places = []
        for name, owner, x, y in static.buildings:
            match = RE_TOWER.fullmatch(name)
            if owner == team and name.endswith('_fort'):
                places.append(((0, ''), ANCIENT[language], x, y))
            elif owner == team and match is not None:
                lane = ('top', 'mid', 'bot', None).index(match.group(2)) + 1
                places.append(((lane, match.group(1)), tower_name(match, language), x, y))
        places.sort(key=lambda place: place[0])
        lines.append(f'{TEAMS[team][language]}: ' + '; '.join(f'{name} ({x:.0f}, {y:.0f})' for _, name, x, y in places))
    for group, spots, positions in (
        ('rune spots', RUNE_SPOTS, static.runes),
        ('landmarks', LANDMARKS, static.landmarks),
    ):
        named = [
            f'{spot_name(spot, language)} ({x:.0f}, {y:.0f})' for spot, (x, y) in zip(spots, positions, strict=True)
        ]
        lines.append(f'{SPOT_GROUPS[group][language]}: ' + '; '.join(named))
    return lines


def describe(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_id: int | None = None,
    cast_slots: dict[int, tuple[str, tuple[str, ...]]] | None = None,
    hero_players: set[int] | None = None,
    trees: TreeTable | None = None,
    language: Language = 'zh',
    mode: GameMode | None = None,
) -> str:
    observation = build_observation(world_state, team_id, player_id, hero_players, trees)
    lines = [f'time {clock(world_state.dota_time)}']
    hero = observation.hero
    if hero is None:
        return lines[0] + '\nhero not spawned'

    lines.append(
        '{} lvl {} {} hp {}/{} mana {:.0f}/{:.0f} gold {} lh/dn {}/{} pos ({:.0f}, {:.0f}) dmg {} range {}'.format(
            display_name(hero.name, language),
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

    static = load_map()
    x, y = hero.location.x, hero.location.y
    here = static.cell(x, y)
    if trees is not None:
        # The first tree, wall or change of ground within one move, looked for along each move direction.
        found = {'wall': [], 'tree': [], 'up': [], 'down': []}
        for direction in range(N_MOVE_DIRECTIONS):
            angle = 2 * math.pi * direction / N_MOVE_DIRECTIONS
            for step in range(RAY_STEP, int(MOVE_DISTANCE) + 1, RAY_STEP):
                dx, dy = step * math.cos(angle), step * math.sin(angle)
                cell = static.cell(x + dx, y + dy)
                if cell == here:
                    continue
                if static.blocked[cell]:
                    kind = 'wall'
                elif trees.counts[cell] > 0:
                    kind = 'tree'
                elif static.height[cell] != static.height[here]:
                    kind = 'up' if static.height[cell] > static.height[here] else 'down'
                else:
                    continue
                found[kind].append(f'({round(dx):+d},{round(dy):+d})')
                break
        offsets = static.tree_positions[trees.standing] - (x, y)
        distances = np.hypot(offsets[:, 0], offsets[:, 1])
        nearest = int(np.argmin(distances))
        if distances[nearest] <= TANGO_TREE_RANGE:
            tree = f'nearest tree {relative(*offsets[nearest])}'
        else:
            tree = f'no tree within {TANGO_TREE_RANGE}'
        ahead = '; '.join(f'{kind} ' + ' '.join(places) for kind, places in found.items() if places)
        lines.append(
            f'terrain: height {LOWEST_LEVEL + static.height[here]:.0f} (0 river .. 3 fountain), {tree}, '
            f'within {MOVE_DISTANCE:.0f}: {ahead or "clear"}'
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

    def labelled(name: str) -> str:
        shown = display_name(name, language)
        return name if shown == name else f'{name} {shown}'

    def item_label(item: CMsgBotWorldState.Ability) -> str:
        """An item the world state names by id only, where lua reports no name: the stash, the TP slot."""
        charges = f' charges {item.charges}' if item.charges else ''
        return labelled(item_names().get(item.ability_id, f'item_{item.ability_id}')) + charges

    lines.append('abilities:')
    for slot, ability in enumerate(hero_abilities(hero)):
        if ability is None:
            continue
        state = cast_state(slot, ability.cooldown_remaining, ability.level > 0)
        name = slots.get(slot, (f'ability_{ability.ability_id}',))[0]
        lines.append(f'  [{slot}] {labelled(name)} lvl {ability.level} {state}')

    talents = hero_talents(hero)
    learned = [f'[{index}] {display_name(name, language)} learned' for index, (name, has) in enumerate(talents) if has]
    ready = [
        f'[{index}] {display_name(name, language)}' for index, (name, _) in enumerate(talents) if mask['talent'][index]
    ]
    if learned or ready:
        lines.append('talents: ' + '; '.join(learned + ([' / '.join(ready) + ' ready: TALENT'] if ready else [])))

    lines.append('items:')
    for slot, item in enumerate(hero_items(hero)):
        if item is None:
            continue
        index = N_ABILITIES + slot
        charges = f' charges {item.charges}' if item.charges else ''
        state = cast_state(index, item.cooldown_remaining, True)
        name = slots.get(index, (f'item_{item.ability_id}',))[0]
        lines.append(f'  [{index}] {labelled(name)}{charges} {state}')
    carried = {item.slot: item for item in hero.items}
    if TP_SLOT in carried:
        tp = carried[TP_SLOT]
        state = (
            'ready: TP'
            if mask['type'][ActionType.TP]
            else f'cd {tp.cooldown_remaining:.1f}s'
            if tp.cooldown_remaining > 0
            else 'unavailable'
        )
        lines.append(f'  TP slot: {item_label(tp)} {state}')
    stash = [item_label(carried[slot]) for slot in STASH_SLOTS if slot in carried]
    courier = observation.courier
    on_courier = [item_label(item) for item in courier.items] if courier is not None else []
    if stash or on_courier:
        if courier is None:
            where = 'no courier'
        elif not courier.is_alive:
            where = 'courier dead'
        else:
            where = f'courier {relative(courier.location.x - x, courier.location.y - y)}'
        carrying = f' carrying {", ".join(on_courier)}' if on_courier else ''
        lines.append(f'stash: {", ".join(stash) or "empty"}; {where}{carrying}')

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
        if trees is not None:
            ground = static.height[static.cell(unit.location.x, unit.location.y)] - static.height[here]
            flags += ['higher'] if ground > 0 else ['lower'] if ground < 0 else []
        lines.append(
            '  [{}] {} {}{} hp {}/{} {} {}'.format(
                row,
                'enemy' if unit.team_id != team_id else 'ally',
                UNIT_KIND.get(unit.unit_type, 'neutral'),
                f' {display_name(unit.name, language)}' if unit.unit_type == UNIT_TYPE_HERO else '',
                unit.health,
                unit.health_max,
                relative(unit.location.x - hero.location.x, unit.location.y - hero.location.y),
                ' '.join(flags),
            )
        )

    # Note (ruidu): the unit table ends at UNIT_RADIUS; these two lines are the rest of what the team sees, put
    # in the world coordinates MOVE_TO takes, so a model can walk to where its lane's creeps meet or away from a gank.
    enemy = TEAM_DIRE if team_id == TEAM_RADIANT else TEAM_RADIANT
    players = lane_players(world_state) if hero_players is None else hero_players
    heroes = []
    for unit in world_state.units:
        if unit.unit_type != UNIT_TYPE_HERO or unit.team_id != enemy or unit.player_id not in players:
            continue
        if unit.is_alive and not unit.is_illusion:
            at = (unit.location.x, unit.location.y)
            heroes.append(
                f'{display_name(unit.name, language)} lvl {unit.level} hp {unit.health}/{unit.health_max} '
                f'at ({at[0]:.0f}, {at[1]:.0f}) dist {math.dist((x, y), at):.0f}'
            )
    lines.append('enemy heroes in sight: ' + ('; '.join(heroes) or 'none'))

    creeps = [unit for unit in world_state.units if unit.unit_type == UNIT_TYPE_LANE_CREEP and unit.is_alive]
    wave_of = list(range(len(creeps)))
    for i, creep in enumerate(creeps):
        for j, other in enumerate(creeps[:i]):
            near = math.dist((creep.location.x, creep.location.y), (other.location.x, other.location.y)) < WAVE_GAP
            if creep.team_id == other.team_id and near and wave_of[i] != wave_of[j]:
                merged = wave_of[i]
                wave_of = [wave_of[j] if wave == merged else wave for wave in wave_of]
    waves = []
    for wave in set(wave_of):
        members = [creep for creep, of in zip(creeps, wave_of, strict=True) if of == wave]
        center = np.mean([(creep.location.x, creep.location.y) for creep in members], axis=0)
        # a wave is on the lane whose path passes nearest, as far along it as the path point nearest to it
        gaps = {lane: np.hypot(*(path - center).T) for lane, path in static.lanes.items()}
        lane = min(gaps, key=lambda name: gaps[name].min())
        health = sum(creep.health for creep in members) / max(sum(creep.health_max for creep in members), 1)
        side = 'ours' if members[0].team_id == team_id else 'enemy'
        described = (
            f'{LANES[lane.replace("bot", "bottom")][language]} {side} {len(members)} hp {100 * health:.0f}% '
            f'at ({center[0]:.0f}, {center[1]:.0f}) dist {math.dist((x, y), center):.0f}'
        )
        waves.append((('top', 'mid', 'bot').index(lane), int(np.argmin(gaps[lane])), described))
    lines.append('creep waves: ' + ('; '.join(described for *_, described in sorted(waves)) or 'none'))

    if trees is not None:
        # Note (ruidu): a lane is found by its towers; the outermost one still standing is where a hero
        # starts laning and falls back to, so it is given for each lane rather than the lane's path.
        towers = {}
        for unit in world_state.units:
            match = RE_TOWER.fullmatch(unit.name)
            if match is not None and match.group(2) is not None and unit.is_alive:
                lane = (unit.team_id, match.group(2))
                if lane not in towers or match.group(1) < towers[lane][0].group(1):
                    towers[lane] = (match, unit)
        for owner, label in ((team_id, 'our'), (enemy, 'enemy')):
            outermost = []
            for match, tower in (towers[owner, lane] for lane in ('top', 'mid', 'bot') if (owner, lane) in towers):
                outermost.append(
                    f'{tower_name(match, language)} {relative(tower.location.x - x, tower.location.y - y)}'
                )
            lines.append(f'{label} towers, outermost standing per lane: ' + ('; '.join(outermost) or 'none'))
        arrays = observation.arrays
        # Note (ruidu): the type rides on the world state's rune_infos whether or not the team sees the spot,
        # and can be older than the rune lying there (docs/VERSION_DIFF.md), so it is only a hint.
        rune_types = {}
        for rune in world_state.rune_infos:
            at = (rune.location.x, rune.location.y)
            nearest = min(range(len(RUNE_SPOTS)), key=lambda index: math.dist(static.runes[index], at))
            if rune.status == RUNE_STATUS_AVAILABLE and rune.type in RUNE_TYPES:
                rune_types[RUNE_SPOTS[nearest]] = RUNE_TYPES[rune.type][language]
        for group, spots, rows in (
            ('rune spots', RUNE_SPOTS, arrays['runes']),
            ('landmarks', LANDMARKS, arrays['landmarks']),
        ):
            named = []
            for index, (spot, (rel_x, rel_y, _, flag)) in enumerate(zip(spots, rows, strict=True)):
                words = [spot_name(spot, language), relative(rel_x * MAP_SCALE, rel_y * MAP_SCALE)]
                if group == 'rune spots':
                    words.insert(0, f'[{index}]')  # the rune PICKUP_RUNE takes
                if spot.startswith(('power', 'bounty')) and flag:
                    words += ['available', rune_types[spot]] if spot in rune_types else ['available']
                elif spot.startswith('outpost'):
                    words.append('ours' if flag else 'enemy')
                named.append(' '.join(words))
            lines.append(f'{group}: ' + '; '.join(named))
        if mode is not None:
            # Note (ruidu): every place in full, with the at (x, y) MOVE takes, rather than the rows of the two lines
            # above, which made the model look each one up.
            spots = dict(zip(RUNE_SPOTS + LANDMARKS, [*static.runes, *static.landmarks], strict=True))
            coming = []
            for time, event in upcoming(mode, world_state.dota_time):
                places = [
                    f'{spot_name(spot, language)} at ({spots[spot][0]:.0f}, {spots[spot][1]:.0f}) '
                    f'dist {math.dist((x, y), spots[spot]):.0f}'
                    for spot in event.spots
                ]
                coming.append(
                    f'  {clock(time)} in {clock(time - world_state.dota_time)} {event_name(event.kind, language)}: '
                    + (' or ' if event.one_of else ', ').join(places)
                )
            lines += ['upcoming:', *coming] if coming else ['upcoming: none']

    # MOVE_TO is legal exactly when MOVE is; a model names it by giving MOVE a point
    legal = [t.name for t in ActionType if mask['type'][t] and t != ActionType.MOVE_TO]
    lines.append('legal action types: ' + ', '.join(legal))
    return '\n'.join(lines)


def hero_blocks(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_ids: list[int],
    cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]] | None = None,
    trees: TreeTable | None = None,
    language: Language = 'zh',
    mode: GameMode | None = None,
) -> list[str]:
    """One describe() block per controlled hero, in team observation row order.

    cast_slots is keyed by player id here, one level deeper than describe() takes it.
    """
    hero_players = {player.player_id for player in world_state.players}
    slots = cast_slots or {}
    return [
        describe(world_state, team_id, player_id, slots.get(player_id), hero_players, trees, language, mode)
        for player_id in player_ids
    ]


def describe_team(
    world_state: CMsgBotWorldState,
    team_id: int,
    player_ids: list[int],
    cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]] | None = None,
    trees: TreeTable | None = None,
) -> str:
    """One describe() block per controlled hero, numbered the way the team observation rows are."""
    blocks = hero_blocks(world_state, team_id, player_ids, cast_slots, trees, mode='allpick5v5')
    return '\n\n'.join(
        f'=== hero {row} (player {player_id}) ===\n{block}'
        for row, (player_id, block) in enumerate(zip(player_ids, blocks, strict=True))
    )
