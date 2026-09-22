"""Gymnasium environment: a full All Pick game with all five heroes of one team under agent control."""

import logging
import queue
import time
from typing import ClassVar

import gymnasium as gym

from dota2_env import actions
from dota2_env.bridge.constants import (
    DOTA_GAMEMODE_AP,
    HOST_MODE_DEDICATED,
    HOST_MODE_GUI,
    TEAM_RADIANT,
)
from dota2_env.bridge.game import CONTROL_AGENT, get_default_game_path
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.bridge.session import DotaSession
from dota2_env.map_features import TreeTable, warn_if_stale
from dota2_env.observation import TEAM_SIZE, build_team_observation, find_hero, team_observation_space, team_player_ids
from dota2_env.rewards import AllPick5v5Rules, TeamReward, opposing
from dota2_env.text import describe_team

logger = logging.getLogger('dota2_env')

DEFAULT_LINEUP = (
    'npc_dota_hero_nevermore',
    'npc_dota_hero_sniper',
    'npc_dota_hero_lina',
    'npc_dota_hero_lion',
    'npc_dota_hero_crystal_maiden',
)
DEFAULT_STARTING_ITEMS = ('item_tango', 'item_branches', 'item_branches', 'item_circlet')
# Ability slots tried in order whenever there is a skill point; lua skips the ones that cannot be
# upgraded yet, so this reads "ultimate first, then slot 0, 3, 4, 1, 2".
DEFAULT_ABILITY_PRIORITY = (5, 0, 3, 4, 1, 2)


