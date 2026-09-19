import json
import socket
import struct
import threading

import gymnasium as gym
import numpy as np
import pytest
from fake_session import (
    ALLY_CREEP_HANDLE,
    ENEMY_CREEP_HANDLE,
    ENEMY_HERO_HANDLE,
    FakeSession,
)
from gymnasium.utils.env_checker import check_env

import dota2_env
from dota2_env.actions import ActionType
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import (
    CMsgBotWorldState,
)
from dota2_env.bridge.worldstate import connect, read_world_state
from dota2_env.rewards import Mid1v1Rules
from dota2_env.wrappers import FlatActionWrapper, FlatObservationWrapper, TextWrapper

NOOP = {'type': 0, 'move': 0, 'target': 0, 'ability': 0}


@pytest.fixture
def env():
    env = gym.make('dota2_env/Mid1v1-v0', session_factory=FakeSession)
    yield env
    env.close()


def last_session():
    return FakeSession.instances[-1]


def test_passes_gymnasium_env_checker(env):
    check_env(env.unwrapped, skip_render_check=True)


def test_reset_waits_for_hero_and_buys_starting_items(env):
    observation, info = env.reset(seed=0)
    assert env.observation_space.contains(observation)
    assert observation['hero'][4] == 1.0  # is_alive
    assert observation['unit_mask'].sum() == 4  # enemy hero, enemy creep, ally creep, own tower
    env.step(NOOP)
    _, actions, extras = last_session().sent[0]
    assert actions == [{'actionType': 'DOTA_UNIT_ORDER_NONE', 'player': 0}]
    purchased = [e['purchaseItem']['itemName'] for e in extras if 'purchaseItem' in e]
    assert purchased == list(dota2_env.envs.mid1v1.DEFAULT_STARTING_ITEMS)
    trained = [e['trainAbility']['ability'] for e in extras if 'trainAbility' in e]
    assert trained[0] == 'slot:5'
    # queued extras are sent once
    env.step(NOOP)
    assert not any('purchaseItem' in e for e in last_session().sent[1][2])


def test_action_mask_and_target_rows(env):
    _, info = env.reset()
    base = env.unwrapped
    handles = base._observation.unit_handles
    mask = info['action_mask']
    assert mask['attack_target'][handles.index(ENEMY_CREEP_HANDLE)]
    assert mask['attack_target'][handles.index(ENEMY_HERO_HANDLE)]
    assert not mask['attack_target'][handles.index(ALLY_CREEP_HANDLE)]  # full health: no deny
    assert list(mask['ability']) == [1, 0, 0, 0, 0, 0]
    assert mask['type'][ActionType.ATTACK] and mask['type'][ActionType.CAST]


def test_last_hit_is_rewarded(env):
    env.reset()
    base = env.unwrapped
    rewards = []
    for _ in range(3):
        row = base._observation.unit_handles.index(ENEMY_CREEP_HANDLE)
        _, reward, terminated, truncated, info = env.step(dict(NOOP, type=int(ActionType.ATTACK), target=row))
        rewards.append(reward)
    sent = last_session().sent[-1][1][0]
    assert sent['attackTarget']['target'] == ENEMY_CREEP_HANDLE
    assert info['reward']['last_hit'] == 1
    assert rewards[-1] == pytest.approx(base.reward_fn.weights['last_hit'])
    assert rewards[0] == 0 and not terminated and not truncated


def test_move_goes_in_requested_direction(env):
    env.reset()
    x, y = last_session().hero_xy
    env.step(dict(NOOP, type=int(ActionType.MOVE), move=4))  # 4/16 of a turn = north
    new_x, new_y = last_session().hero_xy
    assert new_x == pytest.approx(x) and new_y == pytest.approx(y + 300)


def test_deaths_terminate_and_time_truncates():
    env = gym.make('dota2_env/Mid1v1-v0', session_factory=FakeSession, rules=Mid1v1Rules(max_dota_time=-89.0))
    env.reset()
    *_, terminated, truncated, info = env.step(NOOP)
    assert not terminated and not truncated
    for _ in range(5):
        *_, terminated, truncated, info = env.step(NOOP)
    assert truncated and not terminated

    env.reset()
    last_session().deaths = 2  # any cause counts, e.g. dying to the tower
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert terminated and info['winner'] == dota2_env.TEAM_DIRE
    assert info['reward']['win'] == -1.0 and info['reward']['death'] == 2
    env.close()
    assert last_session().closed


