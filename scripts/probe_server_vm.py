"""Compatibility probe: what the server VM (the custom-game VScript VM) can do in a normal match.

Two modes, both headless:

    python scripts/probe_server_vm.py [--timescale 2] [--seconds 240]
    python scripts/probe_server_vm.py --dump-api vscript_api.lua

The first plays one 1v1 and records, on the installed client: which chat calls reach the engine's chat
log and fire player_chat, which game events ListenToGameEvent delivers outside a custom game, the
scenario calls (teleport, level, gold, item, unit, health), dota_dev forcegamestart, host_timescale
changes at runtime, and what keeps running while PauseGame holds the game. The second launches with
-dev +developer 1, without which FDesc / CDesc / EDesc stay nil, and writes DumpScriptBindings() to a
file: every server function with its signature and Valve's description. Findings: docs/SERVER_VM.md.
"""

import argparse
import itertools
import os
import re
import shutil
import threading
import time

from dota2_env.bridge.constants import HOST_MODE_DEDICATED, TEAM_RADIANT
from dota2_env.bridge.game import DotaGame, get_default_game_path
from dota2_env.bridge.worldstate import connect, parse_world_state, read_raw_world_state

# Loaded into the server VM by the servercfgfile. Chat texts start with zq- so the engine's own chat
# lines can be told apart from the probe's prints.
SERVER_LUA = r"""
local dkjson = require('game/dkjson')

local PHASE_GAP_MS = 1200
local PAUSE_HOLD_MS = 4000
local EVENT_PRINT_LIMIT = 3
local ALWAYS_PRINTED = {player_chat = true, dota_match_done = true, game_rules_state_change = true}
local EVENTS = {
    'player_chat', 'game_rules_state_change', 'dota_match_done', 'npc_spawned', 'dota_on_hero_finish_spawn',
    'dota_player_gained_level', 'dota_player_learned_ability', 'dota_player_used_ability',
    'dota_non_player_used_ability', 'dota_player_begin_cast', 'dota_item_purchased', 'dota_item_picked_up',
    'dota_hero_inventory_item_change', 'entity_killed', 'entity_hurt', 'last_hit', 'dota_tower_kill',
    'dota_team_kill_credit', 'dota_rune_activated_server', 'dota_glyph_used',
}
local HERO_PLAYER = 0

local function try(fn, ...)
    local ok, result = pcall(fn, ...)
    if ok then
        return result
    end
    return 'ERR: ' .. tostring(result)
end

local function log(tag, record)
    record = record or {}
    record.clock = {
        game_time = GameRules:GetGameTime(), dota_time = GameRules:GetDOTATime(false, true),
        wall_ms = GetSystemTimeMS(), paused = GameRules:IsGamePaused(), state = GameRules:State_Get(),
    }
    local ok, text = pcall(dkjson.encode, record)
    print('SRVPROBE ' .. tag .. ' ' .. (ok and text or dkjson.encode({encode_error = tostring(text)})))
end

local function vec(v)
    return {x = math.floor(v.x), y = math.floor(v.y), z = math.floor(v.z)}
end

local function hero()
    return PlayerResource:GetSelectedHeroEntity(HERO_PLAYER)
end

local function hero_state()
    local h = hero()
    local items = {}
    for slot = 0, 8 do
        local item = h:GetItemInSlot(slot)
        if item ~= nil then
            items[#items + 1] = item:GetAbilityName()
        end
    end
    return {pos = vec(h:GetAbsOrigin()), level = h:GetLevel(), hp = h:GetHealth(),
            gold = PlayerResource:GetGold(HERO_PLAYER), items = items}
end

local event_counts = {}
for _, name in ipairs(EVENTS) do
    event_counts[name] = 0
    ListenToGameEvent(name, function(event)
        event_counts[name] = event_counts[name] + 1
        if ALWAYS_PRINTED[name] or event_counts[name] <= EVENT_PRINT_LIMIT then
            log('event', {name = name, data = event})
        end
    end, nil)
end

-- whichever thinker still runs while the game is paused lifts the pause after PAUSE_HOLD_MS
local pause_started, unpaused_by = nil, nil
local paused_thinks = {set_think = 0, context_think = 0}
local function paused_think(who)
    if pause_started == nil or not GameRules:IsGamePaused() then
        return
    end
    paused_thinks[who] = paused_thinks[who] + 1
    if unpaused_by == nil and GetSystemTimeMS() - pause_started >= PAUSE_HOLD_MS then
        unpaused_by = who
        PauseGame(false)
        log('unpause_request', {who = who})
    end
end

local function wait_ms(ms)
    local until_ms = GetSystemTimeMS() + ms
    while GetSystemTimeMS() < until_ms do
        coroutine.yield()
    end
end

local function measure_rate(label, ms)
    local game_time, wall_ms = GameRules:GetGameTime(), GetSystemTimeMS()
    wait_ms(ms)
    log('rate', {label = label, timescale = Convars:GetFloat('host_timescale'),
                 game_s_per_wall_s = (GameRules:GetGameTime() - game_time) / ((GetSystemTimeMS() - wall_ms) / 1000)})
end

local phases = {
    {'env', function()
        return {dedicated = IsDedicatedServer(), game_mode_entity = tostring(GameRules:GetGameModeEntity()),
                player0 = tostring(PlayerResource:GetPlayer(HERO_PLAYER)), io = type(io), os = type(os)}
    end},
    {'say_nil', function() return try(Say, nil, 'zq-srv-say-nil', false) end},
    {'say_player', function() return try(Say, PlayerResource:GetPlayer(HERO_PLAYER), 'zq-srv-say-player', false) end},
    {'say_hero', function() return try(Say, hero(), 'zq-srv-say-hero 你好', false) end},
    {'say_hero_team', function() return try(Say, hero(), 'zq-srv-say-hero-team', true) end},
    {'custom_message', function() return try(function() GameRules:SendCustomMessage('zq-srv-custom', 0, 0) end) end},
    {'console_say', function() SendToServerConsole('say zq-srv-console-say') end},
    {'debug_draw_text', function()
        local head = hero():GetAbsOrigin() + Vector(0, 0, hero():GetBaseHealthBarOffset() + 50)
        return try(DebugDrawText, head, 'zq 你好', false, 1.0)
    end},
    {'scenario', function()
        local h = hero()
        local before = hero_state()
        local results = {
            teleport = try(FindClearSpaceForUnit, h, Vector(-1500, -1300, 128), true),
            level = try(function() for _ = 1, 5 do h:HeroLevelUp(false) end return h:GetLevel() end),
            gold = try(function() return PlayerResource:ModifyGold(HERO_PLAYER, 1234, true, 0) end),
            item = try(function() return h:AddItemByName('item_blink'):GetAbilityName() end),
            creep = try(function()
                local unit = CreateUnitByName('npc_dota_creep_badguys_melee', h:GetAbsOrigin() + Vector(400, 0, 0),
                                              true, nil, nil, DOTA_TEAM_BADGUYS)
                return unit:GetUnitName()
            end),
            health = try(function() h:SetHealth(123) return h:GetHealth() end),
        }
        return {before = before, after = hero_state(), results = results}
    end},
    {'dev_forcegamestart', function() SendToServerConsole('dota_dev forcegamestart') end},
}

local function script()
    while GameRules:State_Get() < DOTA_GAMERULES_STATE_PRE_GAME do
        coroutine.yield()
    end
    wait_ms(3000)
    for _, phase in ipairs(phases) do
        local ok, result = pcall(phase[2])
        log('phase', {name = phase[1], ok = ok, result = ok and result or tostring(result)})
        wait_ms(PHASE_GAP_MS)
    end

    local original = Convars:GetFloat('host_timescale')
    measure_rate('baseline', 2000)
    Convars:SetFloat('host_timescale', 0.25)
    measure_rate('convars_0.25', 2000)
    SendToServerConsole('host_timescale 1')
    measure_rate('console_1', 2000)
    SendToServerConsole('host_timescale ' .. original)
    measure_rate('restored', 1500)

    pause_started = GetSystemTimeMS()
    PauseGame(true)
    log('pause_request', {})
    while unpaused_by == nil do
        coroutine.yield()
    end
    local dota_time, since = GameRules:GetDOTATime(false, true), GetSystemTimeMS()
    while GameRules:GetDOTATime(false, true) == dota_time and GetSystemTimeMS() - since < 15000 do
        coroutine.yield()
    end
    log('resumed', {wall_ms_after_unpause = GetSystemTimeMS() - since, paused_thinks = paused_thinks})
    wait_ms(1000)
    log('event_counts', {counts = event_counts})
    log('done', {})
end

local co = coroutine.create(script)
ProbeServer = {}
function ProbeServer:Think()
    paused_think('set_think')
    if coroutine.status(co) ~= 'dead' then
        local ok, err = coroutine.resume(co)
        if not ok then
            log('script_error', {err = tostring(err)})
        end
    end
    return 0.03
end

local set_thinker = SpawnEntityFromTableSynchronous('info_target', {targetname = 'probe_set_think'})
set_thinker:SetThink('Think', ProbeServer, 'probe', 0)
local context_thinker = SpawnEntityFromTableSynchronous('info_target', {targetname = 'probe_context_think'})
context_thinker:SetContextThink('probe', function()
    paused_think('context_think')
    return 0
end, 0)
log('loaded', {})
"""

