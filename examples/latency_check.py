"""How late do actions reach the game? Runs a random policy with an artificial think time and prints
`info["action_delivery"]`.

    python examples/latency_check.py --think 0.0
    python examples/latency_check.py --think 0.5
"""
import argparse
import time

import gymnasium as gym

import dota2_env  # noqa: F401


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--think', type=float, default=0.0, help='seconds the fake policy takes per step')
    parser.add_argument('--timescale', type=float, default=1.0)
    parser.add_argument('--steps', type=int, default=150)
    args = parser.parse_args()

    env = gym.make('dota2_env/Mid1v1-v0', timescale=args.timescale, opponent='idle')
    try:
        _, info = env.reset()
        for _ in range(args.steps):
            time.sleep(args.think)
            _, _, terminated, truncated, info = env.step(env.unwrapped.sample_legal_action())
            if terminated or truncated:
                break
        print('think={}s timescale={}'.format(args.think, args.timescale))
        print('  action_delivery:', info['action_delivery'])
        print('  skipped_observations:', info['skipped_observations'])
    finally:
        env.close()


if __name__ == '__main__':
    main()
