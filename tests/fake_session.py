"""A tiny stand-in for DotaSession: simulates just enough of a 1v1 lane to exercise the env without Dota."""

import queue
from typing import ClassVar

import numpy as np

from dota2_env.bridge.constants import (
    DOTA_GAMERULES_STATE_PRE_GAME,
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_BUILDING,
    UNIT_TYPE_COURIER,
    UNIT_TYPE_FORT,
    UNIT_TYPE_HERO,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_TOWER,
)
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import (
    CMsgBotWorldState,
)
from dota2_env.bridge.session import ActionDelivery
from dota2_env.game_text import load_records
from dota2_env.map_features import LANDMARKS, MAP_RADIUS, RUNE_SPOTS, RUNE_STATUS_AVAILABLE, load_map
from dota2_env.observation import STASH_SLOTS, TEAM_SIZE, TP_SLOT

HERO_HANDLE, ENEMY_HERO_HANDLE, ENEMY_CREEP_HANDLE, ALLY_CREEP_HANDLE = 1, 2, 10, 11
HERO_DAMAGE = 50
SKILL_CAST_RANGE = 700
# what lua's SLOTS line reports for every fake hero: a no-target skill in slot 0 and a tango in inventory slot 0
CAST_SLOTS: dict[int, tuple[str, tuple[str, ...]]] = {
    0: ('nevermore_shadowraze1', ('no_target',)),
    6: ('item_tango', ('self',)),
}


RUNE_STATUS_MISSING = 2
COURIER_HANDLE = 20
# Shadow Fiend's talents in their client slots, 7-14 on 6937 (necromastery, the innate skill, sits in 6)
TALENT_IDS = [
    load_records('abilities')[name]['id'] for name in load_records('heroes')['npc_dota_hero_nevermore']['talents']
]
TALENT_SLOT = 7
OUTPOST_HANDLES = {'outpost_top': 700, 'outpost_bottom': 701}
# The static tree nearest the fake hero's start whose four cells no other tree covers, for the tree tests.
TREE_OFFSETS = load_map().tree_cells.mean(axis=1) - load_map().cell(-1500.0, -1400.0)
TREE_ALONE = (load_map().tree_counts[load_map().tree_cells[..., 0], load_map().tree_cells[..., 1]] == 1).all(axis=1)
LONE_TREE = int(np.argmin(np.where(TREE_ALONE, np.hypot(TREE_OFFSETS[:, 0], TREE_OFFSETS[:, 1]), np.inf)))


def lone_tree_cells(hero_x: float, hero_y: float) -> tuple[np.ndarray, np.ndarray]:
    """Rows and columns of LONE_TREE's four cells in a local map centred on hero_x, hero_y."""
    static = load_map()
    row, col = static.cell(hero_x, hero_y)
    cells = static.tree_cells[LONE_TREE]
    return cells[:, 0] - row + MAP_RADIUS, cells[:, 1] - col + MAP_RADIUS


def add_map_state(
    ws: CMsgBotWorldState,
    available_runes: set[str],
    rune_types: dict[str, int],
    outpost_teams: dict[str, int],
    tree_events: list[tuple[int, bool]],
) -> None:
    """What every real frame carries about the map: the rune spots, both outposts and the tree events since the last."""
    static = load_map()
    for name, (x, y) in zip(RUNE_SPOTS, static.runes, strict=True):
        status = RUNE_STATUS_AVAILABLE if name in available_runes else RUNE_STATUS_MISSING
        rune = ws.rune_infos.add(type=rune_types.get(name, -1), status=status)
        rune.location.x, rune.location.y = x, y
    for name, team in outpost_teams.items():
        x, y = static.landmarks[LANDMARKS.index(name)]
        _add_unit(ws, OUTPOST_HANDLES[name], UNIT_TYPE_BUILDING, team, '#DOTA_OutpostName', x, y, 1000)
    for tree_id, destroyed in tree_events:
        ws.tree_events.add(tree_id=tree_id, destroyed=destroyed, respawned=not destroyed)
    tree_events.clear()


