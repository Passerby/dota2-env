"""LLM match harness: config parsing, reply handling and the async decision loop, with no network."""

import json
import threading
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from fake_session import Fake5v5Session

import dota2_env  # noqa: F401
from dota2_env import actions
from dota2_env.actions import MAX_UNITS, N_CAST_SLOTS, ActionType
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.llm import agent
from dota2_env.llm.config import AgentConfig, GatewayConfig, MatchConfig, TeamConfig, load_match
from dota2_env.llm.gateway import Reply
from dota2_env.llm.runner import TeamRunner
from dota2_env.observation import TEAM_SIZE

ACTION_KEYS = ('type', 'move', 'target', 'ability')
HEROES = [
    'npc_dota_hero_nevermore',
    'npc_dota_hero_sniper',
    'npc_dota_hero_lina',
    'npc_dota_hero_lion',
    'npc_dota_hero_crystal_maiden',
]
ENEMIES = [
    'npc_dota_hero_axe',
    'npc_dota_hero_juggernaut',
    'npc_dota_hero_zuus',
    'npc_dota_hero_witch_doctor',
    'npc_dota_hero_skywrath_mage',
]
MOVE_NORTH = '{"type": "MOVE", "move": 4}'


class FakeGateway:
    """Stands in for llm.gateway.Gateway with the same complete() surface and no HTTP.

    Injected through TeamRunner's gateways argument, the way session_factory injects a fake session.
    """

    def __init__(self, texts=(MOVE_NORTH,), gate=None, error=None, usd=0.001):
        self.config = GatewayConfig(name='fake', base_url='', api_key='k', model='m', timeout_seconds=8.0)
        self.texts = list(texts)
        self.gate = gate  # a threading.Event the test releases, to model a slow model
        self.error = error
        self.usd = usd
        self.calls = []

    def complete(self, messages, params):
        self.calls.append((messages, params))
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            return Reply(text='', input_tokens=0, output_tokens=0, usd=0.0, latency=0.01, error=self.error)
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return Reply(text=text, input_tokens=100, output_tokens=10, usd=self.usd, latency=0.01, error=None)


def hero_mask(attackable=(), castable=()):
    mask = {
        'type': np.ones(len(ActionType), np.int8),
        'ability': np.ones((len(ActionType), N_CAST_SLOTS), np.int8),
        'attack_target': np.zeros(MAX_UNITS, np.int8),
        'cast_target': np.zeros(MAX_UNITS, np.int8),
    }
    for row in attackable:
        mask['attack_target'][row] = 1
    for row in castable:
        mask['cast_target'][row] = 1
    return mask


def decide_one(text, mask, prompt_handles=(), unit_handles=(), all_chat=False, plan_length=6):
    """read_plan + resolve of the first action, which is what the runner does across two frames."""
    plan, _, say, error = agent.read_plan(text, all_chat, plan_length)
    if error:
        return dict(actions.NOOP_ACTION), say, error
    action, error = agent.resolve(plan[0], mask, list(prompt_handles), list(unit_handles))
    return action, say, error


def match_config(interval=1.0, all_chat=True, share_team_state=True, spend_limit_usd=100.0, **fields):
    agents = [
        AgentConfig(nickname=f'A{row}', hero=HEROES[row], position='mid', gateway='fake', decision_interval=interval)
        for row in range(TEAM_SIZE)
    ]
    return MatchConfig(
        mode='allpick5v5',
        all_chat=all_chat,
        share_team_state=share_team_state,
        spend_limit_usd=spend_limit_usd,
        **fields,
        gateways={'fake': GatewayConfig(name='fake', base_url='', api_key='k', model='m')},
        radiant=TeamConfig(team_id=TEAM_RADIANT, control='agent', agents=agents, heroes=list(HEROES)),
        dire=TeamConfig(team_id=TEAM_DIRE, control='builtin', agents=[], heroes=list(ENEMIES)),
    )


@pytest.fixture
def env():
    env = gym.make('dota2_env/AllPick5v5-v0', session_factory=Fake5v5Session)
    yield env
    env.close()


def last_session():
    return Fake5v5Session.instances[-1]


def settle(runner):
    """Wait until no worker is in flight, so a test never races the reply it is about to assert on."""
    deadline = time.time() + 5
    while any(slot.busy.is_set() for slot in runner.slots):
        assert time.time() < deadline, 'a fake gateway never answered'
        time.sleep(0.001)


def drive(env, runner, steps, wait=True):
    """Run the real decide/step loop and return the actions handed to the env on each step."""
    _, info = env.reset(seed=0)
    applied = []
    for _ in range(steps):
        if wait:
            settle(runner)
        masks = [{key: values[row] for key, values in info['action_mask'].items()} for row in range(TEAM_SIZE)]
        hero_actions, chat = runner.decide(info['world_state'], masks, info['player_ids'], env.unwrapped.cast_slots())
        for row, message in chat:
            env.unwrapped.queue_chat(row, message)
        applied.append(hero_actions)
        action = {key: np.array([one[key] for one in hero_actions], np.int64) for key in ACTION_KEYS}
        _, _, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            break
    return applied


