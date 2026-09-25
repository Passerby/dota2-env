"""The long think, the team's memory and blackboard, and the review after a match, with no network."""

from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from fake_session import Fake5v5Session
from test_llm import HEROES, FakeGateway, match_config, read_transcript, settle, write_config

from dota2_env.actions import team_action_space
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT, UNIT_TYPE_HERO, UNIT_TYPE_TOWER
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.game_text import load_records
from dota2_env.llm import agent, memory, review, think
from dota2_env.llm.config import AgentConfig, GatewayConfig, ThinkConfig, load_match
from dota2_env.llm.gateway import Reply
from dota2_env.llm.runner import TeamRunner
from dota2_env.observation import TEAM_SIZE

THINK = 'PLAN, 去中路补兵\nGOAL, -1200, -1100\nNOTE, 敌方莉娜会从河道过来\nREASON, 开局先稳'
LESSON = 'LESSON, 对线, 敌方斧王常从河道绕过来，看不到他时别压线\nREASON, 死得太多'
AXE = load_records('heroes')['npc_dota_hero_axe']['id']
SHADOW_FIEND = load_records('heroes')['npc_dota_hero_nevermore']['id']


class ByTask(FakeGateway):
    """A long think's gateway that answers a post-match review with the review's text."""

    def __init__(self, texts=(THINK,), review=LESSON):
        super().__init__(texts=texts)
        self.review = review

    def stream(self, messages, params):
        if '现在复盘' not in messages[0]['content']:
            yield from super().stream(messages, params)
            return
        self.calls.append((messages, params))
        yield from (line for line in self.review.split('\n') if line.strip())
        yield Reply(
            text=self.review,
            input_tokens=100,
            output_tokens=10,
            usd=self.usd,
            latency=0.01,
            first_line_latency=0.005,
            error=None,
        )


def thinking_match(interval=1.0, think_interval=60.0, **fields):
    match = match_config(interval=interval, **fields)
    match.gateways['think'] = GatewayConfig(name='think', base_url='', api_key='k', model='t')
    for config in match.radiant.agents:
        config.think = ThinkConfig(gateway='think', interval=think_interval)
    return match


@pytest.fixture
def env():
    env = gym.make('dota2_env/AllPick5v5-v0', session_factory=Fake5v5Session)
    yield env
    env.close()


def last_session():
    return Fake5v5Session.instances[-1]


def play(env, runner, steps, info=None):
    """The real sense / decide / step loop, from a reset or on from info; returns the last info."""
    if info is None:
        _, info = env.reset(seed=0)
        runner.sense(info['world_states'])
    for _ in range(steps):
        settle(runner)
        masks = [{key: values[row] for key, values in info['action_mask'].items()} for row in range(TEAM_SIZE)]
        hero_actions, _, _ = runner.decide(
            info['world_state'], masks, info['player_ids'], env.unwrapped.cast_slots(), env.unwrapped.trees
        )
        action = {
            key: np.array([one[key] for one in hero_actions], space.dtype)
            for key, space in team_action_space.spaces.items()
        }
        _, _, _, _, info = env.step(action)
        runner.sense(info['world_states'])
    settle(runner)
    return info


def prompts_of(gateway, nickname):
    return [messages[1]['content'] for messages, _ in gateway.calls if f'你是{nickname}' in messages[0]['content']]