def _add_unit(ws, handle, unit_type, team, name, x, y, health, health_max=None, **fields):
    unit = ws.units.add(
        handle=handle,
        unit_type=unit_type,
        team_id=team,
        name=name,
        health=health,
        health_max=health_max or health,
        is_alive=health > 0,
        **fields,
    )
    unit.location.x, unit.location.y, unit.location.z = x, y, 0
    return unit


class FakeSession:
    instances: ClassVar[list['FakeSession']] = []

    def __init__(self, team_id, keep_files=False, replay_dir=None, **game_kwargs):
        self.team_id = team_id
        self.game_kwargs = game_kwargs
        self.replay_dir = replay_dir
        self.replay_path = None
        self.sent = []  # (dota_time, actions, extra_actions)
        self.closed = False
        self.dota_time = -90.0
        self.hero_xy = [-1500.0, -1400.0]
        self.creep_health = 120
        self.last_hits = 0
        self.kills = 0
        self.deaths = 0
        self.hero_hidden = False  # like a freshly respawned hero on current clients
        self.hero_flags = {}  # fields of our hero unit, e.g. is_silenced, level or reliable_gold
        self.learned_talents = set()  # talent indices, as the TALENT action counts them
        self.tp = None  # (charges, cooldown) of the scroll in the TP slot; None leaves the slot empty
        self.stash = []  # item ids waiting in the stash
        self.courier_items = []  # item ids the courier carries
        self.courier_alive = True
        self.cast_slots = {0: dict(CAST_SLOTS)}
        self.feed_ended = False
        self.winner = None
        self.delivery = ActionDelivery()
        self.skipped_observations = 0
        self.ack_status = 'executed'  # None = lua never sees the file
        self.spawn_delay = 2  # observations without heroes, like the real client
        self.available_runes = set()  # RUNE_SPOTS names with a rune on them
        self.rune_types = {}  # RUNE_SPOTS name: the world state's rune type, -1 (unknown) where not given
        self.outpost_teams = {'outpost_top': TEAM_RADIANT, 'outpost_bottom': TEAM_DIRE}  # as a match starts
        self.tree_events = []  # (tree id, destroyed) to report with the next frame
        FakeSession.instances.append(self)

    def start(self):
        pass

    def observe(self, timeout):
        if self.feed_ended:
            raise queue.Empty
        return [self.frame()]

    def frame(self):
        ws = CMsgBotWorldState(team_id=self.team_id, dota_time=self.dota_time, game_state=DOTA_GAMERULES_STATE_PRE_GAME)
        self.dota_time += 0.2
        ws.players.add(player_id=0, team_id=TEAM_RADIANT, kills=self.kills, deaths=self.deaths, is_alive=True)
        ws.players.add(player_id=5, team_id=TEAM_DIRE, is_alive=True)
        _add_unit(ws, 100, UNIT_TYPE_TOWER, TEAM_RADIANT, 'npc_dota_goodguys_tower1_mid', -1544, -1408, 1800)
        _add_unit(ws, 101, UNIT_TYPE_TOWER, TEAM_DIRE, 'npc_dota_badguys_tower1_mid', 524, 652, 1800)
        add_map_state(ws, self.available_runes, self.rune_types, self.outpost_teams, self.tree_events)
        if self.spawn_delay > 0:
            self.spawn_delay -= 1
            return ws
        if self.hero_hidden:
            return ws

        fields = {
            'player_id': 0,
            'attack_damage': HERO_DAMAGE,
            'attack_range': 500,
            'level': 1,
            'mana': 200,
            'mana_max': 200,
            'last_hits': self.last_hits,
            'ability_points': 1,
            **self.hero_flags,
        }
        hero = _add_unit(
            ws, HERO_HANDLE, UNIT_TYPE_HERO, TEAM_RADIANT, 'npc_dota_hero_nevermore', *self.hero_xy, 500, **fields
        )
        for slot in range(6):
            hero.abilities.add(
                handle=200 + slot,
                ability_id=5059 + slot,
                slot=slot,
                level=1 if slot == 0 else 0,
                is_fully_castable=slot == 0,
                cast_range=SKILL_CAST_RANGE if slot == 0 else 0,
            )
        hero.items.add(handle=250, ability_id=44, slot=0, charges=3, is_fully_castable=True)  # a tango
        for index, talent_id in enumerate(TALENT_IDS):
            level = int(index in self.learned_talents)
            hero.abilities.add(handle=230 + index, ability_id=talent_id, slot=TALENT_SLOT + index, level=level)
        scroll = load_records('items')['item_tpscroll']['id']
        if self.tp is not None:
            charges, cooldown = self.tp
            castable = cooldown == 0
            hero.items.add(
                handle=260,
                ability_id=scroll,
                slot=TP_SLOT,
                charges=charges,
                cooldown_remaining=cooldown,
                is_fully_castable=castable,
            )
        for slot, item_id in zip(STASH_SLOTS, self.stash, strict=False):  # a stash holds six at most
            hero.items.add(handle=270 + slot, ability_id=item_id, slot=slot, charges=1)
        courier = _add_unit(
            ws,
            COURIER_HANDLE,
            UNIT_TYPE_COURIER,
            TEAM_RADIANT,
            'npc_dota_courier',
            -6841,
            -6841,
            6 * self.courier_alive,
            6,
            player_id=0,
        )
        for slot, item_id in enumerate(self.courier_items):
            courier.items.add(ability_id=item_id, slot=slot, charges=1)
        _add_unit(
            ws,
            ENEMY_HERO_HANDLE,
            UNIT_TYPE_HERO,
            TEAM_DIRE,
            'npc_dota_hero_nevermore',
            self.hero_xy[0] + 900,
            self.hero_xy[1] + 900,
            health=500,
            player_id=5,
        )
        if self.creep_health > 0:
            _add_unit(
                ws,
                ENEMY_CREEP_HANDLE,
                UNIT_TYPE_LANE_CREEP,
                TEAM_DIRE,
                'npc_dota_creep_badguys_melee',
                self.hero_xy[0] + 300,
                self.hero_xy[1],
                health=self.creep_health,
                health_max=550,
            )
        _add_unit(
            ws,
            ALLY_CREEP_HANDLE,
            UNIT_TYPE_LANE_CREEP,
            TEAM_RADIANT,
            'npc_dota_creep_goodguys_melee',
            self.hero_xy[0] + 200,
            self.hero_xy[1] + 100,
            health=550,
        )
        return ws

    def act(self, dota_time, actions, extra_actions=(), draw=()):
        self.sent.append((dota_time, list(actions), list(extra_actions)))
        action_id = len(self.sent) - 1
        self.delivery.sent(action_id, list(extra_actions))
        if self.ack_status is not None:
            self.delivery.acked(action_id, self.ack_status, 0.1)
        if self.ack_status != 'executed':
            return
        action = actions[0]
        if action['actionType'] == 'DOTA_UNIT_ORDER_MOVE_TO_POSITION':
            location = action['moveToLocation']['location']
            self.hero_xy = [location['x'], location['y']]
            self.hero_hidden = False
        elif action['actionType'] == 'DOTA_UNIT_ORDER_ATTACK_TARGET':
            if action['attackTarget']['target'] == ENEMY_CREEP_HANDLE and self.creep_health > 0:
                self.creep_health -= HERO_DAMAGE
                if self.creep_health <= 0:
                    self.last_hits += 1

    def poll_delivery(self):
        return self.delivery

    def match_winner(self):
        return self.winner

    def lua_status(self):
        return {0: {'team': self.team_id, 'abilities': {'0': 'nevermore_shadowraze1'}}}

    def close(self):
        self.closed = True