# -- reply handling ---------------------------------------------------------------------------------


def test_a_plain_reply_becomes_an_action():
    action, say, error = decide_one(MOVE_NORTH, hero_mask())
    assert error is None and say is None
    assert action == {'type': int(ActionType.MOVE), 'move': 4, 'target': 0, 'ability': 0}


@pytest.mark.parametrize(
    'text',
    ['```json\n{"type": "MOVE", "move": 4}\n```', 'I should back off. {"type": "MOVE", "move": 4}', MOVE_NORTH],
)
def test_json_is_found_inside_whatever_the_model_wrapped_it_in(text):
    action, _, error = decide_one(text, hero_mask())
    assert error is None and action['type'] == int(ActionType.MOVE) and action['move'] == 4


def test_a_reply_with_no_json_becomes_noop():
    action, _, error = decide_one('I think I will farm for a while.', hero_mask())
    assert action['type'] == int(ActionType.NOOP) and 'no JSON object' in error


def test_an_illegal_action_becomes_noop_and_says_why():
    action, _, error = decide_one('{"type": "ATTACK", "target": 1}', hero_mask(attackable=[0]), [7, 8], [7, 8])
    assert action['type'] == int(ActionType.NOOP) and error == 'target 1 is not valid for ATTACK'


def test_target_follows_its_unit_when_the_table_is_resorted():
    # the model picked row 2 (handle 12); by the time the order goes out that unit sits in row 0
    action, _, error = decide_one(
        '{"type": "ATTACK", "target": 2}', hero_mask(attackable=[0, 1, 2]), [10, 11, 12], [12, 10, 11]
    )
    assert error is None and action['target'] == 0


def test_a_target_that_left_the_unit_table_becomes_noop():
    action, _, error = decide_one(
        '{"type": "ATTACK", "target": 2}', hero_mask(attackable=[0, 1, 2]), [10, 11, 12], [10, 11]
    )
    assert action['type'] == int(ActionType.NOOP) and 'gone from the unit table' in error


def test_say_is_read_only_when_chat_is_on():
    text = '{"type": "MOVE", "move": 4, "say": "  ez mid  "}'
    _, say, error = decide_one(text, hero_mask(), all_chat=True)
    assert say == 'ez mid' and error is None
    _, say, error = decide_one(text, hero_mask(), all_chat=False)
    assert say is None and error is None


def test_a_taunt_is_truncated():
    text = json.dumps({'type': 'NOOP', 'say': 'x' * 200})
    _, say, _ = decide_one(text, hero_mask(), all_chat=True)
    assert len(say) == agent.CHAT_LIMIT


@pytest.mark.parametrize('plan_length', [1, 6])
def test_the_system_prompt_asks_for_plain_json(plan_length):
    match = match_config(plan_length=plan_length)
    prompt = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '{"reason": ' in prompt and '{{' not in prompt


# -- the decision loop -----------------------------------------------------------------------------


def test_every_hero_holds_until_its_first_reply_lands(env):
    gateway = FakeGateway()
    runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway})
    applied = drive(env, runner, 2)
    runner.close()
    assert all(one['type'] == int(ActionType.NOOP) for one in applied[0])  # nothing back yet
    assert all(one['type'] == int(ActionType.MOVE) for one in applied[1])
    assert len(gateway.calls) >= TEAM_SIZE


def test_a_slow_model_never_stalls_the_others(env):
    gate = threading.Event()
    slow, quick = FakeGateway(gate=gate), FakeGateway()
    match = match_config()
    match.gateways = {'slow': slow.config, 'quick': quick.config}
    match.radiant.agents[0].gateway = 'slow'
    for config in match.radiant.agents[1:]:
        config.gateway = 'quick'
    runner = TeamRunner(match, match.radiant, {'slow': slow, 'quick': quick})
    try:
        applied = drive(env, runner, 3, wait=False)
        for _ in range(40):  # the four quick agents only need their replies to come back
            if any(one['type'] == int(ActionType.MOVE) for one in applied[-1][1:]):
                break
            applied = drive(env, runner, 1, wait=False)
            time.sleep(0.01)
        assert applied[-1][0]['type'] == int(ActionType.NOOP)  # the gated hero is still holding
        assert runner.slots[0].holds > 0
    finally:
        gate.set()
        runner.close()