# Replaces the controlled hero's bot script: bot-VM chat and ping, and the bot's own view of the hero,
# which has to show the server's scenario edits on its next Think.
BOT_LUA = r"""
local dkjson = require('game/dkjson')
local step, last_view = 0, nil

local function log(tag, record)
    record.step, record.dota_time, record.real_time = step, DotaTime(), RealTime()
    print('BOTPROBE ' .. tag .. ' ' .. dkjson.encode(record))
end

function Think()
    step = step + 1
    if GetTeam() ~= TEAM_RADIANT then
        return
    end
    local bot = GetBot()
    if step == 40 then
        log('chat_sent', {channel = 'all', ok = pcall(bot.ActionImmediate_Chat, bot, 'zq-bot-all 你好', true)})
    elseif step == 50 then
        log('chat_sent', {channel = 'team', ok = pcall(bot.ActionImmediate_Chat, bot, 'zq-bot-team', false)})
    elseif step == 60 then
        log('ping_sent', {ok = pcall(bot.ActionImmediate_Ping, bot, -600.0, -500.0, true)})
    end
    local items = {}
    for slot = 0, 8 do
        local item = bot:GetItemInSlot(slot)
        if item ~= nil then
            items[#items + 1] = item:GetName()
        end
    end
    local view = {level = bot:GetLevel(), x = math.floor(bot:GetLocation().x / 50) * 50,
                  y = math.floor(bot:GetLocation().y / 50) * 50, items = table.concat(items, ','),
                  low_hp = bot:GetHealth() < 200}
    local signature = dkjson.encode(view)
    if signature ~= last_view then
        last_view = signature
        view.gold, view.hp = bot:GetGold(), bot:GetHealth()
        log('view', view)
    end
end
"""

