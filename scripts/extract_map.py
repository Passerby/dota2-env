"""Static map data of the installed client, merged from three sources into dota2_env/data/map.json.

    python scripts/extract_map.py --vrf ~/.cache/dota2_env/vrf-20.0/Source2Viewer-CLI --patch 7.41f

The gridnav comes from maps/dota.gnv inside the map's VPK, every placed entity from its entity lump
(decompiled with Source2Viewer-CLI of ValveResourceFormat), and trees, height levels, lanes and the
bot API's view of runes, buildings and camps from scripts/map_scan.lua on a headless 1v1, which needs
Steam running. The sources are checked against each other and against one world state before
anything is written; --reuse-scan rebuilds from the last scan in --work without starting Dota.
docs/MAP_DATA.md has the schema, the decisions and the numbers of the last run.
"""

import argparse
import collections
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from collections.abc import Sequence

import numpy as np

from dota2_env.bridge.constants import (
    DOTA_GAMEMODE_1V1MID,
    HOST_MODE_DEDICATED,
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_BARRACKS,
    UNIT_TYPE_BUILDING,
    UNIT_TYPE_FORT,
    UNIT_TYPE_TOWER,
)
from dota2_env.bridge.game import CONTROL_AGENT, CONTROL_IDLE, DotaGame, get_default_game_path
from dota2_env.bridge.worldstate import connect, parse_world_state, read_raw_world_state
from dota2_env.map_features import GRIDNAV_TRAVERSABLE, MAP_PATH, RUNE_STATUS_AVAILABLE, load_map

SCAN_HERO = 'npc_dota_hero_nevermore'
RE_SCAN = re.compile(r'MAPSCAN (\d+) (\w+) (.*)$')
RE_BLOCK = re.compile(r'^====\d+====$', re.MULTILINE)
RE_KEY_VALUE = re.compile(r'^(\w+) +(.+)$', re.MULTILINE)
RE_MODEL = re.compile(r'^\[\d+/\d+\] (.+)$', re.MULTILINE)
RE_BOUNDS = re.compile(r'm_vM(in|ax)Bounds = \[ (.+?) \]')
BUILDING_TYPES = (UNIT_TYPE_TOWER, UNIT_TYPE_BARRACKS, UNIT_TYPE_FORT, UNIT_TYPE_BUILDING)
EXPECTED_ENTITIES = {
    'ent_dota_tree': 2475,
    'npc_dota_tower': 22,
    'npc_dota_barracks': 12,
    'npc_dota_fort': 2,
    'npc_dota_neutral_spawner': 28,
    'npc_dota_watch_tower': 2,
    'npc_dota_lantern': 10,
    'npc_dota_xp_fountain': 2,
    'npc_dota_lotus_pool': 2,
    'dota_item_rune_spawner_bounty': 2,
    'dota_item_rune_spawner_powerup': 2,
}


def vector(text: str) -> list[float]:
    """An entity-dump vector such as [ -3142.633545, 2398.217285, 102.362869 ], rounded to 0.1."""
    return [round(float(part), 1) for part in text.strip('[] ').split(',')]


def top_then_bottom(points: list[list[float]]) -> list[list[float]]:
    """A mirrored pair of map objects as [top, bottom]; top is the one north-west of the mid lane (y > x)."""
    assert len(points) == 2 and (points[0][1] > points[0][0]) != (points[1][1] > points[1][0]), points
    return sorted(points, key=lambda point: point[1] <= point[0])


def nearest(points: Sequence[Sequence[float]], x: float, y: float) -> float:
    """Distance from x, y to the closest of points."""
    return min(math.dist(point[:2], (x, y)) for point in points)


