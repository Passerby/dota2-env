"""Probe: are the tree ids of dota2_env/data/map.json the ids that world-state tree events carry?

    python scripts/probe_trees.py [--timescale 4] [--seconds 400]

Starts a headless 1v1 in which the Radiant hero eats the standing tree of map.json nearest to it with a
tango (Action_UseAbilityOnTree, by bot API id): the destroyed event has to carry that id at that tree's
location and clear the tree channel of the local map. It then plants an Iron Branch, to see what a
temporary tree looks like, and walks back to where it started, because a tree does not grow back while
a unit stands on its spot. Once the tree should have regrown it eats the same tree again: a second
destroyed event means the tree came back, whether or not a respawned event said so. Every tree event is
printed; the findings are written up in docs/MAP_DATA.md.
"""

import argparse
import json
import math

from dota2_env.actions import purchase_item
from dota2_env.bridge.constants import DOTA_GAMEMODE_1V1MID, HOST_MODE_DEDICATED, TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.game import CONTROL_AGENT, CONTROL_IDLE
from dota2_env.bridge.session import DotaSession
from dota2_env.map_features import MAP_FEATURES, MAP_PATH, MAP_RADIUS, TreeTable, load_map, map_arrays
from dota2_env.observation import find_hero

HERO = 'npc_dota_hero_nevermore'
ITEM_IDS = {'item_tango': 44, 'item_branches': 16}  # the datafeed's item ids, which the world state reports
TREE = MAP_FEATURES.index('tree')
REGROWTH = 180.0  # game seconds until a destroyed tree grows back, according to Liquipedia


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--timescale', type=float, default=4.0)
    parser.add_argument('--seconds', type=float, default=400.0, help='game seconds to watch once the tree is eaten')
    args = parser.parse_args()
    with open(MAP_PATH, encoding='utf-8') as handle:
        trees = json.load(handle)['trees']
    static = load_map()
    alone = (static.tree_counts[static.tree_cells[..., 0], static.tree_cells[..., 1]] == 1).all(axis=1)
    session = DotaSession(
        TEAM_RADIANT,
        host_mode=HOST_MODE_DEDICATED,
        game_mode=DOTA_GAMEMODE_1V1MID,
        host_timescale=args.timescale,
        ticks_per_observation=6,
        heroes={TEAM_RADIANT: (HERO,), TEAM_DIRE: (HERO,)},
        control={TEAM_RADIANT: CONTROL_AGENT, TEAM_DIRE: CONTROL_IDLE},
    )
    table = TreeTable()
    target, eaten_at, planted_at, bought, last_order, home = None, None, None, False, -1e9, None
    results = {
        'eaten': False,
        'location_error': None,
        'channel_cleared': None,
        'branch_used': None,
        'respawned_after': None,
        'eaten_again_after': None,
        'temporary': [],
    }
    try:
        session.start()
        while True:
            frames = session.observe(timeout=120)
            for world_state in frames:
                table.update(world_state)
            now = world_state.dota_time
            for event in (event for frame in frames for event in frame.tree_events):
                known = event.tree_id < len(trees)
                print(
                    f'{now:8.1f} tree {event.tree_id} destroyed={event.destroyed} respawned={event.respawned} '
                    f'delayed={event.delayed} at ({event.location.x:.0f}, {event.location.y:.0f}) '
                    f'{"in map.json" if known else "temporary"}',
                    flush=True,
                )
                if not known:
                    results['temporary'].append([now, event.tree_id, event.destroyed, event.respawned])
                elif event.tree_id == target and event.respawned:
                    results['respawned_after'] = now - eaten_at
                elif event.tree_id == target and event.destroyed and eaten_at is not None:
                    results['eaten_again_after'] = now - eaten_at
                elif event.tree_id == target and event.destroyed:
                    eaten_at, results['eaten'] = now, True
                    x, y = trees[target][1:3]
                    results['location_error'] = math.dist((x, y), (event.location.x, event.location.y))
                    local = map_arrays(table, world_state, TEAM_RADIANT, x, y)['local_map'][TREE]
                    corner = local[MAP_RADIUS - 1 : MAP_RADIUS + 1, MAP_RADIUS - 1 : MAP_RADIUS + 1]
                    results['channel_cleared'] = not corner.any()
            hero = find_hero(world_state, TEAM_RADIANT)
            if hero is None:
                continue
            player = hero.player_id
            home = home or (hero.location.x, hero.location.y)
            slots = {item.ability_id: item.slot for item in hero.items}
            order = {'actionType': 'DOTA_UNIT_ORDER_NONE', 'player': player}
            extras = []
            if not bought:
                extras, bought = [purchase_item(player, name) for name in ITEM_IDS], True
            elif target is None and ITEM_IDS['item_tango'] in slots:
                # a tree that shares no cell with another one, so that its cells have to empty
                here = (hero.location.x, hero.location.y)
                standing = (tree for tree in trees if table.standing[tree[0]] and alone[tree[0]])
                target = min(standing, key=lambda tree: math.dist(tree[1:3], here))[0]
                print(f'{now:8.1f} eating tree {target} at {trees[target][1:3]}', flush=True)
            eat_again = eaten_at is not None and now - eaten_at > REGROWTH + 60 and results['eaten_again_after'] is None
            if target is not None and (eaten_at is None or eat_again) and now - last_order > 3:
                slot = -(slots[ITEM_IDS['item_tango']] + 1)
                order |= {
                    'actionType': 'DOTA_UNIT_ORDER_CAST_TARGET_TREE',
                    'castTree': {'abilitySlot': slot, 'tree': target},
                }
                last_order = now
            elif eaten_at is not None and planted_at is None and ITEM_IDS['item_branches'] in slots:
                location = {'x': hero.location.x + 150.0, 'y': hero.location.y, 'z': hero.location.z}
                slot = -(slots[ITEM_IDS['item_branches']] + 1)
                order |= {
                    'actionType': 'DOTA_UNIT_ORDER_CAST_POSITION',
                    'castLocation': {'abilitySlot': slot, 'location': location},
                }
                planted_at = now
            elif planted_at is not None and not eat_again and now - last_order > 3:
                location = {'x': home[0], 'y': home[1], 'z': hero.location.z}
                order |= {'actionType': 'DOTA_UNIT_ORDER_MOVE_DIRECTLY', 'moveDirectly': {'location': location}}
                last_order = now
            if planted_at is not None:
                results['branch_used'] = ITEM_IDS['item_branches'] not in slots
            session.act(now, [order], extras)
            if eaten_at is not None and (now - eaten_at > args.seconds or results['eaten_again_after'] is not None):
                break
            if target is not None and eaten_at is None and now > 120:
                print('the tango never ate the tree', flush=True)
                break
    finally:
        session.close()
    print(json.dumps(results), flush=True)
    ok = results['eaten'] and results['location_error'] < 1 and results['channel_cleared']
    print('tree ids match' if ok else 'TREE IDS DO NOT MATCH', flush=True)
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
