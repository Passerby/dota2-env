import json
import re
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
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT, UNIT_TYPE_HERO, UNIT_TYPE_LANE_CREEP, UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import (
    CMsgBotWorldState,
)
from dota2_env.bridge.worldstate import connect, read_world_state
from dota2_env.game_text import load_records
from dota2_env.map_features import (
    LANDMARK_FEATURES,
    LANDMARKS,
    MAP_FEATURES,
    MAP_RADIUS,
    MAP_SCALE,
    RUNE_FEATURES,
    load_map,
)
from dota2_env.observation import HERO_FEATURES, N_ABILITIES, RESPAWN_LOCATION
from dota2_env.rewards import Mid1v1Rules
from dota2_env.text import describe
from dota2_env.wrappers import FlatActionWrapper, FlatObservationWrapper, TextWrapper

NOOP = {'type': 0, 'move': 0, 'target': 0, 'ability': 0}
SCROLL = load_records('items')['item_tpscroll']['id']


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


def test_a_label_goes_out_once_for_our_hero(env):
    env.reset(seed=0)
    env.unwrapped.queue_label('去中路补刀')
    env.step(NOOP)
    env.step(NOOP)
    labels = [[e for e in extras if e['actionType'] == 'ACTION_LABEL'] for _, _, extras in last_session().sent]
    assert labels == [[{'actionType': 'ACTION_LABEL', 'player': 0, 'label': {'text': '去中路补刀'}}], []]


def test_a_long_label_is_cut_to_the_size_of_the_health_bar_label_between_two_characters(env):
    env.reset()
    env.unwrapped.queue_label('补刀' * 100)  # 600 bytes of UTF-8
    env.step(NOOP)
    sent = next(extra for extra in last_session().sent[-1][2] if extra['actionType'] == 'ACTION_LABEL')
    assert sent['label']['text'] == '补刀' * 42 + '补'  # 85 characters of three bytes each: 255


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


def test_tp_casts_the_scroll_in_the_tp_slot_once_it_is_ready(env):
    env.reset()
    session = last_session()
    session.tp = (1, 40.0)  # the scroll a 5v5 hero spawns with is on cooldown
    observation, _, _, _, info = env.step(NOOP)
    assert not info['action_mask']['type'][ActionType.TP]
    assert observation['hero'][HERO_FEATURES.index('tp_cooldown')] == pytest.approx(4.0)
    session.tp = (1, 0.0)
    *_, info = env.step(NOOP)
    assert info['action_mask']['type'][ActionType.TP]
    env.step(dict(NOOP, type=int(ActionType.TP), point=(-1544.0, 99999.0)))  # our mid tier 1, dragged off the map
    max_y = load_map().world_bounds[3]
    assert session.sent[-1][1][0] == {
        'actionType': 'DOTA_UNIT_ORDER_CAST_POSITION',
        'player': 0,
        'castLocation': {'abilitySlot': -16, 'location': {'x': -1544.0, 'y': max_y, 'z': 0.0}},  # TP slot 15
    }
    session.hero_flags = {'is_rooted': True}
    *_, info = env.step(NOOP)
    assert not info['action_mask']['type'][ActionType.TP]


def test_a_rune_is_picked_up_at_the_spot_the_observation_marks(env):
    env.reset()
    session = last_session()
    *_, info = env.step(NOOP)
    assert not info['action_mask']['type'][ActionType.PICKUP_RUNE]
    session.available_runes.add('bounty_bottom')
    *_, info = env.step(NOOP)
    assert info['action_mask']['rune'].tolist() == [0, 0, 0, 1] and info['action_mask']['type'][ActionType.PICKUP_RUNE]
    env.step(dict(NOOP, type=int(ActionType.PICKUP_RUNE), rune=3))
    assert session.sent[-1][1][0] == {
        'actionType': 'DOTA_UNIT_ORDER_PICKUP_RUNE',
        'player': 0,
        'pickUpRune': {'location': {'x': 595.0, 'y': -4660.0}},  # the bottom bounty spot of map.json
    }