def test_cadence_is_counted_in_game_seconds(env):
    fast, slow = FakeGateway(), FakeGateway()
    quick_match, slow_match = match_config(interval=0.0), match_config(interval=3.0)
    quick = TeamRunner(quick_match, quick_match.radiant, {'fake': fast})
    drive(env, quick, 12)
    quick.close()
    lazy = TeamRunner(slow_match, slow_match.radiant, {'fake': slow})
    drive(env, lazy, 12)
    lazy.close()
    # the fake advances dota_time 0.2s per observation, so 12 steps is about 2.4 game seconds
    assert quick.slots[0].requests >= 10
    assert lazy.slots[0].requests <= 2


def test_a_gateway_failure_leaves_the_hero_holding_its_order(env):
    gateway = FakeGateway(error="ConnectError('nope')")
    runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway})
    applied = drive(env, runner, 3)
    runner.close()
    assert all(one['type'] == int(ActionType.NOOP) for step in applied for one in step)
    assert runner.slots[0].errors > 0 and runner.slots[0].usd == 0.0


def test_tokens_and_cost_are_accumulated_per_agent(env):
    gateway = FakeGateway(usd=0.002)
    runner = TeamRunner(match_config(interval=0.0), match_config(interval=0.0).radiant, {'fake': gateway})
    drive(env, runner, 4)
    runner.close()
    slot = runner.slots[0]
    assert slot.replies > 0 and slot.input_tokens == 100 * slot.replies
    assert runner.usd == pytest.approx(0.002 * sum(one.replies for one in runner.slots))


def test_a_taunt_reaches_the_bridge_as_a_chat_extra_action(env):
    gateway = FakeGateway(texts=['{"type": "MOVE", "move": 4, "say": "ez mid"}'])
    runner = TeamRunner(match_config(all_chat=True), match_config(all_chat=True).radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    chats = [extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_CHAT']
    assert chats, 'no chat action was ever sent'
    assert chats[0]['chat'] == {'message': 'ez mid', 'toAllchat': True}
    assert chats[0]['player'] in range(TEAM_SIZE)


def test_no_chat_is_sent_when_all_chat_is_off(env):
    gateway = FakeGateway(texts=['{"type": "MOVE", "move": 4, "say": "ez mid"}'])
    match = match_config(all_chat=False)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    assert not [
        extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_CHAT'
    ]


def test_the_prompt_carries_the_ally_lines_and_real_names(env):
    gateway = FakeGateway()
    runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway})
    drive(env, runner, 2)
    runner.close()
    # all five submit on the same frame, so which request arrived first is up to the scheduler
    prompts = [messages[1]['content'] for messages, _ in gateway.calls[:TEAM_SIZE]]
    nicknames = {f'A{row}' for row in range(TEAM_SIZE)}
    for prompt in prompts:
        assert 'nevermore_shadowraze1' in prompt  # what the running client reported, not ability_5059
        assert '[6] item_tango charges 3 ready: CAST' in prompt  # likewise, not item_44
        allies = prompt.split('你的队友：')[1]
        assert len({name for name in nicknames if name in allies}) == TEAM_SIZE - 1  # everyone but yourself


