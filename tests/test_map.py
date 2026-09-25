import json
import os
import re
from typing import get_args

import numpy as np
import pytest

from dota2_env.bridge.constants import TEAM_RADIANT
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.map_features import (
    DATA_DIR,
    LANDMARKS,
    MAP_FEATURES,
    MAP_RADIUS,
    RUNE_FEATURES,
    RUNE_SPOTS,
    GameMode,
    MapEvent,
    TreeTable,
    load_events,
    load_map,
    map_arrays,
    upcoming,
)


def data_file(name: str) -> dict:
    with open(os.path.join(DATA_DIR, name), encoding='utf-8') as handle:
        return json.load(handle)


def test_mirrored_pairs_are_named_after_their_side_of_the_mid_lane():
    static = load_map()
    for name, (x, y) in zip(RUNE_SPOTS + LANDMARKS, np.concatenate([static.runes, static.landmarks]), strict=True):
        assert (y > x) == name.endswith('_top'), name


def test_each_pair_mirrors_through_the_map_centre():
    # Note (ruidu): the map is only roughly point-symmetric; the outposts are furthest off, at about 1,140.
    spots = np.concatenate([load_map().runes, load_map().landmarks])
    for top, bottom in zip(spots[::2], spots[1::2], strict=True):
        assert np.hypot(*(top + bottom)) < 1500


def test_heroes_can_walk_to_the_runes_and_the_fountain_stands_above_the_river():
    static = load_map()
    assert not any(static.blocked[static.cell(x, y)] for x, y in static.runes)
    power_top = static.runes[RUNE_SPOTS.index('power_top')]
    assert static.height[static.cell(-7456, -6938)] > static.height[static.cell(*power_top)]


def test_tree_table_applies_each_event_once():
    static, table = load_map(), TreeTable()
    rows, cols = static.tree_cells[7, :, 0], static.tree_cells[7, :, 1]
    cut = CMsgBotWorldState()
    cut.tree_events.add(tree_id=7, destroyed=True)
    cut.tree_events.add(tree_id=7, destroyed=True)  # the same tree reported twice
    cut.tree_events.add(tree_id=len(table.standing) + 3, destroyed=True)  # a temporary tree
    table.update(cut)
    assert not table.standing[7] and table.standing.sum() == len(table.standing) - 1
    assert (table.counts[rows, cols] == static.tree_counts[rows, cols] - 1).all()
    regrown = CMsgBotWorldState()
    regrown.tree_events.add(tree_id=7, respawned=True)
    table.update(regrown)
    assert table.standing.all() and (table.counts == static.tree_counts).all()


def test_local_map_is_centred_on_the_hero_and_blocked_past_the_map_edge():
    static, table = load_map(), TreeTable()
    x, y = static.runes[RUNE_SPOTS.index('power_top')]
    arrays = map_arrays(table, CMsgBotWorldState(), TEAM_RADIANT, x, y)
    assert arrays['local_map'][MAP_FEATURES.index('height'), MAP_RADIUS, MAP_RADIUS] == 0
    assert arrays['runes'][RUNE_SPOTS.index('power_top'), RUNE_FEATURES.index('distance')] == 0
    corner = map_arrays(table, CMsgBotWorldState(), TEAM_RADIANT, static.x0 + 1, static.y0 + 1)['local_map']
    assert corner[MAP_FEATURES.index('blocked'), :MAP_RADIUS].all()  # the rows south of the map


@pytest.mark.parametrize('name', ['map.json', 'items.json', 'abilities.json', 'heroes.json', 'events.json'])
def test_data_files_name_their_patch(name):
    assert re.fullmatch(r'7\.\d+[a-z]?', data_file(name)['patch'])


def test_an_event_comes_at_its_listed_times_or_every_interval_from_its_first():
    bounty = MapEvent(kind='bounty', spots=('bounty_top', 'bounty_bottom'), first=240, every=240)
    assert [bounty.next_time(t) for t in (-90.0, 0.0, 239.9, 240.0, 250.0)] == [240, 240, 240, 480, 480]
    water = MapEvent(kind='water', spots=('power_top', 'power_bottom'), times=(120, 240))
    assert [water.next_time(t) for t in (0.0, 120.0, 239.9, 240.0)] == [120, 240, 240, None]


@pytest.mark.parametrize('mode', get_args(GameMode))
def test_every_kind_comes_next_once_soonest_first_to_spots_of_its_own(mode):
    for event in load_events(mode):
        runes = event.kind in ('bounty', 'water', 'power')
        own = RUNE_SPOTS if runes else tuple(spot for spot in LANDMARKS if spot.startswith(event.kind))
        assert event.spots and set(event.spots) <= set(own), event
        # listed times or a first time and an interval, never both
        assert (event.first is None, event.every is None) == ((True, True) if event.times else (False, False)), event
    for dota_time in (-90.0, 0.0, 120.0, 419.5, 1000.0):
        coming = upcoming(mode, dota_time)
        times, kinds = [time for time, _ in coming], [event.kind for _, event in coming]
        assert times == sorted(times) and all(time > dota_time for time in times)
        assert len(kinds) == len(set(kinds))
