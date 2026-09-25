"""Measure when runes spawn, the Shrines of Wisdom activate and the Lotus Pools grow, and write events.json.

One headless match per game mode with idle heroes, on the installed client (Steam running, about 12 minutes):

    python scripts/probe_events.py --patch 7.41f
    python scripts/probe_events.py --patch 7.41f --reuse-scan   # rebuild from the last run in --work

The server VM sees the whole map. It logs every rune entity as it appears and takes those lying on a rune spot
away 15 game seconds later, so an unpicked rune never stands in the way of the next one. Heroes stand at a
Shrine of Wisdom and in a Lotus Pool from the horn on, so an activation or a lotus is taken the moment it is
there, and near the end of the 5v5 a wisp walks into the untouched pool to count how many lotuses a pool holds.
The rune times are checked against GameRules' own count-downs before anything is written. Findings:
docs/VERSION_DIFF.md 3.3.
"""

import argparse
import dataclasses
import itertools
import json
import os
import re
import shutil
import tempfile
import time

from dota2_env.bridge.constants import (
    DOTA_GAMEMODE_1V1MID,
    DOTA_GAMEMODE_AP,
    HOST_MODE_DEDICATED,
    TEAM_DIRE,
    TEAM_RADIANT,
)
from dota2_env.bridge.game import CONTROL_IDLE, DEFAULT_HERO, DotaGame, get_default_game_path
from dota2_env.game_text import load_records
from dota2_env.map_features import EVENTS_PATH, LANDMARKS, RUNE_SPOTS, EventKind, GameMode, MapEvent, load_map

# game mode and the dota_time the match is played to: three shrine activations in the 5v5, two in the 1v1
MODES: dict[GameMode, tuple[int, int]] = {'allpick5v5': (DOTA_GAMEMODE_AP, 1320), 'mid1v1': (DOTA_GAMEMODE_1V1MID, 900)}
# (team, unit, landmark, dx, dy, from): the server VM keeps the unit standing there from dota_time "from" on,
# counted back from the end of the match when negative. Unit "hero" is DEFAULT_HERO, "wisp" a 5v5 filler.
PARKED: dict[GameMode, tuple[tuple[int, str, str, int, int, int], ...]] = {
    'allpick5v5': (
        (TEAM_RADIANT, 'hero', 'wisdom_top', 200, 0, 0),
        (TEAM_DIRE, 'hero', 'wisdom_bottom', -200, 0, 0),
        (TEAM_RADIANT, 'wisp', 'lotus_top', 150, 0, 0),
        (TEAM_DIRE, 'wisp', 'lotus_bottom', -150, 0, -50),
    ),
    'mid1v1': (
        (TEAM_RADIANT, 'hero', 'wisdom_top', 200, 0, 0),
        (TEAM_DIRE, 'hero', 'lotus_top', 150, 0, 0),
    ),
}
UNIT_NAMES = {'hero': DEFAULT_HERO, 'wisp': 'npc_dota_hero_wisp'}
# the special values the server VM reads off the passive ability of each shrine and pool
VALUES = {
    'npc_dota_xp_fountain': ('radius', 'countdown_time'),
    'npc_dota_lotus_pool': ('radius', 'first_lotus_pickup_time', 'pickup_time_reduction_pct', 'min_lotus_pickup_time'),
}
LOAD_SECONDS = 240  # wall seconds a headless client may take to reach the pre-game
SPOT_ORDER = RUNE_SPOTS + LANDMARKS

