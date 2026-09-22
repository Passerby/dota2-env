"""Reward functions and episode-end rules. Both are plain objects passed to the env, so they are easy to swap."""

from typing import ClassVar

from dota2_env.bridge.constants import (
    DOTA_GAMERULES_STATE_POST_GAME,
    TEAM_DIRE,
    TEAM_RADIANT,
    UNIT_TYPE_FORT,
    UNIT_TYPE_TOWER,
)
from dota2_env.observation import find_hero, team_player_ids


def player_stat(world_state, team_id, stat):
    """kills/deaths of the first (non-filler) player of `team_id`."""
    players = sorted((p for p in world_state.players if p.team_id == team_id), key=lambda p: p.player_id)
    return getattr(players[0], stat) if players else 0


def mid_tower(world_state, team_id):
    name = f'npc_dota_{"goodguys" if team_id == TEAM_RADIANT else "badguys"}_tower1_mid'
    for unit in world_state.units:
        if unit.unit_type == UNIT_TYPE_TOWER and unit.name == name:
            return unit
    return None


def team_stat(world_state, team_id, stat):
    """kills/deaths/... summed over every player of team_id."""
    return sum(getattr(player, stat) for player in world_state.players if player.team_id == team_id)


def standing_towers(world_state, team_id):
    return sum(
        1
        for unit in world_state.units
        if unit.unit_type == UNIT_TYPE_TOWER and unit.team_id == team_id and unit.is_alive
    )


def ancient_health_frac(world_state, team_id):
    """Health fraction of the team's ancient; 0 once it is gone (destroyed buildings stop being reported)."""
    for unit in world_state.units:
        if unit.unit_type == UNIT_TYPE_FORT and unit.team_id == team_id:
            return unit.health / max(unit.health_max, 1)
    return 0.0


def opposing(team_id):
    return TEAM_DIRE if team_id == TEAM_RADIANT else TEAM_RADIANT


class LaningReward:
    """Dense 1v1 laning reward: weighted sum of per-step deltas. `components` ends up in `info["reward"]`."""

    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        'last_hit': 0.16,
        'deny': 0.12,
        'xp_level': 0.3,
        'health': 1.0,  # own health fraction gained/lost
        'enemy_health': 0.8,  # enemy hero health fraction lost, only counted while the enemy stays visible
        'kill': 2.0,
        'death': -2.0,
        'tower_health': 1.5,  # (enemy tower fraction lost) - (own tower fraction lost)
        'win': 5.0,
    }

    def __init__(self, weights=None):
        self.weights = dict(self.DEFAULT_WEIGHTS, **(weights or {}))

    def reset(self, world_state, team_id):
        self.team_id = team_id

    def __call__(self, previous, current, winner=None):
        team, enemy_team = self.team_id, opposing(self.team_id)
        components = dict.fromkeys(self.weights, 0.0)

        hero, previous_hero = find_hero(current, team), find_hero(previous, team)
        if hero is not None and previous_hero is not None:
            components['last_hit'] = hero.last_hits - previous_hero.last_hits
            components['deny'] = hero.denies - previous_hero.denies
            components['xp_level'] = hero.level - previous_hero.level
            if hero.is_alive and previous_hero.is_alive:
                components['health'] = hero.health / max(hero.health_max, 1) - previous_hero.health / max(
                    previous_hero.health_max, 1
                )

        enemy, previous_enemy = find_hero(current, enemy_team), find_hero(previous, enemy_team)
        if enemy is not None and previous_enemy is not None and enemy.is_alive and previous_enemy.is_alive:
            components['enemy_health'] = previous_enemy.health / max(previous_enemy.health_max, 1) - enemy.health / max(
                enemy.health_max, 1
            )

        components['kill'] = player_stat(current, team, 'kills') - player_stat(previous, team, 'kills')
        components['death'] = player_stat(current, team, 'deaths') - player_stat(previous, team, 'deaths')

        for tower_team, sign in ((enemy_team, 1.0), (team, -1.0)):
            tower, previous_tower = mid_tower(current, tower_team), mid_tower(previous, tower_team)
            if tower is not None and previous_tower is not None:
                components['tower_health'] += sign * (previous_tower.health - tower.health) / max(tower.health_max, 1)

        if winner is not None:
            components['win'] = 1.0 if winner == team else -1.0

        return sum(self.weights[key] * value for key, value in components.items()), components