def frame(dota_time, deaths=0, kills=0, tower=True, enemy_at=None, roshan_by=None, alive=True):
    """A 1v1 world state: player 0 of ours, player 5 theirs, both mid towers, and their hero where given."""
    world_state = CMsgBotWorldState(dota_time=dota_time)
    world_state.players.add(player_id=0, team_id=TEAM_RADIANT, deaths=deaths, is_alive=True, hero_id=SHADOW_FIEND)
    world_state.players.add(player_id=5, team_id=TEAM_DIRE, kills=kills, is_alive=True, hero_id=AXE)
    ours = world_state.units.add(handle=1, unit_type=UNIT_TYPE_HERO, team_id=TEAM_RADIANT, player_id=0, is_alive=alive)
    ours.name, ours.location.x, ours.location.y = 'npc_dota_hero_nevermore', -1000.0, -1000.0
    unit = world_state.units.add(handle=100, unit_type=UNIT_TYPE_TOWER, team_id=TEAM_RADIANT, is_alive=True)
    unit.name, unit.location.x, unit.location.y = 'npc_dota_goodguys_tower1_mid', -1544.0, -1408.0
    if tower:
        unit = world_state.units.add(handle=101, unit_type=UNIT_TYPE_TOWER, team_id=TEAM_DIRE, is_alive=True)
        unit.name, unit.location.x, unit.location.y = 'npc_dota_badguys_tower1_mid', 524.0, 652.0
    if enemy_at is not None:
        enemy = world_state.units.add(
            handle=2, unit_type=UNIT_TYPE_HERO, team_id=TEAM_DIRE, player_id=5, level=3, health=300, health_max=600
        )
        enemy.name, enemy.is_alive, (enemy.location.x, enemy.location.y) = 'npc_dota_hero_axe', True, enemy_at
    if roshan_by is not None:
        world_state.roshan_killed_events.add(killer_player_id=roshan_by)
    return world_state


# -- reading replies --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('line', 'kind', 'text'),
    [
        ('INTENT，去下路补兵', 'intent', '去下路补兵'),
        ('CALL, A2, 来中路', 'call', 'A2, 来中路'),
        ('ask: 塔已经倒了', 'ask', '塔已经倒了'),
        ('PLAN, 去中路', 'plan', '去中路'),
        ('GOAL, (1180, -1216)', 'goal', '(1180, -1216)'),
        ('NOTE, 斧王会绕后', 'note', '斧王会绕后'),
        ('FORGET, 2', 'forget', '2'),
        ('LESSON, 出装, 先出黑皇杖', 'lesson', '出装, 先出黑皇杖'),
        ('MOVE, 4', 'step', 'MOVE, 4'),
    ],
)
def test_every_tag_reads_as_its_kind_and_anything_else_is_a_step(line, kind, text):
    assert agent.read_line(line) == (kind, text)


def test_a_call_names_a_teammate_or_the_whole_team():
    nicknames = ['A0', 'A1', 'A2']
    assert agent.read_call('a2, 来中路', nicknames) == (2, '来中路')
    assert agent.read_call('全队，集合推中', nicknames) == (None, '集合推中')
    assert agent.read_call('来中路集合', nicknames) == (None, '来中路集合')  # names nobody: the whole team


def test_a_goal_is_two_numbers_and_a_lesson_a_kind_and_a_text():
    assert think.read_goal('1180, -1216') == (1180.0, -1216.0)
    assert think.read_goal('(1180，-1216)') == (1180.0, -1216.0)
    assert think.read_goal('敌方中路一塔') is None
    assert review.read_lesson('出装，先出黑皇杖') == ('出装', '先出黑皇杖')
    assert review.read_lesson('没有逗号的一句话') is None


# -- memory -----------------------------------------------------------------------------------------


def test_a_death_is_a_counter_going_up_and_its_killer_the_kills_that_went_up_with_it():
    tracker = memory.MemoryTracker(TEAM_RADIANT, 1)
    assert tracker.update(frame(10.0), [None]) == []
    events = tracker.update(frame(10.2, deaths=1, kills=1), [None])
    assert [(event.kind, event.team, event.name, event.killer) for event in events] == [
        ('death', TEAM_RADIANT, 'npc_dota_hero_nevermore', 'npc_dota_hero_axe')  # named by hero id, never seen
    ]
    assert events[0].position == (-1000.0, -1000.0) and tracker.update(frame(10.4, deaths=1, kills=1), [None]) == []


def test_a_tower_missing_from_a_frame_has_fallen_and_roshan_is_a_frame_event():
    tracker = memory.MemoryTracker(TEAM_RADIANT, 1)
    tracker.update(frame(10.0), [None])
    events = tracker.update(frame(10.2, tower=False, roshan_by=5), [None])
    assert [(event.kind, event.team, event.name) for event in events] == [
        ('tower', TEAM_DIRE, 'npc_dota_badguys_tower1_mid'),
        ('roshan', TEAM_DIRE, ''),
    ]
    assert events[1].killer == 'npc_dota_hero_axe'