SERVER_LUA = r"""
local dkjson = require('game/dkjson')

local UNTIL = __UNTIL__
local RUNE_SPOTS = __RUNE_SPOTS__
local LANDMARKS = __LANDMARKS__
local PARKED = __PARKED__
local VALUES = __VALUES__
local REMOVE_AFTER = 15
local SPOT_RADIUS = 400
local PARK_SLACK = 250
local THINK = 0.1

local function log(tag, record)
    record.dota_time = GameRules:GetDOTATime(false, true)
    print('EVTPROBE ' .. tag .. ' ' .. dkjson.encode(record))
end

local function rune_spot(position)
    for index, spot in ipairs(RUNE_SPOTS) do
        local dx, dy = position.x - spot[1], position.y - spot[2]
        if dx * dx + dy * dy < SPOT_RADIUS * SPOT_RADIUS then
            return index - 1
        end
    end
    return nil
end

local runes = {}
local function track_runes(now)
    local present = {}
    for _, rune in ipairs(Entities:FindAllByClassname('dota_item_rune')) do
        local index = rune:entindex()
        present[index] = true
        local known = runes[index]
        if known == nil then
            local position = rune:GetAbsOrigin()
            runes[index] = {seen = now, spot = rune_spot(position)}
            log('rune_spawn', {spot = runes[index].spot, x = math.floor(position.x), y = math.floor(position.y),
                               model = rune:GetModelName()})
        elseif known.spot ~= nil and not known.removed and now - known.seen >= REMOVE_AFTER then
            known.removed = true
            UTIL_Remove(rune)
        end
    end
    for index, _ in pairs(runes) do
        if not present[index] then
            runes[index] = nil
        end
    end
end

local last_next = nil
local function track_next()
    local text = GameRules:GetNextBountyRuneSpawnTime() .. ' ' .. GameRules:GetNextRuneSpawnTime()
    if text ~= last_next then
        last_next = text
        log('next', {bounty = GameRules:GetNextBountyRuneSpawnTime(), rune = GameRules:GetNextRuneSpawnTime()})
    end
end

local function find_unit(team, name)
    for _, hero in ipairs(HeroList:GetAllHeroes()) do
        if hero:GetTeamNumber() == team and hero:GetUnitName() == name then
            return hero
        end
    end
    return nil
end

local watched = {}
local function park(now)
    for index, plan in ipairs(PARKED) do
        local unit = find_unit(plan.team, plan.unit)
        if unit ~= nil then  -- a hero spawns a moment into the pre-game
            local spot = LANDMARKS[plan.spot]
            local target = Vector(spot[1] + plan.dx, spot[2] + plan.dy, 0)
            if now >= plan.from and unit:IsAlive() and (unit:GetAbsOrigin() - target):Length2D() > PARK_SLACK then
                FindClearSpaceForUnit(unit, target, true)
                log('parked', {plan = index - 1})
            end
            local items = {}
            for slot = 0, 16 do
                local item = unit:GetItemInSlot(slot)
                if item ~= nil then
                    items[#items + 1] = item:GetAbilityName() .. ':' .. item:GetCurrentCharges()
                end
            end
            table.sort(items)
            local signature = unit:GetCurrentXP() .. ' ' .. table.concat(items, ',') .. ' ' .. tostring(unit:IsAlive())
            if watched[index] ~= signature then
                watched[index] = signature
                local position = unit:GetAbsOrigin()
                log('unit', {plan = index - 1, xp = unit:GetCurrentXP(), alive = unit:IsAlive(), items = items,
                             x = math.floor(position.x), y = math.floor(position.y)})
            end
        end
    end
end

local started = false
local thinker = SpawnEntityFromTableSynchronous('info_target', {targetname = 'probe_events'})
thinker:SetContextThink('probe_events', function()
    if GameRules:State_Get() < DOTA_GAMERULES_STATE_PRE_GAME then
        return THINK
    end
    local now = GameRules:GetDOTATime(false, true)
    if not started then
        started = true
        for classname, keys in pairs(VALUES) do
            local ability = Entities:FindAllByClassname(classname)[1]:GetAbilityByIndex(0)
            local values = {}
            for _, key in ipairs(keys) do
                values[key] = ability:GetSpecialValueFor(key)
            end
            log('values', {ability = ability:GetAbilityName(), values = values})
        end
        log('started', {})
    end
    track_runes(now)
    track_next()
    park(now)
    if now >= UNTIL then
        log('done', {})
        return nil
    end
    return THINK
end, 0)
log('loaded', {})
"""

RE_PROBE = re.compile(r'EVTPROBE\s+(\S+)\s+(.*)$')


