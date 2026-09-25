"""Adapters between the dict-based env and what RL libraries / LLM agents usually want."""

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from dota2_env import actions
from dota2_env.actions import N_CAST_SLOTS, N_MOVE_DIRECTIONS, ActionType
from dota2_env.observation import MAX_UNITS
from dota2_env.text import describe


class FlatActionWrapper(gym.ActionWrapper):
    """MultiDiscrete([type, move, target, ability]) instead of a Dict, for libraries without Dict actions.

    1v1 only: the 5v5 action space is already MultiDiscrete, one column per hero. The types from MOVE_TO on are
    left out: MOVE_TO and TP need a point, which no MultiDiscrete carries, and cutting there keeps the rest's
    numbers."""

    def __init__(self, env):
        super().__init__(env)
        self.action_space = spaces.MultiDiscrete([ActionType.MOVE_TO, N_MOVE_DIRECTIONS, MAX_UNITS, N_CAST_SLOTS])

    def action(self, action):
        action_type, move, target, ability = (int(value) for value in action)
        return {'type': action_type, 'move': move, 'target': target, 'ability': ability}


class FlatObservationWrapper(gym.ObservationWrapper):
    """All observation arrays concatenated into one float32 vector."""

    def __init__(self, env):
        super().__init__(env)
        self._keys = sorted(env.observation_space.spaces)
        size = sum(int(np.prod(env.observation_space[key].shape)) for key in self._keys)
        self.observation_space = spaces.Box(-np.inf, np.inf, (size,), np.float32)

    def observation(self, observation):
        return np.concatenate([np.asarray(observation[key], np.float32).ravel() for key in self._keys])


class TextWrapper(gym.Wrapper):
    """For LLM agents: observations are text, actions are JSON strings (or dicts) such as
    {"type": "ATTACK", "target": 3}, {"type": "MOVE_TO", "point": [1180, -1216]}, {"type": "MOVE", "move": 4},
    {"type": "CAST_DIRECTION", "ability": 0, "move": 2}.

    Unparseable or illegal actions become NOOP and are reported in `info["action_error"]`.
    1v1 only; the 5v5 env renders its own per-hero text through render().
    """

    def __init__(self, env):
        super().__init__(env)
        self.observation_space = spaces.Text(max_length=1 << 16)
        self.action_space = spaces.Text(max_length=1 << 10)

    def _text(self, info):
        base = self.env.unwrapped
        return describe(
            info['world_state'], base.team_id, cast_slots=base.cast_slots(), trees=base.trees, mode='mid1v1'
        )

    def reset(self, **kwargs):
        _, info = self.env.reset(**kwargs)
        return self._text(info), info

    def step(self, action):
        parsed, error = actions.parse_action(action, self.env.unwrapped.action_masks())
        _, reward, terminated, truncated, info = self.env.step(parsed)
        info['action_error'] = error
        return self._text(info), reward, terminated, truncated, info