def test_a_talent_tier_keeps_a_point_back_until_the_agent_picks(env):
    env.reset()
    session = last_session()
    session.hero_flags = {'level': 10, 'ability_points': 2, 'is_stunned': True}
    env.step(NOOP)
    *_, info = env.step(NOOP)
    assert info['action_mask']['talent'].tolist() == [1, 1, 0, 0, 0, 0, 0, 0]  # stunned, but points can be spent
    assert info['action_mask']['type'][ActionType.TALENT] and not info['action_mask']['type'][ActionType.MOVE]
    trained = [extra['trainAbility'] for extra in session.sent[-1][2] if 'trainAbility' in extra]
    assert trained and all(extra['keep'] == 1 for extra in trained)
    env.step(dict(NOOP, type=int(ActionType.TALENT), talent=1))
    assert session.sent[-1][1][0] == {
        'actionType': 'DOTA_UNIT_ORDER_TRAIN_ABILITY',
        'player': 0,
        'trainAbility': {'ability': 'special_bonus_unique_nevermore_4', 'keep': 0},
    }
    session.learned_talents.add(1)
    env.step(NOOP)
    observation, _, _, _, info = env.step(NOOP)
    assert observation['talents'].tolist() == [0, 1, 0, 0, 0, 0, 0, 0]
    assert not info['action_mask']['talent'].any()  # taking one closes its tier; tier 15 is five levels away
    assert all(extra['trainAbility']['keep'] == 0 for extra in session.sent[-1][2] if 'trainAbility' in extra)


def test_the_courier_is_sent_for_whatever_waits_in_the_stash(env):
    env.reset()
    session = last_session()
    *_, info = env.step(NOOP)
    assert not info['action_mask']['type'][ActionType.COURIER]  # the stash is empty
    session.stash = [SCROLL]
    observation, _, _, _, info = env.step(NOOP)
    assert (
        info['action_mask']['type'][ActionType.COURIER] and observation['hero'][HERO_FEATURES.index('stash_items')] == 1
    )
    env.step(dict(NOOP, type=int(ActionType.COURIER)))
    assert session.sent[-1][1][0] == {
        'actionType': 'ACTION_COURIER',
        'player': 0,
        'courier': {'action': 'COURIER_ACTION_TAKE_AND_TRANSFER_ITEMS'},
    }
    session.courier_alive = False
    *_, info = env.step(NOOP)
    assert not info['action_mask']['type'][ActionType.COURIER]


def bought(session):
    return [extra['purchaseItem']['itemName'] for extra in session.sent[-1][2] if 'purchaseItem' in extra]


def test_a_scroll_is_bought_only_while_the_hero_has_none_anywhere(env):
    env.reset()
    session = last_session()
    session.hero_flags = {'reliable_gold': 500}
    env.step(NOOP)
    env.step(NOOP)
    assert bought(session) == ['item_tpscroll']
    session.courier_items = [SCROLL]  # on its way from the stash
    env.step(NOOP)
    env.step(NOOP)
    assert bought(session) == []
    session.courier_items, session.stash = [], [SCROLL]
    env.step(NOOP)
    env.step(NOOP)
    assert bought(session) == []
    session.hero_flags = {'reliable_gold': 99}
    session.stash = []
    env.step(NOOP)
    env.step(NOOP)
    assert bought(session) == []  # a scroll costs 100


def test_restock_tp_can_be_turned_off():
    env = gym.make('dota2_env/Mid1v1-v0', session_factory=FakeSession, restock_tp=False)
    env.reset()
    last_session().hero_flags = {'reliable_gold': 500}
    env.step(NOOP)
    env.step(NOOP)
    assert bought(last_session()) == []
    env.close()


