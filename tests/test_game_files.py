import json
import os
import queue
import struct

import pytest

from dota2_env.bridge import worldstate
from dota2_env.bridge.constants import DOTA_GAMERULES_STATE_PRE_GAME, TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.game import DotaGame
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.bridge.session import DotaSession


@pytest.fixture
def game(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts').mkdir(parents=True)
    game = DotaGame(
        dota_path=str(dota_path),
        session_root=str(tmp_path / 'sessions'),
        heroes={TEAM_RADIANT: ('npc_dota_hero_nevermore', 'npc_dota_hero_lina'), TEAM_DIRE: ('npc_dota_hero_sniper',)},
    )
    yield game
    game.remove_dota_files()


def lua_string_payload(path):
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    assert text.startswith("return '") and text.endswith("'")
    # undo the lua escaping the same way lua would
    return text[len("return '") : -1].replace("\\'", "'").replace('\\\\', '\\')


def test_session_folder_layout(game):
    assert os.path.islink(game.dota_bot_path)
    files = set(os.listdir(game.bot_path))
    expected = {'bot_nevermore.lua', 'bot_lina.lua', 'bot_sniper.lua', 'hero_selection.lua', 'config_auto.lua'}
    expected |= {'server_actions.lua'}
    assert expected | {'actions'} <= files
    config = json.loads(lua_string_payload(os.path.join(game.bot_path, 'config_auto.lua')))
    assert config['heroes'] == {'2': ['npc_dota_hero_nevermore', 'npc_dota_hero_lina'], '3': ['npc_dota_hero_sniper']}
    assert config['control'] == {'2': 'agent', '3': 'idle'}


def test_the_server_cfg_loads_the_server_vm_script_and_leaves_with_the_symlink(game):
    args = game.launch_args()
    assert args[args.index('+servercfgfile') + 1] == 'dota2_env_server.cfg'
    assert args[args.index('+lservercfgfile') + 1] == 'dota2_env_server.cfg'
    with open(os.path.join(game.dota_path, 'dota', 'cfg', 'dota2_env_server.cfg'), encoding='utf-8') as handle:
        assert handle.read() == 'script_reload_code bots/server_actions\n'
    game.remove_dota_files()
    assert not os.path.exists(game.dota_cfg_path) and not os.path.lexists(game.dota_bot_path)


def test_stop_dota_takes_the_launcher_and_its_child_down_together(game, monkeypatch):
    # dota.sh is a bash wrapper around the game; a signal to the wrapper alone would leave the game running
    monkeypatch.setattr(game, 'launch_args', lambda: ['/bin/bash', '-c', 'sleep 60 & wait'])
    monkeypatch.setattr(game, 'stop_dota_pids', lambda: None)
    process = game.run_dota()
    group = os.getpgid(process.pid)
    assert group == process.pid
    game.stop_dota(timeout=5.0)
    assert game.process is None
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)


def test_action_file_escaping_roundtrip(game):
    data = {'dotaTime': 1.5, 'actions': [{'actionType': 'ACTION_CHAT', 'chat': {'message': 'it\'s a "test" \\ ok'}}]}
    game.write_action(data, TEAM_RADIANT)
    path = os.path.join(game.bot_path, 'actions_t2.lua')
    assert json.loads(lua_string_payload(path)) == data
    assert not os.path.exists(path + '.tmp')