def test_the_transcript_records_every_decision(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    gateway = FakeGateway()
    with open(path, 'w') as transcript:
        runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records and all(record['kind'] == 'decision' for record in records)
    assert {record['nickname'] for record in records} == {f'A{row}' for row in range(TEAM_SIZE)}
    first = records[0]
    assert first['input_tokens'] == 100 and first['usd'] > 0 and first['reply'] == MOVE_NORTH
    assert first['plan'] == [{'type': 'MOVE', 'move': 4}] and first['error'] is None
    assert 'prompt' not in first  # log_prompts is off by default


def test_five_agents_drive_every_hero_through_the_fake_session(env):
    gateway = FakeGateway()
    runner = TeamRunner(match_config(interval=0.0), match_config(interval=0.0).radiant, {'fake': gateway})
    drive(env, runner, 5)
    runner.close()
    for _, sent, _ in last_session().sent:
        assert sorted(action['player'] for action in sent) == list(range(TEAM_SIZE))


# -- the config file -------------------------------------------------------------------------------


CONFIG = """
gateways:
  cheap:
    base_url: http://localhost:11434/v1
    api_key: ${TEST_LLM_KEY}
    model: tiny
    price_per_mtok: {input: 1.0, output: 2.0}
match:
  mode: allpick5v5
  max_dota_time: 300.0
radiant:
  control: agent
  agents:
AGENTS
dire:
  control: builtin
  heroes: [ENEMIES]
"""


def write_config(tmp_path, heroes=None, positions=None, enemies=None):
    heroes = heroes or HEROES
    positions = positions or ['mid'] * len(heroes)
    agents = '\n'.join(
        f'    - {{nickname: A{row}, hero: {hero}, position: {positions[row]}, gateway: cheap}}'
        for row, hero in enumerate(heroes)
    )
    path = tmp_path / 'match.yaml'
    body = CONFIG.replace('AGENTS', agents).replace('ENEMIES', ', '.join(enemies or ENEMIES))
    path.write_text(body)
    return str(path)


def test_a_key_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    match = load_match(write_config(tmp_path))
    assert match.gateways['cheap'].api_key == 'sk-secret'
    assert match.gateways['cheap'].input_usd_per_mtok == 1.0
    assert match.max_dota_time == 300.0 and match.mode == 'allpick5v5'
    assert [config.hero for config in match.radiant.agents] == HEROES
    assert match.radiant.heroes == HEROES and match.dire.heroes == ENEMIES


def test_an_unset_environment_variable_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv('TEST_LLM_KEY', raising=False)
    with pytest.raises(ValueError, match='TEST_LLM_KEY'):
        load_match(write_config(tmp_path))


def test_a_hero_cannot_be_picked_twice(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    with pytest.raises(ValueError, match='npc_dota_hero_axe'):
        load_match(write_config(tmp_path, enemies=['npc_dota_hero_axe'] * 5))


def test_a_short_lineup_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    with pytest.raises(ValueError, match='needs 5 heroes'):
        load_match(write_config(tmp_path, heroes=HEROES[:3]))


def test_an_unknown_position_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    positions = ['mid', 'safe', 'jungle', 'support', 'support']
    with pytest.raises(ValueError, match='position must be one of'):
        load_match(write_config(tmp_path, positions=positions))


# -- plans ------------------------------------------------------------------------------------------


PLAN_SIX = json.dumps(
    {
        'reason': 'wave is pushing, walk up and hit the ranged creep',
        'actions': [{'type': 'MOVE', 'move': 4}] * 5 + [{'type': 'STOP'}],
    }
)


def test_a_plan_is_read_whole_and_kept_in_order():
    plan, reason, say, error = agent.read_plan(PLAN_SIX, all_chat=False, plan_length=6)
    assert error is None and say is None
    assert reason.startswith('wave is pushing')
    assert len(plan) == 6 and plan[-1] == {'type': 'STOP'}


def test_a_plan_longer_than_asked_for_is_truncated():
    plan, _, _, error = agent.read_plan(PLAN_SIX, all_chat=False, plan_length=2)
    assert error is None and len(plan) == 2


def test_a_plan_is_carried_out_one_action_per_frame(env):
    gateway = FakeGateway(texts=[PLAN_SIX])
    match = match_config(interval=3.0, plan_length=6)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    applied = drive(env, runner, 8)
    runner.close()
    # frame 0 has no reply yet; then the six planned actions land in order, then the hero holds
    first = [step[0]['type'] for step in applied]
    assert first[0] == int(ActionType.NOOP)
    assert first[1:6] == [int(ActionType.MOVE)] * 5
    assert first[6] == int(ActionType.STOP)
    assert first[7] == int(ActionType.NOOP)  # plan exhausted, hero keeps its last order
    assert runner.slots[0].steps == 6


def test_a_non_numeric_target_is_rejected_instead_of_raising():
    action, _, error = decide_one(
        '{"type": "ATTACK", "target": "the ranged creep"}', hero_mask(attackable=[0]), [7], [7]
    )
    assert action['type'] == int(ActionType.NOOP) and 'not a row number' in error


def test_the_reason_and_the_prompt_reach_the_transcript(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    gateway = FakeGateway(texts=[PLAN_SIX])
    match = match_config(plan_length=6, log_prompts=True)
    with open(path, 'w') as transcript:
        runner = TeamRunner(match, match.radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    decisions = [r for r in records if r['kind'] == 'decision']
    assert decisions and all(r['reason'].startswith('wave is pushing') for r in decisions)
    assert all(len(r['plan']) == 6 for r in decisions)
    prompt = decisions[0]['prompt']
    assert 'units within 1600' in prompt and '你的队友：' in prompt


def test_a_rejected_planned_action_is_recorded_when_it_is_applied(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    # row 31 is never a legal attack target in the fake world state
    gateway = FakeGateway(texts=[json.dumps({'reason': 'hit it', 'actions': [{'type': 'ATTACK', 'target': 31}]})])
    match = match_config(plan_length=6)
    with open(path, 'w') as transcript:
        runner = TeamRunner(match, match.radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    rejected = [json.loads(line) for line in path.read_text().splitlines() if '"rejected"' in line]
    assert rejected and rejected[0]['step'] == {'type': 'ATTACK', 'target': 31}
    assert runner.slots[0].parse_errors > 0


def test_plan_length_must_be_at_least_one(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    config = Path(write_config(tmp_path))
    config.write_text(config.read_text().replace('max_dota_time: 300.0', 'max_dota_time: 300.0\n  plan_length: 0'))
    with pytest.raises(ValueError, match='plan_length'):
        load_match(str(config))
