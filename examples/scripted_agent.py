"""A small hand-written laning policy that only uses the gym observation: walk to the lane, last-hit,
deny, harass with razes. Useful as a sanity check of observations, actions, rewards and the opponent.

    python examples/scripted_agent.py --timescale 4 --max-steps 3000
"""

import argparse
import math
import time

import gymnasium as gym
import numpy as np

import dota2_env
from dota2_env.actions import N_MOVE_DIRECTIONS, ActionType
from dota2_env.observation import HERO_FEATURES, MAP_SCALE, UNIT_FEATURES, UNIT_RADIUS

H = {name: i for i, name in enumerate(HERO_FEATURES)}
U = {name: i for i, name in enumerate(UNIT_FEATURES)}
LANE_POSITION = {dota2_env.TEAM_RADIANT: (-650.0, -550.0), dota2_env.TEAM_DIRE: (-250.0, -150.0)}
RAZE_RANGES = (200.0, 450.0, 700.0)
RAZE_RADIUS = 250.0


def move_towards(dx, dy):
    direction = round(math.atan2(dy, dx) / (2 * math.pi) * N_MOVE_DIRECTIONS) % N_MOVE_DIRECTIONS
    return {'type': int(ActionType.MOVE), 'move': direction, 'target': 0, 'ability': 0}


def policy(observation, mask, team_id):
    hero, units = observation['hero'], observation['units']
    noop = {'type': int(ActionType.NOOP), 'move': 0, 'target': 0, 'ability': 0}
    if not mask['type'][ActionType.MOVE]:
        return noop
    x, y = hero[H['x']] * MAP_SCALE, hero[H['y']] * MAP_SCALE
    lane_x, lane_y = LANE_POSITION[team_id]
    rows = np.flatnonzero(observation['unit_mask'])

    # retreat when low
    if hero[H['health_frac']] < 0.3:
        sign = -1 if team_id == dota2_env.TEAM_RADIANT else 1
        return move_towards(sign, sign)

    # raze an enemy hero standing in one of the three raze circles in front of us
    for row in rows:
        unit = units[row]
        if unit[U['is_enemy']] and unit[U['is_hero']]:
            dx, dy = unit[U['rel_x']] * UNIT_RADIUS, unit[U['rel_y']] * UNIT_RADIUS
            forward = dx * hero[H['facing_cos']] + dy * hero[H['facing_sin']]
            sideways = abs(-dx * hero[H['facing_sin']] + dy * hero[H['facing_cos']])
            for slot, raze_range in enumerate(RAZE_RANGES):
                if (
                    mask['type'][ActionType.CAST]
                    and mask['ability'][ActionType.CAST][slot]
                    and sideways < 120
                    and abs(forward - raze_range) < RAZE_RADIUS - 100
                ):
                    return dict(noop, type=int(ActionType.CAST), ability=slot)

    # last hits and denies
    killable = [
        row
        for row in rows
        if mask['attack_target'][row] and units[row][U['is_lane_creep']] and units[row][U['hits_to_kill']] * 20 <= 1.2
    ]
    if killable and mask['type'][ActionType.ATTACK]:
        target = min(killable, key=lambda row: units[row][U['distance']])
        return dict(noop, type=int(ActionType.ATTACK), target=int(target))

    # otherwise hold position slightly behind our creeps, or at the lane spot while waiting for the wave
    if math.hypot(lane_x - x, lane_y - y) > 250:
        return move_towards(lane_x - x, lane_y - y)
    return noop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-steps', type=int, default=3000)
    parser.add_argument('--timescale', type=float, default=4.0)
    parser.add_argument('--opponent', default='builtin', choices=['builtin', 'idle'])
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--team', default='radiant', choices=['radiant', 'dire'])
    parser.add_argument(
        '--replay', nargs='?', const='replays', metavar='DIR', help='record the match into DIR (default: replays/)'
    )
    args = parser.parse_args()

    env = gym.make(
        'dota2_env/Mid1v1-v0',
        render_mode='human' if args.render else 'ansi',
        timescale=args.timescale,
        opponent=args.opponent,
        team_id=dota2_env.TEAM_RADIANT if args.team == 'radiant' else dota2_env.TEAM_DIRE,
        replay_dir=args.replay,
    )
    team_id = env.unwrapped.team_id
    try:
        observation, info = env.reset()
        print(f'spawned at ({observation["hero"][0] * MAP_SCALE:.0f}, {observation["hero"][1] * MAP_SCALE:.0f})')
        totals, episode_return, start = {}, 0.0, time.time()
        for step in range(1, args.max_steps + 1):
            action = policy(observation, info['action_mask'], team_id)
            observation, reward, terminated, truncated, info = env.step(action)
            episode_return += reward
            for key, value in info['reward'].items():
                totals[key] = totals.get(key, 0.0) + value
            if step % 500 == 0 or terminated or truncated:
                print(f'\n--- step {step} return {episode_return:.2f} ({step / (time.time() - start):.1f} steps/s) ---')
                print(env.render())
            if terminated or truncated:
                print(f'episode over: terminated={terminated} truncated={truncated} winner={info["winner"]}')
                break
        print('reward components (unweighted sums):', {k: round(v, 2) for k, v in totals.items()})
    finally:
        env.close()
        if env.unwrapped.replay_path is not None:
            print(f'replay: {env.unwrapped.replay_path}')


if __name__ == '__main__':
    main()