def test_refuses_to_replace_a_real_bots_directory(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts' / 'bots').mkdir(parents=True)
    with pytest.raises(ValueError):
        DotaGame(dota_path=str(dota_path), session_root=str(tmp_path))


def test_replay_recording_is_opt_in(game, tmp_path):
    assert '+tv_enable' not in game.launch_args()
    recording = DotaGame(dota_path=game.dota_path, session_root=str(tmp_path / 'recorded'), record_replay=True)
    assert {'+tv_enable', '+tv_autorecord'} <= set(recording.launch_args())


def test_collect_replay_moves_the_dem_out_of_the_dota_install(game, tmp_path, monkeypatch, caplog):
    assert game.collect_replay(str(tmp_path / 'replays')) is None  # nothing recorded
    os.makedirs(game.replay_folder)
    recorded = os.path.join(game.replay_folder, 'auto-20260920-0117-start-dota2_env.dem')
    with open(recorded, 'wb') as handle:
        handle.write(b'PBDEMS2\x00' + struct.pack('<ii', 0, 0))  # a demo the client never closed

    monkeypatch.chdir(tmp_path)
    destination = game.collect_replay('replays')  # relative to the working directory
    assert destination == os.path.join('replays', os.path.basename(recorded))  # GOTV's name is kept
    assert os.path.isfile(destination) and not os.path.exists(recorded)
    assert 'unfinalized' in caplog.text


def test_cast_slots_follow_the_newest_slots_line_of_each_hero(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts').mkdir(parents=True)
    session = DotaSession(TEAM_RADIANT, dota_path=str(dota_path), session_root=str(tmp_path / 'sessions'))
    orb = {'name': 'puck_illusory_orb', 'kinds': ['point']}
    lines = [
        (TEAM_RADIANT, 0, {'0': orb, '6': {'name': 'item_tango', 'kinds': ['self']}}),
        (TEAM_DIRE, 5, {'0': orb}),  # the other team
        (TEAM_RADIANT, 1, []),  # how dkjson writes an empty table
        # the tango is eaten; a passive's kinds are an empty table too
        (TEAM_RADIANT, 0, {'0': orb, '10': {'name': 'item_circlet', 'kinds': []}}),
    ]
    with open(session.game.console_log_path, 'w', encoding='utf-8') as log:
        for team, player_id, slots in lines:
            log.write('SLOTS\t' + json.dumps({'team': team, 'player_id': player_id, 'slots': slots}) + '\n')
    session.poll_delivery()
    assert session.cast_slots == {0: {0: ('puck_illusory_orb', ('point',)), 10: ('item_circlet', ())}, 1: {}}
    session.close()


def test_close_releases_the_symlink_even_when_shutting_down_fails(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts').mkdir(parents=True)
    session = DotaSession(TEAM_RADIANT, dota_path=str(dota_path), session_root=str(tmp_path / 'sessions'))

    def wedged():
        raise RuntimeError('client will not quit')

    session.game.stop_dota = wedged
    with pytest.raises(RuntimeError):
        session.close()
    assert not os.path.exists(session.game.dota_bot_path)
    assert not os.path.exists(session.game.dota_cfg_path)
    assert not os.path.isdir(session.game.session_folder)


def frame(dota_time: float, tree_ids: list[int], units: int = 1) -> bytes:
    """A serialized PRE_GAME world state with one tree cut per id; without units it is not actionable."""
    world_state = CMsgBotWorldState(dota_time=dota_time, game_state=DOTA_GAMERULES_STATE_PRE_GAME)
    for tree_id in tree_ids:
        world_state.tree_events.add(tree_id=tree_id, destroyed=True)
    for handle in range(units):
        world_state.units.add(handle=handle)
    return world_state.SerializeToString()


def test_observe_hands_over_every_frame_since_the_last_call(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts').mkdir(parents=True)
    session = DotaSession(TEAM_RADIANT, dota_path=str(dota_path), session_root=str(tmp_path / 'sessions'))
    session._queue = queue.Queue()
    for raw in (frame(1.0, [1], units=3), frame(2.0, [2, 3], units=2), frame(3.0, [4])):
        session._queue.put(raw)
    frames = session.observe(timeout=1)
    assert [world_state.dota_time for world_state in frames] == [1.0, 2.0, 3.0]
    assert [[event.tree_id for event in world_state.tree_events] for world_state in frames] == [[1], [2, 3], [4]]
    assert session.skipped_observations == 2  # the frames a policy acting on the newest one did not act on
    session.close()


def test_listener_forwards_every_playable_frame(monkeypatch):
    frames = [frame(1.0, [1]), frame(2.0, [2]), frame(3.0, [3], units=0), frame(4.0, [4])]

    def read(sock):
        if not frames:
            raise EOFError  # ends the listener's loop, which only handles connection errors
        return frames.pop(0)

    monkeypatch.setattr(worldstate, 'connect', lambda port, **kwargs: None)
    monkeypatch.setattr(worldstate, 'read_raw_world_state', read)
    forwarded = queue.Queue()
    with pytest.raises(EOFError):
        worldstate.worldstate_listener(12120, forwarded)
    received = [worldstate.parse_world_state(forwarded.get_nowait()) for _ in range(forwarded.qsize())]
    # the frame without units cannot be played; its tree events ride on with the next one
    assert [[event.tree_id for event in ws.tree_events] for ws in received] == [[1], [2], [3, 4]]
    assert received[2].dota_time == 4.0 and len(received[2].units) == 1
