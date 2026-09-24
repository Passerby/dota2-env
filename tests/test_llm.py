"""LLM match harness: config parsing, reply handling and the async decision loop, with no network."""

import dataclasses
import json
import threading
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from fake_session import Fake5v5Session

import dota2_env  # noqa: F401
from dota2_env.actions import MAX_UNITS, N_CAST_SLOTS, NOOP_ACTION, ActionType, team_action_space
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import item_text
from dota2_env.llm import agent
from dota2_env.llm.config import MODES, POSITIONS, AgentConfig, GatewayConfig, MatchConfig, TeamConfig, load_match
from dota2_env.llm.gateway import Reply
from dota2_env.llm.runner import TeamRunner
from dota2_env.map_features import RUNE_SPOTS
from dota2_env.observation import N_TALENTS, TEAM_SIZE

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
MOVE_NORTH = 'MOVE, 4'


class FakeGateway:
    """Stands in for llm.gateway.Gateway with the same stream() surface and no HTTP.

    Injected through TeamRunner's gateways argument, the way session_factory injects a fake session.
    """

    def __init__(self, texts=(MOVE_NORTH,), gate=None, gate_after=0, error=None, usd=0.001):
        self.config = GatewayConfig(name='fake', base_url='', api_key='k', model='m', timeout_seconds=8.0)
        self.texts = list(texts)
        self.gate = gate  # a threading.Event the test releases, to model a slow model
        self.gate_after = gate_after  # lines streamed before the gate holds the rest back
        self.error = error  # the reply fails once its lines are out
        self.usd = usd
        self.calls = []

    def stream(self, messages, params):
        self.calls.append((messages, params))
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        lines = [line for line in text.split('\n') if line.strip()]
        yield from lines[: self.gate_after]
        if self.gate is not None:
            self.gate.wait(timeout=5)
        yield from lines[self.gate_after :]
        first_line_latency = 0.005 if lines else None
        if self.error is not None:
            yield Reply(
                text=text,
                input_tokens=0,
                output_tokens=0,
                usd=0.0,
                latency=0.01,
                first_line_latency=first_line_latency,
                error=self.error,
            )
            return
        yield Reply(
            text=text,
            input_tokens=100,
            output_tokens=10,
            usd=self.usd,
            latency=0.01,
            first_line_latency=first_line_latency,
            error=None,
        )


def hero_mask(attackable=(), castable=(), runes=(), talents=()):
    mask = {
        'type': np.ones(len(ActionType), np.int8),
        'ability': np.ones((len(ActionType), N_CAST_SLOTS), np.int8),
        'attack_target': np.zeros(MAX_UNITS, np.int8),
        'cast_target': np.zeros(MAX_UNITS, np.int8),
        'rune': np.zeros(len(RUNE_SPOTS), np.int8),
        'talent': np.zeros(N_TALENTS, np.int8),
    }
    mask['rune'][list(runes)] = 1
    mask['talent'][list(talents)] = 1
    for row in attackable:
        mask['attack_target'][row] = 1
    for row in castable:
        mask['cast_target'][row] = 1
    return mask


def decide_one(text, mask, prompt_handles=(), unit_handles=(), position=None):
    """The first step of a reply resolved on the frame it goes out on, which is what the runner does with it."""
    steps = [line for kind, line in map(agent.read_line, text.split('\n')) if kind == 'step']
    return agent.resolve(steps[0], mask, list(prompt_handles), list(unit_handles), position)


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
        hero_actions, chat, reasons = runner.decide(
            info['world_state'], masks, info['player_ids'], env.unwrapped.cast_slots(), env.unwrapped.trees
        )
        for row, message in chat:
            env.unwrapped.queue_chat(row, message)
        for row, reason in reasons:
            env.unwrapped.queue_label(row, reason)
        applied.append(hero_actions)
        action = {
            key: np.array([one[key] for one in hero_actions], space.dtype)
            for key, space in team_action_space.spaces.items()
        }
        _, _, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            break
    return applied