def test_an_enemy_hero_is_remembered_where_the_team_saw_it_last():
    tracker = memory.MemoryTracker(TEAM_RADIANT, 1)
    tracker.update(frame(10.0, enemy_at=(300.0, 400.0)), [None])
    tracker.update(frame(15.0), [None])
    seen = tracker.seen[5]
    assert (seen.dota_time, seen.name, seen.level, seen.health, seen.position) == (
        10.0,
        'npc_dota_hero_axe',
        3,
        0.5,
        (300.0, 400.0),
    )


def test_follow_through_counts_the_walk_the_time_near_the_goal_and_the_deaths():
    tracker = memory.MemoryTracker(TEAM_RADIANT, 1)
    start = frame(10.0)
    tracker.update(start, [None])
    tracker.restart(0, start.units[0], 10.0)
    goal = (-1000.0, 0.0)  # 1000 away as the plan starts
    for dota_time, y in ((11.0, -800.0), (12.0, -600.0), (13.0, -600.0)):
        world_state = frame(dota_time)
        world_state.units[0].location.y = y
        tracker.update(world_state, [goal])
    tracker.update(frame(14.0, deaths=1, alive=False), [goal])
    progress = tracker.restart(0, None, 14.0)
    assert progress.moved == pytest.approx(400.0)  # a dead body is no walk
    assert progress.near_goal == pytest.approx(3.0) and progress.closest == pytest.approx(600.0)
    assert progress.deaths == 1


def test_a_repeated_call_only_keeps_itself_up():
    board = memory.Blackboard(directives=[None] * 3, intents=[None] * 3)
    assert board.post(memory.Call(row=0, target=2, text='来中路', dota_time=1.0, expires=31.0))
    assert not board.post(memory.Call(row=0, target=2, text='来中路', dota_time=2.0, expires=32.0))
    assert board.post(memory.Call(row=1, target=None, text='集合', dota_time=3.0, expires=33.0))
    assert [call.text for call in board.calls_for(2, 4.0)] == ['来中路', '集合']
    assert [call.text for call in board.calls_for(1, 4.0)] == []  # neither to it nor from someone else
    assert board.post(memory.Call(row=0, target=2, text='算了', dota_time=5.0, expires=35.0))  # replaces the first
    assert [call.text for call in board.calls_for(2, 32.5)] == ['集合', '算了']
    assert [call.text for call in board.calls_for(2, 34.0)] == ['算了']


def test_lessons_are_appended_and_the_newest_read_back(tmp_path):
    first = {'kind': '对线', 'text': '别压线', 'position': 'mid', 'match': 'a', 'source': 'review'}
    second = {'kind': '出装', 'text': '先出黑皇杖', 'position': 'mid', 'match': 'b', 'source': 'review'}
    assert memory.load_lessons(str(tmp_path), HEROES[0], 5) == []
    memory.save_lessons(str(tmp_path), HEROES[0], [first])
    memory.save_lessons(str(tmp_path), HEROES[0], [second])
    assert memory.load_lessons(str(tmp_path), HEROES[0], 5) == [first, second]
    assert memory.load_lessons(str(tmp_path), HEROES[0], 1) == [second]
    assert '别压线' in (tmp_path / 'heroes' / f'{HEROES[0]}.yaml').read_text(encoding='utf-8')  # readable as it is


# -- prompts ----------------------------------------------------------------------------------------


