"""Compatibility probe: launch Dota, record what the current client actually does.

    python scripts/probe_worldstate.py [--mode HOST_MODE_DEDICATED] [--ticks 6] [--seconds 240] [--out probe_out]
    python scripts/probe_worldstate.py --game-mode allpick5v5 --timescale 4 --seconds 300

Checks, in order: does the worldstate port open, do payloads parse with our proto (and are there
unknown fields), does our lua load (console.log), and does a written action get acknowledged.
A payload that does not parse is saved with the one before it and reading goes on; a stream that
is out of sync is reconnected, like the env's listener does (docs/VERSION_DIFF.md 1.5).
"""

import argparse
import collections
import os
import re
import shutil
import time

from google.protobuf.message import DecodeError
from google.protobuf.unknown_fields import UnknownFieldSet

from dota2_env.bridge.constants import DOTA_GAMEMODE_1V1MID, DOTA_GAMEMODE_AP, TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.game import CONTROL_AGENT, CONTROL_BUILTIN, DotaGame
from dota2_env.bridge.worldstate import connect, parse_world_state, read_raw_world_state, top_level_fields
from dota2_env.envs.allpick5v5 import DEFAULT_LINEUP

LOG_PATTERNS = re.compile(r'LUARDY|sync key|each step|\[ERROR\]|<ERROR>|\.lua|VScript|bots/|Decode|botworldstate', re.I)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='HOST_MODE_DEDICATED')
    parser.add_argument('--game-mode', choices=('mid1v1', 'allpick5v5'), default='mid1v1')
    parser.add_argument('--timescale', type=float, default=1.0)
    parser.add_argument('--ticks', type=int, default=6)
    parser.add_argument('--seconds', type=int, default=240)
    parser.add_argument('--out', default='probe_out')
    parser.add_argument('--keep-running', action='store_true')
    args = parser.parse_args()

    shutil.rmtree(args.out, ignore_errors=True)
    os.makedirs(args.out)
    allpick = args.game_mode == 'allpick5v5'
    game = DotaGame(
        host_mode=args.mode,
        host_timescale=args.timescale,
        ticks_per_observation=args.ticks,
        game_mode=DOTA_GAMEMODE_AP if allpick else DOTA_GAMEMODE_1V1MID,
        # the 5v5 the envs play, against Valve's bots; only our first hero is ever given an action
        heroes={TEAM_RADIANT: DEFAULT_LINEUP, TEAM_DIRE: DEFAULT_LINEUP} if allpick else None,
        control={TEAM_RADIANT: CONTROL_AGENT, TEAM_DIRE: CONTROL_BUILTIN} if allpick else None,
    )
    print('session folder:', game.session_folder)
    game.run_dota()
    deadline = time.time() + args.seconds

    try:
        print('waiting for worldstate port ...')
        port = game.PORT_WORLDSTATES[TEAM_RADIANT]
        sock = connect(port, timeout=args.seconds)
        print('connected after launch; reading payloads')
        sock.settimeout(30)

        n, reconnects, undecodable = 0, 0, []
        sizes, states, dts = [], collections.Counter(), []
        last_dota_time, hero, previous = None, None, b''
        while time.time() < deadline:
            try:
                raw = read_raw_world_state(sock)
            except ConnectionError as e:
                print(f'after #{n}: {e}, reconnecting')
                reconnects += 1
                sock.close()
                sock = connect(port, timeout=30, retry_interval=0.5)
                sock.settimeout(30)
                continue
            n += 1
            sizes.append(len(raw))
            before, previous = previous, raw
            try:
                ws = parse_world_state(raw)
            except DecodeError as e:
                undecodable.append(n)
                fields, framing_end = top_level_fields(raw)
                for name, data in ((f'ws_{n - 1:05d}.bin', before), (f'ws_{n:05d}_undecodable.bin', raw)):
                    with open(os.path.join(args.out, name), 'wb') as f:
                        f.write(data)
                print(
                    f'#{n} does not decode ({e}): bytes={len(raw)} before={len(before)} whole top-level fields '
                    f'{[number for number, _ in fields][-6:]} up to byte {framing_end}, '
                    f'then {raw[framing_end : framing_end + 16].hex(" ") or "nothing"}'
                )
                continue
            states[ws.game_state] += 1
            if last_dota_time is not None:
                dts.append(ws.dota_time - last_dota_time)
            last_dota_time = ws.dota_time
            if n <= 5 or n % 200 == 0:
                with open(os.path.join(args.out, f'ws_{n:05d}.bin'), 'wb') as f:
                    f.write(raw)
                unknown = sorted({fld.field_number for fld in UnknownFieldSet(ws)})
                print(
                    f'#{n} bytes={len(raw)} state={ws.game_state} dota_time={ws.dota_time:.2f} '
                    f'units={len(ws.units)} players={len(ws.players)} unknown_fields={unknown}'
                )

            heroes = [u for u in ws.units if u.unit_type == 1 and u.team_id == TEAM_RADIANT]
            if heroes and n % 5 == 0:
                hero = heroes[0]
                game.write_action(
                    team_id=TEAM_RADIANT,
                    data={
                        'dotaTime': ws.dota_time,
                        'extraData': f'###probe_{n}###',
                        'actions': [
                            {
                                'actionType': 'DOTA_UNIT_ORDER_MOVE_DIRECTLY',
                                'player': hero.player_id,
                                'moveDirectly': {'location': {'x': -600.0, 'y': -500.0, 'z': 0.0}},
                            }
                        ],
                    },
                )

        print('\n== summary ==')
        print('payloads:', n, 'game_state histogram:', dict(states), 'reconnects:', reconnects)
        if sizes:
            print(f'payload bytes min/avg/max: {min(sizes)}/{sum(sizes) / len(sizes):.0f}/{max(sizes)}')
        for bad in undecodable:
            print(f'undecodable #{bad}; bytes of #{max(bad - 5, 1)} to #{bad + 5}: {sizes[max(bad - 6, 0) : bad + 5]}')
        if dts:
            dts.sort()
            print(
                f'dota_time delta between payloads, median: {dts[len(dts) // 2]:.4f}s '
                f'(30 ticks/s => {dts[len(dts) // 2] * 30:.1f} ticks)'
            )
        if hero is not None:
            print('radiant hero:', hero.name, 'player_id', hero.player_id, 'at', hero.location.x, hero.location.y)
    finally:
        if os.path.isfile(game.console_log_path):
            shutil.copy(game.console_log_path, os.path.join(args.out, 'console.log'))
            with open(game.console_log_path, encoding='utf-8', errors='replace') as f:
                hits = [line.rstrip() for line in f if LOG_PATTERNS.search(line)]
            print(f'\n== console.log: {len(hits)} interesting lines (first 40) ==')
            print('\n'.join(hits[:40]))
            print('sync key acks:', sum('sync key' in h for h in hits))
        else:
            print('no console.log at', game.console_log_path)
        if not args.keep_running:
            game.stop_dota_pids()
            game.remove_dota_files()


if __name__ == '__main__':
    main()