def read_transcript(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# -- reply handling ---------------------------------------------------------------------------------


def test_a_plain_reply_becomes_an_action():
    action, error = decide_one(MOVE_NORTH, hero_mask())
    assert error is None
    assert action == dict(NOOP_ACTION, type=int(ActionType.MOVE), move=4)


@pytest.mark.parametrize('text', ['```\nMOVE, 4\n```', '\n  move ,4  \nREASON, 往北走', 'MOVE, 4\nREASON, 往北走'])
def test_steps_are_found_whatever_the_model_wrapped_them_in(text):
    action, error = decide_one(text, hero_mask())
    assert error is None and action['type'] == int(ActionType.MOVE) and action['move'] == 4


def test_a_line_is_a_step_the_reason_or_a_taunt():
    assert agent.read_line('  ATTACK, 3 ') == ('step', 'ATTACK, 3')
    assert agent.read_line('REASON, 兵线在推, 往前走') == ('reason', '兵线在推, 往前走')
    assert agent.read_line('REASON：兵线在推') == agent.read_line('reason: 兵线在推') == ('reason', '兵线在推')
    assert agent.read_line('say,  ez mid ') == ('say', 'ez mid')
    assert agent.read_line('SAY, ' + 'x' * 200) == ('say', 'x' * agent.CHAT_LIMIT)
    assert agent.read_line('   ') == (None, '') and agent.read_line('```text') == (None, '')


def test_an_illegal_action_becomes_noop_and_says_why():
    action, error = decide_one('ATTACK, 1', hero_mask(attackable=[0]), [7, 8], [7, 8])
    assert action['type'] == int(ActionType.NOOP) and error == 'target 1 is not valid for ATTACK'


def test_target_follows_its_unit_when_the_table_is_resorted():
    # the model picked row 2 (handle 12); by the time the order goes out that unit sits in row 0
    action, error = decide_one('ATTACK, 2', hero_mask(attackable=[0, 1, 2]), [10, 11, 12], [12, 10, 11])
    assert error is None and action['target'] == 0


@pytest.mark.parametrize('step', ['MOVE, 1180, -1216', 'MOVE, (1180, -1216)'])
def test_a_move_to_a_point_hands_the_point_to_the_game(step):
    action, error = decide_one(step, hero_mask())  # the bottom power rune, however far the hero stands from it
    assert error is None and action['type'] == int(ActionType.MOVE_TO) and action['point'] == (1180.0, -1216.0)


def test_a_cast_towards_a_point_is_aimed_from_where_the_hero_stands_now():
    # the bottom power rune: 2,680 east and 184 north of the hero, then straight north of it
    action, error = decide_one('CAST_DIRECTION, 2, 1180, -1216', hero_mask(), position=(-1500.0, -1400.0))
    assert error is None and action['type'] == int(ActionType.CAST_DIRECTION) and action['move'] == 0
    action, error = decide_one('CAST_DIRECTION, 2, 1180, -1216', hero_mask(), position=(1180.0, -6000.0))
    assert error is None and action['move'] == 4


def test_one_number_is_a_direction():
    action, error = decide_one('CAST_DIRECTION, 2, 12', hero_mask())
    assert error is None
    assert action == dict(NOOP_ACTION, type=int(ActionType.CAST_DIRECTION), move=12, ability=2)


@pytest.mark.parametrize(
    ('step', 'error'),
    [
        ('MOVE, 1, 2, 3', 'MOVE is written MOVE, x, y or MOVE, D'),
        ('ATTACK', 'ATTACK is written ATTACK, N'),
        ('ATTACK, the ranged creep', "'the ranged creep' is not a number"),
        ('MOVE, nan, 0', "'nan' is not a number"),
        ('ATTACK, 1111111111', "'1111111111' is not a number"),
        ('MOVE 4', "'MOVE 4' is not an action type"),
    ],
)
def test_a_step_that_does_not_read_is_rejected_with_the_reason(step, error):
    action, message = decide_one(step, hero_mask(attackable=[0]), [7], [7], position=(0.0, 0.0))
    assert action['type'] == int(ActionType.NOOP) and message == error


@pytest.mark.parametrize(
    ('step', 'fields'),
    [
        ('PICKUP_RUNE, 3', {'type': int(ActionType.PICKUP_RUNE), 'rune': 3}),
        ('TP, -1544, -1408', {'type': int(ActionType.TP), 'point': (-1544.0, -1408.0)}),  # our mid tier 1
        ('TALENT, 1', {'type': int(ActionType.TALENT), 'talent': 1}),
        ('COURIER', {'type': int(ActionType.COURIER)}),
    ],
)
def test_a_rune_a_teleport_a_talent_and_the_courier_are_one_line_each(step, fields):
    # a teleport takes its point as it is, wherever the hero stands
    action, error = decide_one(step, hero_mask(runes=[3], talents=[0, 1]), position=(-6700.0, -6700.0))
    assert error is None and action == dict(NOOP_ACTION, **fields)


def test_a_rune_or_talent_the_mask_rules_out_is_rejected():
    action, error = decide_one('PICKUP_RUNE, 2', hero_mask(runes=[3]))
    assert action['type'] == int(ActionType.NOOP) and error == 'rune 2 is not valid for PICKUP_RUNE'
    action, error = decide_one('TALENT, 2', hero_mask(talents=[0, 1]))
    assert action['type'] == int(ActionType.NOOP) and error == 'talent 2 is not valid for TALENT'


def test_aiming_at_a_point_needs_the_hero_on_the_map():
    action, error = decide_one('CAST_DIRECTION, 2, 0, 0', hero_mask())
    assert action['type'] == int(ActionType.NOOP) and 'not on the map' in error


def test_a_target_that_left_the_unit_table_becomes_noop():
    action, error = decide_one('ATTACK, 2', hero_mask(attackable=[0, 1, 2]), [10, 11, 12], [10, 11])
    assert action['type'] == int(ActionType.NOOP) and 'gone from the unit table' in error


@pytest.mark.parametrize('plan_length', [1, 6])
def test_the_system_prompt_asks_for_one_action_per_line(plan_length):
    match = match_config(plan_length=plan_length)
    prompt = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '\n  REASON, <一句简短的话>\n' in prompt and '"type"' not in prompt and '{{' not in prompt
    # every way of writing an action that resolve() reads is one the prompt teaches
    assert set(agent.FORMS) == {kind.name for kind in ActionType} - {'MOVE_TO'}  # written as MOVE with a point
    for kind, forms in agent.FORMS.items():
        for names in forms:
            assert '\n  ' + ', '.join((kind, *names)) + '  ' in prompt


def test_the_system_prompt_describes_the_heros_own_skills():
    match = match_config()
    prompt = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '用影魔（Shadow Fiend）打' in prompt
    assert '- nevermore_shadowraze1 毁灭阴影（Shadowraze）：冷却时间 9 秒，魔法消耗 75' in prompt
    assert '- nevermore_shadowraze3 毁灭阴影（Shadowraze）：同 nevermore_shadowraze1，距离：700' in prompt
    assert '- nevermore_necromastery 支配死灵（Necromastery）：先天技能，被动' in prompt
    lina = agent.system_prompt(match.radiant.agents[2], match, TEAM_RADIANT)
    assert '- lina_flame_cloak 腾焰斗篷（Flame Cloak）：需要阿哈利姆神杖' in lina and 'nevermore' not in lina


def test_the_system_prompt_lists_the_heros_talents_tier_by_tier():
    match = match_config()
    prompt = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '10 级：[0] +30 毁灭阴影连中伤害，或 [1] +30 灵魂盛宴攻击速度\n' in prompt
    assert '25 级：[6] 灵魂盛宴+30% 施法速度，或 [7] 毁灭阴影施加攻击伤害\n' in prompt
    assert (
        '回城卷轴在 "TP slot" 那一格。TP, x, y 持续施法 3 秒' in prompt
    )  # the scroll's name and numbers come from the data


def test_the_system_prompt_says_where_the_teams_safe_lane_is():
    match = match_config()
    assert '你们的优势路是下路，劣势路是上路' in agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '你们的优势路是上路，劣势路是下路' in agent.system_prompt(match.radiant.agents[0], match, TEAM_DIRE)


def test_the_system_prompt_places_both_bases_in_the_coordinates_of_pos():
    match = match_config()
    prompt = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '天辉: 遗迹 (-5920, -5352); 上路一塔 (-6336, 1856);' in prompt and '夜魇: 遗迹 (5528, 5000);' in prompt
    assert prompt.count('塔 (') == 22  # three per lane and two in the base, on each side
    assert '下路强化神符 (1180, -1216)' in prompt and '上路前哨 (-4096, -448)' in prompt


def test_every_position_and_mode_has_its_own_lines():
    match = match_config()
    config = match.radiant.agents[0]
    roles = {
        agent.system_prompt(dataclasses.replace(config, position=position), match, TEAM_RADIANT).splitlines()[1]
        for position in POSITIONS
    }
    goals = {
        agent.system_prompt(config, dataclasses.replace(match, mode=mode), TEAM_RADIANT).splitlines()[3]
        for mode in MODES
    }
    # a position or mode the template has no branch for comes out as an empty line
    assert len(roles) == len(POSITIONS) and '' not in roles
    assert len(goals) == len(MODES) and '' not in goals


def test_the_system_prompt_tells_noop_from_stop():
    match = match_config()
    lines = agent.system_prompt(match.radiant.agents[0], match, TEAM_RADIANT).splitlines()
    noop = next(line for line in lines if line.startswith('  NOOP '))
    stop = next(line for line in lines if line.startswith('  STOP '))
    # NOOP leaves the hero's current order alone; STOP cancels a swing that has not landed and a channel
    assert '接着做手上的事' in noop and '打断' in stop


def test_the_user_message_lists_items_then_state_then_teammates():
    lina = CMsgBotWorldState.Unit(name='npc_dota_hero_lina', level=3, health=250, health_max=500, last_hits=4, denies=1)
    lina.location.x, lina.location.y = -1200.4, 300.6
    blaze = AgentConfig(nickname='Blaze', hero='npc_dota_hero_lina', position='offlane', gateway='fake')
    frost = AgentConfig(nickname='Frost', hero='npc_dota_hero_crystal_maiden', position='hard_support', gateway='fake')
    items = ['item_tango', 'item_tango', 'item_from_a_newer_client']  # a repeat, and a name the data does not know
    assert agent.user_prompt('STATE', items, [(blaze, lina), (frost, None)], None) == (
        f'你身上的物品：\n{item_text("item_tango")}\n\nSTATE\n\n你的队友：\n'
        '  Blaze 3号位（劣势路） 莉娜 lvl 3 hp 50% 位于 (-1200, 301) lh/dn 4/1\n'
        '  Frost 5号位（纯辅助） 状态未上报\n\n'
        '你还没有下过命令'
    )
    assert agent.user_prompt('STATE', [], [], []) == 'STATE\n\n你还没有下过命令'


def test_the_user_message_ends_with_the_last_commands_and_where_the_hero_stood():
    history = [
        agent.Note(kind='issued', dota_time=-5.4, position=(-1500.4, -1399.6), step='MOVE, 1180, -1216'),
        agent.Note(kind='rejected', dota_time=61.0, position=(-1200.0, -1300.0), step='ATTACK, 3', error='gone'),
        agent.Note(kind='rejected', dota_time=62.0, step='MOVE, 4', error='MOVE is not legal right now'),
        agent.Note(kind='undelivered', dota_time=63.9, error='ReadTimeout()'),
        agent.Note(kind='unusable', dota_time=65.0, error='reply carried no actions'),
    ]
    assert agent.user_prompt('STATE', [], [], history) == (
        'STATE\n\n你最近的命令，旧的在前，括号里是命令下发时你站的位置：\n'
        '  -0:05 (-1500, -1400) MOVE, 1180, -1216 已经下达\n'
        '  1:01 (-1200, -1300) ATTACK, 3 被拒绝了：gone\n'
        '  1:02 MOVE, 4 被拒绝了：MOVE is not legal right now\n'
        '  1:03 你的回复没能送达：ReadTimeout()\n'
        '  1:05 你的回复无法使用：reply carried no actions'
    )


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
    gateway = FakeGateway(texts=[''], error="ConnectError('nope')")
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
    gateway = FakeGateway(texts=['MOVE, 4\nSAY, ez mid\nSAY, and again'])
    runner = TeamRunner(match_config(all_chat=True), match_config(all_chat=True).radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    chats = [extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_CHAT']
    assert chats, 'no chat action was ever sent'
    assert chats[0]['chat'] == {'message': 'ez mid', 'toAllchat': True}
    assert chats[0]['player'] in range(TEAM_SIZE)
    assert all(chat['chat']['message'] == 'ez mid' for chat in chats)  # one taunt per reply


def test_no_chat_is_sent_when_all_chat_is_off(env):
    gateway = FakeGateway(texts=['MOVE, 4\nSAY, ez mid'])
    match = match_config(all_chat=False)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    assert not [
        extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_CHAT'
    ]


def test_each_reason_reaches_the_bridge_as_a_label_for_its_own_hero(env):
    gateway = FakeGateway(texts=['MOVE, 4\nREASON, 兵线在推，往前补刀'])
    runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    labels = [
        extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_LABEL'
    ]
    assert {label['player'] for label in labels} == set(range(TEAM_SIZE))
    assert all(label['label'] == {'text': '兵线在推，往前补刀'} for label in labels)


def test_no_label_is_sent_when_show_reason_is_off(env):
    gateway = FakeGateway(texts=['MOVE, 4\nREASON, 兵线在推'])
    match = match_config(show_reason=False)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    drive(env, runner, 3)
    runner.close()
    assert not [
        extra for _, _, extras in last_session().sent for extra in extras if extra['actionType'] == 'ACTION_LABEL'
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
        assert '[6] item_tango 树之祭祀 charges 3 ready: CAST' in prompt  # likewise, not item_44
        allies = prompt.split('你的队友：')[1]
        assert len({name for name in nicknames if name in allies}) == TEAM_SIZE - 1  # everyone but yourself


def test_the_user_message_opens_with_the_items_and_carries_the_map(env):
    gateway = FakeGateway()
    runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway})
    drive(env, runner, 2)
    runner.close()
    prompt = gateway.calls[0][0][1]['content']
    assert prompt.startswith('你身上的物品：\n- item_tango 树之祭祀（Tango）：价格 90')
    assert prompt.index('你身上的物品') < prompt.index('time ') < prompt.index('你的队友')
    assert 'terrain: height ' in prompt and 'rune spots: [0] 上路强化神符 dist ' in prompt


def test_the_prompt_repeats_the_command_that_went_out(env):
    gateway = FakeGateway()
    runner = TeamRunner(match_config(interval=0.0), match_config(interval=0.0).radiant, {'fake': gateway})
    drive(env, runner, 4)
    runner.close()
    prompts = [messages[1]['content'] for messages, _ in gateway.calls]
    assert prompts[0].endswith('你还没有下过命令')
    assert prompts[-1].endswith(') MOVE, 4 已经下达')  # after where the hero stood as it went out


def test_the_transcript_records_every_decision(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    gateway = FakeGateway()
    with open(path, 'w') as transcript:
        runner = TeamRunner(match_config(), match_config().radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    records = read_transcript(path)
    assert records and all(record['kind'] == 'decision' for record in records)
    assert {record['nickname'] for record in records} == {f'A{row}' for row in range(TEAM_SIZE)}
    first = records[0]
    assert first['input_tokens'] == 100 and first['usd'] > 0 and first['reply'] == MOVE_NORTH
    assert first['plan'] == [MOVE_NORTH] and first['error'] is None and first['first_line_latency'] == 0.005
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


def test_an_unknown_hero_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'k')
    with pytest.raises(ValueError, match='no such hero: npc_dota_hero_shadow_fiend'):
        load_match(write_config(tmp_path, heroes=['npc_dota_hero_shadow_fiend', *HEROES[1:]]))


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


PLAN_SIX = 'MOVE, 4\n' * 5 + 'STOP\nREASON, wave is pushing, walk up and hit the ranged creep'


def test_steps_past_plan_length_are_dropped_but_the_reason_is_still_read(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    gateway = FakeGateway(texts=[PLAN_SIX])
    match = match_config(interval=3.0, plan_length=2)
    with open(path, 'w') as transcript:
        runner = TeamRunner(match, match.radiant, {'fake': gateway}, transcript)
        applied = drive(env, runner, 4)
        runner.close()
    first = [step[0]['type'] for step in applied]
    assert first[1:] == [int(ActionType.MOVE)] * 2 + [int(ActionType.NOOP)]
    decision = read_transcript(path)[0]
    assert decision['plan'] == ['MOVE, 4'] * 2
    assert decision['reason'] == 'wave is pushing, walk up and hit the ranged creep'


def test_a_hero_acts_on_its_first_line_while_the_model_is_still_writing(env):
    gate = threading.Event()
    gateway = FakeGateway(texts=[PLAN_SIX], gate=gate, gate_after=1)
    match = match_config(plan_length=6)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    _, info = env.reset(seed=0)
    masks = [{key: values[row] for key, values in info['action_mask'].items()} for row in range(TEAM_SIZE)]

    def decide():
        world_state, player_ids = info['world_state'], info['player_ids']
        return runner.decide(world_state, masks, player_ids, env.unwrapped.cast_slots(), env.unwrapped.trees)[0]

    try:
        assert all(one['type'] == int(ActionType.NOOP) for one in decide())  # every agent asks
        deadline = time.time() + 5
        while any(slot.outbox.empty() for slot in runner.slots):
            assert time.time() < deadline, 'a first line never arrived'
            time.sleep(0.001)
        assert all(one['type'] == int(ActionType.MOVE) for one in decide())
        assert all(slot.busy.is_set() for slot in runner.slots)  # the rest of every reply is still to come
    finally:
        gate.set()
        runner.close()


def test_a_reply_cut_off_after_some_steps_still_carries_them_out(env):
    gateway = FakeGateway(texts=['MOVE, 4\nMOVE, 4'], error="ReadTimeout('The read operation timed out')")
    match = match_config(interval=0.0, plan_length=6)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    applied = drive(env, runner, 3)
    runner.close()
    assert all(one['type'] == int(ActionType.MOVE) for one in applied[1])
    assert runner.slots[0].errors > 0
    # what the model hears about is the step that went out, not the timeout after it
    prompt = gateway.calls[-1][0][1]['content']
    assert prompt.endswith(' MOVE, 4 已经下达') and '没能送达' not in prompt


def test_a_reply_without_a_step_is_unusable(env):
    gateway = FakeGateway(texts=['REASON, nothing to do'])
    match = match_config(interval=0.0)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    applied = drive(env, runner, 3)
    runner.close()
    assert all(one['type'] == int(ActionType.NOOP) for step in applied for one in step)
    assert gateway.calls[-1][0][1]['content'].endswith(' 你的回复无法使用：reply carried no actions')


def test_the_history_keeps_the_last_commands_without_the_noops(env):
    gateway = FakeGateway(texts=['MOVE, -1000, -1000\nNOOP\nMOVE, 2\nSTOP\nREASON, walk up'])
    match = match_config(interval=1.0, plan_length=6, history_length=2)
    runner = TeamRunner(match, match.radiant, {'fake': gateway})
    drive(env, runner, 8)
    runner.close()
    # the fake hero 0 starts at (-1500, -1400) and stands wherever its last move went
    second = [messages[1]['content'] for messages, _ in gateway.calls if '你是A0' in messages[0]['content']][1]
    history = second.split('括号里是命令下发时你站的位置：\n')[1].splitlines()
    assert len(history) == 2  # the MOVE_TO went out first and fell off the end
    assert history[0].endswith(' (-1000, -1000) MOVE, 2 已经下达')
    assert history[1].endswith(' (-788, -788) STOP 已经下达')


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


def test_the_reason_and_the_prompt_reach_the_transcript(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    gateway = FakeGateway(texts=[PLAN_SIX])
    match = match_config(plan_length=6, log_prompts=True)
    with open(path, 'w') as transcript:
        runner = TeamRunner(match, match.radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    decisions = [r for r in read_transcript(path) if r['kind'] == 'decision']
    assert decisions and all(r['reason'].startswith('wave is pushing') for r in decisions)
    assert all(len(r['plan']) == 6 for r in decisions)
    prompt = decisions[0]['prompt']
    assert 'units within 1600' in prompt and '你的队友：' in prompt


def test_a_rejected_planned_action_is_recorded_when_it_is_applied(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    # row 31 is never a legal attack target in the fake world state
    gateway = FakeGateway(texts=['ATTACK, 31\nREASON, hit it'])
    match = match_config(plan_length=6)
    with open(path, 'w') as transcript:
        runner = TeamRunner(match, match.radiant, {'fake': gateway}, transcript)
        drive(env, runner, 3)
        runner.close()
    rejected = [record for record in read_transcript(path) if record['kind'] == 'rejected']
    assert rejected and rejected[0]['step'] == 'ATTACK, 31'
    assert runner.slots[0].parse_errors > 0


def test_plan_length_must_be_at_least_one(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'sk-secret')
    config = Path(write_config(tmp_path))
    config.write_text(config.read_text().replace('max_dota_time: 300.0', 'max_dota_time: 300.0\n  plan_length: 0'))
    with pytest.raises(ValueError, match='plan_length'):
        load_match(str(config))
