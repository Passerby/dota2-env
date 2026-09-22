"""A hand-written 5v5 policy that only uses the gym observation: walk the assigned lane towards the
enemy ancient, last-hit, and attack whatever comes into range. A sanity check of the 5v5 spaces.

    python examples/scripted_5v5.py --timescale 4 --max-steps 3000
"""

import argparse
import math
import time

import gymnasium as gym
import numpy as np

import dota2_env
from dota2_env.actions import N_MOVE_DIRECTIONS, ActionType
from dota2_env.observation import (
    HERO_FEATURES,
    MAP_SCALE,
    N_ABILITIES,
    TEAM_FEATURES,
    TEAM_SIZE,
    UNIT_FEATURES,
    UNIT_RADIUS,
    find_hero,
)

H = {name: i for i, name in enumerate(HERO_FEATURES)}
U = {name: i for i, name in enumerate(UNIT_FEATURES)}
T = {name: i for i, name in enumerate(TEAM_FEATURES)}

# Lane paths from own tier-2 to the enemy ancient, taken from the tower coordinates the client
# reports. A hero advances to the next waypoint once it is within WAYPOINT_RADIUS of the current
# one, and stops advancing while an enemy tower is close, which parks it at the lane equilibrium.
LANE_PATHS = {
    dota2_env.TEAM_RADIANT: {
        'mid': ((-3190, -2926), (-1544, -1408), (524, 652), (2496, 2112), (4272, 3759), (5528, 5000)),
        'top': ((-6501, -872), (-6336, 1856), (-5275, 6036), (-128, 6016), (3552, 5776), (5528, 5000)),
        'bot': ((-360, -6256), (4860, -6379), (6269, -2240), (6400, 384), (6336, 3032), (5528, 5000)),
    },
    dota2_env.TEAM_DIRE: {
        'mid': ((2496, 2112), (524, 652), (-1544, -1408), (-3190, -2926), (-4640, -4144), (-5920, -5352)),
        'top': ((-128, 6016), (-5275, 6036), (-6336, 1856), (-6501, -872), (-6592, -3408), (-5920, -5352)),
        'bot': ((6400, 384), (6269, -2240), (4860, -6379), (-360, -6256), (-3952, -6112), (-5920, -5352)),
    },
}
LANES = ('mid', 'bot', 'bot', 'top', 'top')  # hero row -> lane, the same split Valve's bots use
WAYPOINT_RADIUS = 450.0
TOWER_KEEPOUT = 900.0  # tower range is 700
RETREAT_HEALTH_FRACTION = 0.25


def move_towards(dx, dy):
    return round(math.atan2(dy, dx) / (2 * math.pi) * N_MOVE_DIRECTIONS) % N_MOVE_DIRECTIONS