def test_the_per_second_message_has_the_plan_before_the_state_and_its_progress_after():
    blaze = AgentConfig(nickname='Blaze', hero='npc_dota_hero_lina', position='offlane', gateway='fake')
    directive = memory.Directive(decided_at=60.0, plan='去下路补兵', goal=(1180.0, -1216.0))
    call = memory.Call(row=1, target=0, text='来中路', dota_time=62.0, expires=92.0)
    prompt = agent.act_user_prompt(
        'STATE',
        [],
        [(blaze, None, (61.0, '去上路'))],
        [],
        directive=directive,
        dota_time=75.5,
        goal_distance=812.4,
        calls=[('Blaze', call)],
        intent=(63.0, '补这波兵'),
    )
    assert prompt == (
        '你的计划（1:00 定）：去下路补兵\n  目标点 (1180, -1216)\n\n'
        'STATE\n计划进度：定下已经 0:15，离目标点 812\n\n'
        '你的队友：\n  Blaze 3号位（劣势路） 状态未上报 打算：去上路\n\n'
        '队友的呼叫：\n  1:02 Blaze（对你）：来中路\n\n'
        '你现在的打算（1:03 起）：补这波兵\n\n'
        '你还没有下过命令'
    )


def test_only_an_agent_with_a_long_think_is_taught_about_plans_and_ask():
    alone, thinking = match_config(), thinking_match()
    prompt = agent.act_system_prompt(alone.radiant.agents[0], alone, TEAM_RADIANT)
    assert '\n  INTENT, <最多 40 个字>' in prompt and '\n  CALL, <队友昵称或 全队>, <最多 40 个字>' in prompt
    assert 'ASK' not in prompt and '你的计划' not in prompt
    prompt = agent.act_system_prompt(thinking.radiant.agents[0], thinking, TEAM_RADIANT)
    assert '\n  ASK, <一句话>' in prompt and '"你的计划"是你的长期规划写给你的：它大约每 60 游戏秒' in prompt


def test_the_long_think_system_prompt_shares_the_heros_partials_and_carries_the_lessons():
    match = thinking_match()
    config = match.radiant.agents[0]
    act = agent.act_system_prompt(config, match, TEAM_RADIANT)
    lessons = [{'kind': '对线', 'text': '别压线'}]
    prompt = think.think_system_prompt(config, match, match.radiant, lessons)
    assert prompt.startswith(act.split('\n\n每一轮你都会收到')[0])  # identity, map and the hero's skills
    assert '你们队伍：\n  A0 2号位（中路） 影魔（你）\n  A1 2号位（中路） 狙击手\n' in prompt
    assert '\n- 对线：别压线\n' in prompt
    assert '\n  PLAN, <最多 150 个字>' in prompt and '笔记最多留 8 条' in prompt
    assert '{{' not in prompt and 'MOVE, D' not in prompt  # the action table is the per-second channel's


def test_the_review_is_asked_for_lessons_about_the_finished_match():
    match = thinking_match()
    prompt = review.review_system_prompt(match.radiant.agents[0], match, TEAM_RADIANT)
    assert '现在复盘' in prompt and '\n  LESSON, <类别>, <最多 80 个字>' in prompt
    world_state = frame(600.0, deaths=2)
    plans = [memory.Directive(decided_at=0.0, plan='去中路', goal=(-1200.0, -1100.0))]
    events = [memory.Event(kind='tower', dota_time=300.0, team=TEAM_DIRE, name='npc_dota_badguys_tower1_mid')]
    lessons = [{'kind': '对线', 'text': '别压线'}]
    prompt = review.review_user_prompt(world_state, TEAM_RADIANT, 0, 'lost', plans, events, [], lessons)
    assert prompt.startswith('结果：你们输了，比赛打到 10:00。\n你最后：K/D/A 0/2/0')
    assert '\n  0:00 去中路（目标点 (-1200, -1100)）\n' in prompt and '\n  5:00 敌方中路一塔被摧毁\n' in prompt
    assert prompt.endswith('你已经有的经验：\n- 对线：别压线')


def test_a_think_is_read_from_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'k')
    path = tmp_path / 'match.yaml'
    body = Path(write_config(tmp_path)).read_text()
    path.write_text(body.replace('gateway: cheap}', 'gateway: cheap, think: {gateway: cheap, interval: 30}}'))
    assert load_match(str(path)).radiant.agents[0].think == ThinkConfig(gateway='cheap', interval=30.0)
    path.write_text(body.replace('gateway: cheap}', 'gateway: cheap, think: {gateway: nowhere}}'))
    with pytest.raises(ValueError, match='thinks through undefined gateway'):
        load_match(str(path))


