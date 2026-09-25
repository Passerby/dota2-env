"""Gymnasium environment: 1v1 mid lane, one agent-controlled hero."""

import logging
import queue
import time
from typing import ClassVar

import gymnasium as gym

from dota2_env import actions
from dota2_env.bridge.constants import (
    DOTA_GAMEMODE_1V1MID,
    HOST_MODE_DEDICATED,
    HOST_MODE_GUI,
    TEAM_RADIANT,
)
from dota2_env.bridge.game import CONTROL_AGENT, DEFAULT_HERO, get_default_game_path
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.bridge.session import DotaSession
from dota2_env.map_features import TreeTable, warn_if_stale
from dota2_env.observation import build_observation, find_hero, observation_space
from dota2_env.rewards import LaningReward, Mid1v1Rules, opposing
from dota2_env.text import describe

logger = logging.getLogger('dota2_env')

DEFAULT_STARTING_ITEMS = ('item_tango', 'item_faerie_fire', 'item_branches', 'item_branches', 'item_circlet')
# Ability slots tried in order whenever there is a skill point; lua skips the ones that cannot be
# upgraded yet, so this reads "ultimate first, then slot 0, 3, 4, 1, 2".
DEFAULT_ABILITY_PRIORITY = (5, 0, 3, 4, 1, 2)


