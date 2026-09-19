"""Launching the Dota 2 client and the file-based Python -> Lua channel."""
import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from sys import platform

from dota2_env.bridge.constants import TEAM_RADIANT, TEAM_DIRE, HOST_MODE_DEDICATED, HOST_MODE_GUI, HOST_MODE_GUI_MENU

logger = logging.getLogger("dota2_env")

LUA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lua')

DEFAULT_HERO = 'npc_dota_hero_nevermore'
CONTROL_AGENT, CONTROL_BUILTIN, CONTROL_IDLE = 'agent', 'builtin', 'idle'

DEFAULT_GAME_PATHS = {
    'darwin': "~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game",
    'win32': r"C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game",
    'linux': "~/.steam/steam/steamapps/common/dota 2 beta/game",
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
    PORT_WORLDSTATES = {TEAM_RADIANT: 12120, TEAM_DIRE: 12121}

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
    ):
        """
        heroes:  {team_id: hero unit name} for the first player of each team (the rest are idle wisps).
        control: {team_id: "agent" | "builtin" | "idle"}; "agent" heroes execute the action files
                 written by `write_action`, "builtin" heroes are played by Valve's default bot AI.
        """
        self.heroes = dict(heroes or {TEAM_RADIANT: DEFAULT_HERO, TEAM_DIRE: DEFAULT_HERO})
        self.control = dict(control or {TEAM_RADIANT: CONTROL_AGENT, TEAM_DIRE: CONTROL_IDLE})
        self.dota_path = dota_path or get_default_game_path()
        if not os.path.isdir(self.dota_path):
            raise FileNotFoundError('Dota 2 not found at {}; set DOTA_GAME_PATH'.format(self.dota_path))
        self.host_timescale = host_timescale
        self.ticks_per_observation = ticks_per_observation
        self.game_mode = game_mode
        self.host_mode = host_mode
        self.game_id = game_id or str(uuid.uuid1())
        self.session_root = session_root or tempfile.gettempdir()
        self.dota_bot_path = os.path.join(self.dota_path, 'dota', 'scripts', 'vscripts', self.BOTS_FOLDER_NAME)
        self.bot_path = self._create_bot_path()
        self._write_config()

    @property
    def console_log_path(self):
        return os.path.join(self.bot_path, self.CONSOLE_LOG_FILENAME)

    def _write_config(self):
        config = {
            'game_id': self.game_id,
            'ticks_per_observation': self.ticks_per_observation,
            'heroes': {str(team): hero for team, hero in self.heroes.items()},
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
        filename = os.path.join(self.bot_path, '{}.lua'.format(filename_stem))
        # The payload sits inside a single-quoted lua string.
        payload = json.dumps(data, separators=(',', ':')).replace('\\', '\\\\').replace("'", "\\'")
        tmp_filename = filename + '.tmp'
        with open(tmp_filename, 'w', encoding='utf-8') as f:
            f.write("return '{}'".format(payload))
        os.replace(tmp_filename, filename)

    def _create_bot_path(self):
        """Copy our lua into a session folder and symlink Dota's vscripts/bots to it."""
        if os.path.islink(self.dota_bot_path):
            os.remove(self.dota_bot_path)
        elif os.path.exists(self.dota_bot_path):
            raise ValueError('There is already a bots directory ({})! Please remove manually.'.format(self.dota_bot_path))

        self.session_folder = os.path.join(self.session_root, 'dota2_env_' + str(self.game_id))
        bot_path = os.path.join(self.session_folder, self.BOTS_FOLDER_NAME)
        if os.path.isdir(bot_path):
            shutil.rmtree(bot_path)
        os.makedirs(bot_path)

        for filename in glob.glob(os.path.join(LUA_DIR, '*.lua')):
            shutil.copy(filename, bot_path)
        shutil.copytree(os.path.join(LUA_DIR, 'actions'), os.path.join(bot_path, 'actions'))
        # Dota loads bot_<hero>.lua for each bot hero; every configured hero gets the same script.
        for hero in set(self.heroes.values()):
            shutil.copy(os.path.join(LUA_DIR, 'bot_controlled.lua.tpl'),
                        os.path.join(bot_path, 'bot_{}.lua'.format(hero.replace('npc_dota_hero_', ''))))

        # On Windows this needs an admin shell (or developer mode).
        os.symlink(src=bot_path, dst=self.dota_bot_path, target_is_directory=True)
        logger.info('bots folder: %s -> %s', self.dota_bot_path, bot_path)
        return bot_path

    def remove_bot_symlink(self):
        if os.path.islink(self.dota_bot_path):
            os.remove(self.dota_bot_path)

    @staticmethod
    def stop_dota_pids():
        """Only one client can be active at a time, so kill anything left over."""
        if platform != 'win32':
            os.system("pkill dota2")
        else:
            os.system("taskkill /F /IM dota2.exe")

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
            '-botworldstatetosocket_frames', str(self.ticks_per_observation),
            '-botworldstatetosocket_radiant', str(self.PORT_WORLDSTATES[TEAM_RADIANT]),
            '-botworldstatetosocket_dire', str(self.PORT_WORLDSTATES[TEAM_DIRE]),
            '-con_logfile', self.console_log_path,
            '-con_timestamp',
            '-console',
            '-insecure',
            '-noip',
            # Without it the engine kills the process once lua has blocked for 60 s (verified 2026-09,
            # see docs/VERSION_DIFF.md); a blocking lua is what a lockstep mode would rely on.
            '-nowatchdog',
            '+clientport', '27006',  # Relates to steam client.
            '+dota_surrender_on_disconnect', '0',
            '+host_timescale', str(self.host_timescale),
            '+hostname', 'dota2_env',
            '+sv_cheats', '1',
            '+sv_hibernate_when_empty', '0',
            '+dota_1v1_skip_strategy', '1',
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
        return args

    def run_dota(self):
        self.stop_dota_pids()
        args = self.launch_args()
        logger.info(' '.join(args))
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