def lua_table(value: object) -> str:
    """A Lua literal for a JSON-like value: lists become sequences, dicts records."""
    if isinstance(value, dict):
        return '{' + ', '.join(f'{key} = {lua_table(item)}' for key, item in value.items()) + '}'
    if isinstance(value, list | tuple):
        return '{' + ', '.join(lua_table(item) for item in value) + '}'
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def play(mode: GameMode, timescale: float, raw_path: str) -> None:
    """Play one match of mode and write what the server VM logged to raw_path, one JSON record per line."""
    game_mode, until = MODES[mode]
    game = DotaGame(
        host_mode=HOST_MODE_DEDICATED,
        host_timescale=timescale,
        ticks_per_observation=6,
        game_mode=game_mode,
        heroes={TEAM_RADIANT: (DEFAULT_HERO,), TEAM_DIRE: (DEFAULT_HERO,)},
        control={TEAM_RADIANT: CONTROL_IDLE, TEAM_DIRE: CONTROL_IDLE},
    )
    static = load_map()
    parked = [
        {
            'team': team,
            'unit': UNIT_NAMES[unit],
            'spot': spot,
            'dx': dx,
            'dy': dy,
            'from': start if start >= 0 else until + start,
        }
        for team, unit, spot, dx, dy, start in PARKED[mode]
    ]
    lua = (
        SERVER_LUA.replace('__UNTIL__', str(until))
        .replace('__RUNE_SPOTS__', lua_table([[round(x), round(y)] for x, y in static.runes]))
        .replace(
            '__LANDMARKS__',
            lua_table({spot: [round(x), round(y)] for spot, (x, y) in zip(LANDMARKS, static.landmarks, strict=True)}),
        )
        .replace('__PARKED__', lua_table(parked))
        .replace('__VALUES__', lua_table(VALUES))
    )
    with open(os.path.join(game.bot_path, 'probe_events.lua'), 'w', encoding='utf-8') as f:
        f.write(lua)
    with open(game.dota_cfg_path, 'w', encoding='utf-8') as f:
        f.write('script_reload_code bots/probe_events\n')
    print(f'{mode}: session folder {game.session_folder}', flush=True)

    records: list[dict[str, object]] = [{'source': 'probe', 'tag': 'plan', 'parked': parked, 'until': until}]
    game.run_dota()
    offset, done = 0, False
    deadline = time.time() + LOAD_SECONDS + (until + 90) / timescale * 1.5
    try:
        while time.time() < deadline and not done:
            time.sleep(0.5)
            if not os.path.isfile(game.console_log_path):
                continue
            with open(game.console_log_path, 'rb') as f:
                f.seek(offset)
                chunk = f.read()
            end = chunk.rfind(b'\n') + 1  # the client may be in the middle of a line
            offset += end
            for line in chunk[:end].decode('utf-8', 'replace').splitlines():
                match = RE_PROBE.search(line)
                if match is None:
                    if 'Script Runtime Error' in line:
                        print(f'LUA {line.strip()}', flush=True)
                    continue
                records.append({'source': 'server', 'tag': match.group(1), **json.loads(match.group(2))})
                if match.group(1) in ('started', 'done', 'values'):
                    print(f'  {match.group(1)} {match.group(2)[:200]}', flush=True)
                done = match.group(1) == 'done'
    finally:
        game.stop_dota()
        game.remove_dota_files()
        shutil.rmtree(game.session_folder, ignore_errors=True)
    if not done:
        raise RuntimeError(f'{mode}: the probe never reached dota_time {until}')
    with open(raw_path, 'w', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(f'{mode}: {len(records)} records -> {raw_path}', flush=True)


def rune_kind(model: str) -> EventKind:
    """bounty, water or power: a rune entity tells what it is only by its model."""
    name = os.path.basename(model)
    return 'bounty' if name.startswith('rune_goldxp') else 'water' if name.startswith('rune_water') else 'power'


def lotuses(items: list[str]) -> int:
    """Healing Lotuses in an inventory of name:charges, a combined lotus counting as the lotuses it was made of."""
    records = load_records('items')

    def count(name: str) -> int:
        components = records[name]['components']
        return sum(count(part) for part in components[0]) if components else 1

    total = 0
    for item in items:
        name, charges = item.rsplit(':', 1)
        if name in ('item_famango', 'item_great_famango', 'item_greater_famango'):
            total += count(name) * max(int(charges), 1)
    return total


def schedule(kind: EventKind, spawns: dict[int, set[str]], until: int) -> list[MapEvent]:
    """The spawn times of one kind as events.json entries.

    A run of equally spaced times on the same spots that lasts until the probe stops repeats (first, every);
    other times are listed. A kind that comes to a single spot each time, not always the same one, comes to
    one_of the spots it was seen at.
    """
    times = sorted(spawns)
    spots = {spawn: tuple(sorted(spawns[spawn], key=SPOT_ORDER.index)) for spawn in times}
    seen = tuple(sorted(set().union(*spawns.values()), key=SPOT_ORDER.index))
    one_of = all(len(spots[spawn]) == 1 for spawn in times) and len(seen) > 1
    if one_of:
        spots = dict.fromkeys(times, seen)
    events: list[MapEvent] = []
    i = 0
    while i < len(times):
        j = i + 1
        while j < len(times) and spots[times[j]] == spots[times[i]]:
            if j > i + 1 and times[j] - times[j - 1] != times[i + 1] - times[i]:
                break
            j += 1
        run = tuple(times[i:j])
        every = run[1] - run[0] if len(run) > 1 else None
        if every is not None and run[-1] + every > until - 5:
            events.append(MapEvent(kind=kind, spots=spots[run[0]], first=run[0], every=every, one_of=one_of))
        elif events and events[-1].every is None and events[-1].spots == spots[run[0]]:
            events[-1] = dataclasses.replace(events[-1], times=events[-1].times + run)
        else:
            events.append(MapEvent(kind=kind, spots=spots[run[0]], times=run, one_of=one_of))
        i = j
    return events


def derive(mode: GameMode, raw_path: str) -> tuple[list[MapEvent], dict[str, dict[str, float]], list[str]]:
    """events.json entries of one mode from its raw records, the abilities' values and the checks that failed."""
    with open(raw_path, encoding='utf-8') as f:
        records = [json.loads(line) for line in f]
    plan = next(record for record in records if record['tag'] == 'plan')
    until, parked = plan['until'], plan['parked']
    server = [record for record in records if record['source'] == 'server']
    values = {record['ability']: record['values'] for record in server if record['tag'] == 'values'}
    failures = []

    runes: dict[EventKind, dict[int, set[str]]] = {}
    for record in server:
        if record['tag'] == 'rune_spawn' and record.get('spot') is not None:
            spawn = round(record['dota_time'])
            runes.setdefault(rune_kind(record['model']), {}).setdefault(spawn, set()).add(RUNE_SPOTS[record['spot']])
    events = [event for kind, spawns in runes.items() for event in schedule(kind, spawns, until)]

    # Note (ruidu): a shrine hands out its experience countdown_time seconds after a hero steps in, and a pool
    # its first lotus first_lotus_pickup_time seconds after; the parked heroes stand there from the horn on.
    delays = {
        'wisdom': values['ability_xp_fountain']['countdown_time'],
        'lotus': values['ability_lotus_pool']['first_lotus_pickup_time'],
    }
    found: dict[EventKind, dict[int, set[str]]] = {}
    experience: dict[int, int] = {}
    for index, park in enumerate(parked):
        kind = park['spot'].split('_')[0]
        states = [record for record in server if record['tag'] == 'unit' and record['plan'] == index]
        for before, after in itertools.pairwise(states):
            if kind == 'wisdom':
                gained = after['xp'] > before['xp']
            else:
                gained = lotuses(after['items']) > lotuses(before['items'])
            if gained and park['from'] == 0:
                spawn = round(after['dota_time'] - delays[kind])
                found.setdefault(kind, {})[spawn] = {spot for spot in LANDMARKS if spot.startswith(kind)}
                if kind == 'wisdom':
                    experience[spawn] = after['xp'] - before['xp']
    for kind, spawns in found.items():
        events += schedule(kind, spawns, until)
    amounts = tuple(experience[spawn] for spawn in sorted(experience))
    events = [dataclasses.replace(event, amounts=amounts) if event.kind == 'wisdom' else event for event in events]

    for index, park in enumerate(parked):
        if park['from'] == 0:
            continue
        states = [record for record in server if record['tag'] == 'unit' and record['plan'] == index]
        held = lotuses(states[-1]['items']) - lotuses(states[0]['items'])
        grown = [spawn for spawn in found.get('lotus', {}) if spawn <= park['from']]
        print(f'{mode}: {held} lotuses in a pool left alone until {park["from"]}, {len(grown)} grown by then')
        if held < len(grown):
            events = [dataclasses.replace(event, max=held) if event.kind == 'lotus' else event for event in events]

    # Note (ruidu): every rune has to come at a time GameRules' own count-downs announced, which catches a wrong first
    # time or interval; the other way round does not hold, the 1v1 counts down to 0:00 and 2:00 and spawns nothing.
    announced: dict[str, set[int]] = {'bounty': set(), 'rune': set()}
    for record in server:
        if record['tag'] == 'next':
            announced['bounty'].add(round(record['bounty']))
            announced['rune'].add(round(record['rune']))
    for event in events:
        if event.kind not in ('bounty', 'water', 'power'):
            continue
        times = event.times if event.every is None else range(event.first or 0, until + 1, event.every)
        unannounced = [time for time in times if time not in announced['bounty' if event.kind == 'bounty' else 'rune']]
        if unannounced:
            failures.append(f'{mode}: {event.kind} at {unannounced}, which GameRules never counted down to')
    return events, values, failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--patch', required=True, help='gameplay patch of the installed client, e.g. 7.41f')
    parser.add_argument('--timescale', type=float, default=4.0)
    parser.add_argument('--modes', nargs='+', choices=tuple(MODES), default=list(MODES))
    parser.add_argument('--work', default=os.path.join(tempfile.gettempdir(), 'dota2_env_events'))
    parser.add_argument('--reuse-scan', action='store_true', help='use the raw records already in --work')
    parser.add_argument('--out', default=EVENTS_PATH)
    args = parser.parse_args()
    os.makedirs(args.work, exist_ok=True)
    with open(os.path.join(get_default_game_path(), 'dota', 'steam.inf'), encoding='utf-8') as handle:
        steam_inf = dict(line.strip().split('=', 1) for line in handle if '=' in line)

    document: dict[str, object] = {
        'client_version': int(steam_inf['ClientVersion']),
        'patch': args.patch,
        'source': {
            'client': {key: steam_inf[key] for key in ('ClientVersion', 'VersionDate', 'SourceRevision')},
            'match': f'headless, idle heroes, host_timescale {args.timescale:g}, '
            + ', '.join(f'{mode} to dota_time {MODES[mode][1]}' for mode in MODES),
            'generated_by': 'scripts/probe_events.py',
        },
    }
    failures, modes = [], {}
    for mode in MODES:
        raw_path = os.path.join(args.work, f'events_{mode}.jsonl')
        if not args.reuse_scan and mode in args.modes:
            play(mode, args.timescale, raw_path)
        events, values, failed = derive(mode, raw_path)
        document['values'] = values
        modes[mode] = [dataclasses.asdict(event) for event in events]
        failures += failed
        for event in modes[mode]:
            print(f'{mode}: {json.dumps(event)}', flush=True)
    document.update(modes)
    if failures:
        print('\n'.join(['FAILED:', *failures]), flush=True)
        raise SystemExit(1)
    # Plain JSON, but one list element per line, so that a refresh diffs line by line.
    fields = []
    for key, value in document.items():
        if isinstance(value, list):
            body = ',\n'.join(json.dumps(item, separators=(',', ':')) for item in value)
            fields.append(f'{json.dumps(key)}:[\n{body}\n]')
        else:
            fields.append(f'{json.dumps(key)}:{json.dumps(value, separators=(",", ":"))}')
    with open(args.out, 'w', encoding='utf-8') as handle:
        handle.write('{\n' + ',\n'.join(fields) + '\n}\n')
    print(f'wrote {args.out}', flush=True)


if __name__ == '__main__':
    main()
