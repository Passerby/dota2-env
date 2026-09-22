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
    LONE_TREE,
    SKILL_CAST_RANGE,
    FakeSession,
    lone_tree_cells,
)
from gymnasium.utils.env_checker import check_env

import dota2_env
from dota2_env.actions import MOVE_DISTANCE, ActionType
from dota2_env.bridge.constants import TEAM_RADIANT
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import (
    CMsgBotWorldState,
)
from dota2_env.bridge.worldstate import connect, read_world_state
from dota2_env.map_features import (
    LANDMARK_FEATURES,
    LANDMARKS,
    MAP_FEATURES,
    MAP_RADIUS,
    MAP_SCALE,
    RUNE_FEATURES,
    load_map,
)
from dota2_env.observation import N_ABILITIES, RESPAWN_LOCATION
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
    observation, _ = env.reset(seed=0)
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
    # the fake's no-target shadowraze in slot 0 and its tango at 6, which CAST eats a tree with
    assert list(mask['ability'][ActionType.CAST]) == [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert not mask['ability'][ActionType.CAST_TARGET].any() and not mask['ability'][ActionType.CAST_DIRECTION].any()
    assert mask['type'][ActionType.ATTACK] and mask['type'][ActionType.CAST]
    assert not mask['type'][ActionType.CAST_TARGET] and not mask['type'][ActionType.CAST_DIRECTION]


def test_the_mask_follows_what_lua_says_a_slot_can_aim_at(env):
    env.reset()
    slots = last_session().cast_slots[0]
    slots[0] = ('lina_dragon_slave', ('enemy', 'point'))
    slots[N_ABILITIES] = ('item_circlet', ())  # a passive
    *_, info = env.step(NOOP)
    mask = info['action_mask']
    assert not mask['ability'][ActionType.CAST].any() and not mask['type'][ActionType.CAST]
    assert mask['ability'][ActionType.CAST_TARGET][0] and mask['type'][ActionType.CAST_TARGET]
    assert mask['ability'][ActionType.CAST_DIRECTION][0] and mask['type'][ActionType.CAST_DIRECTION]
    del slots[0]  # a slot lua has not reported (yet) is not castable at all
    *_, info = env.step(NOOP)
    assert not info['action_mask']['ability'][:, 0].any()


def test_cast_direction_aims_cast_range_away(env):
    env.reset()
    x, y = last_session().hero_xy
    env.step(dict(NOOP, type=int(ActionType.CAST_DIRECTION), ability=0, move=4))  # 4/16 of a turn = north
    sent = last_session().sent[-1][1][0]
    assert sent['actionType'] == 'DOTA_UNIT_ORDER_CAST_POSITION' and sent['castLocation']['abilitySlot'] == 0
    location = sent['castLocation']['location']
    assert location['x'] == pytest.approx(x) and location['y'] == pytest.approx(y + SKILL_CAST_RANGE)
    env.step(dict(NOOP, type=int(ActionType.CAST_DIRECTION), ability=N_ABILITIES, move=0))
    sent = last_session().sent[-1][1][0]  # the fake tango reports no cast range, so a short step
    assert sent['castLocation']['abilitySlot'] == -1
    assert sent['castLocation']['location']['x'] == pytest.approx(x + MOVE_DISTANCE)


def test_items_are_observed_and_used_through_negative_slots(env):
    observation, info = env.reset()
    assert list(observation['items'][0]) == pytest.approx([44, 0.3, 0.0, 1.0])  # id, charges / 10, cd, castable
    assert info['action_mask']['ability'][ActionType.CAST][N_ABILITIES]
    assert env.unwrapped.cast_slots()[N_ABILITIES] == ('item_tango', ('self',))
    env.step(dict(NOOP, type=int(ActionType.CAST), ability=N_ABILITIES))
    assert last_session().sent[-1][1][0]['cast'] == {'abilitySlot': -1}  # lua's inventory slot 0
    row = env.unwrapped._observation.unit_handles.index(ENEMY_HERO_HANDLE)
    env.step(dict(NOOP, type=int(ActionType.CAST_TARGET), ability=N_ABILITIES, target=row))
    assert last_session().sent[-1][1][0]['castTarget'] == {'abilitySlot': -1, 'target': ENEMY_HERO_HANDLE}


def test_silence_blocks_skills_and_mute_blocks_items(env):
    env.reset()
    last_session().hero_flags = {'is_silenced': True}
    *_, info = env.step(NOOP)
    cast = info['action_mask']['ability'][ActionType.CAST]
    assert not cast[0] and cast[N_ABILITIES] and info['action_mask']['type'][ActionType.CAST]
    last_session().hero_flags = {'is_muted': True}
    *_, info = env.step(NOOP)
    cast = info['action_mask']['ability'][ActionType.CAST]
    assert cast[0] and not cast[N_ABILITIES]


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
    _, _, terminated, truncated, info = env.step(NOOP)
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
    assert '[0] nevermore_shadowraze1 lvl 1 ready: CAST' in text and '[6] item_tango charges 3 ready: CAST' in text
    row = env.unwrapped._observation.unit_handles.index(ENEMY_CREEP_HANDLE)
    _, _, _, _, info = text_env.step(json.dumps({'type': 'attack', 'target': row}))
    assert info['action_error'] is None
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_ATTACK_TARGET'
    _, _, _, _, info = text_env.step('{"type": "CAST", "ability": 3}')
    assert 'not castable' in info['action_error']
    _, _, _, _, info = text_env.step('{"type": "CAST_DIRECTION", "ability": 0, "move": 4}')  # a no-target skill
    assert 'CAST_DIRECTION is not legal' in info['action_error']
    _, _, _, _, info = text_env.step('walk to the river')
    assert 'cannot parse' in info['action_error']
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_NONE'


def test_a_vector_skill_is_cast_with_a_second_point(env):
    env.reset()
    last_session().cast_slots[0][0] = ('pangolier_swashbuckle', ('point', 'vector'))
    x, y = last_session().hero_xy
    env.step(NOOP)  # the mask picks the new kinds up on the next step
    env.step(dict(NOOP, type=int(ActionType.CAST_DIRECTION), ability=0, move=4))  # north
    sent = last_session().sent[-1][1][0]
    assert sent['actionType'] == 'DOTA_UNIT_ORDER_CAST_VECTOR' and sent['castVector']['abilitySlot'] == 0
    location, direction = sent['castVector']['location'], sent['castVector']['direction']
    assert location['x'] == pytest.approx(x) and location['y'] == pytest.approx(y + SKILL_CAST_RANGE)
    assert direction['x'] == pytest.approx(0) and direction['y'] == pytest.approx(1)
    *_, info = env.step(NOOP)
    creep = info['world_state'].units[[unit.handle for unit in info['world_state'].units].index(ENEMY_CREEP_HANDLE)]
    row = env.unwrapped._observation.unit_handles.index(ENEMY_CREEP_HANDLE)
    env.step(dict(NOOP, type=int(ActionType.CAST_TARGET), ability=0, target=row))
    sent = last_session().sent[-1][1][0]  # through the creep, which stands 300 east of the hero
    assert sent['actionType'] == 'DOTA_UNIT_ORDER_CAST_VECTOR'
    assert sent['castVector']['location'] == {'x': creep.location.x, 'y': creep.location.y, 'z': creep.location.z}
    assert sent['castVector']['direction'] == {'x': pytest.approx(1), 'y': pytest.approx(0)}
    env.step(dict(NOOP, type=int(ActionType.CAST_DIRECTION), ability=N_ABILITIES, move=0))  # the tango is no vector
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_CAST_POSITION'


def test_text_says_where_a_vector_skill_swings(env):
    text_env = TextWrapper(env)
    text_env.reset()
    last_session().cast_slots[0][0] = ('pangolier_swashbuckle', ('point', 'vector'))
    text, *_ = text_env.step('{"type": "NOOP"}')
    ready = 'ready: CAST_TARGET CAST_DIRECTION (vector: runs on past the target / along the direction)'
    assert f'[0] pangolier_swashbuckle lvl 1 {ready}' in text
    assert '[6] item_tango charges 3 ready: CAST\n' in text  # only the vector skill carries the note


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
            conn.sendall(frame[i : i + 1000])
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
    observation, *_ = env.step(dict(NOOP, type=int(ActionType.MOVE), move=4))
    location = last_session().sent[-1][1][0]['moveDirectly']['location']
    assert location['x'] == pytest.approx(-6700) and location['y'] == pytest.approx(-6400)  # north, as asked
    assert observation['unit_mask'].sum() > 0  # and is reported again


def test_unreported_hero_is_walked_out_instead_of_waiting(env):
    env.reset()
    last_session().hero_hidden = True
    env.step(NOOP)
    observation, *_ = env.step(NOOP)
    location = last_session().sent[-1][1][0]['moveDirectly']['location']
    assert location['x'] > -6700 and location['y'] > -6700  # towards the map centre
    assert observation['unit_mask'].sum() > 0


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


TREE = MAP_FEATURES.index('tree')


def test_local_map_shows_a_tree_until_it_is_cut_and_again_once_it_regrows(env):
    observation, _ = env.reset()
    rows, cols = lone_tree_cells(*last_session().hero_xy)
    assert observation['local_map'][TREE][rows, cols].all()
    assert observation['local_map'][MAP_FEATURES.index('blocked'), MAP_RADIUS, MAP_RADIUS] == 0  # the hero's own cell
    last_session().tree_events.append((LONE_TREE, True))
    observation, *_ = env.step(NOOP)
    assert not observation['local_map'][TREE][rows, cols].any()
    last_session().tree_events.append((LONE_TREE, False))
    observation, *_ = env.step(NOOP)
    assert observation['local_map'][TREE][rows, cols].all()


def test_a_tree_cut_before_the_hero_spawns_is_not_missed():
    def early_cut(**kwargs):
        session = FakeSession(**kwargs)
        session.tree_events.append((LONE_TREE, True))  # goes out with the first frame, which has no heroes yet
        return session

    env = gym.make('dota2_env/Mid1v1-v0', session_factory=early_cut)
    observation, _ = env.reset()
    rows, cols = lone_tree_cells(*last_session().hero_xy)
    assert not observation['local_map'][TREE][rows, cols].any()
    env.close()


def test_rune_spots_and_outposts(env):
    env.reset()
    last_session().available_runes.add('bounty_top')
    observation, *_ = env.step(NOOP)
    runes = observation['runes']
    assert runes[:, RUNE_FEATURES.index('available')].tolist() == [0, 0, 1, 0]
    assert runes[:, :2] * MAP_SCALE + last_session().hero_xy == pytest.approx(load_map().runes, abs=0.1)
    is_ours = observation['landmarks'][:, LANDMARK_FEATURES.index('is_ours')]
    assert [LANDMARKS[i] for i in np.flatnonzero(is_ours)] == ['outpost_top']  # Radiant's as a match starts
    last_session().outpost_teams['outpost_bottom'] = TEAM_RADIANT
    observation, *_ = env.step(NOOP)
    assert observation['landmarks'][:, LANDMARK_FEATURES.index('is_ours')].sum() == 2


def test_map_of_an_unreported_hero_is_measured_from_its_spawn_point(env):
    env.reset()
    last_session().hero_hidden = True
    observation, *_ = env.step(NOOP)
    spawn = RESPAWN_LOCATION[TEAM_RADIANT]
    assert observation['landmarks'][:, :2] * MAP_SCALE + spawn == pytest.approx(load_map().landmarks, abs=0.1)