class Mid1v1Rules:
    """Standard 1v1 mid: a side loses on its `deaths_to_lose`-th death (any cause, the client ends the match
    the same way) or when its tier-1 mid tower falls; truncated at `max_dota_time`."""

    def __init__(self, deaths_to_lose=2, max_dota_time=600.0):
        self.deaths_to_lose = deaths_to_lose
        self.max_dota_time = max_dota_time

    def reset(self, world_state, team_id):
        self.team_id = team_id
        self._tower_seen = {TEAM_RADIANT: False, TEAM_DIRE: False}

    def __call__(self, world_state):
        """-> (terminated, truncated, winner team id or None)"""
        for team in (TEAM_RADIANT, TEAM_DIRE):
            if player_stat(world_state, team, 'deaths') >= self.deaths_to_lose:
                return True, False, opposing(team)
        for team in (TEAM_RADIANT, TEAM_DIRE):
            tower = mid_tower(world_state, team)
            if tower is not None and tower.is_alive:
                self._tower_seen[team] = True
            elif self._tower_seen[team]:
                return True, False, opposing(team)
        if world_state.game_state == DOTA_GAMERULES_STATE_POST_GAME:
            return True, False, None
        return False, world_state.dota_time >= self.max_dota_time, None


class TeamReward:
    """Dense 5v5 reward: the laning terms summed over our five heroes plus the objectives that decide
    a full game (towers, ancient). components ends up in info["reward"], unweighted."""

    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        'last_hit': 0.02,
        'deny': 0.02,
        'xp_level': 0.2,
        'health': 0.5,  # summed health-fraction change over our five heroes
        'enemy_health': 0.4,  # only counted while the enemy hero stays visible
        'kill': 1.0,
        'death': -1.0,
        'tower': 2.0,  # whole towers: (enemy lost) - (ours lost)
        'ancient': 3.0,  # ancient health fraction, same sign convention
        'win': 10.0,
    }

    def __init__(self, weights=None):
        self.weights = dict(self.DEFAULT_WEIGHTS, **(weights or {}))

    def reset(self, world_state, team_id):
        self.team_id = team_id

    def __call__(self, previous, current, winner=None):
        team, enemy_team = self.team_id, opposing(self.team_id)
        components = dict.fromkeys(self.weights, 0.0)

        for player_id in team_player_ids(current, team):
            hero, previous_hero = find_hero(current, team, player_id), find_hero(previous, team, player_id)
            if hero is None or previous_hero is None:
                continue
            components['last_hit'] += hero.last_hits - previous_hero.last_hits
            components['deny'] += hero.denies - previous_hero.denies
            components['xp_level'] += hero.level - previous_hero.level
            if hero.is_alive and previous_hero.is_alive:
                components['health'] += hero.health / max(hero.health_max, 1) - previous_hero.health / max(
                    previous_hero.health_max, 1
                )

        for player_id in team_player_ids(current, enemy_team):
            enemy, previous_enemy = (
                find_hero(current, enemy_team, player_id),
                find_hero(previous, enemy_team, player_id),
            )
            if enemy is None or previous_enemy is None or not (enemy.is_alive and previous_enemy.is_alive):
                continue
            components['enemy_health'] += previous_enemy.health / max(
                previous_enemy.health_max, 1
            ) - enemy.health / max(enemy.health_max, 1)

        components['kill'] = team_stat(current, team, 'kills') - team_stat(previous, team, 'kills')
        components['death'] = team_stat(current, team, 'deaths') - team_stat(previous, team, 'deaths')
        components['tower'] = (standing_towers(previous, enemy_team) - standing_towers(current, enemy_team)) - (
            standing_towers(previous, team) - standing_towers(current, team)
        )
        components['ancient'] = (
            ancient_health_frac(previous, enemy_team) - ancient_health_frac(current, enemy_team)
        ) - (ancient_health_frac(previous, team) - ancient_health_frac(current, team))

        if winner is not None:
            components['win'] = 1.0 if winner == team else -1.0

        return sum(self.weights[key] * value for key, value in components.items()), components


class AllPick5v5Rules:
    """A full game: the side whose ancient falls loses; truncated at max_dota_time."""

    def __init__(self, max_dota_time=2400.0):
        self.max_dota_time = max_dota_time

    def reset(self, world_state, team_id):
        self.ancient_seen = {TEAM_RADIANT: False, TEAM_DIRE: False}

    def __call__(self, world_state):
        """-> (terminated, truncated, winner team id or None)"""
        for team in (TEAM_RADIANT, TEAM_DIRE):
            if ancient_health_frac(world_state, team) > 0:
                self.ancient_seen[team] = True
            elif self.ancient_seen[team]:
                return True, False, opposing(team)
        if world_state.game_state == DOTA_GAMERULES_STATE_POST_GAME:
            return True, False, None
        return False, world_state.dota_time >= self.max_dota_time, None