def run_vrf(vrf: str, *args: str) -> str:
    return subprocess.run([vrf, *args], capture_output=True, text=True, check=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--vrf', required=True, help='path to Source2Viewer-CLI (docs/MAP_DATA.md)')
    parser.add_argument('--patch', required=True, help='gameplay patch of the installed client, e.g. 7.41f')
    parser.add_argument('--work', default=os.path.join(tempfile.gettempdir(), 'dota2_env_mapscan'))
    parser.add_argument('--out', default=MAP_PATH)
    parser.add_argument('--timescale', type=float, default=4.0)
    parser.add_argument('--seconds', type=int, default=300, help='give up on the scan after this long')
    parser.add_argument('--reuse-scan', action='store_true', help='use the scan already in --work')
    args = parser.parse_args()
    game_path = get_default_game_path()
    maps_vpk = os.path.join(game_path, 'dota', 'maps', 'dota.vpk')
    os.makedirs(args.work, exist_ok=True)
    failures = []

    # The client build all of this belongs to.
    with open(os.path.join(game_path, 'dota', 'steam.inf'), encoding='utf-8') as handle:
        steam_inf = dict(line.strip().split('=', 1) for line in handle if '=' in line)
    print(f'client {steam_inf["ClientVersion"]} of {steam_inf["VersionDate"]}, patch {args.patch}', flush=True)

    # maps/dota.gnv from the map's VPK (version 2: a tree of extension / directory / name strings).
    with open(maps_vpk, 'rb') as vpk:
        signature, version, tree_size = struct.unpack('<III', vpk.read(12))
        assert (signature, version) == (0x55AA1234, 2), f'{maps_vpk} is not a version 2 VPK'
        vpk.read(16)

        def read_string() -> str:
            text = bytearray()
            while (char := vpk.read(1)) != b'\0':
                text += char
            return text.decode()

        entries = {}
        while extension := read_string():
            while directory := read_string():
                while name := read_string():
                    _, preload, archive, offset, length, _ = struct.unpack('<IHHIIH', vpk.read(18))
                    entries[f'{directory}/{name}.{extension}'] = (archive, offset, length, vpk.read(preload))
        archive, offset, length, preload = entries['maps/dota.gnv']
        assert archive == 0x7FFF, 'the gridnav is expected inside dota.vpk itself'
        vpk.seek(28 + tree_size + offset)
        gridnav = preload + vpk.read(length)
    magic, cell, offset_x, offset_y, width, height, min_x, min_y = struct.unpack_from('<Ifffiiii', gridnav)
    assert magic == 0xFADEBEAD and offset_x == offset_y == cell / 2 and len(gridnav) == 32 + width * height
    cell, x0, y0 = int(cell), int(min_x * cell), int(min_y * cell)
    flags = np.frombuffer(gridnav[32:], np.uint8).reshape(height, width)
    print(f'gridnav {width} x {height} cells of {cell}, from ({x0}, {y0})', flush=True)

    # Every placed entity, from the decompiled entity lumps: blocks of "key   value" lines.
    run_vrf(args.vrf, '-i', maps_vpk, '-f', 'maps/dota/entities/', '-e', 'vents_c', '-d', '-o', f'{args.work}/vrf')
    entity_dir = os.path.join(args.work, 'vrf', 'maps', 'dota', 'entities')
    entities = []
    for name in sorted(os.listdir(entity_dir)):
        with open(os.path.join(entity_dir, name), encoding='utf-8') as handle:
            for block in RE_BLOCK.split(handle.read())[1:]:
                entity = {'file': name}
                for key, value in RE_KEY_VALUE.findall(block):
                    entity.setdefault(key, value.strip('"'))
                entities.append(entity)
    by_class = collections.defaultdict(list)
    for entity in entities:
        by_class[entity['classname']].append(entity)
    by_target = {(e['classname'], e['targetname'].removeprefix('[PR#]')): e for e in entities if 'targetname' in e}
    for classname, expected in EXPECTED_ENTITIES.items():
        print(f'{classname:34} {len(by_class[classname]):5}', flush=True)
        if len(by_class[classname]) != expected:
            failures.append(f'{len(by_class[classname])} {classname}, expected {expected}')

    # Neutral camp boxes: the trigger's origin plus the bounds of its physics hull.
    run_vrf(
        args.vrf, '-i', maps_vpk, '-f', 'maps/dota/entities/neutralcamp_', '-e', 'vmdl_c', '-o', f'{args.work}/models'
    )
    physics = run_vrf(args.vrf, '-i', f'{args.work}/models/maps/dota/entities', '--recursive', '-b', 'PHYS')
    model_bounds = {}
    for model, text in zip(RE_MODEL.findall(physics), RE_MODEL.split(physics)[2::2], strict=True):
        bounds = dict(RE_BOUNDS.findall(text))
        model_bounds[os.path.basename(model).removesuffix('.vmdl_c')] = (vector(bounds['in']), vector(bounds['ax']))
    boxes = {}
    for trigger in by_class['trigger_multiple']:
        camp = trigger.get('targetname', '').removeprefix('[PR#]')
        if not camp.startswith('neutralcamp_'):
            continue
        assert trigger['angles'] == '[ 0.0, 0.0, 0.0 ]', f'{camp} is rotated'
        low, high = model_bounds[os.path.basename(trigger['model']).removesuffix('.vmdl')]
        x, y, _ = vector(trigger['origin'])
        boxes[camp] = [round(x + low[0], 1), round(y + low[1], 1), round(x + high[0], 1), round(y + high[1], 1)]

    # The bot API's view, printed by map_scan.lua from a headless 1v1, plus one world state of the same match.
    scan_path, frame_path = os.path.join(args.work, 'scan.log'), os.path.join(args.work, 'worldstate.bin')
    if not args.reuse_scan:
        game = DotaGame(
            host_mode=HOST_MODE_DEDICATED,
            game_mode=DOTA_GAMEMODE_1V1MID,
            host_timescale=args.timescale,
            ticks_per_observation=30,
            heroes={TEAM_RADIANT: (SCAN_HERO,), TEAM_DIRE: (SCAN_HERO,)},
            control={TEAM_RADIANT: CONTROL_AGENT, TEAM_DIRE: CONTROL_IDLE},
        )
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'map_scan.lua'), encoding='utf-8') as handle:
            lua = handle.read()
        for placeholder, value in {'__X0__': x0, '__Y0__': y0, '__CELL__': cell, '__WIDTH__': width}.items():
            lua = lua.replace(placeholder, str(value))
        lua = lua.replace('__HEIGHT__', str(height))
        for name in os.listdir(game.bot_path):
            if name.startswith('bot_') and name != 'bot_wisp.lua':
                with open(os.path.join(game.bot_path, name), 'w', encoding='utf-8') as handle:
                    handle.write(lua)
        game.run_dota()
        lines, frame, log_offset, finished = [], None, 0, False
        deadline = time.time() + args.seconds
        try:
            sock = connect(DotaGame.PORT_WORLDSTATES[TEAM_RADIANT], timeout=args.seconds)
            sock.settimeout(60)
            while not finished:
                if time.time() > deadline:
                    raise TimeoutError(f'the scan did not finish within {args.seconds}s ({len(lines)} lines)')
                raw = read_raw_world_state(sock)  # keeps the socket drained as well
                if frame is None and len(parse_world_state(raw).units) >= 50:
                    frame = raw
                if not os.path.isfile(game.console_log_path):
                    continue
                with open(game.console_log_path, 'rb') as handle:
                    handle.seek(log_offset)
                    chunk = handle.read()
                end = chunk.rfind(b'\n') + 1  # the client may be in the middle of a line
                log_offset += end
                for line in chunk[:end].decode('utf-8', 'replace').splitlines():
                    match = RE_SCAN.search(line)
                    if match is not None:
                        lines.append(match.group(0))
                        finished = finished or match.group(2) in ('done', 'error')
        finally:
            game.stop_dota_pids()
            game.remove_dota_files()
            shutil.rmtree(game.session_folder, ignore_errors=True)
        assert frame is not None, 'no world state with the buildings in it arrived'
        with open(scan_path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')
        with open(frame_path, 'wb') as handle:
            handle.write(frame)
    with open(scan_path, encoding='utf-8') as handle:
        records = [line.split(' ', 3)[1:] for line in handle.read().splitlines()]
    with open(frame_path, 'rb') as handle:
        world_state = parse_world_state(handle.read())
    assert [int(seq) for seq, _, _ in records] == list(range(1, len(records) + 1)), 'the console lost scan lines'
    tagged = collections.defaultdict(list)
    for _, tag, payload in records:
        tagged[tag].append(payload)
    assert not tagged['error'], f'map_scan.lua failed: {tagged["error"]}'
    api = {}
    for payload in tagged['globals']:
        api |= json.loads(payload)
    with open(os.path.join(args.work, 'globals.json'), 'w', encoding='utf-8') as handle:
        json.dump(api, handle, indent=1, sort_keys=True)
    print(f'probe {tagged["probe"][0]}', flush=True)

    # Trees by bot API id: trailing misses mark the end of the list, a miss before that is a hole.
    trees = []
    for payload in tagged['trees']:
        first, text = payload.split(' ', 1)
        trees += [[int(first) + i, *(float(v) for v in triple.split(','))] for i, triple in enumerate(text.split(';'))]
    while trees[-1][1:3] == [0.0, 0.0]:
        trees.pop()
    holes = [tree_id for tree_id, x, y, _ in trees if (x, y) == (0.0, 0.0)]
    if holes:
        failures.append(f'{len(holes)} tree ids without a location, first {holes[:5]}')
    lump_trees = {(round(x), round(y)) for x, y, _ in (vector(e['origin']) for e in by_class['ent_dota_tree'])}
    scan_trees = {(round(x), round(y)) for _, x, y, _ in trees}
    print(
        f'trees: {len(trees)} from the bot API, {len(lump_trees)} in the entity lumps, '
        f'{len(scan_trees - lump_trees)} only in the API, {len(lump_trees - scan_trees)} only in the lumps',
        flush=True,
    )
    if scan_trees != lump_trees:
        failures.append('the bot API trees and the entity-lump trees differ')
    if any((x - x0) % cell or (y - y0) % cell for _, x, y, _ in trees):
        failures.append('a tree is off the cell corners, so the 2 x 2 footprint of load_map is wrong')

    # Height levels and passability per cell centre; D4: a cell without a level must be unwalkable anyway.
    height_rows = dict(payload.split(' ', 1) for payload in tagged['height'])
    passable_rows = dict(payload.split(' ', 1) for payload in tagged['passable'])
    assert sorted(map(int, height_rows)) == list(range(height)), 'height rows are missing'
    levels = np.array([list(height_rows[str(row)]) for row in range(height)])
    passable = np.array([list(passable_rows[str(row)]) for row in range(height)]) == '1'
    walkable = (flags & GRIDNAV_TRAVERSABLE) != 0
    odd = ~np.char.isdigit(levels)
    print(
        f'height levels: {collections.Counter(levels[walkable].tolist())} on walkable cells, '
        f'{int(odd.sum())} cells without a level ({int((odd & walkable).sum())} of them walkable)',
        flush=True,
    )
    if (odd & walkable).any():
        failures.append('walkable cells without a height level')
    levels[odd] = '0'
    height_level = [''.join(row) for row in levels]

    # Landmarks, runes and camps from the entity lump, where each has exactly one entity per spot.
    runes, landmarks = {}, {}
    for kind, classname in (('power', 'dota_item_rune_spawner_powerup'), ('bounty', 'dota_item_rune_spawner_bounty')):
        runes[f'{kind}_top'], runes[f'{kind}_bottom'] = top_then_bottom(
            [vector(e['origin']) for e in by_class[classname]]
        )
    landmark_entities = {
        'roshan': [by_target['info_player_start_dota', f'roshan_location_{i}'] for i in (1, 2)],
        'tormentor': [by_target['info_player_start_dota', f'miniboss_location_{i}'] for i in (1, 2)],
        'wisdom': by_class['npc_dota_xp_fountain'],
        'lotus': by_class['npc_dota_lotus_pool'],
        'outpost': by_class['npc_dota_watch_tower'],
    }
    for kind, pair in landmark_entities.items():
        landmarks[f'{kind}_top'], landmarks[f'{kind}_bottom'] = top_then_bottom([vector(e['origin']) for e in pair])
    api_spawners = [json.loads(payload) for payload in tagged['spawner']]
    neutral_camps = []
    for spawner in by_class['npc_dota_neutral_spawner']:
        camp = spawner['volumename'].removeprefix('[PR#]')
        location = vector(spawner['origin'])
        seen = min(api_spawners, key=lambda s: math.dist(s['location'][:2], location[:2]))
        if math.dist(seen['location'][:2], location[:2]) > 2:
            failures.append(f'{camp}: the bot API has no spawner at {location}')
        if max(abs(a - b) for a, b in zip(boxes[camp], seen['min'][:2] + seen['max'][:2], strict=True)) > 1:
            failures.append(f'{camp}: the bot API box {seen["min"]} {seen["max"]} is not the trigger {boxes[camp]}')
        neutral_camps.append(
            {
                'name': camp,
                'team': seen['team'],
                'type': seen['type'],
                'speed': seen.get('speed'),  # the bot API leaves it out for some camps
                'spawner': location,
                'box': boxes[camp],
            }
        )
    neutral_camps.sort(key=lambda c: c['name'])

    # Cross-checks between the entity lump, the bot API and the world state.
    if api['TEAM_RADIANT'] != TEAM_RADIANT or api['TEAM_DIRE'] != TEAM_DIRE:
        failures.append('team constants differ from the bot API')
    if api['RUNE_STATUS_AVAILABLE'] != RUNE_STATUS_AVAILABLE:
        failures.append(f'RUNE_STATUS_AVAILABLE is {api["RUNE_STATUS_AVAILABLE"]} in the bot API')
    api_runes = json.loads(tagged['runes'][0])
    rune_infos = [[r.location.x, r.location.y] for r in world_state.rune_infos]
    print(f'bot API rune spots {api_runes}', flush=True)
    for name, (x, y, _) in runes.items():
        api_points = [point for point in api_runes.values() if isinstance(point, list)]
        if nearest(api_points, x, y) > 2 or nearest(rune_infos, x, y) > 2:
            failures.append(f'rune spot {name} at ({x}, {y}) is not where the bot API and the world state put it')
    buildings = sorted(
        (
            {
                'name': u.name,
                'type': u.unit_type,
                'team': u.team_id,
                'x': round(u.location.x, 1),
                'y': round(u.location.y, 1),
                'z': round(u.location.z, 1),
            }
            for u in world_state.units
            if u.unit_type in BUILDING_TYPES
        ),
        key=lambda b: (b['type'], b['team'], b['name'], b['x'], b['y']),
    )
    neutral_buildings = [[b['x'], b['y']] for b in buildings if b['type'] == UNIT_TYPE_BUILDING]
    for name in ('wisdom_top', 'wisdom_bottom', 'lotus_top', 'lotus_bottom', 'outpost_top', 'outpost_bottom'):
        if nearest(neutral_buildings, *landmarks[name][:2]) > 2:
            failures.append(f'{name} is not a building in the world state')
    api_buildings = [json.loads(payload) for payload in tagged['building']]
    lump_towers = [vector(e['origin']) for e in by_class['npc_dota_tower']]
    for tower in (b for b in buildings if b['type'] == UNIT_TYPE_TOWER):
        api_distance = nearest([b['location'] for b in api_buildings if b['kind'] == 'tower'], tower['x'], tower['y'])
        if nearest(lump_towers, tower['x'], tower['y']) > 2 or api_distance > 2:
            failures.append(f'tower {tower["name"]} is not where the entity lump and the bot API put it')
    # Note (ruidu): IsLocationPassable reads like the gridnav grown by a cell or two around every obstacle
    # (a unit's collision size, presumably), so it only has to agree away from trees and the map edge.
    # 6934: 0.947, and shifting the gridnav by a cell in any direction changes that by under 0.01.
    near_tree = np.zeros(flags.shape, bool)
    for _, x, y, _ in trees:
        row, col = int((y - y0) // cell), int((x - x0) // cell)
        near_tree[max(row - 3, 0) : row + 3, max(col - 3, 0) : col + 3] = True
    near_tree[:5], near_tree[-5:], near_tree[:, :5], near_tree[:, -5:] = True, True, True, True
    agreement = (passable == walkable)[~near_tree].mean()
    print(f'gridnav walkable vs IsLocationPassable: {agreement:.4f} agree away from trees', flush=True)
    if agreement < 0.9:
        failures.append(f'the gridnav and IsLocationPassable agree on only {agreement:.4f} of the cells')
    # GetHeightLevel counts downwards: under every building one terrace has to give one level, and a
    # higher terrace a smaller level.
    terraces = collections.defaultdict(set)
    for u in world_state.units:
        if u.unit_type in BUILDING_TYPES:
            row, col = int((u.location.y - y0) // cell), int((u.location.x - x0) // cell)
            terraces[u.ground_height // 128].add(int(levels[row, col]))
    print(f'terrace (ground_height // 128) -> height level: {dict(sorted(terraces.items()))}', flush=True)
    by_terrace = [sorted(levels_seen) for _, levels_seen in sorted(terraces.items())]
    if any(len(levels_seen) != 1 for levels_seen in by_terrace) or by_terrace != sorted(by_terrace, reverse=True):
        failures.append('height levels do not follow the terrain terraces under the buildings')

    # Side shops are gone since 7.23 (their location is 0, 0, 0) and a secret shop is the same for both teams.
    shops = []
    for shop in (json.loads(payload) for payload in tagged['shop']):
        if shop['location'][:2] != [0, 0] and all(shop['location'] != known['location'] for known in shops):
            shops.append(shop)

    document = {
        'client_version': int(steam_inf['ClientVersion']),
        'patch': args.patch,
        'source': {
            'client': {key: steam_inf[key] for key in ('ClientVersion', 'VersionDate', 'SourceRevision')},
            'gridnav': 'maps/dota.gnv in game/dota/maps/dota.vpk',
            'entities': f'maps/dota/entities/*.vents_c via {run_vrf(args.vrf, "--version").splitlines()[0]}',
            'scan': 'bot script API on a headless 1v1, scripts/map_scan.lua',
            'world_state': f'the scan match at dota_time {world_state.dota_time:.1f}',
            'generated_by': 'scripts/extract_map.py',
        },
        'grid': {'cell_size': cell, 'x0': x0, 'y0': y0, 'width': width, 'height': height},
        'gridnav_flags': {
            'traversable': 1,
            'blocked': 2,
            'hero_blocking': 4,
            'creature_blocking': 8,
            'ward_blocking': 16,
        },
        'gridnav': [flags[row].tobytes().hex() for row in range(height)],
        'height_level': height_level,
        'trees': trees,
        'runes': runes,
        'landmarks': landmarks,
        'buildings': buildings,
        'shops': shops,
        'neutral_camps': neutral_camps,
        'watchers': sorted(vector(e['origin']) for e in by_class['npc_dota_lantern']),
        'lanes': {json.loads(p)['lane'].removeprefix('LANE_').lower(): json.loads(p)['points'] for p in tagged['lane']},
        'world_bounds': json.loads(tagged['bounds'][0])['bounds'],
    }
    # Plain JSON, but one list element per line, so that a refresh diffs line by line.
    fields = []
    for key, value in document.items():
        if isinstance(value, list):
            body = ',\n'.join(json.dumps(item, separators=(',', ':')) for item in value)
            fields.append(f'{json.dumps(key)}:[\n{body}\n]')
        else:
            fields.append(f'{json.dumps(key)}:{json.dumps(value, separators=(",", ":"))}')
    candidate = os.path.join(args.work, 'map.json')
    with open(candidate, 'w', encoding='utf-8') as handle:
        handle.write('{\n' + ',\n'.join(fields) + '\n}\n')
    static = load_map(candidate)  # the loader has to read back what was written
    assert static.tree_counts.sum() == 4 * len(trees)
    # Every spot a hero walks to has to be walkable; lotus pools and outposts are buildings on blocked cells.
    spots = {**runes, **{name: landmarks[name] for name in landmarks if not name.startswith(('lotus', 'outpost'))}}
    # A lane starts and ends in the fountains, which are buildings, so only its 0.1 to 0.9 stretch counts.
    spots |= {f'{lane}_lane_{i}': points[i] for lane, points in document['lanes'].items() for i in range(2, 19)}
    blocked_spots = [name for name, (x, y, *_) in spots.items() if static.blocked[static.cell(x, y)]]
    print(f'{len(spots)} spots checked for walkability, blocked: {blocked_spots}', flush=True)
    if blocked_spots:
        failures.append(f'spots on blocked cells: {blocked_spots}')

    if failures:
        print('\n'.join(['FAILED:', *failures]), flush=True)
        raise SystemExit(1)
    shutil.copyfile(candidate, args.out)
    print(f'wrote {args.out} ({os.path.getsize(args.out) // 1024} KB)', flush=True)


if __name__ == '__main__':
    main()
