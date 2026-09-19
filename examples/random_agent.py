"""Random legal actions against the built-in bot, headless.

    python examples/random_agent.py --steps 600 --timescale 2
"""
import argparse
import time

import gymnasium as gym

import dota2_env  # noqa: F401  (registers the env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--steps', type=int, default=600)
    parser.add_argument('--timescale', type=float, default=1.0)
    parser.add_argument('--opponent', default='builtin', choices=['builtin', 'idle'])
    parser.add_argument('--render', action='store_true', help='open the game window instead of running headless')
    args = parser.parse_args()

    env = gym.make('dota2_env/Mid1v1-v0', render_mode='human' if args.render else 'ansi',
                   timescale=args.timescale, opponent=args.opponent)
    try:
        start = time.time()
        observation, info = env.reset(seed=0)
        print('reset took {:.1f}s, dota_time {:.1f}'.format(time.time() - start, info['dota_time']))
        episode_return, start = 0.0, time.time()
        for step in range(1, args.steps + 1):
            action = env.unwrapped.sample_legal_action()
            observation, reward, terminated, truncated, info = env.step(action)
            episode_return += reward
            if step % 100 == 0:
                print(f'\n--- step {step} return {episode_return:.2f} ({step / (time.time() - start):.1f} steps/s) ---')
                print(env.render())
            if terminated or truncated:
                print('episode over: terminated={} truncated={} winner={}'.format(terminated, truncated, info['winner']))
                break
        print('ability names:', env.unwrapped.ability_names())
    finally:
        env.close()


if __name__ == '__main__':
    main()
