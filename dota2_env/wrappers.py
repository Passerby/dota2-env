"""Adapters between the dict-based env and what RL libraries / LLM agents usually want."""

import json

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from dota2_env.actions import N_MOVE_DIRECTIONS, ActionType
from dota2_env.observation import MAX_UNITS, N_ABILITIES
from dota2_env.text import describe


class FlatActionWrapper(gym.ActionWrapper):
    """MultiDiscrete([type, move, target, ability]) instead of a Dict, for libraries without Dict actions."""

    def __init__(self, env):
        super().__init__(env)
        self.action_space = spaces.MultiDiscrete([len(ActionType), N_MOVE_DIRECTIONS, MAX_UNITS, N_ABILITIES])

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
    {"type": "ATTACK", "target": 3}, {"type": "MOVE", "move": 4}, {"type": "CAST", "ability": 0}.

    Unparseable or illegal actions become NOOP and are reported in `info["action_error"]`.
    """

    def __init__(self, env):
        super().__init__(env)
        self.observation_space = spaces.Text(max_length=1 << 16)
        self.action_space = spaces.Text(max_length=1 << 10)

    def _text(self, info):
        base = self.env.unwrapped
        return describe(info['world_state'], base.team_id, ability_names=base.ability_names())

    def reset(self, **kwargs):
        _, info = self.env.reset(**kwargs)
        return self._text(info), info

    def step(self, action):
        parsed, error = self.parse_action(action, self.env.unwrapped.action_masks())
        _, reward, terminated, truncated, info = self.env.step(parsed)
        info['action_error'] = error
        return self._text(info), reward, terminated, truncated, info

    @staticmethod
    def parse_action(action, mask):
        noop = {'type': int(ActionType.NOOP), 'move': 0, 'target': 0, 'ability': 0}
        try:
            data = json.loads(action) if isinstance(action, str) else dict(action)
            action_type = ActionType[str(data['type']).upper()]
            parsed = dict(
                noop,
                type=int(action_type),
                move=int(data.get('move', 0)) % N_MOVE_DIRECTIONS,
                target=int(data.get('target', 0)),
                ability=int(data.get('ability', 0)),
            )
        except (ValueError, KeyError, TypeError) as e:
            return noop, f'cannot parse action: {e!r}'

        if not mask['type'][action_type]:
            return noop, f'{action_type.name} is not legal right now'
        if action_type in (ActionType.CAST, ActionType.CAST_TARGET) and not (
            0 <= parsed['ability'] < N_ABILITIES and mask['ability'][parsed['ability']]
        ):
            return noop, f'ability {parsed["ability"]} is not castable'
        target_mask = {ActionType.ATTACK: mask['attack_target'], ActionType.CAST_TARGET: mask['cast_target']}
        if action_type in target_mask and not (
            0 <= parsed['target'] < MAX_UNITS and target_mask[action_type][parsed['target']]
        ):
            return noop, f'target {parsed["target"]} is not valid for {action_type.name}'
        return parsed, None