ANCIENT_HANDLE = {TEAM_RADIANT: 300, TEAM_DIRE: 301}
TEAM_CREEP_HANDLE = 400
# handle bases for the ten heroes of the 5v5 fake; row i of the team observation is base + i
TEAM_HERO_HANDLE, TEAM_ENEMY_HANDLE = 500, 600


class Fake5v5Session:
    """Ten heroes, both ancients and one creep next to hero 0: enough to exercise the 5v5 env."""

    instances: ClassVar[list['Fake5v5Session']] = []

    def __init__(self, team_id, keep_files=False, replay_dir=None, **game_kwargs):
        self.team_id = team_id
        self.game_kwargs = game_kwargs
        self.replay_dir = replay_dir
        self.replay_path = None
        self.sent = []  # (dota_time, actions, extra_actions)
        self.closed = False
        self.dota_time = -75.0
        self.hero_xy = [[-1500.0 + 150.0 * row, -1400.0] for row in range(TEAM_SIZE)]
        self.creep_health = 120
        self.last_hits = 0
        self.kills = 0
        self.deaths = 0
        self.ancient_health = {TEAM_RADIANT: 4500, TEAM_DIRE: 4500}
        self.hidden_heroes = set()  # rows hidden like freshly respawned heroes on current clients
        self.cast_slots = {row: dict(CAST_SLOTS) for row in range(TEAM_SIZE)}
        self.feed_ended = False
        self.winner = None
        self.delivery = ActionDelivery()
        self.skipped_observations = 0
        self.ack_status = 'executed'
        self.spawn_delay = 2  # observations without heroes, like the real client
        self.available_runes = set()  # RUNE_SPOTS names with a rune on them
        self.rune_types = {}  # RUNE_SPOTS name: the world state's rune type, -1 (unknown) where not given
        self.outpost_teams = {'outpost_top': TEAM_RADIANT, 'outpost_bottom': TEAM_DIRE}  # as a match starts
        self.tree_events = []  # (tree id, destroyed) to report with the next frame
        self.frames_per_observe = 1  # more is a policy that fell behind: observe() hands over all of them
        self.dead_rows = set()  # rows whose heroes are dead and not reported
        self.row_deaths = [0] * TEAM_SIZE  # deaths of rows 1-4; row 0 counts self.deaths
        self.enemy_kills = [0] * TEAM_SIZE  # kills of the enemy players, TEAM_SIZE + row
        self.enemy_tower_standing = True
        self.roshan_killer = None  # a player id: the next frame reports Roshan killed by it
        Fake5v5Session.instances.append(self)

    def start(self):
        pass

    def observe(self, timeout):
        if self.feed_ended:
            raise queue.Empty
        self.skipped_observations += self.frames_per_observe - 1
        return [self.frame() for _ in range(self.frames_per_observe)]

    def frame(self):
        ws = CMsgBotWorldState(team_id=self.team_id, dota_time=self.dota_time, game_state=DOTA_GAMERULES_STATE_PRE_GAME)
        self.dota_time += 0.2
        for row in range(TEAM_SIZE):
            # only player 0 kills, so a test setting kills/deaths reads back the same number
            kills, deaths = (self.kills, self.deaths) if row == 0 else (0, self.row_deaths[row])
            alive = row not in self.dead_rows
            ws.players.add(player_id=row, team_id=TEAM_RADIANT, kills=kills, deaths=deaths, is_alive=alive)
            ws.players.add(player_id=TEAM_SIZE + row, team_id=TEAM_DIRE, kills=self.enemy_kills[row], is_alive=True)
        if self.roshan_killer is not None:
            ws.roshan_killed_events.add(killer_player_id=self.roshan_killer)
            self.roshan_killer = None
        for team, x, y in ((TEAM_RADIANT, -6000, -6000), (TEAM_DIRE, 6000, 6000)):
            name = f'npc_dota_{"goodguys" if team == TEAM_RADIANT else "badguys"}_fort'
            if self.ancient_health[team] > 0:
                _add_unit(ws, ANCIENT_HANDLE[team], UNIT_TYPE_FORT, team, name, x, y, self.ancient_health[team], 4500)
        _add_unit(ws, 100, UNIT_TYPE_TOWER, TEAM_RADIANT, 'npc_dota_goodguys_tower1_mid', -1544, -1408, 1800)
        if self.enemy_tower_standing:
            _add_unit(ws, 101, UNIT_TYPE_TOWER, TEAM_DIRE, 'npc_dota_badguys_tower1_mid', 524, 652, 1800)
        add_map_state(ws, self.available_runes, self.rune_types, self.outpost_teams, self.tree_events)
        if self.spawn_delay > 0:
            self.spawn_delay -= 1
            return ws

        for row in range(TEAM_SIZE):
            if row in self.hidden_heroes or row in self.dead_rows:
                continue
            x, y = self.hero_xy[row]
            hero = _add_unit(
                ws,
                TEAM_HERO_HANDLE + row,
                UNIT_TYPE_HERO,
                TEAM_RADIANT,
                'npc_dota_hero_nevermore',
                x,
                y,
                health=500,
                player_id=row,
                attack_damage=HERO_DAMAGE,
                attack_range=500,
                level=1,
                mana=200,
                mana_max=200,
                last_hits=self.last_hits if row == 0 else 0,
                ability_points=1,
            )
            for slot in range(6):
                hero.abilities.add(
                    handle=200 + 10 * row + slot,
                    ability_id=5059 + slot,
                    slot=slot,
                    level=1 if slot == 0 else 0,
                    is_fully_castable=slot == 0,
                    cast_range=SKILL_CAST_RANGE if slot == 0 else 0,
                )
            hero.items.add(handle=250 + row, ability_id=44, slot=0, charges=3, is_fully_castable=True)
            _add_unit(
                ws,
                TEAM_ENEMY_HANDLE + row,
                UNIT_TYPE_HERO,
                TEAM_DIRE,
                'npc_dota_hero_lina',
                x + 900,
                y + 900,
                health=500,
                player_id=TEAM_SIZE + row,
            )
        if self.creep_health > 0:
            x, y = self.hero_xy[0]
            _add_unit(
                ws,
                TEAM_CREEP_HANDLE,
                UNIT_TYPE_LANE_CREEP,
                TEAM_DIRE,
                'npc_dota_creep_badguys_melee',
                x + 300,
                y,
                health=self.creep_health,
                health_max=550,
            )
        return ws

    def act(self, dota_time, actions, extra_actions=(), draw=()):
        self.sent.append((dota_time, list(actions), list(extra_actions)))
        action_id = len(self.sent) - 1
        self.delivery.sent(action_id, list(extra_actions))
        if self.ack_status is not None:
            self.delivery.acked(action_id, self.ack_status, 0.1)
        if self.ack_status != 'executed':
            return
        for action in actions:
            row = action['player']
            if action['actionType'] == 'DOTA_UNIT_ORDER_MOVE_TO_POSITION':
                location = action['moveToLocation']['location']
                self.hero_xy[row] = [location['x'], location['y']]
                self.hidden_heroes.discard(row)
            elif action['actionType'] == 'DOTA_UNIT_ORDER_ATTACK_TARGET':
                if action['attackTarget']['target'] == TEAM_CREEP_HANDLE and self.creep_health > 0:
                    self.creep_health -= HERO_DAMAGE
                    if self.creep_health <= 0:
                        self.last_hits += 1

    def poll_delivery(self):
        return self.delivery

    def match_winner(self):
        return self.winner

    def lua_status(self):
        return {row: {'team': self.team_id, 'abilities': {'0': 'nevermore_shadowraze1'}} for row in range(TEAM_SIZE)}

    def close(self):
        self.closed = True
