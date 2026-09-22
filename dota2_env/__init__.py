"""Gymnasium environments for Dota 2, driven through the bot-script API.

>>> import gymnasium as gym, dota2_env
>>> env = gym.make('dota2_env/Mid1v1-v0')   # or 'dota2_env/AllPick5v5-v0'
"""

from gymnasium.envs.registration import register

from dota2_env.actions import ActionType, action_mask_space, action_space, team_action_mask_space, team_action_space
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.envs.allpick5v5 import DotaAllPick5v5Env
from dota2_env.envs.mid1v1 import DotaMid1v1Env
from dota2_env.observation import observation_space, team_observation_space
from dota2_env.rewards import AllPick5v5Rules, LaningReward, Mid1v1Rules, TeamReward
from dota2_env.text import describe, describe_team

__all__ = [
    'TEAM_DIRE',
    'TEAM_RADIANT',
    'ActionType',
    'AllPick5v5Rules',
    'DotaAllPick5v5Env',
    'DotaMid1v1Env',
    'LaningReward',
    'Mid1v1Rules',
    'TeamReward',
    'action_mask_space',
    'action_space',
    'describe',
    'describe_team',
    'observation_space',
    'team_action_mask_space',
    'team_action_space',
    'team_observation_space',
]

register(id='dota2_env/Mid1v1-v0', entry_point='dota2_env.envs.mid1v1:DotaMid1v1Env', disable_env_checker=True)
register(
    id='dota2_env/AllPick5v5-v0', entry_point='dota2_env.envs.allpick5v5:DotaAllPick5v5Env', disable_env_checker=True
)
