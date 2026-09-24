"""Skeleton of an LLM-controlled hero: text observation in, JSON action out (needs `pip install anthropic`).

    python examples/llm_agent.py --steps 200 --ticks 30

The game does not pause while the model answers, so the hero keeps executing its last order in the
meantime. `--ticks 30` (one observation per game second) keeps the number of requests reasonable.
Each request is stateless apart from the last few (observation, action) pairs.
"""

import argparse

import anthropic
import gymnasium as gym

import dota2_env  # noqa: F401
from dota2_env.bridge.game import DEFAULT_HERO
from dota2_env.game_text import display_name, hero_text
from dota2_env.wrappers import TextWrapper

SYSTEM_PROMPT = """你在一场 Dota 2 1v1 中路对局里操控一个英雄（天辉方，中路从左下通向右上，\
在 (-600, -500) 附近与河道相交）。你第二次阵亡，或者己方中路一塔被摧毁，就输了。\
优先级：活下来；给敌方小兵补刀（小兵带 `one_hit` 标记时就攻击它）；反补血量低于一半的己方小兵；\
安全的时候骚扰敌方英雄；不要站在敌方防御塔下。

每一轮你都会收到当前状态。单位行带有编号，把这个编号当作 `target`。
只回复一个 JSON 对象，除此之外什么都不要写：
  {"type": "MOVE", "move": D}        D 为 0..15，朝角度 D * 22.5° 走约 300：0 是 +x，4 是 +y，8 是 -x，12 是 -y
  {"type": "ATTACK", "target": ROW}
  {"type": "CAST", "ability": SLOT}                   无目标技能（例如影魔的毁灭阴影：槽位 0 / 1 / 2
                                                      分别打在英雄正前方 200 / 450 / 700 距离处的一个圆）
  {"type": "CAST_TARGET", "ability": SLOT, "target": ROW}   对单位用，指地面的技能会打在它脚下
  {"type": "CAST_DIRECTION", "ability": SLOT, "move": D}    朝方向 D 放到最远施法距离
  {"type": "NOOP"}                                    不下新命令，英雄接着做手上的事，攻击、持续施法都不打断
  {"type": "STOP"}                                    打断手上的事，原地停下
  {"type": "TP", "point": [X, Y]}                     用回城卷轴传送到 (X, Y)，持续施法 3 秒（"TP slot" 行）
  {"type": "TALENT", "talent": T}                     学 "talents" 行 ready 后面编号 T 的天赋，不打断手上的事
  {"type": "COURIER"}                                 让信使把储藏处（"stash" 行）的东西送来，不打断手上的事
  {"type": "PICKUP_RUNE", "rune": R}                  捡 "rune spots" 行里编号 R 的神符点上的神符
SLOT 是 "abilities"（0 到 5）或 "items"（6 到 11）下方括号里的编号，每行 "ready:" 后面列出了它能用哪几种施法；
对物品用 CAST 就是对自己使用，比如吃树（自动吃最近的树）、仙灵火、治疗药膏。
标着 "(vector: ...)" 的是矢量技能：只能给第一个点，第二个点固定在地图中心，挥砍、弧线都会偏向那一侧。
只能使用 "legal action types" 下列出的动作类型。
状态里的 (dx, dy) 是相对你的位置（它的坐标减去你的 pos），dist 是直线距离；要朝 (dx, dy) 走，
D 取 atan2(dy, dx) / 22.5° 四舍五入，负数加 16。"terrain" 行：height 是你脚下的地势，站在低处看不到高处；
"within 300" 是 300 内的 wall（走不过去）/ tree（树）/ up / down（坡）在哪。
"our towers" / "enemy towers" 是两方每条路最外面那座塔；"rune spots" 和 "landmarks" 是神符点和地标。"""
SKILLS = f'{display_name(DEFAULT_HERO)}的技能（状态里 "abilities" 下写的是每条开头的内部名称）：'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--steps', type=int, default=200)
    parser.add_argument('--ticks', type=int, default=30, help='game ticks per observation (30 = 1 game second)')
    parser.add_argument('--model', default='claude-opus-5')
    parser.add_argument('--history', type=int, default=4, help='previous turns shown to the model')
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()

    client = anthropic.Anthropic()
    system = f'{SYSTEM_PROMPT}\n\n{SKILLS}\n{hero_text(DEFAULT_HERO)}'  # the env's default hero
    env = TextWrapper(
        gym.make('dota2_env/Mid1v1-v0', render_mode='human' if args.render else None, ticks_per_observation=args.ticks)
    )
    history = []
    try:
        observation, info = env.reset()
        for step in range(args.steps):
            messages = []
            for past_observation, past_action in history[-args.history :]:
                messages += [
                    {'role': 'user', 'content': past_observation},
                    {'role': 'assistant', 'content': past_action},
                ]
            messages.append({'role': 'user', 'content': observation})
            response = client.messages.create(
                model=args.model,
                max_tokens=2000,
                system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
                output_config={'effort': 'low'},
                messages=messages,
            )
            action = next((block.text for block in response.content if block.type == 'text'), '')
            if response.stop_reason != 'end_turn':
                action = '{"type": "NOOP"}'
            history.append((observation, action))

            observation, reward, terminated, truncated, info = env.step(action.strip())
            print(
                'step {} t={:.0f} action={} reward={:+.2f} {}'.format(
                    step, info['dota_time'], action.strip(), reward, info['action_error'] or ''
                )
            )
            if terminated or truncated:
                print('episode over, winner:', info['winner'])
                break
    finally:
        env.close()


if __name__ == '__main__':
    main()
