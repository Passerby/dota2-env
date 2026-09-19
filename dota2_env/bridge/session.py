"""One running Dota match seen from one team: launch, observe, act, close."""

import itertools
import json
import os
import queue as queue_lib
import re
import shutil
from multiprocessing import Process, Queue

from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.game import DotaGame
from dota2_env.bridge.worldstate import parse_world_state, worldstate_listener

RE_LUARDY = re.compile(r'LUARDY\s+(\{.*\})')
RE_ACK = re.compile(r'ACK\s+(\{.*\})')
RE_FORT_DESTROYED = re.compile(r'Building: npc_dota_(goodguys|badguys)_fort destroyed')


class ActionDelivery:
    """What happened to the action files we wrote, reconstructed from lua's ACK lines.

    executed: lua ran the main action, `delay` game seconds after the observation it was based on.
    lost:     never seen by lua, because a newer file replaced it first (acks are in order, so an
              unacked action older than the newest ack can no longer arrive).
    pending:  written, no ack yet.
    """

    def __init__(self):
        self.counts = {'sent': 0, 'executed': 0, 'lost': 0}
        self.delays = []  # game seconds, executed actions only
        self.last = None  # (action id, status, delay) of the newest ack
        self._pending = {}  # action id -> extra_actions
        self.lost_extra_actions = []  # extras of lost actions, for the caller to send again

    def sent(self, action_id, extra_actions):
        self.counts['sent'] += 1
        self._pending[action_id] = extra_actions

    def acked(self, action_id, status, delay):
        if self._pending.pop(action_id, None) is None:
            return
        self.counts[status] += 1
        self.last = (action_id, status, delay)
        if status == 'executed':
            self.delays.append(delay)
        for older in [i for i in self._pending if i < action_id]:
            self.lost_extra_actions += self._pending.pop(older)
            self.counts['lost'] += 1

    def summary(self):
        delays = sorted(self.delays)
        return dict(
            self.counts,
            pending=len(self._pending),
            last_status=self.last[1] if self.last else None,
            last_delay=self.last[2] if self.last else None,
            delay_median=delays[len(delays) // 2] if delays else None,
            delay_max=delays[-1] if delays else None,
        )


class DotaSession:
    def __init__(self, team_id, keep_files=False, **game_kwargs):
        self.team_id = team_id
        self.keep_files = keep_files  # keep the session folder (lua, console.log) after close()
        self.game = DotaGame(**game_kwargs)
        self._queue = None
        self._listener = None
        self._action_ids = itertools.count()
        self.delivery = ActionDelivery()
        self.skipped_observations = 0
        self._log_offset = 0

    def start(self):
        self.game.run_dota()
        self._queue = Queue()
        self._listener = Process(
            target=worldstate_listener, args=(self.game.PORT_WORLDSTATES[self.team_id], self._queue), daemon=True
        )
        self._listener.start()

    def observe(self, timeout):
        """Newest CMsgBotWorldState; blocks until one arrives, raises queue.Empty after `timeout` seconds."""
        raw = self._queue.get(timeout=timeout)
        while True:  # skip ahead if we have fallen behind
            try:
                raw = self._queue.get_nowait()
                self.skipped_observations += 1
            except queue_lib.Empty:
                return parse_world_state(raw)

    def act(self, dota_time, actions, extra_actions=(), draw=()):
        """Write one action file. Lua runs the first of `actions` per player and all `extra_actions`.

        `actions` needs an entry for the controlled player (DOTA_UNIT_ORDER_NONE to do nothing): lua
        only marks a file as executed once it found one, and would re-run `extra_actions` every tick.
        """
        action_id = next(self._action_ids)
        self.delivery.sent(action_id, list(extra_actions))
        data = {
            # lua ignores a file whose dotaTime it has already executed
            'dotaTime': dota_time,
            'extraData': f'###{action_id}###',
            'actions': list(actions),
        }
        if extra_actions:
            data['extra_actions'] = {'actions': list(extra_actions)}
        if draw:
            data['draw'] = list(draw)
        self.game.write_action(data=data, team_id=self.team_id)

    def poll_delivery(self):
        """Read the ACK lines lua has printed since the last call and update `self.delivery`."""
        path = self.game.console_log_path
        if not os.path.isfile(path):
            return self.delivery
        with open(path, 'rb') as f:
            f.seek(self._log_offset)
            chunk = f.read()
        # only consume complete lines; the client may be in the middle of writing one
        end = chunk.rfind(b'\n') + 1
        self._log_offset += end
        for line in chunk[:end].decode('utf-8', errors='replace').splitlines():
            match = RE_ACK.search(line)
            if match:
                record = json.loads(match.group(1))
                if record.get('team') == self.team_id:
                    self.delivery.acked(int(record['id'].strip('#')), record['status'], record['delay'])
        return self.delivery

    def lua_status(self):
        """The LUARDY records printed by our lua once a controlled hero is thinking, keyed by team id.

        Each holds `player_id` and `abilities` ({slot: ability name}) as reported by the running client.
        """
        status = {}
        if os.path.isfile(self.game.console_log_path):
            with open(self.game.console_log_path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    match = RE_LUARDY.search(line)
                    if match:
                        record = json.loads(match.group(1))
                        status[record.get('team')] = record
        return status

    def match_winner(self):
        """Winning team id once the client has ended the match, else None.

        The client ends a 1v1 by destroying the loser's ancient and stops streaming world states right
        away, so the console log is the only place where the result shows up.
        """
        if os.path.isfile(self.game.console_log_path):
            with open(self.game.console_log_path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    match = RE_FORT_DESTROYED.search(line)
                    if match:
                        return TEAM_DIRE if match.group(1) == 'goodguys' else TEAM_RADIANT
        return None

    def close(self):
        if self._listener is not None:
            self._listener.terminate()
            self._listener.join(timeout=5)
            self._listener = None
        self.game.stop_dota_pids()
        self.game.remove_bot_symlink()
        if not self.keep_files:
            shutil.rmtree(self.game.session_folder, ignore_errors=True)
