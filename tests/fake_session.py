"""A tiny stand-in for DotaSession: simulates just enough of a 1v1 lane to exercise the env without Dota."""

import queue
from typing import ClassVar

from dota2_env.bridge.constants import (
    DOTA_GAMERULES_STATE_PRE_GAME,
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_HERO,
    UNIT_TYPE_LANE_CREEP,
    UNIT_TYPE_TOWER,
)
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import (
    CMsgBotWorldState,
)
from dota2_env.bridge.session import ActionDelivery

HERO_HANDLE, ENEMY_HERO_HANDLE, ENEMY_CREEP_HANDLE, ALLY_CREEP_HANDLE = 1, 2, 10, 11
HERO_DAMAGE = 50


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

    def __init__(self, team_id, keep_files=False, **game_kwargs):
        self.team_id = team_id
        self.game_kwargs = game_kwargs
        self.sent = []  # (dota_time, actions, extra_actions)
        self.closed = False
        self.dota_time = -90.0
        self.hero_xy = [-1500.0, -1400.0]
        self.creep_health = 120
        self.last_hits = 0
        self.kills = 0
        self.deaths = 0
        self.hero_hidden = False  # like a freshly respawned hero on current clients
        self.feed_ended = False
        self.winner = None
        self.delivery = ActionDelivery()
        self.skipped_observations = 0
        self.ack_status = 'executed'  # None = lua never sees the file
        self.spawn_delay = 2  # observations without heroes, like the real client
        FakeSession.instances.append(self)

    def start(self):
        pass

    def observe(self, timeout):
        if self.feed_ended:
            raise queue.Empty
        ws = CMsgBotWorldState(team_id=self.team_id, dota_time=self.dota_time, game_state=DOTA_GAMERULES_STATE_PRE_GAME)
        self.dota_time += 0.2
        ws.players.add(player_id=0, team_id=TEAM_RADIANT, kills=self.kills, deaths=self.deaths, is_alive=True)
        ws.players.add(player_id=5, team_id=TEAM_DIRE, is_alive=True)
        _add_unit(ws, 100, UNIT_TYPE_TOWER, TEAM_RADIANT, 'npc_dota_goodguys_tower1_mid', -1544, -1408, 1800)
        _add_unit(ws, 101, UNIT_TYPE_TOWER, TEAM_DIRE, 'npc_dota_badguys_tower1_mid', 524, 652, 1800)
        if self.spawn_delay > 0:
            self.spawn_delay -= 1
            return ws
        if self.hero_hidden:
            return ws

        hero = _add_unit(
            ws,
            HERO_HANDLE,
            UNIT_TYPE_HERO,
            TEAM_RADIANT,
            'npc_dota_hero_nevermore',
            *self.hero_xy,
            health=500,
            player_id=0,
            attack_damage=HERO_DAMAGE,
            attack_range=500,
            level=1,
            mana=200,
            mana_max=200,
            last_hits=self.last_hits,
            ability_points=1,
        )
        for slot in range(6):
            hero.abilities.add(
                handle=200 + slot,
                ability_id=5059 + slot,
                slot=slot,
                level=1 if slot == 0 else 0,
                is_fully_castable=slot == 0,
            )
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
        if action['actionType'] == 'DOTA_UNIT_ORDER_MOVE_DIRECTLY':
            location = action['moveDirectly']['location']
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
        return {self.team_id: {'player_id': 0, 'abilities': {'0': 'nevermore_shadowraze1'}}}

    def close(self):
        self.closed = True
