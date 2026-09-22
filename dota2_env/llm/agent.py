"""What one LLM agent reads and what its reply means: prompt text in, a plan of actions out.

Pure functions, no threads and no HTTP, so a test can drive them against a hand-built world state.
A reply is read once (read_plan) and then resolved one action per frame (resolve), because legality
and unit rows are properties of the frame an order actually goes out on, not of the frame the model saw.
"""

import json

import numpy as np

from dota2_env import actions
from dota2_env.bridge.constants import TEAM_RADIANT
from dota2_env.llm.config import AgentConfig, MatchConfig, Mode, Position
from dota2_env.observation import find_hero

CHAT_LIMIT = 80  # all-chat is a taunt channel, not a place to think out loud
REASON_LIMIT = 200
TICKS_PER_GAME_SECOND = 30

# Note (ruidu): the names are the client's own (DOTA_LaneSelection* in dota_english.txt and
# dota_schinese.txt). The guidance follows the Dota 2 Wiki's 1-5 position system, minus buying
# wards and items, which the env does for the agent.
POSITION_NAME: dict[Position, str] = {
    'safe': '1号位（优势路）',
    'mid': '2号位（中路）',
    'offlane': '3号位（劣势路）',
    'support': '4号位（辅助）',
    'hard_support': '5号位（纯辅助）',
}

ROLE_GUIDANCE: dict[Position, str] = {
    'safe': (
        '前期最弱、最依赖打钱，但后期成长最好。和一到两名辅助一起走优势路，由他们保护；'
        '补刀优先级全队最高，前期专心补刀打钱，不要冒险参战。'
    ),
    'mid': '独自守住中路，靠中路的经验和金钱拿到等级和装备，前中期往往是全队最强的人，可以较早开始对敌人施压。',
    'offlane': (
        '在劣势路首先要活下来，能吃多少经验就吃多少，同时不让敌方1号位安稳打钱。'
        '1到3号位是队伍的主力，后期要打出大部分伤害。'
    ),
    'support': '通常和3号位一起走劣势路，骚扰敌方1号位，并换路参与抓人；能让的钱尽量让给1到3号位。',
    'hard_support': (
        '几乎把所有补刀和钱都让给队友。通常和1号位一起走优势路，骚扰敌方劣势路英雄，保护1号位不被骚扰，让他安全打钱。'
    ),
}

WIN_CONDITION: dict[Mode, str] = {
    'allpick5v5': '摧毁敌方遗迹就赢，己方遗迹被摧毁就输。',
    'mid1v1': '你第二次阵亡，或者己方中路一塔被摧毁，就输了。',
}

# Note (ruidu): the prompt is Chinese but quotes the state's English labels ("units within 1600",
# "abilities", "legal action types") verbatim, because text.py renders the state in English.
SYSTEM_TEMPLATE = """你是{nickname}，在一场 Dota 2 比赛中为{side}出战，用 {hero} 打{position}。
{role}

{win_condition}

每一轮你都会收到自己英雄的当前状态。状态里没有写明的几点：
- target N 指你正在回答的这一轮里 "units within 1600" 表的第 N 行。
- move D 是 16 个罗盘方向之一，移动约 300 距离：0 = 东（+x），4 = 北（+y），8 = 西，12 = 南。
- ability S 是 "abilities" 或 "items" 下方括号里的编号：0 到 5 是技能，6 到 11 是物品；
  每行 "ready:" 后面列出了它现在能用哪几种施法。
- 标着 "(vector: ...)" 的是矢量技能，第二个点顺着施法方向延伸：CAST_TARGET 从我方英雄穿过目标继续往前，
  CAST_DIRECTION 沿方向 D 继续，挥砍、法球的弧线、墙的朝向都跟着它。
- 物品和技能点会自动替你购买和加点，不要尝试购买或加点。
- 你回答时游戏不会暂停，所以在你发出下一条命令之前，英雄会一直执行上一条命令。

动作是下面其中一种：
  {{"type": "MOVE", "move": D}}
  {{"type": "ATTACK", "target": N}}
  {{"type": "CAST", "ability": S}}                        无目标，或者对自己用（吃树会自动吃最近的树）
  {{"type": "CAST_TARGET", "ability": S, "target": N}}    对第 N 行的单位用，指地面的技能会打在它脚下
  {{"type": "CAST_DIRECTION", "ability": S, "move": D}}   朝方向 D 放到最远施法距离，比如扔法球、冲锋、逃跑
  {{"type": "STOP"}} 或 {{"type": "NOOP"}}
只能使用 "legal action types" 下列出的类型。"""

SINGLE_RULE = """
只回复一个 JSON 对象，除此之外什么都不要写——不要解释，也不要 markdown 代码块：
  {"reason": "<一句简短的话>", "actions": [<一个动作>]}"""

