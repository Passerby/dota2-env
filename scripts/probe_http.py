"""Compatibility probe: what can the bot script VM do over HTTP?

    python scripts/probe_http.py [--port 8121] [--timescale 2] [--seconds 200]

The bot VM exposes CreateHTTPRequest(":<port>/<path>") -- the engine prefixes http://localhost --
and CreateRemoteHTTPRequest("<url>"); Send(callback) on the returned handle is asynchronous. This
records, on the client that is actually installed: whether the two functions exist, which URL forms
work, what the response table holds, what a localhost round trip costs (wall ms and Think ticks),
how large a body may be, and whether a callback can be delivered while lua blocks, which is what
lock-step would need. The findings are written up in docs/IPC_CHANNELS.md.
"""

import argparse
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from dota2_env.bridge.game import DotaGame

LUA = r"""
local dkjson = require('game/dkjson')

local PORT = '__PORT__'
local step, round = 0, 0
local phase = 'existence'
local pending = false
local working = nil
local block_send_rt, block_cb_rt = nil, nil

local function log(tag, tbl)
    tbl.step = step
    print('HTTPPROBE', tag, dkjson.encode(tbl))
end

local function keys_of(t)
    local out = {}
    if type(t) ~= 'table' then return {tostring(type(t))} end
    for k, v in pairs(t) do out[#out + 1] = tostring(k) .. '=' .. tostring(type(v)) end
    return out
end

-- Create + Send one request. Returns false if the handle could not even be created.
local function send(url, remote, body, cb)
    local create = CreateHTTPRequest
    if remote then create = CreateRemoteHTTPRequest end
    if create == nil then
        log('create_missing', {url = url, remote = remote})
        return false
    end
    local ok, req = pcall(create, url)
    if not ok or req == nil then
        log('create_failed', {url = url, remote = remote, returned = tostring(req), threw = not ok})
        return false
    end
    pcall(function() req:SetHTTPRequestAbsoluteTimeoutMS(10000) end)
    if body ~= nil then
        pcall(function() req:SetHTTPRequestRawPostBody('application/json', body) end)
    end
    local sent_rt, sent_step = RealTime(), step
    local sent_ok, err = pcall(function()
        req:Send(function(res)
            cb(res, math.floor((RealTime() - sent_rt) * 1000), step - sent_step)
        end)
    end)
    if not sent_ok then
        log('send_failed', {url = url, remote = remote, err = tostring(err)})
        return false
    end
    return true
end

-- url forms for the localhost helper
local forms = {
    {name = 'colon_port', url = ':' .. PORT .. '/colon_port'},
    {name = 'host_port', url = 'localhost:' .. PORT .. '/host_port'},
    {name = 'full_url', url = 'http://localhost:' .. PORT .. '/full_url'},
}
-- CreateRemoteHTTPRequest targets: two local, two public
local remotes = {
    {name = 'remote_127', url = 'http://127.0.0.1:' .. PORT .. '/remote_127'},
    {name = 'remote_localhost', url = 'http://localhost:' .. PORT .. '/remote_localhost'},
    {name = 'remote_http_public', url = 'http://example.com/'},
    {name = 'remote_https_public', url = 'https://example.com/'},
}
-- request body / response body sizes in bytes
local payloads = {0, 1024, 65536, 262144, 1048576}

function Think()
    step = step + 1
    if GetTeam() ~= TEAM_RADIANT then return end
    if pending then return end

    if phase == 'existence' then
        if step < 10 then return end
        log('env', {
            dota_time = DotaTime(), game_state = GetGameState(),
            CreateHTTPRequest = type(CreateHTTPRequest),
            CreateRemoteHTTPRequest = type(CreateRemoteHTTPRequest),
            io = type(io), os = type(os), require = type(require), loadfile = type(loadfile),
            dofile = type(dofile), package = type(package), DebugPause = type(DebugPause),
            jit = type(jit), lua_version = _VERSION,
        })
        phase = 'forms'
        return
    end

    if phase == 'forms' then
        local form = table.remove(forms, 1)
        if form == nil then
            phase = 'remote'
            log('forms_done', {working = working and working.name or 'none'})
            return
        end
        pending = true
        local ok = send(form.url, false, dkjson.encode({probe = form.name}), function(res, rtt, dstep)
            local info = {form = form.name, url = form.url, rtt_ms = rtt, dstep = dstep,
                          res_keys = keys_of(res)}
            if type(res) == 'table' then
                info.status = res.StatusCode
                info.body = tostring(res.Body):sub(1, 80)
                if res.StatusCode == 200 and working == nil then working = form end
            end
            log('form_result', info)
            pending = false
        end)
        if not ok then pending = false end
        return
    end

    if phase == 'remote' then
        local target = table.remove(remotes, 1)
        if target == nil then
            phase = working and 'payload' or 'done'
            return
        end
        pending = true
        local ok = send(target.url, true, dkjson.encode({probe = target.name}), function(res, rtt, dstep)
            local info = {target = target.name, url = target.url, rtt_ms = rtt, dstep = dstep}
            if type(res) == 'table' then
                info.status = res.StatusCode
                info.body_len = #tostring(res.Body)
                info.body = tostring(res.Body):sub(1, 60)
            else
                info.res = tostring(res)
            end
            log('remote_result', info)
            pending = false
        end)
        if not ok then pending = false end
        return
    end

    if phase == 'payload' then
        local size = table.remove(payloads, 1)
        if size == nil then
            phase = 'roundtrip'
            return
        end
        pending = true
        local body = string.rep('x', size)
        -- ask the server for a response of the same size
        local url = ':' .. PORT .. '/payload?resp=' .. size
        local ok = send(url, false, body, function(res, rtt, dstep)
            local info = {sent_bytes = size, rtt_ms = rtt, dstep = dstep}
            if type(res) == 'table' then
                info.status = res.StatusCode
                info.got_bytes = #tostring(res.Body)
            else
                info.res = tostring(res)
            end
            log('payload_result', info)
            pending = false
        end)
        if not ok then pending = false end
        return
    end

    if phase == 'roundtrip' then
        if round >= 30 then
            phase = 'block'
            log('roundtrip_done', {rounds = round})
            return
        end
        round = round + 1
        pending = true
        local dota_sent = DotaTime()
        local ok = send(':' .. PORT .. '/rt', false, dkjson.encode({round = round}), function(res, rtt, dstep)
            log('rt', {round = round, rtt_ms = rtt, dstep = dstep,
                       dota_ms = math.floor((DotaTime() - dota_sent) * 1000),
                       status = type(res) == 'table' and res.StatusCode or -1})
            pending = false
        end)
        if not ok then pending = false; phase = 'done' end
        return
    end

    -- Can a Send() callback be delivered while lua blocks? That is what lock-step needs.
    if phase == 'block' then
        pending = true
        block_send_rt = RealTime()
        send(':' .. PORT .. '/block', false, '{}', function(res, rtt, dstep)
            block_cb_rt = RealTime()
            log('block_cb', {rtt_ms = rtt, dstep = dstep,
                             after_spin_ms = math.floor((RealTime() - block_send_rt) * 1000)})
            pending = false
        end)
        -- counted loop, so it terminates even though the bot VM has no os.clock
        local x = 0
        for _ = 1, 8 do
            for i = 1, 20000000 do x = x + i % 7 end
            if RealTime() - block_send_rt > 3.0 then break end
        end
        log('block_spin_end', {x = x, callback_ran_during_spin = block_cb_rt ~= nil,
                               spun_ms = math.floor((RealTime() - block_send_rt) * 1000)})
        phase = 'after_block'
        return
    end

    if phase == 'after_block' then
        log('done', {dota_time = DotaTime()})
        phase = 'done'
    end
end
"""


class ProbeHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        ProbeHandler.requests.append(
            {
                'path': self.path,
                'method': self.command,
                'body_bytes': len(body),
                'content_type': self.headers.get('Content-Type'),
                'user_agent': self.headers.get('User-Agent'),
                'host': self.headers.get('Host'),
            }
        )
        # lua asks for a response of a given size with ?resp=N, to find the download limit
        padding = int(self.path.split('resp=')[1].split('&')[0]) if 'resp=' in self.path else 0
        payload = json.dumps({'got': len(body)}).encode()
        payload += b'.' * max(0, padding - len(payload))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8121)
    parser.add_argument('--timescale', type=float, default=2.0)
    parser.add_argument('--seconds', type=int, default=200)
    parser.add_argument('--mode', default='HOST_MODE_DEDICATED')
    args = parser.parse_args()

    server = ThreadingHTTPServer(('127.0.0.1', args.port), ProbeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f'http server on 127.0.0.1:{args.port}', flush=True)

    game = DotaGame(host_mode=args.mode, host_timescale=args.timescale, ticks_per_observation=6)
    for name in os.listdir(game.bot_path):  # the probe replaces the controlled hero's script
        if name.startswith('bot_') and name != 'bot_wisp.lua':
            with open(os.path.join(game.bot_path, name), 'w') as f:
                f.write(LUA.replace('__PORT__', str(args.port)))
            print(f'probe lua -> {name}', flush=True)
    print(f'session folder: {game.session_folder}', flush=True)
    game.run_dota()

    records: list[tuple[str, dict]] = []
    pattern = re.compile(r'HTTPPROBE\s+(\S+)\s+(\{.*\})')
    offset, done, deadline = 0, False, time.time() + args.seconds
    try:
        while time.time() < deadline and not done:
            time.sleep(1.0)
            if not os.path.isfile(game.console_log_path):
                continue
            with open(game.console_log_path, 'rb') as f:
                f.seek(offset)
                chunk = f.read()
            end = chunk.rfind(b'\n') + 1  # the client may be mid-line
            offset += end
            for line in chunk[:end].decode('utf-8', 'replace').splitlines():
                match = pattern.search(line)
                if match is None:
                    continue
                tag, record = match.group(1), json.loads(match.group(2))
                records.append((tag, record))
                if tag != 'rt':
                    print(f'lua {tag:<16} {json.dumps(record)}', flush=True)
                done = done or tag == 'done'
    finally:
        print('\n== summary ==', flush=True)
        round_trips = sorted(record['rtt_ms'] for tag, record in records if tag == 'rt')
        ticks = sorted(record['dstep'] for tag, record in records if tag == 'rt')
        if round_trips:
            wall = f'{round_trips[0]}/{round_trips[len(round_trips) // 2]}/{round_trips[-1]}'
            think = f'{ticks[0]}/{ticks[len(ticks) // 2]}/{ticks[-1]}'
            print(
                f'localhost round trips: n={len(round_trips)} wall ms min/median/max={wall} think ticks {think}',
                flush=True,
            )
        print(f'server saw {len(ProbeHandler.requests)} requests', flush=True)
        for request in ProbeHandler.requests[:3]:
            print(f'   {json.dumps(request)}', flush=True)
        payload_sizes = {r['path']: r['body_bytes'] for r in ProbeHandler.requests if 'payload' in str(r['path'])}
        if payload_sizes:
            print(f'   payload bytes received: {json.dumps(payload_sizes)}', flush=True)
        game.stop_dota_pids()
        game.remove_bot_symlink()
        shutil.rmtree(game.session_folder, ignore_errors=True)
        server.shutdown()


if __name__ == '__main__':
    main()