def test_text_shows_the_tp_slot_the_stash_and_the_talents(env):
    text_env = TextWrapper(env)
    text_env.reset()
    session = last_session()
    session.tp, session.stash = (1, 0.0), [SCROLL]
    session.hero_flags = {'level': 15, 'ability_points': 1}
    session.learned_talents.add(1)
    text, *_ = text_env.step('{"type": "NOOP"}')
    assert '\n  TP slot: item_tpscroll 回城卷轴 charges 1 ready: TP\n' in text
    assert '\nstash: item_tpscroll 回城卷轴 charges 1; courier dist ' in text
    talents = next(line for line in text.splitlines() if line.startswith('talents: '))
    learned, ready = talents.removeprefix('talents: ').split('; ')
    assert learned == '[1] +30 灵魂盛宴攻击速度 learned'
    assert ready == '[2] +1.5 魔王降临降低护甲 / [3] +2 灵魂盛宴每名英雄收集灵魂 ready: TALENT'
    legal = text.split('legal action types: ')[1].split(', ')
    assert {'TP', 'TALENT', 'COURIER'} <= set(legal) and 'PICKUP_RUNE' not in legal
    _, _, _, _, info = text_env.step('{"type": "TALENT", "talent": 0}')
    assert info['action_error'] == 'talent 0 is not valid for TALENT'  # its tier is taken
    _, _, _, _, info = text_env.step('{"type": "TALENT", "talent": 3}')
    assert info['action_error'] is None
    assert (
        last_session().sent[-1][1][0]['trainAbility']['ability']
        == 'special_bonus_unique_nevermore_frenzy_max_collection_count'
    )


def test_text_names_the_runes_the_team_knows_and_what_comes_next(env):
    text_env = TextWrapper(env)
    text_env.reset()
    session = last_session()
    session.available_runes, session.rune_types = {'power_top', 'bounty_top'}, {'power_top': 1}
    text, *_ = text_env.step('{"type": "NOOP"}')
    runes = next(line for line in text.splitlines() if line.startswith('rune spots: '))
    assert ' available 极速神符; [1] ' in runes  # a haste rune
    assert ' available; [3] ' in runes  # a rune whose kind the world state does not give
    coming = text.split('\nupcoming:\n')[1].split('\nlegal action types')[0].splitlines()
    # the 1v1's own schedule (events.json): nothing at 0:00 or 2:00, then soonest first, every place written out
    assert [row.split()[0] for row in coming] == ['3:00', '4:00', '4:00', '6:00', '7:00']
    lotus = r'  3:00 in 4:2\d 疗伤莲花: 上路莲花池 at \(-7548, 4209\) dist \d+, 下路莲花池 at \(7504, -4405\) dist \d+'
    assert re.fullmatch(lotus, coming[0])
    assert ' 赏金神符: 上路赏金神符 at (-996, 4431) dist ' in coming[1]
    assert re.search(r' 强化神符: 上路强化神符 at \(-1640, 1112\) dist \d+ or 下路强化神符 at ', coming[3])
    session.dota_time = 250.0
    text, *_ = text_env.step('{"type": "NOOP"}')
    assert '圣水神符' not in text.split('\nupcoming:\n')[1]  # the 1v1 has water runes at 4:00 only


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
    assert flat.action_space.nvec[0] == ActionType.MOVE_TO  # no point to carry, so MOVE_TO is left out
    observation, *_ = flat.step(np.array([ActionType.MOVE, 0, 0, 0]))
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_MOVE_TO_POSITION'


def test_move_to_hands_the_point_itself_to_the_client(env):
    env.reset()
    env.step(dict(NOOP, type=int(ActionType.MOVE_TO), point=(4860.0, -6379.0)))  # the bottom tier 1, 11,000 away
    assert last_session().sent[-1][1][0]['moveToLocation']['location'] == {'x': 4860.0, 'y': -6379.0, 'z': 0.0}
    env.step(dict(NOOP, type=int(ActionType.MOVE_TO), point=(99999.0, -99999.0)))
    _, min_y, max_x, _ = load_map().world_bounds  # brought onto the map, at its south-east corner
    assert last_session().sent[-1][1][0]['moveToLocation']['location'] == {'x': max_x, 'y': min_y, 'z': 0.0}