PLAN_RULE = """
只回复一个 JSON 对象，除此之外什么都不要写——不要解释，也不要 markdown 代码块：
  {{"reason": "<一句简短的话>", "actions": [<{count} 个动作>]}}

这 {count} 个动作按顺序执行，每 {frame:.1f} 游戏秒一个，所以这个计划覆盖接下来的 {span:.1f} 秒。
想想这段时间里会发生什么，而不只是眼下这一刻：说清楚你要往哪走，走到之后打什么。
计划执行完之前你会被再问一次，所以一个执行到一半依然有用的计划，比把同一个动作重复 {count} 遍更好。
轮到某个动作时如果它已经不合法，它会被跳过，所以计划写得乐观一些也没关系。"""

CHAT_RULE = """
你可以加上 "say": "<最多 {limit} 个字符>"，在所有人聊天里嘲讽敌方队伍。不要频繁使用。
你收到的状态里看不到任何人的回复：这里的聊天只能发，不能读。"""


def system_prompt(agent: AgentConfig, match: MatchConfig, team_id: int) -> str:
    """Fixed for the whole match, so gateways that cache prompt prefixes actually get a hit."""
    frame = match.ticks_per_observation / TICKS_PER_GAME_SECOND
    prompt = SYSTEM_TEMPLATE.format(
        nickname=agent.nickname,
        hero=agent.hero.replace('npc_dota_hero_', ''),
        position=POSITION_NAME[agent.position],
        side='天辉' if team_id == TEAM_RADIANT else '夜魇',
        role=ROLE_GUIDANCE[agent.position],
        win_condition=WIN_CONDITION[match.mode],
    )
    if match.plan_length > 1:
        prompt += PLAN_RULE.format(count=match.plan_length, frame=frame, span=match.plan_length * frame)
    else:
        prompt += SINGLE_RULE
    return prompt + CHAT_RULE.format(limit=CHAT_LIMIT) if match.all_chat else prompt


def team_summary(world_state, team_id: int, player_ids: list[int], agents: list[AgentConfig], row: int) -> str:
    """One line per teammate, so five models can play as a team instead of five solo heroes."""
    lines = []
    for other, player_id in enumerate(player_ids):
        if other == row:
            continue
        agent = agents[other]
        hero = find_hero(world_state, team_id, player_id)
        if hero is None:
            lines.append(f'  {agent.nickname} {POSITION_NAME[agent.position]} 状态未上报')
            continue
        lines.append(
            '  {} {} {} lvl {} hp {:.0f}% 位于 ({:.0f}, {:.0f}) lh/dn {}/{}'.format(
                agent.nickname,
                POSITION_NAME[agent.position],
                hero.name.replace('npc_dota_hero_', ''),
                hero.level,
                100 * hero.health / hero.health_max,
                hero.location.x,
                hero.location.y,
                hero.last_hits,
                hero.denies,
            )
        )
    return '你的队友：\n' + '\n'.join(lines)


def read_plan(text: str, all_chat: bool, plan_length: int) -> tuple[list[dict], str, str | None, str | None]:
    """A model reply as up to plan_length raw action dicts, plus its reason and taunt.

    Nothing is validated here: an action is only checked against the frame it goes out on, by resolve().
    A bare action object is accepted too, which is what plan_length 1 asks for and what a cheap model
    returns anyway often enough to be worth handling.
    """
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        return [], '', None, f'no JSON object in reply: {text[:80]!r}'
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return [], '', None, f'cannot parse reply: {e!r}'

    reason = str(data.get('reason', ''))[:REASON_LIMIT]
    said = str(data.pop('say', '') or '').strip()[:CHAT_LIMIT] if all_chat else ''
    steps = data.get('actions')
    plan = [step for step in steps if isinstance(step, dict)][:plan_length] if isinstance(steps, list) else [data]
    if not plan:
        return [], reason, said or None, 'reply carried no actions'
    return plan, reason, said or None, None


def resolve(
    data: dict,
    mask: dict[str, np.ndarray],
    prompt_handles: list[int],
    unit_handles: list[int],
) -> tuple[dict[str, int], str | None]:
    """One planned action against the frame it is actually going out on.

    prompt_handles are the unit handles of the frame the model saw, unit_handles those of this frame.
    The table is re-sorted every frame, so a row number only survives the wait as a handle.
    """
    if str(data.get('type', '')).upper() not in ('ATTACK', 'CAST_TARGET'):
        return actions.parse_action(data, mask)
    try:
        row = int(data.get('target', 0))
    except (TypeError, ValueError):
        return dict(actions.NOOP_ACTION), f'target {data.get("target")!r} is not a row number'
    if not 0 <= row < len(prompt_handles):
        return dict(actions.NOOP_ACTION), f'target {row} was not in the unit table'
    handle = prompt_handles[row]
    if handle not in unit_handles:
        return dict(actions.NOOP_ACTION), f'target {row} is gone from the unit table'
    return actions.parse_action(dict(data, target=unit_handles.index(handle)), mask)
