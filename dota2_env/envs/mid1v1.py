"""Gymnasium environment: 1v1 mid lane, one agent-controlled hero."""
import logging
import queue
import time

import gymnasium as gym

from dota2_env import actions as A
from dota2_env.bridge.constants import (
    TEAM_RADIANT, DOTA_GAMEMODE_1V1MID, HOST_MODE_DEDICATED, HOST_MODE_GUI,
)
from dota2_env.bridge.game import DEFAULT_HERO, CONTROL_AGENT
from dota2_env.bridge.session import DotaSession
from dota2_env.observation import observation_space, build_observation, find_hero
from dota2_env.rewards import LaningReward, Mid1v1Rules, opposing
from dota2_env.text import describe

logger = logging.getLogger("dota2_env")

DEFAULT_STARTING_ITEMS = ('item_tango', 'item_faerie_fire', 'item_branches', 'item_branches', 'item_circlet')
# Ability slots tried in order whenever there is a skill point; lua skips the ones that cannot be
# upgraded yet, so this reads "ultimate first, then slot 0, 3, 4, 1, 2".
DEFAULT_ABILITY_PRIORITY = (5, 0, 3, 4, 1, 2)


class DotaMid1v1Env(gym.Env):
    """
    Observation: `dota2_env.observation.observation_space` (hero vector, ability table, nearest-unit table).
    Action:      `dota2_env.actions.action_space` (type / move direction / target row / ability slot).
    Info:        `action_mask`, `world_state` (raw CMsgBotWorldState), `dota_time`, `reward` components, `winner`.

    Every `reset()` restarts the Dota client (about 20 s), and one `step()` lasts `ticks_per_observation`
    game ticks (30 ticks = 1 game second) divided by `timescale` in wall-clock time. Dota cannot be seeded,
    so `reset(seed=...)` only seeds `self.np_random`.
    """

    metadata = {'render_modes': ['human', 'ansi']}

    def __init__(
        self,
        render_mode=None,
        team_id=TEAM_RADIANT,
        hero=DEFAULT_HERO,
        opponent_hero=DEFAULT_HERO,
        opponent='builtin',
        timescale=1.0,
        ticks_per_observation=6,
        reward_fn=None,
        rules=None,
        starting_items=DEFAULT_STARTING_ITEMS,
        ability_priority=DEFAULT_ABILITY_PRIORITY,
        step_timeout=20.0,
        keep_files=False,
        dota_path=None,
        session_factory=DotaSession,
    ):
        """
        render_mode: "human" opens the game window (type `jointeam spec` in the console to get a camera);
                     None or "ansi" run the headless dedicated server. "ansi" makes `render()` return text.
        opponent:    "builtin" (Valve's default bot AI) or "idle".
        starting_items / ability_priority: bought / levelled automatically; pass () to do it yourself
                     through `queue_purchase` and `queue_train_ability`.
        step_timeout: seconds without a new world state before the episode is truncated with `info["error"]`.
        keep_files:  keep the session folder (generated lua, console.log) in the temp dir for debugging.
        session_factory: replaces the Dota bridge, used by the tests.
        """
        assert render_mode is None or render_mode in self.metadata['render_modes']
        self.render_mode = render_mode
        self.observation_space = observation_space
        self.action_space = A.action_space

        self.team_id = team_id
        self.step_timeout = step_timeout
        self.reward_fn = reward_fn or LaningReward()
        self.rules = rules or Mid1v1Rules()
        self.starting_items = tuple(starting_items)
        self.ability_priority = tuple(ability_priority)
        self._session_factory = session_factory
        self._session_kwargs = dict(
            team_id=team_id,
            keep_files=keep_files,
            host_timescale=timescale,
            ticks_per_observation=ticks_per_observation,
            game_mode=DOTA_GAMEMODE_1V1MID,
            host_mode=HOST_MODE_GUI if render_mode == 'human' else HOST_MODE_DEDICATED,
            dota_path=dota_path,
            heroes={team_id: hero, opposing(team_id): opponent_hero},
            control={team_id: CONTROL_AGENT, opposing(team_id): opponent},
        )
        self._session = None
        self._world_state = None
        self._observation = None
        self._player_id = None
        self._pending_extra_actions = []
        self._ability_names = {}

    # -- gymnasium API ---------------------------------------------------------------------------

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        timeout = (options or {}).get('timeout', 300)
        self.close()
        self._session = self._session_factory(**self._session_kwargs)
        self._session.start()

        # Buildings are reported a few frames before the heroes spawn; wait for ours.
        deadline = time.time() + timeout
        while True:
            world_state = self._session.observe(timeout=max(deadline - time.time(), 0.001))
            hero = find_hero(world_state, self.team_id)
            if hero is not None:
                break
        self._player_id = hero.player_id
        self._ability_names = {}
        self._pending_extra_actions = [A.purchase_item(self._player_id, item) for item in self.starting_items]
        self.reward_fn.reset(world_state, self.team_id)
        self.rules.reset(world_state, self.team_id)
        self.rules(world_state)
        self._set_state(world_state)
        return self._observation.arrays, self._info()

    def step(self, action):
        assert self._session is not None, 'call reset() first'
        bridge_action = A.to_bridge_action(action, self._observation, self._player_id)
        if bridge_action is None:
            bridge_action = {'actionType': 'DOTA_UNIT_ORDER_NONE', 'player': self._player_id}

        extra_actions, self._pending_extra_actions = self._pending_extra_actions, []
        # purchases / skill points that went down with a lost action file are sent again
        delivery = self._session.poll_delivery()
        extra_actions += delivery.lost_extra_actions
        delivery.lost_extra_actions = []
        hero = self._observation.hero
        if hero is not None and hero.ability_points > 0:
            extra_actions += [A.train_ability(self._player_id, 'slot:{}'.format(slot))
                              for slot in self.ability_priority]
        self._session.act(self._world_state.dota_time, [bridge_action], extra_actions)

        previous = self._world_state
        deadline = time.time() + self.step_timeout
        while True:
            try:
                world_state = self._session.observe(timeout=min(1.0, self.step_timeout))
                break
            except queue.Empty:
                if time.time() >= deadline or self._session.match_winner() is not None:
                    return self._feed_ended()
        terminated, truncated, winner = self.rules(world_state)
        reward, components = self.reward_fn(previous, world_state, winner)
        self._set_state(world_state)
        info = self._info()
        info['reward'] = components
        info['winner'] = winner
        return self._observation.arrays, float(reward), terminated, truncated, info

    def _feed_ended(self):
        """No new world state: either the client ended the match (it stops streaming at once, so the
        deciding death / tower kill never shows up in a world state) or it crashed."""
        winner = self._session.match_winner()
        info = self._info()
        info['winner'] = winner
        if winner is None:
            logger.warning('no world state for %.0fs, truncating the episode', self.step_timeout)
            info.update(reward=dict.fromkeys(self.reward_fn.weights, 0.0), error='worldstate feed ended')
            return self._observation.arrays, 0.0, False, True, info
        reward, info['reward'] = self.reward_fn(self._world_state, self._world_state, winner)
        return self._observation.arrays, float(reward), True, False, info

    def render(self):
        if self.render_mode == 'ansi' and self._world_state is not None:
            return describe(self._world_state, self.team_id, self._player_id, self.ability_names())
        return None

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- extras ----------------------------------------------------------------------------------

    def queue_purchase(self, item_name):
        """Buy `item_name` (e.g. "item_boots") with the next step."""
        self._pending_extra_actions.append(A.purchase_item(self._player_id, item_name))

    def queue_train_ability(self, ability):
        """Level an ability with the next step; `ability` is a name or "slot:N"."""
        self._pending_extra_actions.append(A.train_ability(self._player_id, ability))

    def ability_names(self):
        """{slot: ability name} of our hero as reported by the running client (empty until lua has started)."""
        if not self._ability_names and self._session is not None:
            record = self._session.lua_status().get(self.team_id, {})
            self._ability_names = {int(slot): name for slot, name in record.get('abilities', {}).items()}
        return self._ability_names

    def action_masks(self):
        return A.build_action_mask(self._observation, self.team_id)

    def sample_legal_action(self):
        return A.sample_masked_action(self.action_masks(), self.np_random)

    def _set_state(self, world_state):
        self._world_state = world_state
        self._observation = build_observation(world_state, self.team_id, self._player_id)

    def _info(self):
        return {
            'action_mask': self.action_masks(),
            'world_state': self._world_state,
            'dota_time': self._world_state.dota_time,
            'action_delivery': self._session.poll_delivery().summary(),
            'skipped_observations': self._session.skipped_observations,
        }
