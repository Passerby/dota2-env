"""Gymnasium environments for Dota 2, driven through the bot-script API.

    import gymnasium as gym, dota2_env
    env = gym.make("dota2_env/Mid1v1-v0")
"""
from gymnasium.envs.registration import register

from dota2_env.actions import ActionType, action_mask_space, action_space  # noqa: F401
from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT  # noqa: F401
from dota2_env.envs.mid1v1 import DotaMid1v1Env  # noqa: F401
from dota2_env.observation import observation_space  # noqa: F401
from dota2_env.rewards import LaningReward, Mid1v1Rules  # noqa: F401
from dota2_env.text import describe  # noqa: F401

register(id='dota2_env/Mid1v1-v0', entry_point='dota2_env.envs.mid1v1:DotaMid1v1Env', disable_env_checker=True)
