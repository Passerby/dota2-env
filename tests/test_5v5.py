import gymnasium as gym
import numpy as np
import pytest
from fake_session import LONE_TREE, TEAM_CREEP_HANDLE, TEAM_ENEMY_HANDLE, Fake5v5Session, lone_tree_cells
from gymnasium.utils.env_checker import check_env

import dota2_env
from dota2_env.actions import ActionType
from dota2_env.envs.allpick5v5 import DEFAULT_STARTING_ITEMS
from dota2_env.map_features import LANDMARK_FEATURES, LANDMARKS, MAP_FEATURES, RUNE_FEATURES, RUNE_SPOTS
from dota2_env.observation import MAP_SIDE, N_ABILITIES, TEAM_SIZE
from dota2_env.rewards import AllPick5v5Rules
from dota2_env.wrappers import FlatObservationWrapper

NOOP = {key: np.zeros(TEAM_SIZE, np.int64) for key in ('type', 'move', 'target', 'ability')}


@pytest.fixture
def env():
    env = gym.make('dota2_env/AllPick5v5-v0', session_factory=Fake5v5Session)
    yield env
    env.close()


def last_session():
    return Fake5v5Session.instances[-1]


def team_action(row, **fields):
    """A no-op for everyone but row, which gets fields."""
    action = {key: values.copy() for key, values in NOOP.items()}
    for key, value in fields.items():
        action[key][row] = value
    return action


def test_passes_gymnasium_env_checker(env):
    check_env(env.unwrapped, skip_render_check=True)


def test_reset_reports_five_heroes_and_buys_for_each(env):
    observation, info = env.reset(seed=0)
    assert env.observation_space.contains(observation)
    assert info['player_ids'] == [0, 1, 2, 3, 4]
    assert list(observation['heroes'][:, 4]) == [1.0] * TEAM_SIZE  # is_alive
    assert observation['unit_mask'].shape == (TEAM_SIZE, 32)
    assert observation['team'][2] == 1.0 and observation['team'][3] == 1.0  # both sides at full strength

    env.step(NOOP)
    _, sent, extras = last_session().sent[0]
    assert [action['player'] for action in sent] == [0, 1, 2, 3, 4]
    assert {action['actionType'] for action in sent} == {'DOTA_UNIT_ORDER_NONE'}
    purchased = [e['purchaseItem']['itemName'] for e in extras if 'purchaseItem' in e]
    assert purchased.count('item_tango') == TEAM_SIZE
    assert len(purchased) == TEAM_SIZE * len(DEFAULT_STARTING_ITEMS)
    trained = [(e['player'], e['trainAbility']['ability']) for e in extras if 'trainAbility' in e]
    assert {player for player, _ in trained} == {0, 1, 2, 3, 4}


def test_each_hero_acts_on_its_own_unit_rows(env):
    _, info = env.reset()
    base = env.unwrapped
    row = base._observation.heroes[2].unit_handles.index(TEAM_ENEMY_HANDLE + 2)
    assert info['action_mask']['attack_target'].shape == (TEAM_SIZE, 32)
    assert info['action_mask']['attack_target'][2][row]

    env.step(team_action(2, type=int(ActionType.ATTACK), target=row))
    sent = last_session().sent[-1][1]
    assert sent[2]['attackTarget']['target'] == TEAM_ENEMY_HANDLE + 2
    assert [action['actionType'] for action in sent].count('DOTA_UNIT_ORDER_NONE') == TEAM_SIZE - 1

    env.step(team_action(0, type=int(ActionType.MOVE), move=4))  # 4/16 of a turn = north
    assert last_session().hero_xy[0][1] == pytest.approx(-1400.0 + 300.0)
    assert last_session().hero_xy[1][1] == pytest.approx(-1400.0)


def test_every_hero_sees_and_uses_its_own_items(env):
    observation, _ = env.reset()
    assert list(observation['items'][:, 0, 0]) == [44] * TEAM_SIZE  # a tango each
    env.step(team_action(2, type=int(ActionType.CAST), ability=N_ABILITIES))
    sent = last_session().sent[-1][1]
    assert sent[2]['cast'] == {'abilitySlot': -1} and sent[2]['player'] == 2
    assert env.unwrapped.cast_slots()[2][N_ABILITIES] == ('item_tango', ('self',))


def test_every_hero_is_masked_by_what_its_own_slots_can_aim_at(env):
    env.reset()
    last_session().cast_slots[2][0] = ('puck_illusory_orb', ('point',))
    *_, info = env.step(NOOP)
    direction = info['action_mask']['ability'][:, ActionType.CAST_DIRECTION, 0]
    assert list(direction) == [0, 0, 1, 0, 0]
    assert list(info['action_mask']['type'][:, ActionType.CAST_DIRECTION]) == [0, 0, 1, 0, 0]


def test_a_vector_skill_is_cast_through_its_own_heros_slots(env):
    env.reset()
    last_session().cast_slots[2][0] = ('puck_illusory_orb', ('point', 'vector'))
    env.step(NOOP)
    env.step(team_action(2, type=int(ActionType.CAST_DIRECTION), ability=0, move=0))
    sent = last_session().sent[-1][1]
    assert sent[2]['actionType'] == 'DOTA_UNIT_ORDER_CAST_VECTOR' and sent[2]['player'] == 2
    assert sent[2]['castVector']['direction'] == {'x': pytest.approx(1), 'y': pytest.approx(0)}
    assert all(action['actionType'] == 'DOTA_UNIT_ORDER_NONE' for row, action in enumerate(sent) if row != 2)