# -- the runner -------------------------------------------------------------------------------------


def test_a_long_think_plan_reaches_the_next_per_second_prompt(env):
    act, thinker = FakeGateway(), FakeGateway(texts=[THINK])
    match = thinking_match(interval=0.0)
    runner = TeamRunner(match, match.radiant, {'fake': act, 'think': thinker})
    play(env, runner, 4)
    runner.close()
    first, last = prompts_of(act, 'A0')[0], prompts_of(act, 'A0')[-1]
    assert '你的计划' not in first  # the first turn goes out before any long think is back
    assert '\n\n你的计划（-1:14 定）：去中路补兵\n  目标点 (-1200, -1100)\n\ntime ' in last
    assert '\n计划进度：定下已经 0:0' in last
    think_prompt = prompts_of(thinker, 'A0')[0]
    assert think_prompt.startswith('这次想的原因：\n  开局，这是第一次\n\n时间 -1:14，我们是天辉，击杀 0 : 0。')
    assert (
        '\n  A0（你） 2号位（中路） 影魔 0/0/0 lvl 1 ' in think_prompt
        and '\n还立着的塔：\n  我方：中路一塔 100%' in think_prompt
    )


def test_long_thinks_keep_their_own_pace_in_game_seconds(env):
    # without a GOAL, which a hero walking by would reach and so ask for another plan
    slow, quick = FakeGateway(texts=['PLAN, 去中路补兵']), FakeGateway(texts=['PLAN, 去中路补兵'])
    lazy_match, busy_match = thinking_match(think_interval=60.0), thinking_match(think_interval=0.4)
    lazy = TeamRunner(lazy_match, lazy_match.radiant, {'fake': FakeGateway(), 'think': slow})
    play(env, lazy, 10)
    lazy.close()
    busy = TeamRunner(busy_match, busy_match.radiant, {'fake': FakeGateway(), 'think': quick})
    play(env, busy, 10)
    busy.close()
    assert lazy.thinks.thinkers[0].channel.requests == 1  # at the start, then nothing asked for another
    assert busy.thinks.thinkers[0].channel.requests >= 4


def test_a_death_rests_the_per_second_channel_and_wakes_every_long_think(env):
    act, thinker = FakeGateway(), FakeGateway(texts=[THINK])
    match = thinking_match(interval=0.0)
    runner = TeamRunner(match, match.radiant, {'fake': act, 'think': thinker})
    info = play(env, runner, 3)
    session = last_session()
    session.deaths, session.enemy_kills[0] = 1, 1
    session.dead_rows.add(0)
    info = play(env, runner, 1, info)  # decided on the frame from before, the hero still alive in it
    asked, others = runner.slots[0].act.requests, runner.slots[1].act.requests
    play(env, runner, 4, info)
    runner.close()
    assert runner.slots[0].act.requests == asked and runner.slots[1].act.requests > others
    assert '\n  -1:13 你阵亡了，被莉娜击杀\n' in prompts_of(thinker, 'A0')[-1]
    assert '我方影魔阵亡，被莉娜击杀' in prompts_of(thinker, 'A1')[-1]


def test_a_call_wakes_the_long_think_of_the_teammate_it_names_only(env):
    act, thinker = FakeGateway(texts=['MOVE, 4\nCALL, A2, 来中路帮我']), FakeGateway(texts=[THINK])
    match = thinking_match(interval=0.0)
    runner = TeamRunner(match, match.radiant, {'fake': act, 'think': thinker})
    play(env, runner, 4)
    runner.close()
    assert any('A0喊你：来中路帮我' in prompt for prompt in prompts_of(thinker, 'A2'))
    assert not any('喊你' in prompt for prompt in prompts_of(thinker, 'A1'))
    calls = prompts_of(act, 'A2')[-1].split('\n队友的呼叫：\n')[1].split('\n\n')[0].splitlines()
    # the four others each called it; a call made again only stays up, its time the latest
    assert sorted(line.split(' ', 3)[-1] for line in calls) == [f'A{row}（对你）：来中路帮我' for row in (0, 1, 3, 4)]


