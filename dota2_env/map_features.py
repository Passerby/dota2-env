"""Static map data (dota2_env/data/map.json) and the map part of an observation.

map.json is extracted from the installed client by scripts/extract_map.py (docs/MAP_DATA.md). Its
tree ids are the bot API's, which are also the ids that world-state tree events carry.
"""

import functools
import json
import logging
import math
import os
from dataclasses import dataclass

import numpy as np

from dota2_env.bridge.constants import UNIT_TYPE_BUILDING
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState

logger = logging.getLogger('dota2_env')

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
MAP_PATH = os.path.join(DATA_DIR, 'map.json')
MAP_SCALE = 8192.0
MAP_RADIUS = 16  # cells on either side of the hero's own: the local map is 33 x 33 cells of 64 units
SPOT_RADIUS = 200.0  # a rune or building this close to a spot of map.json is the one standing on it
GRIDNAV_TRAVERSABLE = 0x1
RUNE_STATUS_AVAILABLE = 1  # the bot API's value, checked against the client by scripts/extract_map.py

MAP_FEATURES = ('blocked', 'tree', 'height')
RUNE_SPOTS = ('power_top', 'power_bottom', 'bounty_top', 'bounty_bottom')
RUNE_FEATURES = ('rel_x', 'rel_y', 'distance', 'available')
LANDMARKS = (
    'roshan_top',
    'roshan_bottom',
    'tormentor_top',
    'tormentor_bottom',
    'wisdom_top',
    'wisdom_bottom',
    'lotus_top',
    'lotus_bottom',
    'outpost_top',
    'outpost_bottom',
)
LANDMARK_FEATURES = ('rel_x', 'rel_y', 'distance', 'is_ours')