def test_text_wrapper(env):
    text_env = TextWrapper(env)
    text, info = text_env.reset()
    assert 'nevermore_shadowraze1' in text and 'attackable' in text and 'legal action types' in text
    assert (
        '[0] nevermore_shadowraze1 毁灭阴影 lvl 1 ready: CAST' in text
        and '[6] item_tango 树之祭祀 charges 3 ready: CAST' in text
    )
    row = env.unwrapped._observation.unit_handles.index(ENEMY_CREEP_HANDLE)
    _, _, _, _, info = text_env.step(json.dumps({'type': 'attack', 'target': row}))
    assert info['action_error'] is None
    assert last_session().sent[-1][1][0]['actionType'] == 'DOTA_UNIT_ORDER_ATTACK_TARGET'
    _, _, _, _, info = text_env.step('{"type": "CAST", "ability": 3}')
    assert 'not castable' in info['action_error']
    _, _, _, _, info = text_env.step('{"type": "CAST_DIRECTION", "ability": 0, "move": 4}')  # a no-target skill
    assert 'CAST_DIRECTION is not legal' in info['action_error']
    _, _, _, _, info = text_env.step('{"type": "MOVE_TO", "point": [0, 0]}')
    assert info['action_error'] is None
    assert last_session().sent[-1][1][0]['moveToLocation']['location'] == {'x': 0.0, 'y': 0.0, 'z': 0.0}
    _, _, _, _, info = text_env.step('{"type": "MOVE_TO", "point": [NaN, 0]}')
    assert 'not on the map' in info['action_error']
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
    assert f'[0] pangolier_swashbuckle 虚张声势 lvl 1 {ready}' in text
    assert '[6] item_tango 树之祭祀 charges 3 ready: CAST\n' in text  # only the vector skill carries the note


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
    assert list(np.flatnonzero(info['action_mask']['type'])) == [ActionType.NOOP, ActionType.MOVE, ActionType.MOVE_TO]
    observation, *_ = env.step(dict(NOOP, type=int(ActionType.MOVE), move=4))
    location = last_session().sent[-1][1][0]['moveToLocation']['location']
    assert location['x'] == pytest.approx(-6700) and location['y'] == pytest.approx(-6400)  # north, as asked
    assert observation['unit_mask'].sum() > 0  # and is reported again


def test_unreported_hero_is_walked_out_instead_of_waiting(env):
    env.reset()
    last_session().hero_hidden = True
    env.step(NOOP)
    observation, *_ = env.step(NOOP)
    location = last_session().sent[-1][1][0]['moveToLocation']['location']
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


def test_text_names_rune_spots_and_landmarks_in_the_clients_words(env):
    text_env = TextWrapper(env)
    text_env.reset()
    last_session().available_runes.add('power_top')
    text, *_ = text_env.step('{"type": "NOOP"}')
    lines = dict(line.split(': ', 1) for line in text.splitlines() if line.startswith(('rune spots', 'landmarks')))
    runes, landmarks = lines['rune spots'].split('; '), lines['landmarks'].split('; ')
    assert (
        runes[0] == '[0] 上路强化神符 dist 2516 (-140,+2512) available'
    )  # at (-1640, 1112), the hero at (-1500, -1400)
    assert [spot.split()[1] for spot in runes[1:]] == ['下路强化神符', '上路赏金神符', '下路赏金神符']
    assert not any(spot.endswith('available') for spot in runes[1:])
    assert (
        landmarks[-2].startswith('上路前哨 dist ')
        and landmarks[-2].endswith(' ours')
        and landmarks[-1].endswith(' enemy')
    )