class DotaMid1v1Env(gym.Env):
    """
    Observation: `dota2_env.observation.observation_space` (hero vector, ability table, nearest-unit table).
    Action:      `dota2_env.actions.action_space` (type / move direction / target row / ability slot).
    Info:        action_mask, world_state (raw CMsgBotWorldState), dota_time, reward components, winner,
                 and world_states: every frame since the last step, oldest first, world_state last.

    Every `reset()` restarts the Dota client (about 20 s), and one `step()` lasts `ticks_per_observation`
    game ticks (30 ticks = 1 game second) divided by `timescale` in wall-clock time. Dota cannot be seeded,
    so `reset(seed=...)` only seeds `self.np_random`.
    """

    metadata: ClassVar[dict[str, list[str]]] = {'render_modes': ['human', 'ansi']}

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
        restock_tp=True,
        step_timeout=20.0,
        replay_dir=None,
        keep_files=False,
        dota_path=None,
        session_factory=DotaSession,
    ):
        """
        render_mode: "human" opens the game window and puts you on the spectator team for a camera;
                     None or "ansi" run the headless dedicated server. "ansi" makes `render()` return text.
        opponent:    "builtin" (Valve's default bot AI) or "idle".
        starting_items / ability_priority: bought / levelled automatically; pass () to do it yourself
                     through `queue_purchase` and `queue_train_ability`. A skill point per talent tier
                     the hero has reached is kept back for the agent's TALENT action.
        restock_tp:  buy a Town Portal Scroll whenever the hero has none left; away from the shop it
                     waits in the stash for the COURIER action.
        step_timeout: seconds without a new world state before the episode is truncated with `info["error"]`.
        replay_dir:  record the match and move the .dem there on close(), e.g. "replays";
                     a relative path lands under the current working directory. None records nothing.
        keep_files:  keep the session folder (generated lua, console.log) in the temp dir for debugging.
        session_factory: replaces the Dota bridge, used by the tests.
        """
        assert render_mode is None or render_mode in self.metadata['render_modes']
        self.render_mode = render_mode
        self.observation_space = observation_space
        self.action_space = actions.action_space

        self.team_id = team_id
        self.step_timeout = step_timeout
        self.reward_fn = reward_fn or LaningReward()
        self.rules = rules or Mid1v1Rules()
        self.starting_items = tuple(starting_items)
        self.ability_priority = tuple(ability_priority)
        self.restock_tp = restock_tp
        self._session_factory = session_factory
        self._session_kwargs = {
            'team_id': team_id,
            'keep_files': keep_files,
            'replay_dir': replay_dir,
            'host_timescale': timescale,
            'ticks_per_observation': ticks_per_observation,
            'game_mode': DOTA_GAMEMODE_1V1MID,
            'host_mode': HOST_MODE_GUI if render_mode == 'human' else HOST_MODE_DEDICATED,
            'dota_path': dota_path,
            'heroes': {team_id: (hero,), opposing(team_id): (opponent_hero,)},
            'control': {team_id: CONTROL_AGENT, opposing(team_id): opponent},
        }
        self._session = None
        self.trees = None  # the TreeTable of the current episode, fed every world state the session returns
        warn_if_stale(dota_path or get_default_game_path())
        self.replay_path = None  # the .dem of the last episode, once close() has collected it
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
        self.trees = TreeTable()
        seen = []
        while True:
            frames = self._session.observe(timeout=max(deadline - time.time(), 0.001))
            for world_state in frames:
                self.trees.update(world_state)
            seen += frames
            hero = find_hero(world_state, self.team_id)
            if hero is not None:
                break
        self._player_id = hero.player_id
        self._ability_names = {}
        self._pending_extra_actions = [actions.purchase_item(self._player_id, item) for item in self.starting_items]
        self.reward_fn.reset(world_state, self.team_id)
        self.rules.reset(world_state, self.team_id)
        self.rules(world_state)
        self._set_state(world_state)
        info = self._info()
        info['world_states'] = seen
        return self._observation.arrays, info

    def step(self, action):
        assert self._session is not None, 'call reset() first'
        bridge_action = actions.to_bridge_action(action, self._observation, self._player_id, self.cast_slots())
        if bridge_action is None:
            bridge_action = {'actionType': 'DOTA_UNIT_ORDER_NONE', 'player': self._player_id}

        extra_actions, self._pending_extra_actions = self._pending_extra_actions, []
        # purchases / skill points that went down with a lost action file are sent again
        delivery = self._session.poll_delivery()
        extra_actions += delivery.lost_extra_actions
        delivery.lost_extra_actions = []
        hero = self._observation.hero
        if hero is not None:
            courier = self._observation.courier
            extra_actions += actions.upkeep(self._player_id, hero, courier, self.ability_priority, self.restock_tp)
        self._session.act(self._world_state.dota_time, [bridge_action], extra_actions)

        previous = self._world_state
        deadline = time.time() + self.step_timeout
        while True:
            try:
                frames = self._session.observe(timeout=min(1.0, self.step_timeout))
                break
            except queue.Empty:
                if time.time() >= deadline or self._session.match_winner() is not None:
                    return self._feed_ended()
        for world_state in frames:
            self.trees.update(world_state)
        terminated, truncated, winner = self.rules(world_state)
        reward, components = self.reward_fn(previous, world_state, winner)
        self._set_state(world_state)
        info = self._info()
        info['reward'] = components
        info['winner'] = winner
        info['world_states'] = frames
        return self._observation.arrays, float(reward), terminated, truncated, info

    def _feed_ended(self):
        """No new world state: either the client ended the match (it stops streaming at once, so the
        deciding death / tower kill never shows up in a world state) or it crashed."""
        winner = self._session.match_winner()
        info = self._info()
        info['winner'] = winner
        info['world_states'] = []
        if winner is None:
            logger.warning(f'no world state for {self.step_timeout:.0f}s, truncating the episode')
            info.update(reward=dict.fromkeys(self.reward_fn.weights, 0.0), error='worldstate feed ended')
            return self._observation.arrays, 0.0, False, True, info
        reward, info['reward'] = self.reward_fn(self._world_state, self._world_state, winner)
        return self._observation.arrays, float(reward), True, False, info

    def render(self):
        if self.render_mode == 'ansi' and self._world_state is not None:
            return describe(
                self._world_state, self.team_id, self._player_id, self.cast_slots(), trees=self.trees, mode='mid1v1'
            )
        return None

    def close(self):
        if self._session is not None:
            self._session.close()
            self.replay_path = self._session.replay_path
            self._session = None

    # -- extras ----------------------------------------------------------------------------------

    def queue_purchase(self, item_name):
        """Buy `item_name` (e.g. "item_boots") with the next step."""
        self._pending_extra_actions.append(actions.purchase_item(self._player_id, item_name))

    def queue_train_ability(self, ability):
        """Level an ability with the next step; `ability` is a name or "slot:N"."""
        self._pending_extra_actions.append(actions.train_ability(self._player_id, ability))

    def queue_chat(self, message, all_chat=True):
        """Say `message` with the next step; `all_chat` False keeps it inside the team."""
        self._pending_extra_actions.append(actions.chat(self._player_id, message, all_chat))

    def queue_label(self, text: str) -> None:
        """Show text over our hero's health bar from the next step on, until another label replaces it.

        Only a game window (render_mode "human") shows it; see docs/SERVER_VM.md.
        """
        self._pending_extra_actions.append(actions.label(self._player_id, text))

    def ability_names(self):
        """{slot: ability name} of our hero as reported by the running client (empty until lua has started)."""
        if not self._ability_names and self._session is not None:
            record = self._session.lua_status().get(self._player_id, {})
            self._ability_names = {int(slot): name for slot, name in record.get('abilities', {}).items()}
        return self._ability_names

    def cast_slots(self) -> dict[int, tuple[str, tuple[str, ...]]]:
        """{ability index: (name, kinds)} of our hero as lua last reported them (empty until it has).

        Indices are those of the ability action: skills 0-5, inventory slots 6-11. kinds says what the slot can
        be aimed at (no_target, self, enemy, point) and is empty for a passive.
        """
        return dict(self._session.cast_slots.get(self._player_id, {})) if self._session is not None else {}

    def action_masks(self):
        return actions.build_action_mask(self._observation, self.team_id, self.cast_slots())

    def sample_legal_action(self):
        return actions.sample_masked_action(self.action_masks(), self.np_random)

    def _set_state(self, world_state: CMsgBotWorldState) -> None:
        self._world_state = world_state
        self._observation = build_observation(world_state, self.team_id, self._player_id, trees=self.trees)

    def _info(self):
        return {
            'action_mask': self.action_masks(),
            'world_state': self._world_state,
            'dota_time': self._world_state.dota_time,
            'action_delivery': self._session.poll_delivery().summary(),
            'skipped_observations': self._session.skipped_observations,
        }