DUMP_LUA = r"""
print('SRVDUMP begin FDesc=' .. type(FDesc) .. ' CDesc=' .. type(CDesc) .. ' EDesc=' .. type(EDesc))
if FDesc ~= nil and CDesc ~= nil and EDesc ~= nil then
    DumpScriptBindings()
end
print('SRVDUMP done')
"""

RE_PROBE = re.compile(r'(SRVPROBE|BOTPROBE)\s+(\S+)\s+(.*)$')
RE_LOG_PREFIX = re.compile(r'^\d\d/\d\d \d\d:\d\d:\d\d \[VScript\] ', re.M)


class DevGame(DotaGame):
    """The dump needs developer mode: FDesc / CDesc / EDesc are nil without it."""

    def launch_args(self) -> list[str]:
        return [*super().launch_args(), '-dev', '+developer', '1']


def install(game: DotaGame, server_lua: str, bot_lua: str | None) -> None:
    """Point the servercfgfile at server_lua and, if given, run bot_lua for every configured hero."""
    with open(os.path.join(game.bot_path, 'probe_server.lua'), 'w', encoding='utf-8') as f:
        f.write(server_lua)
    with open(game.dota_cfg_path, 'w', encoding='utf-8') as f:
        f.write('script_reload_code bots/probe_server\n')
    if bot_lua is None:
        return
    for name in os.listdir(game.bot_path):
        if name.startswith('bot_') and name != 'bot_wisp.lua':
            with open(os.path.join(game.bot_path, name), 'w', encoding='utf-8') as f:
                f.write(bot_lua)