def test_the_nearest_tree_is_one_the_team_still_knows_to_stand(env):
    text_env = TextWrapper(env)
    text, _ = text_env.reset()
    assert 'nearest tree dist 516 (+476,-200)' in text  # LONE_TREE, at (-1024, -1600)
    last_session().tree_events.append((LONE_TREE, True))
    text, *_ = text_env.step('{"type": "NOOP"}')
    assert 'nearest tree dist 564 ' in text


def test_terrain_says_where_the_trees_within_one_move_are(env):
    text_env = TextWrapper(env)
    text_env.reset()
    tree_x, tree_y = load_map().tree_positions[LONE_TREE]
    last_session().hero_xy = [float(tree_x) - 150, float(tree_y) + 1]
    text, *_ = text_env.step('{"type": "NOOP"}')
    groups = text.split('within 300: ')[1].split('\n')[0].split('; ')
    trees = next(group for group in groups if group.startswith('tree '))
    assert '(+96,+0)' in trees.split()  # straight ahead, where its cells start 64 short of it


def test_each_lane_is_given_by_its_outermost_standing_tower(env):
    _, info = env.reset()
    world_state = info['world_state']
    text = describe(world_state, TEAM_RADIANT, trees=env.unwrapped.trees)
    assert 'our towers, outermost standing per lane: 中路一塔 dist 45 (-44,-8)\n' in text
    assert 'enemy towers, outermost standing per lane: 中路一塔 dist 2882 (+2024,+2052)\n' in text
    next(unit for unit in world_state.units if unit.name == 'npc_dota_goodguys_tower1_mid').is_alive = False
    tier2 = world_state.units.add(name='npc_dota_goodguys_tower2_mid', unit_type=UNIT_TYPE_TOWER, is_alive=True)
    tier2.team_id, tier2.location.x, tier2.location.y = TEAM_RADIANT, -4000.0, -4000.0
    text = describe(world_state, TEAM_RADIANT, trees=env.unwrapped.trees)
    assert 'our towers, outermost standing per lane: 中路二塔 dist 3607 (-2500,-2600)\n' in text


def test_text_lists_the_enemy_heroes_in_sight_and_the_creep_waves(env):
    _, info = env.reset()
    world_state = info['world_state']
    text = describe(world_state, TEAM_RADIANT, trees=env.unwrapped.trees)
    # the enemy hero stands 900 north-east of ours at (-1500, -1400), one creep of each side next to it
    assert 'enemy heroes in sight: 影魔 lvl 0 hp 500/500 at (-600, -500) dist 1273\n' in text
    waves = text.split('creep waves: ')[1].split('\n')[0].split('; ')
    assert sorted(waves) == [
        '中路 enemy 1 hp 22% at (-1200, -1400) dist 300',
        '中路 ours 1 hp 100% at (-1300, -1300) dist 224',
    ]
    for x, y in ((-1000.0, -1300.0), (-6100.0, 3000.0)):  # one creep joining the enemy wave, one alone on the top lane
        creep = world_state.units.add(unit_type=UNIT_TYPE_LANE_CREEP, team_id=TEAM_DIRE, is_alive=True)
        creep.health, creep.health_max, creep.location.x, creep.location.y = 550, 550, x, y
    world_state.units.add(
        unit_type=UNIT_TYPE_HERO, team_id=TEAM_DIRE, player_id=9, is_alive=True, name='npc_dota_hero_wisp'
    )
    text = describe(world_state, TEAM_RADIANT, trees=env.unwrapped.trees)
    waves = text.split('creep waves: ')[1].split('\n')[0].split('; ')
    assert waves[0] == '上路 enemy 1 hp 100% at (-6100, 3000) dist 6366'  # lanes in order, top first
    assert '中路 enemy 2 hp 61% at (-1100, -1350) dist 403' in waves
    assert '小精灵' not in text  # a 1v1 filler hero is no enemy in sight