def test_move_to_sends_each_hero_to_its_own_point(env):
    env.reset()
    action = dict(team_action(1, type=int(ActionType.MOVE_TO)), point=np.zeros((TEAM_SIZE, 2), np.float32))
    action['point'][1] = (4860.0, -6379.0)
    env.step(action)
    sent = last_session().sent[-1][1]
    assert sent[1]['moveToLocation']['location'] == {'x': 4860.0, 'y': -6379.0, 'z': 0.0}
    assert all(order['actionType'] == 'DOTA_UNIT_ORDER_NONE' for row, order in enumerate(sent) if row != 1)


def test_a_rune_pickup_goes_out_for_its_own_hero_only(env):
    env.reset()
    last_session().available_runes.add('power_bottom')
    *_, info = env.step(NOOP)
    assert info['action_mask']['rune'].shape == (TEAM_SIZE, len(RUNE_SPOTS))
    assert info['action_mask']['rune'][:, RUNE_SPOTS.index('power_bottom')].all()
    rune = np.zeros(TEAM_SIZE, np.int64)
    rune[2] = RUNE_SPOTS.index('power_bottom')
    env.step(dict(team_action(2, type=int(ActionType.PICKUP_RUNE)), rune=rune))
    sent = last_session().sent[-1][1]
    assert sent[2] == {
        'actionType': 'DOTA_UNIT_ORDER_PICKUP_RUNE',
        'player': 2,
        'pickUpRune': {'location': {'x': 1180.0, 'y': -1216.0}},
    }
    assert all(order['actionType'] == 'DOTA_UNIT_ORDER_NONE' for row, order in enumerate(sent) if row != 2)


def test_hidden_hero_is_walked_out_while_the_others_wait(env):
    env.reset()
    last_session().hidden_heroes.add(3)
    env.step(NOOP)
    observation, *_ = env.step(NOOP)
    sent = last_session().sent[-1][1]
    assert [action['actionType'] for action in sent].count('DOTA_UNIT_ORDER_NONE') == TEAM_SIZE - 1
    location = sent[3]['moveToLocation']['location']
    assert location['x'] > -6700 and location['y'] > -6700  # towards the map centre
    assert observation['unit_mask'][3].sum() > 0


def test_last_hit_and_enemy_damage_are_rewarded(env):
    env.reset()
    base = env.unwrapped
    for _ in range(3):
        row = base._observation.heroes[0].unit_handles.index(TEAM_CREEP_HANDLE)
        *_, info = env.step(team_action(0, type=int(ActionType.ATTACK), target=row))
    assert info['reward']['last_hit'] == 1
    assert info['reward']['kill'] == 0 and info['reward']['tower'] == 0


def test_kills_and_deaths_are_summed_over_the_team(env):
    env.reset()
    last_session().kills, last_session().deaths = 2, 1
    *_, info = env.step(NOOP)
    assert info['reward']['kill'] == 2 and info['reward']['death'] == 1


def test_ancient_falling_terminates_with_a_winner(env):
    env.reset()
    *_, terminated, truncated, info = env.step(NOOP)
    assert not terminated and not truncated and info['winner'] is None
    last_session().ancient_health[dota2_env.TEAM_DIRE] = 0
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert terminated and info['winner'] == dota2_env.TEAM_RADIANT
    assert info['reward']['ancient'] == pytest.approx(1.0) and info['reward']['win'] == 1.0
    assert reward > 0


def test_time_truncates():
    env = gym.make(
        'dota2_env/AllPick5v5-v0', session_factory=Fake5v5Session, rules=AllPick5v5Rules(max_dota_time=-74.0)
    )
    env.reset()
    for _ in range(5):
        *_, truncated, _ = env.step(NOOP)
    assert truncated
    env.close()


def test_sampled_actions_are_legal_and_render_covers_every_hero(env):
    env.reset()
    base = env.unwrapped
    action = base.sample_legal_action()
    assert env.action_space.contains(action)
    mask = base.action_masks()
    assert all(mask['type'][row][action['type'][row]] for row in range(TEAM_SIZE))

    text = gym.make('dota2_env/AllPick5v5-v0', session_factory=Fake5v5Session, render_mode='ansi')
    text.reset()
    rendered = text.render()
    assert all(f'=== hero {row} (player {row}) ===' in rendered for row in range(TEAM_SIZE))
    assert 'nevermore_shadowraze1' in rendered
    text.close()


def test_flat_observation_wrapper(env):
    flat = FlatObservationWrapper(env)
    observation, _ = flat.reset()
    assert flat.observation_space.contains(observation)


def test_every_hero_has_its_own_map(env):
    observation, _ = env.reset()
    assert observation['local_map'].shape == (TEAM_SIZE, len(MAP_FEATURES), MAP_SIDE, MAP_SIDE)
    assert observation['runes'].shape == (TEAM_SIZE, len(RUNE_SPOTS), len(RUNE_FEATURES))
    assert observation['landmarks'].shape == (TEAM_SIZE, len(LANDMARKS), len(LANDMARK_FEATURES))
    tree = MAP_FEATURES.index('tree')
    rows, cols = lone_tree_cells(*last_session().hero_xy[0])
    assert observation['local_map'][0, tree][rows, cols].all()
    last_session().tree_events.append((LONE_TREE, True))
    observation, *_ = env.step(NOOP)
    assert not observation['local_map'][0, tree][rows, cols].any()
    assert not np.array_equal(observation['landmarks'][0], observation['landmarks'][4])  # measured from each hero
