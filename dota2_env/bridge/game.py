"""Launching the Dota 2 client and the file-based Python -> Lua channel."""

import contextlib
import glob
import json
import logging
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import uuid
from sys import platform
from typing import ClassVar

from dota2_env.bridge.constants import HOST_MODE_DEDICATED, HOST_MODE_GUI, HOST_MODE_GUI_MENU, TEAM_DIRE, TEAM_RADIANT

logger = logging.getLogger('dota2_env')

LUA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lua')

DEFAULT_HERO = 'npc_dota_hero_nevermore'
CONTROL_AGENT, CONTROL_BUILTIN, CONTROL_IDLE = 'agent', 'builtin', 'idle'

DEFAULT_GAME_PATHS = {
    'darwin': '~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game',
    'win32': r'C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game',
    'linux': '~/.steam/steam/steamapps/common/dota 2 beta/game',
}


def get_default_game_path():
    """`DOTA_GAME_PATH` env var, falling back to the default Steam library location."""
    key = 'linux' if platform.startswith('linux') else platform
    return os.path.expanduser(os.getenv('DOTA_GAME_PATH', DEFAULT_GAME_PATHS[key]))


class DotaGame:
    ACTIONS_FILENAME_FMT = 'actions_t{team_id}'
    BOTS_FOLDER_NAME = 'bots'
    CONFIG_FILENAME = 'config_auto'
    CONSOLE_LOG_FILENAME = 'console.log'
    LIVE_CONFIG_FILENAME = 'live_config_auto'
    SERVER_CFG_FILENAME = 'dota2_env_server.cfg'
    PORT_WORLDSTATES: ClassVar[dict[int, int]] = {TEAM_RADIANT: 12120, TEAM_DIRE: 12121}

    def __init__(
        self,
        host_timescale=1,
        ticks_per_observation=6,
        game_mode=21,
        host_mode=HOST_MODE_GUI_MENU,
        game_id=None,
        dota_path=None,
        session_root=None,
        heroes=None,
        control=None,
        record_replay=False,
    ):
        """
        heroes:  {team_id: (hero unit name, ...)} in player-slot order; slots past the end of the
                 tuple are filled with idle wisps.
        control: {team_id: "agent" | "builtin" | "idle"}; "agent" heroes execute the action files
                 written by `write_action`, "builtin" heroes are played by Valve's default bot AI.
        record_replay: let GOTV write a .dem of the match, to be picked up by collect_replay.
        """
        heroes = heroes or {TEAM_RADIANT: (DEFAULT_HERO,), TEAM_DIRE: (DEFAULT_HERO,)}
        self.heroes = {team: tuple(names) for team, names in heroes.items()}
        self.control = dict(control or {TEAM_RADIANT: CONTROL_AGENT, TEAM_DIRE: CONTROL_IDLE})
        self.dota_path = dota_path or get_default_game_path()
        if not os.path.isdir(self.dota_path):
            raise FileNotFoundError(f'Dota 2 not found at {self.dota_path}; set DOTA_GAME_PATH')
        self.host_timescale = host_timescale
        self.ticks_per_observation = ticks_per_observation
        self.game_mode = game_mode
        self.host_mode = host_mode
        self.game_id = game_id or str(uuid.uuid1())
        self.record_replay = record_replay
        if record_replay:
            logger.warning(
                'replay recording needs GOTV, and GOTV crashes the 2026 macOS client about two '
                'minutes into a match (segfault in its HLTVServerAsync thread, see '
                'docs/VERSION_DIFF.md). The episode ends there. Other platforms are untested.'
            )
        self.process = None
        # replays written after this are ours; set before the client can start one
        self.started_at = time.time()
        self.session_root = session_root or tempfile.gettempdir()
        self.dota_bot_path = os.path.join(self.dota_path, 'dota', 'scripts', 'vscripts', self.BOTS_FOLDER_NAME)
        self.bot_path = self._create_bot_path()
        # Note (ruidu): the server execs its servercfgfile once it activates, when the script VM exists; a
        # +script_reload_code on the launch line runs before the map loads and is lost (docs/VERSION_DIFF.md 3.1).
        self.dota_cfg_path = os.path.join(self.dota_path, 'dota', 'cfg', self.SERVER_CFG_FILENAME)
        os.makedirs(os.path.dirname(self.dota_cfg_path), exist_ok=True)
        with open(self.dota_cfg_path, 'w', encoding='utf-8') as f:
            f.write('script_reload_code bots/server_actions\n')
        self._write_config()

    @property
    def console_log_path(self):
        return os.path.join(self.bot_path, self.CONSOLE_LOG_FILENAME)

    @property
    def replay_folder(self):
        """Where the client writes its .dem files; not ours, it is part of the Dota install."""
        return os.path.join(self.dota_path, 'dota', 'replays')

    def _write_config(self):
        config = {
            'game_id': self.game_id,
            'ticks_per_observation': self.ticks_per_observation,
            'heroes': {str(team): list(names) for team, names in self.heroes.items()},
            'control': {str(team): mode for team, mode in self.control.items()},
        }

        self.write_static_config(data=config)

    def write_static_config(self, data):
        self._write_bot_data_file(filename_stem=self.CONFIG_FILENAME, data=data)

    def write_live_config(self, data):
        self._write_bot_data_file(filename_stem=self.LIVE_CONFIG_FILENAME, data=data)

    def write_action(self, data, team_id):
        """`data` is the JSON-able action dict read by lua/bot_controlled.lua.tpl (see docs/ACTIONS.md)."""
        filename_stem = self.ACTIONS_FILENAME_FMT.format(team_id=team_id)
        self._write_bot_data_file(filename_stem=filename_stem, data=data)

    def _write_bot_data_file(self, filename_stem, data):
        """Write a lua file returning a JSON string, atomically so lua never loads a half-written file."""
        filename = os.path.join(self.bot_path, f'{filename_stem}.lua')
        # The payload sits inside a single-quoted lua string.
        payload = json.dumps(data, separators=(',', ':')).replace('\\', '\\\\').replace("'", "\\'")
        tmp_filename = filename + '.tmp'
        with open(tmp_filename, 'w', encoding='utf-8') as f:
            f.write(f"return '{payload}'")
        os.replace(tmp_filename, filename)

    def _create_bot_path(self):
        """Copy our lua into a session folder and symlink Dota's vscripts/bots to it."""
        if os.path.islink(self.dota_bot_path):
            os.remove(self.dota_bot_path)
        elif os.path.exists(self.dota_bot_path):
            raise ValueError(f'There is already a bots directory ({self.dota_bot_path})! Please remove manually.')

        self.session_folder = os.path.join(self.session_root, 'dota2_env_' + str(self.game_id))
        bot_path = os.path.join(self.session_folder, self.BOTS_FOLDER_NAME)
        if os.path.isdir(bot_path):
            shutil.rmtree(bot_path)
        os.makedirs(bot_path)

        for filename in glob.glob(os.path.join(LUA_DIR, '*.lua')):
            shutil.copy(filename, bot_path)
        shutil.copytree(os.path.join(LUA_DIR, 'actions'), os.path.join(bot_path, 'actions'))
        # Dota loads bot_<hero>.lua for each bot hero; every configured hero gets the same script.
        for hero in {name for names in self.heroes.values() for name in names}:
            shutil.copy(
                os.path.join(LUA_DIR, 'bot_controlled.lua.tpl'),
                os.path.join(bot_path, f'bot_{hero.replace("npc_dota_hero_", "")}.lua'),
            )

        # On Windows this needs an admin shell (or developer mode).
        os.symlink(src=bot_path, dst=self.dota_bot_path, target_is_directory=True)
        logger.info(f'bots folder: {self.dota_bot_path} -> {bot_path}')
        return bot_path

    def remove_dota_files(self) -> None:
        """Take the bots symlink and the server cfg back out of the Dota install."""
        if os.path.islink(self.dota_bot_path):
            os.remove(self.dota_bot_path)
        if os.path.exists(self.dota_cfg_path):
            os.remove(self.dota_cfg_path)

    @staticmethod
    def stop_dota_pids(timeout: float = 15.0) -> None:
        """Only one client can be active at a time, so kill anything left over and wait until it is gone.

        A client still shutting down while the next one starts makes the new one exit during its own start-up
        (measured 2026-09, docs/VERSION_DIFF.md 1.3), hence the wait.
        """
        if platform == 'win32':
            os.system('taskkill /F /IM dota2.exe')
            return
        os.system('pkill -x dota2')
        deadline = time.time() + timeout
        while subprocess.run(['pgrep', '-x', 'dota2'], capture_output=True, check=False).returncode == 0:
            if time.time() >= deadline:
                os.system('pkill -9 -x dota2')
                time.sleep(1.0)
                break
            time.sleep(0.5)

    def _executable(self):
        if platform == 'win32':
            return os.path.join(self.dota_path, 'bin', 'win64', 'dota2.exe')
        return os.path.join(self.dota_path, 'dota.sh')

    def launch_args(self):
        args = [
            self._executable(),
            # NOTE: `_frames` and `_threaded` no longer appear in the 2026 server binary (see
            # docs/VERSION_DIFF.md). Unknown switches are ignored, so they stay for old builds.
            '-botworldstatesocket_threaded',
            '-botworldstatetosocket_frames',
            str(self.ticks_per_observation),
            '-botworldstatetosocket_radiant',
            str(self.PORT_WORLDSTATES[TEAM_RADIANT]),
            '-botworldstatetosocket_dire',
            str(self.PORT_WORLDSTATES[TEAM_DIRE]),
            '-con_logfile',
            self.console_log_path,
            '-con_timestamp',
            '-console',
            '-insecure',
            '-noip',
            # Without it the engine kills the process once lua has blocked for 60 s (verified 2026-09,
            # see docs/VERSION_DIFF.md); a blocking lua is what a lockstep mode would rely on.
            '-nowatchdog',
            '+clientport',
            '27006',  # Relates to steam client.
            '+dota_surrender_on_disconnect',
            '0',
            '+host_timescale',
            str(self.host_timescale),
            '+hostname',
            'dota2_env',
            '+sv_cheats',
            '1',
            '+sv_hibernate_when_empty',
            '0',
            '+dota_1v1_skip_strategy',
            '1',
            # the cfg that loads bridge/lua/server_actions.lua into the server VM (dedicated and listen servers)
            '+servercfgfile',
            self.SERVER_CFG_FILENAME,
            '+lservercfgfile',
            self.SERVER_CFG_FILENAME,
        ]
        if self.host_mode == HOST_MODE_DEDICATED:
            args.append('-dedicated')
        if self.host_mode in (HOST_MODE_DEDICATED, HOST_MODE_GUI):
            args.append('-fill_with_bots')
            args.extend(['+map', 'start', 'gamemode', str(self.game_mode)])
            args.extend(['+sv_lan', '1'])
        if self.host_mode == HOST_MODE_GUI_MENU:
            args.append('-novid')
            args.extend(['+sv_lan', '0'])
        if self.record_replay:
            # Note (ruidu): do not add +tv_delay here. GOTV crashes the client tv_delay seconds
            # after it starts broadcasting, and the demo only gets gameplay once it does, so a
            # short delay crashes sooner and a long one records nothing at all. The default delay
            # is the least bad point on that trade-off (measured, see docs/VERSION_DIFF.md).
            args.extend(['+tv_enable', '1', '+tv_autorecord', '1'])
        return args

    def run_dota(self):
        self.stop_dota_pids()
        args = self.launch_args()
        logger.info(' '.join(args))
        # Note (ruidu): dota.sh is a bash wrapper around the dota2 binary; its own session lets stop_dota
        # signal the whole group, since a SIGTERM to the wrapper alone leaves the game running.
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=platform != 'win32'
        )
        return self.process

    def stop_dota(self, timeout=20.0):
        """Ask our own client to quit and wait for it, instead of killing every dota2 on the box.

        A client that ignores the request for timeout seconds is killed. It never shuts down
        cleanly either way: SIGTERM and SIGINT both kill it on the spot and it reads no commands
        from stdin, so a replay always ends up unfinalized (see docs/VERSION_DIFF.md).
        """
        if self.process is None:
            return
        self.signal_dota(signal.SIGTERM)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f'dota did not exit within {timeout:.0f}s, killing it')
            self.signal_dota(signal.SIGKILL)
            self.process.wait(timeout=5)
        if platform != 'win32':
            self.stop_dota_pids()
        self.process = None

    def signal_dota(self, signum: int) -> None:
        """Send signum to the wrapper and the game together (the wrapper's process group)."""
        if platform == 'win32':
            self.process.send_signal(signum)
            return
        with contextlib.suppress(ProcessLookupError):  # the game may have died on its own already
            os.killpg(self.process.pid, signum)

    def collect_replay(self, replay_dir):
        """Move the .dem our client wrote into replay_dir, returning its new path or None.

        Call it after stop_dota(): the file is only complete once the client has exited. A relative
        replay_dir lands under the current working directory.
        """
        ours = [
            path
            for path in glob.glob(os.path.join(self.replay_folder, '*.dem'))
            if os.path.getmtime(path) >= self.started_at
        ]
        if not ours:
            logger.warning(f'no replay was written to {self.replay_folder}')
            return None
        os.makedirs(replay_dir, exist_ok=True)
        # GOTV names it auto-<date>-<time>-<map>-<hostname>.dem, which is worth keeping; only two
        # matches recorded in the same minute would collide.
        destination = os.path.join(replay_dir, os.path.basename(max(ours, key=os.path.getmtime)))
        if os.path.exists(destination):
            destination = f'{os.path.splitext(destination)[0]}-{time.strftime("%H%M%S")}.dem'
        shutil.move(max(ours, key=os.path.getmtime), destination)

        # A Source 2 demo carries the offset of its CDemoFileInfo block at byte 8, and the client
        # only fills it in when it closes the recording (see docs/VERSION_DIFF.md).
        with open(destination, 'rb') as handle:
            file_info_offset = struct.unpack_from('<i', handle.read(16), 8)[0]
        if file_info_offset == 0:
            logger.warning(f'replay {destination} is unfinalized: the match did not run to its end')
        logger.info(f'replay: {destination}')
        return destination