@dataclass(kw_only=True, frozen=True)
class StaticMap:
    """map.json as arrays. Grids grow northwards by row and eastwards by column, padded by MAP_RADIUS."""

    client_version: int
    x0: float  # world x of the west edge of the unpadded grid
    y0: float  # world y of its south edge
    cell_size: float
    blocked: np.ndarray  # float32, 1 where the gridnav cannot be walked; the padding is blocked
    height: np.ndarray  # float32 terrain step per cell, higher is higher; the padding repeats the map edge
    tree_counts: np.ndarray  # int16, trees covering each cell at the start of a match
    tree_cells: np.ndarray  # (trees, 4, 2) padded (row, col) of the four cells each tree covers
    tree_positions: np.ndarray  # (trees, 2) x, y
    runes: np.ndarray  # (len(RUNE_SPOTS), 2) x, y
    landmarks: np.ndarray  # (len(LANDMARKS), 2) x, y
    buildings: tuple[tuple[str, int, float, float], ...]  # name, team, x, y as the match starts
    lanes: dict[str, np.ndarray]  # top / mid / bot: (21, 2) x, y along the lane, from the Radiant fountain on
    world_bounds: tuple[float, float, float, float]  # min x, min y, max x, max y a unit can be ordered to

    def cell(self, x: float, y: float) -> tuple[int, int]:
        """Padded (row, col) of the cell holding world point x, y."""
        return int((y - self.y0) // self.cell_size) + MAP_RADIUS, int((x - self.x0) // self.cell_size) + MAP_RADIUS


@functools.cache
def load_map(path: str = MAP_PATH) -> StaticMap:
    """Read map.json once per process; the arrays are shared, so they are read-only."""
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    grid = data['grid']
    cell_size, x0, y0 = grid['cell_size'], grid['x0'], grid['y0']
    flags = np.frombuffer(bytes.fromhex(''.join(data['gridnav'])), np.uint8).reshape(grid['height'], grid['width'])
    # Note (ruidu): trees are not part of the gridnav (9,112 of the 9,886 cells under a tree are plain
    # traversable on 6934), so the gridnav decides what is blocked and the tree channel adds the trees.
    blocked = np.pad((flags & GRIDNAV_TRAVERSABLE) == 0, MAP_RADIUS, constant_values=True).astype(np.float32)
    # GetHeightLevel counts downwards (1 is the fountain terrace, 4 the river), so its negative rises with
    # the ground, one step per 128 units of terrain height.
    height = -np.array([[float(level) for level in row] for row in data['height_level']], np.float32)
    height = np.pad(height, MAP_RADIUS, mode='edge')

    trees = np.array(data['trees'], np.float64)
    assert (trees[:, 0] == np.arange(len(trees))).all(), 'tree ids must run from 0 without gaps'
    # A tree stands on a cell corner and covers the 2 x 2 cells that meet there.
    rows = ((trees[:, 2] - y0) // cell_size).astype(int) + MAP_RADIUS
    cols = ((trees[:, 1] - x0) // cell_size).astype(int) + MAP_RADIUS
    tree_cells = np.stack([np.stack([rows - dr, cols - dc], axis=1) for dr in (0, 1) for dc in (0, 1)], axis=1)
    tree_counts = np.zeros(blocked.shape, np.int16)
    np.add.at(tree_counts, (tree_cells[:, :, 0].ravel(), tree_cells[:, :, 1].ravel()), 1)

    runes = np.array([data['runes'][name][:2] for name in RUNE_SPOTS], np.float32)
    landmarks = np.array([data['landmarks'][name][:2] for name in LANDMARKS], np.float32)
    tree_positions = trees[:, 1:3].astype(np.float32)
    lanes = {name: np.array(points, np.float32) for name, points in data['lanes'].items()}
    min_x, min_y, max_x, max_y = (float(bound) for bound in data['world_bounds'])
    for array in (blocked, height, tree_counts, tree_cells, tree_positions, runes, landmarks, *lanes.values()):
        array.setflags(write=False)
    return StaticMap(
        client_version=data['client_version'],
        x0=x0,
        y0=y0,
        cell_size=cell_size,
        blocked=blocked,
        height=height,
        tree_counts=tree_counts,
        tree_cells=tree_cells,
        tree_positions=tree_positions,
        runes=runes,
        landmarks=landmarks,
        buildings=tuple(
            (building['name'], building['team'], building['x'], building['y']) for building in data['buildings']
        ),
        lanes=lanes,
        world_bounds=(min_x, min_y, max_x, max_y),
    )


def warn_if_stale(dota_path: str) -> None:
    """Warn when the installed client is not the build map.json was extracted from."""
    steam_inf = os.path.join(dota_path, 'dota', 'steam.inf')
    if not os.path.isfile(steam_inf):
        return  # no Dota install, as in the tests
    with open(steam_inf, encoding='utf-8') as handle:
        installed = dict(line.strip().split('=', 1) for line in handle if '=' in line)
    extracted = load_map().client_version
    if int(installed['ClientVersion']) != extracted:
        logger.warning(
            f'map.json comes from client {extracted} but client {installed["ClientVersion"]} is installed; '
            'trees and landmarks may have moved, rerun scripts/extract_map.py (docs/MAP_DATA.md)'
        )


class TreeTable:
    """The trees one team knows to stand: map.json's at the start, then every tree event applied.

    Events follow the team's vision: a tree that falls or grows back out of sight is reported once the
    team sees its spot again (with delayed set), so the table is what the team knows, not the truth.
    """

    def __init__(self) -> None:
        static = load_map()
        self.standing = np.ones(len(static.tree_cells), bool)
        self.counts = static.tree_counts.copy()

    def update(self, world_state: CMsgBotWorldState) -> None:
        cells = load_map().tree_cells
        for event in world_state.tree_events:
            # Note (ruidu): ids past map.json's trees would be temporary trees (Sprout, a planted Iron
            # Branch); a planted branch produced no event at all on 6934 (docs/MAP_DATA.md), so any that
            # do turn up are left out.
            if event.tree_id >= len(self.standing):
                continue
            if event.destroyed:
                standing = False
            elif event.respawned:
                standing = True
            else:
                continue
            if standing == self.standing[event.tree_id]:
                continue  # an event seen twice must not count twice
            self.standing[event.tree_id] = standing
            self.counts[cells[event.tree_id, :, 0], cells[event.tree_id, :, 1]] += 1 if standing else -1


def map_arrays(
    trees: TreeTable, world_state: CMsgBotWorldState, team_id: int, x: float, y: float
) -> dict[str, np.ndarray]:
    """The local_map, runes and landmarks arrays of an observation centred on world point x, y."""
    static = load_map()
    row, col = static.cell(x, y)
    window = np.s_[row - MAP_RADIUS : row + MAP_RADIUS + 1, col - MAP_RADIUS : col + MAP_RADIUS + 1]
    local_map = np.stack(
        [static.blocked[window], trees.counts[window] > 0, static.height[window] - static.height[row, col]]
    )
    runes = [(r.location.x, r.location.y) for r in world_state.rune_infos if r.status == RUNE_STATUS_AVAILABLE]
    ours = [
        (u.location.x, u.location.y)
        for u in world_state.units
        if u.unit_type == UNIT_TYPE_BUILDING and u.team_id == team_id
    ]

    def spot_rows(spots: np.ndarray, present: list[tuple[float, float]]) -> np.ndarray:
        offsets = (spots - (x, y)) / MAP_SCALE
        flags = [any(math.dist(spot, point) < SPOT_RADIUS for point in present) for spot in spots]
        return np.column_stack([offsets, np.hypot(offsets[:, 0], offsets[:, 1]), flags]).astype(np.float32)

    return {
        'local_map': local_map.astype(np.float32),
        'runes': spot_rows(static.runes, runes),
        'landmarks': spot_rows(static.landmarks, ours),
    }