def test_reset_closes_previous_session(env):
    env.reset()
    first = last_session()
    env.reset()
    assert first.closed and last_session() is not first


def test_flat_wrappers(env):
    flat = FlatObservationWrapper(FlatActionWrapper(env))
    observation, _ = flat.reset()
    assert flat.observation_space.contains(observation)
    observation, *_ = flat.step(np.array([ActionType.MOVE, 0, 0, 0]))
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_MOVE_DIRECTLY'


def test_text_wrapper(env):
    text_env = TextWrapper(env)
    text, info = text_env.reset()
    assert 'nevermore_shadowraze1' in text and 'attackable' in text and 'legal action types' in text
    row = env.unwrapped._observation.unit_handles.index(ENEMY_CREEP_HANDLE)
    _, _, _, _, info = text_env.step(json.dumps({'type': 'attack', 'target': row}))
    assert info['action_error'] is None
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_ATTACK_TARGET'
    _, _, _, _, info = text_env.step('{"type": "CAST", "ability": 3}')
    assert 'not castable' in info['action_error']
    _, _, _, _, info = text_env.step('walk to the river')
    assert 'cannot parse' in info['action_error']
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_NONE'


def test_worldstate_socket_framing_survives_fragmentation():
    message = CMsgBotWorldState(dota_time=12.5)
    message.units.add(handle=7, name='x' * 5000)
    payload = message.SerializeToString()
    frame = struct.pack('<I', len(payload)) + payload

    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        for i in range(0, len(frame), 1000):  # dribble it out in pieces
            conn.sendall(frame[i:i + 1000])
        conn.sendall(frame)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    sock = connect(server.getsockname()[1], timeout=5)
    for _ in range(2):
        world_state = read_world_state(sock)
        assert world_state.dota_time == 12.5 and world_state.units[0].handle == 7
    with pytest.raises(ConnectionError):
        read_world_state(sock)


def test_unreported_respawned_hero_can_still_walk(env):
    env.reset()
    last_session().hero_hidden = True
    observation, _, _, _, info = env.step(NOOP)
    assert observation['hero'][4] == 1.0 and observation['unit_mask'].sum() == 0
    assert list(np.flatnonzero(info['action_mask']['type'])) == [ActionType.NOOP, ActionType.MOVE]
    observation, *_ = env.step(dict(NOOP, type=int(ActionType.MOVE), move=2))
    location = last_session().sent[-1][1][0]['moveDirectly']['location']
    assert location['x'] > -6700 and location['y'] > -6700  # walked away from the respawn point
    assert observation['unit_mask'].sum() > 0  # and is reported again


def test_feed_ending_truncates_instead_of_raising():
    env = gym.make('dota2_env/Mid1v1-v0', session_factory=FakeSession, step_timeout=0.01)
    env.reset()
    last_session().feed_ended = True
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert truncated and not terminated and reward == 0 and 'error' in info
    env.close()


def test_match_ended_by_client_terminates_with_winner():
    env = gym.make('dota2_env/Mid1v1-v0', session_factory=FakeSession, step_timeout=0.01)
    env.reset()
    last_session().feed_ended = True
    last_session().winner = dota2_env.TEAM_DIRE
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert terminated and not truncated and info['winner'] == dota2_env.TEAM_DIRE
    assert reward == pytest.approx(-env.unwrapped.reward_fn.weights['win'])
    env.close()


def test_action_delivery_is_reported(env):
    env.reset()
    *_, info = env.step(NOOP)
    assert info['action_delivery']['executed'] == 1 and info['action_delivery']['last_status'] == 'executed'
    assert info['action_delivery']['last_delay'] == pytest.approx(0.1)



def test_extras_of_lost_actions_are_sent_again(env):
    env.reset()
    last_session().ack_status = None  # lua never sees this file ...
    env.unwrapped.queue_purchase('item_boots')
    env.step(NOOP)
    last_session().ack_status = 'executed'  # ... because this one replaced it first
    *_, info = env.step(NOOP)
    assert info['action_delivery']['lost'] == 1
    env.step(NOOP)
    resent = [e['purchaseItem']['itemName'] for e in last_session().sent[-1][2] if 'purchaseItem' in e]
    assert 'item_boots' in resent