def read_worldstate(port: int, frames: list[tuple[float, float]]) -> None:
    """Record (wall time, dota_time) of every frame, to see whether the feed goes on during a pause."""
    sock = connect(port, timeout=180)
    try:
        while True:
            frames.append((time.time(), parse_world_state(read_raw_world_state(sock)).dota_time))
    except (ConnectionError, OSError):
        return  # the client is gone


def probe(timescale: float, seconds: int) -> None:
    game = DotaGame(host_mode=HOST_MODE_DEDICATED, host_timescale=timescale, ticks_per_observation=6)
    install(game, SERVER_LUA, BOT_LUA)
    print(f'session folder: {game.session_folder}', flush=True)
    frames: list[tuple[float, float]] = []
    game.run_dota()
    threading.Thread(target=read_worldstate, args=(game.PORT_WORLDSTATES[TEAM_RADIANT], frames), daemon=True).start()
    offset, done, deadline = 0, False, time.time() + seconds
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
                if match is not None:
                    print(f'{match.group(1)} {match.group(2)} {match.group(3)[:600]}', flush=True)
                    done = done or match.group(2) == 'done'
                elif 'zq-' in line:  # a chat line the engine wrote itself
                    print(f'ENGINE {line.strip()}', flush=True)
    finally:
        game.stop_dota()
        game.remove_dota_files()
        shutil.rmtree(game.session_folder, ignore_errors=True)
    gaps = [(later - earlier, dota_time) for (earlier, _), (later, dota_time) in itertools.pairwise(frames)]
    longest = max(gaps, default=(0.0, 0.0))
    print(f'\nworldstate: {len(frames)} frames, longest gap {longest[0]:.1f} s wall before dota_time {longest[1]:.2f}')


def dump_api(path: str, seconds: int) -> None:
    game = DevGame(host_mode=HOST_MODE_DEDICATED, host_timescale=1, ticks_per_observation=6)
    install(game, DUMP_LUA, None)
    with open(os.path.join(get_default_game_path(), 'dota', 'steam.inf'), encoding='utf-8') as handle:
        steam_inf = dict(line.strip().split('=', 1) for line in handle if '=' in line)
    game.run_dota()
    text, deadline = '', time.time() + seconds
    try:
        while 'SRVDUMP done' not in text:
            if time.time() >= deadline:
                raise RuntimeError(f'no SRVDUMP done line within {seconds}s')
            time.sleep(1.0)
            if os.path.isfile(game.console_log_path):
                with open(game.console_log_path, encoding='utf-8', errors='replace') as f:
                    text = f.read()
    finally:
        game.stop_dota()
        game.remove_dota_files()
        shutil.rmtree(game.session_folder, ignore_errors=True)
    # a multi-line print only gets the timestamp and [VScript] prefix on its first line
    dump = RE_LOG_PREFIX.sub('', text[text.index('SRVDUMP begin') : text.index('SRVDUMP done')])
    begin, _, body = dump.partition('\n')
    if 'FDesc=table' not in begin:
        raise RuntimeError(f'the server VM has no function descriptions: {begin}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(
            f'-- Dota 2 server VM API from DumpScriptBindings(), client {steam_inf["ClientVersion"]} of '
            f'{steam_inf["VersionDate"]}, written by scripts/probe_server_vm.py --dump-api\n\n{body}'
        )
    functions = len(re.findall(r'^function \w+\(', body, re.M))
    methods = len(re.findall(r'^function \w+:\w+\(', body, re.M))
    print(f'{functions} functions, {methods} methods, {body.count("--- Enum ")} enums -> {path}')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--timescale', type=float, default=2.0)
    parser.add_argument('--seconds', type=int, default=240)
    parser.add_argument('--dump-api', metavar='PATH', help='write the server API reference to PATH instead')
    args = parser.parse_args()
    if args.dump_api:
        dump_api(args.dump_api, args.seconds)
    else:
        probe(args.timescale, args.seconds)


if __name__ == '__main__':
    main()