class DotaAllPick5v5Env(gym.Env):
    """
    Observation: dota2_env.observation.team_observation_space (five hero vectors, ability and
                 nearest-unit tables, plus one map-wide team vector).
    Action:      dota2_env.actions.team_action_space, one column per hero: row i of every array
                 drives the hero of player_ids[i], the i-th player id of the team.
    Info:        action_mask, world_state (raw CMsgBotWorldState), dota_time, reward
                 components, winner, player_ids.

    Every reset() restarts the Dota client and waits out hero selection and the strategy phase
    (about a minute of game time), so give it a generous options={"timeout": ...}. One step()
    lasts ticks_per_observation game ticks (30 ticks = 1 game second) divided by timescale in
    wall-clock time. Dota cannot be seeded, so reset(seed=...) only seeds self.np_random.
    """

    metadata: ClassVar[dict[str, list[str]]] = {'render_modes': ['human', 'ansi']}

    def __init__(
        self,
        render_mode=None,
        team_id=TEAM_RADIANT,
        heroes=DEFAULT_LINEUP,
        opponent_heroes=DEFAULT_LINEUP,
        opponent='builtin',
        timescale=1.0,
        ticks_per_observation=6,
        reward_fn=None,
        rules=None,
        starting_items=DEFAULT_STARTING_ITEMS,
        ability_priority=DEFAULT_ABILITY_PRIORITY,
        step_timeout=20.0,
        replay_dir=None,
        keep_files=False,
        dota_path=None,
        session_factory=DotaSession,
    ):
        """
        render_mode: "human" opens the game window (type jointeam spec in the console to get a camera);
                     None or "ansi" run the headless dedicated server. "ansi" makes render() return text.
        heroes / opponent_heroes: five hero unit names each, handed out in player-id order.
        opponent:    "builtin" (Valve's default bot AI) or "idle".
        starting_items / ability_priority: bought / levelled automatically for every hero; pass () to
                     do it yourself through queue_purchase and queue_train_ability.
        step_timeout: seconds without a new world state before the episode is truncated with info["error"].
        replay_dir:  record the match and move the .dem there on close(), e.g. "replays";
                     a relative path lands under the current working directory. None records nothing.
        keep_files:  keep the session folder (generated lua, console.log) in the temp dir for debugging.
        session_factory: replaces the Dota bridge, used by the tests.
        """
        assert render_mode is None or render_mode in self.metadata['render_modes']
        assert len(heroes) == TEAM_SIZE and len(opponent_heroes) == TEAM_SIZE
        self.render_mode = render_mode
        self.observation_space = team_observation_space
        self.action_space = actions.team_action_space

        self.team_id = team_id
        self.step_timeout = step_timeout
        self.reward_fn = reward_fn or TeamReward()
        self.rules = rules or AllPick5v5Rules()
        self.starting_items = tuple(starting_items)
        self.ability_priority = tuple(ability_priority)
        self._session_factory = session_factory
        self._session_kwargs = {
            'team_id': team_id,
            'keep_files': keep_files,
            'replay_dir': replay_dir,
            'host_timescale': timescale,
            'ticks_per_observation': ticks_per_observation,
            'game_mode': DOTA_GAMEMODE_AP,
            'host_mode': HOST_MODE_GUI if render_mode == 'human' else HOST_MODE_DEDICATED,
            'dota_path': dota_path,
            'heroes': {team_id: tuple(heroes), opposing(team_id): tuple(opponent_heroes)},
            'control': {team_id: CONTROL_AGENT, opposing(team_id): opponent},
        }
        self._session = None
        self.trees = None  # the TreeTable of the current episode, fed every world state the session returns
        warn_if_stale(dota_path or get_default_game_path())
        self.replay_path = None  # the .dem of the last episode, once close() has collected it
        self._world_state = None
        self._observation = None
        self._player_ids = []
        self._pending_extra_actions = []
        self._ability_names = {}

    # -- gymnasium API ---------------------------------------------------------------------------

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        timeout = (options or {}).get('timeout', 600)
        self.close()
        self._session = self._session_factory(**self._session_kwargs)
        self._session.start()

        # Buildings are reported a few frames before the heroes spawn; wait for one of ours. The
        # others follow within a frame or two, and a hero still missing reads as standing in the
        # fountain (see docs/VERSION_DIFF.md), which is where it is.
        deadline = time.time() + timeout
        self.trees = TreeTable()
        while True:
            world_state = self._session.observe(timeout=max(deadline - time.time(), 0.001))
            self.trees.update(world_state)
            if find_hero(world_state, self.team_id) is not None:
                break
        self._player_ids = team_player_ids(world_state, self.team_id)
        assert len(self._player_ids) == TEAM_SIZE, f'expected {TEAM_SIZE} players, got {self._player_ids}'
        self._ability_names = {}
        self._pending_extra_actions = [
            actions.purchase_item(player_id, item) for player_id in self._player_ids for item in self.starting_items
        ]
        self.reward_fn.reset(world_state, self.team_id)
        self.rules.reset(world_state, self.team_id)
        self.rules(world_state)
        self._set_state(world_state)
        return self._observation.arrays, self._info()

    def step(self, action):
        assert self._session is not None, 'call reset() first'
        bridge_actions = []
        cast_slots = self.cast_slots()
        for row, player_id in enumerate(self._player_ids):
            hero_observation = self._observation.heroes[row]
            bridge_action = actions.to_bridge_action(
                actions.hero_action(action, row), hero_observation, player_id, cast_slots.get(player_id)
            )
            # Every controlled hero needs an entry, or lua never marks the file as executed and
            # repeats its extra_actions every tick.
            bridge_actions.append(bridge_action or {'actionType': 'DOTA_UNIT_ORDER_NONE', 'player': player_id})

        extra_actions, self._pending_extra_actions = self._pending_extra_actions, []
        # purchases / skill points that went down with a lost action file are sent again
        delivery = self._session.poll_delivery()
        extra_actions += delivery.lost_extra_actions
        delivery.lost_extra_actions = []
        for row, player_id in enumerate(self._player_ids):
            hero = self._observation.heroes[row].hero
            if hero is not None and hero.ability_points > 0:
                extra_actions += [actions.train_ability(player_id, f'slot:{slot}') for slot in self.ability_priority]
        self._session.act(self._world_state.dota_time, bridge_actions, extra_actions)

        previous = self._world_state
        deadline = time.time() + self.step_timeout
        while True:
            try:
                world_state = self._session.observe(timeout=min(1.0, self.step_timeout))
                break
            except queue.Empty:
                if time.time() >= deadline or self._session.match_winner() is not None:
                    return self._feed_ended()
        self.trees.update(world_state)
        terminated, truncated, winner = self.rules(world_state)
        reward, components = self.reward_fn(previous, world_state, winner)
        self._set_state(world_state)
        info = self._info()
        info['reward'] = components
        info['winner'] = winner
        return self._observation.arrays, float(reward), terminated, truncated, info

    def _feed_ended(self):
        """No new world state: either the client ended the match (it stops streaming at once, so the
        ancient falling never shows up in a world state) or it crashed."""
        winner = self._session.match_winner()
        info = self._info()
        info['winner'] = winner
        if winner is None:
            logger.warning(f'no world state for {self.step_timeout:.0f}s, truncating the episode')
            info.update(reward=dict.fromkeys(self.reward_fn.weights, 0.0), error='worldstate feed ended')
            return self._observation.arrays, 0.0, False, True, info
        reward, info['reward'] = self.reward_fn(self._world_state, self._world_state, winner)
        return self._observation.arrays, float(reward), True, False, info

    def render(self):
        if self.render_mode == 'ansi' and self._world_state is not None:
            return describe_team(self._world_state, self.team_id, self._player_ids, self.cast_slots())
        return None

    def close(self):
        if self._session is not None:
            self._session.close()
            self.replay_path = self._session.replay_path
            self._session = None

    # -- extras ----------------------------------------------------------------------------------

    def queue_purchase(self, row, item_name):
        """Buy item_name (e.g. "item_boots") for hero row with the next step."""
        self._pending_extra_actions.append(actions.purchase_item(self._player_ids[row], item_name))

    def queue_train_ability(self, row, ability):
        """Level an ability of hero row with the next step; ability is a name or "slot:N"."""
        self._pending_extra_actions.append(actions.train_ability(self._player_ids[row], ability))

    def queue_chat(self, row, message, all_chat=True):
        """Have hero row say message with the next step; all_chat False keeps it inside the team."""
        self._pending_extra_actions.append(actions.chat(self._player_ids[row], message, all_chat))

    def ability_names(self):
        """{player_id: {slot: ability name}} as reported by the running client (empty until lua has started)."""
        if len(self._ability_names) < TEAM_SIZE and self._session is not None:
            status = self._session.lua_status()
            self._ability_names = {
                player_id: {int(slot): name for slot, name in status[player_id].get('abilities', {}).items()}
                for player_id in self._player_ids
                if player_id in status
            }
        return self._ability_names

    def cast_slots(self) -> dict[int, dict[int, tuple[str, tuple[str, ...]]]]:
        """{player_id: {ability index: (name, kinds)}} as lua last reported them; see DotaMid1v1Env.cast_slots."""
        if self._session is None:
            return {}
        return {player_id: dict(self._session.cast_slots.get(player_id, {})) for player_id in self._player_ids}

    def action_masks(self):
        return actions.build_team_action_mask(self._observation, self.team_id, self.cast_slots())

    def sample_legal_action(self):
        return actions.sample_masked_team_action(self.action_masks(), self.np_random)

    def _set_state(self, world_state: CMsgBotWorldState) -> None:
        self._world_state = world_state
        self._observation = build_team_observation(world_state, self.team_id, self._player_ids, trees=self.trees)

    def _info(self):
        return {
            'action_mask': self.action_masks(),
            'world_state': self._world_state,
            'dota_time': self._world_state.dota_time,
            'player_ids': list(self._player_ids),
            'action_delivery': self._session.poll_delivery().summary(),
            'skipped_observations': self._session.skipped_observations,
        }