def test_an_ask_wakes_its_own_long_think(env):
    act, thinker = FakeGateway(texts=['MOVE, 4\nASK, 计划里的塔已经倒了']), FakeGateway(texts=[THINK])
    match = thinking_match()
    runner = TeamRunner(match, match.radiant, {'fake': act, 'think': thinker})
    play(env, runner, 4)
    runner.close()
    assert runner.thinks.thinkers[0].channel.requests >= 2
    assert any('操作你英雄的模型请你重新想：计划里的塔已经倒了' in prompt for prompt in prompts_of(thinker, 'A0'))


def test_forget_drops_the_note_it_names_and_a_repeated_note_is_kept_once(env):
    mine = FakeGateway(texts=['PLAN, 一\nNOTE, 甲\nNOTE, 乙\nNOTE, 甲', 'PLAN, 二\nFORGET, 1', 'PLAN, 三'])
    match = thinking_match(think_interval=0.4)
    match.gateways['mine'] = mine.config
    match.radiant.agents[0].think = ThinkConfig(gateway='mine', interval=0.4)
    runner = TeamRunner(
        match, match.radiant, {'fake': FakeGateway(), 'think': FakeGateway(texts=[THINK]), 'mine': mine}
    )
    play(env, runner, 8)
    runner.close()
    assert [memo.text for memo in runner.thinks.thinkers[0].memos] == ['乙']
    assert prompts_of(mine, 'A0')[-1].endswith('你的笔记：\n  [1] -1:14 乙')


def test_the_transcript_records_the_long_thinks_and_what_happened(env, tmp_path):
    path = tmp_path / 'match.jsonl'
    match = thinking_match()
    with open(path, 'w') as transcript:
        runner = TeamRunner(
            match, match.radiant, {'fake': FakeGateway(), 'think': FakeGateway(texts=[THINK])}, transcript
        )
        info = play(env, runner, 2)
        session = last_session()
        session.deaths, session.enemy_kills[0] = 1, 1
        session.dead_rows.add(0)
        play(env, runner, 3, info)
        runner.close()
    records = read_transcript(path)
    header = records[0]
    assert header['think']['system'] == runner.thinks.thinkers[0].channel.system and header['think']['interval'] == 60.0
    thinks = [record for record in records if record['kind'] == 'think' and record['nickname'] == 'A0']
    assert (
        thinks[0]['triggers'] == ['start'] and thinks[0]['plan'] == '去中路补兵' and thinks[0]['goal'] == [-1200, -1100]
    )
    assert thinks[0]['notes'] == ['敌方莉娜会从河道过来'] and thinks[0]['error'] is None
    assert any('death' in record['triggers'] for record in thinks)
    events = [record for record in records if record['kind'] == 'event']
    assert [(record['event'], record['name'], record['killer']) for record in events] == [
        ('death', 'npc_dota_hero_nevermore', 'npc_dota_hero_lina')
    ]


def test_a_review_leaves_lessons_that_the_next_match_reads(env, tmp_path):
    memory_dir = tmp_path / 'memory'
    match = thinking_match(memory_dir=str(memory_dir))
    runner = TeamRunner(match, match.radiant, {'fake': FakeGateway(), 'think': ByTask()})
    info = play(env, runner, 3)
    review.review(runner.thinks, info['world_state'], info['player_ids'], None, '20260925T000000Z', timeout=5.0)
    runner.close()
    lessons = memory.load_lessons(str(memory_dir), HEROES[0], 10)
    assert lessons == [
        {
            'kind': '对线',
            'text': '敌方斧王常从河道绕过来，看不到他时别压线',
            'position': 'mid',
            'match': '20260925T000000Z',
            'source': 'review',
        }
    ]
    following = TeamRunner(match, match.radiant, {'fake': FakeGateway(), 'think': ByTask()})
    following.close()
    assert '\n- 对线：敌方斧王常从河道绕过来，看不到他时别压线\n' in following.thinks.thinkers[0].channel.system
