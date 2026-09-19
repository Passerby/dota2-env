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
from dota2_env.wrappers import TextWrapper

SYSTEM_PROMPT = """You control a hero in a Dota 2 1v1 mid lane match (Radiant side, lane runs from bottom-left \
to top-right; the lane meets the river around (-600, -500)). You lose on your second death or when your tier-1 \
mid tower falls. Priorities: stay alive, last-hit enemy creeps (attack when a creep is `one_hit`), deny allied \
creeps below half health, harass the enemy hero when it is safe, and do not stand under the enemy tower.

Every turn you receive the current state. Unit rows are numbered; use that number as `target`.
Reply with exactly one JSON object and nothing else:
  {"type": "MOVE", "move": D}        D in 0..15, 0 = east (+x), 4 = north (+y), 8 = west, 12 = south; ~300 units
  {"type": "ATTACK", "target": ROW}
  {"type": "CAST", "ability": SLOT}                   no-target abilities (e.g. Shadow Fiend razes hit a circle
                                                      200 / 450 / 700 units in front of the hero for slots 0 / 1 / 2)
  {"type": "CAST_TARGET", "ability": SLOT, "target": ROW}
  {"type": "STOP"} or {"type": "NOOP"}
Only use action types listed under "legal action types"."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--steps', type=int, default=200)
    parser.add_argument('--ticks', type=int, default=30, help='game ticks per observation (30 = 1 game second)')
    parser.add_argument('--model', default='claude-opus-5')
    parser.add_argument('--history', type=int, default=4, help='previous turns shown to the model')
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()

    client = anthropic.Anthropic()
    env = TextWrapper(gym.make('dota2_env/Mid1v1-v0', render_mode='human' if args.render else None,
                               ticks_per_observation=args.ticks))
    history = []
    try:
        observation, info = env.reset()
        for step in range(args.steps):
            messages = []
            for past_observation, past_action in history[-args.history:]:
                messages += [{'role': 'user', 'content': past_observation},
                             {'role': 'assistant', 'content': past_action}]
            messages.append({'role': 'user', 'content': observation})
            response = client.messages.create(
                model=args.model,
                max_tokens=2000,
                system=[{'type': 'text', 'text': SYSTEM_PROMPT, 'cache_control': {'type': 'ephemeral'}}],
                output_config={'effort': 'low'},
                messages=messages,
            )
            action = next((block.text for block in response.content if block.type == 'text'), '')
            if response.stop_reason != 'end_turn':
                action = '{"type": "NOOP"}'
            history.append((observation, action))

            observation, reward, terminated, truncated, info = env.step(action.strip())
            print('step {} t={:.0f} action={} reward={:+.2f} {}'.format(
                step, info['dota_time'], action.strip(), reward, info['action_error'] or ''))
            if terminated or truncated:
                print('episode over, winner:', info['winner'])
                break
    finally:
        env.close()


if __name__ == '__main__':
    main()