def hero_policy(hero, units, unit_mask, mask, home, goal):
    """-> (type, move, target, ability) for one hero, walking from home towards goal."""
    if not mask['type'][ActionType.MOVE]:
        return int(ActionType.NOOP), 0, 0, 0
    x, y = hero[H['x']] * MAP_SCALE, hero[H['y']] * MAP_SCALE
    rows = np.flatnonzero(unit_mask)

    towers = [row for row in rows if units[row][U['is_enemy']] and units[row][U['is_tower']]]
    hurt = hero[H['health_frac']] < RETREAT_HEALTH_FRACTION
    under_tower = any(units[row][U['distance']] * UNIT_RADIUS < TOWER_KEEPOUT for row in towers)
    if hurt or under_tower:
        return int(ActionType.MOVE), move_towards(home[0] - x, home[1] - y), 0, 0

    # nuke a hero standing in front of us, then last-hit, then attack anything else in range
    enemy_heroes = [row for row in rows if units[row][U['is_enemy']] and units[row][U['is_hero']]]
    if enemy_heroes and mask['type'][ActionType.CAST_TARGET]:
        target = min(enemy_heroes, key=lambda row: units[row][U['distance']])
        slots = np.flatnonzero(mask['ability'][ActionType.CAST_TARGET][:N_ABILITIES])  # skills only, no items
        if mask['cast_target'][target] and len(slots) and units[target][U['distance']] * UNIT_RADIUS < 600:
            return int(ActionType.CAST_TARGET), 0, int(target), int(slots[0])

    if mask['type'][ActionType.ATTACK]:
        reachable = [row for row in rows if mask['attack_target'][row] and not units[row][U['is_tower']]]
        killable = [row for row in reachable if units[row][U['hits_to_kill']] * 20 <= 1.2]
        in_range = [row for row in reachable if units[row][U['in_attack_range']]]
        candidates = killable or in_range
        if candidates:
            target = min(candidates, key=lambda row: units[row][U['distance']])
            return int(ActionType.ATTACK), 0, int(target), 0

    return int(ActionType.MOVE), move_towards(goal[0] - x, goal[1] - y), 0, 0


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
        'dota2_env/AllPick5v5-v0',
        render_mode='human' if args.render else 'ansi',
        timescale=args.timescale,
        opponent=args.opponent,
        team_id=dota2_env.TEAM_RADIANT if args.team == 'radiant' else dota2_env.TEAM_DIRE,
        replay_dir=args.replay,
    )
    team_id = env.unwrapped.team_id
    paths = [list(LANE_PATHS[team_id][lane]) for lane in LANES]
    try:
        observation, info = env.reset()
        picked = [find_hero(info['world_state'], team_id, player_id) for player_id in info['player_ids']]
        names = [hero.name.replace('npc_dota_hero_', '') if hero else '?' for hero in picked]
        print(f'player ids {info["player_ids"]}, heroes {names}, lanes {LANES}')
        totals, episode_return, start = {}, 0.0, time.time()
        for step in range(1, args.max_steps + 1):
            action = {key: np.zeros(TEAM_SIZE, np.int64) for key in ('type', 'move', 'target', 'ability')}
            for row in range(TEAM_SIZE):
                hero = observation['heroes'][row]
                x, y = hero[H['x']] * MAP_SCALE, hero[H['y']] * MAP_SCALE
                if len(paths[row]) > 1 and math.hypot(paths[row][0][0] - x, paths[row][0][1] - y) < WAYPOINT_RADIUS:
                    paths[row].pop(0)
                mask = {key: values[row] for key, values in info['action_mask'].items()}
                home = LANE_PATHS[team_id][LANES[row]][0]
                units, unit_mask = observation['units'][row], observation['unit_mask'][row]
                values = hero_policy(hero, units, unit_mask, mask, home, paths[row][0])
                for key, value in zip(('type', 'move', 'target', 'ability'), values, strict=True):
                    action[key][row] = value

            observation, reward, terminated, truncated, info = env.step(action)
            episode_return += reward
            for key, value in info['reward'].items():
                totals[key] = totals.get(key, 0.0) + value
            if step % 250 == 0 or terminated or truncated:
                team = observation['team']
                heroes = observation['heroes']
                print(
                    'step {:5d} t {:7.1f} return {:7.2f} lvl {:4.0f} lh {:4.0f} alive {:.0f}/{:.0f} '
                    'towers {:.0f}/{:.0f} ({:.1f} steps/s)'.format(
                        step,
                        info['dota_time'],
                        episode_return,
                        heroes[:, H['level']].sum() * 30,
                        heroes[:, H['last_hits']].sum() * 100,
                        team[T['heroes_alive']] * TEAM_SIZE,
                        team[T['enemy_heroes_visible']] * TEAM_SIZE,
                        team[T['towers']] * 11,
                        team[T['enemy_towers']] * 11,
                        step / (time.time() - start),
                    )
                )
            if terminated or truncated:
                print(f'episode over: terminated={terminated} truncated={truncated} winner={info["winner"]}')
                break
        print('reward components (unweighted sums):', {k: round(v, 2) for k, v in totals.items()})
        print('action delivery:', info['action_delivery'], 'skipped observations:', info['skipped_observations'])
    finally:
        env.close()
        if env.unwrapped.replay_path is not None:
            print(f'replay: {env.unwrapped.replay_path}')


if __name__ == '__main__':
    main()
