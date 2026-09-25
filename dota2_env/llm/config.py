"""The YAML match file: LLM gateways, the agents that use them, and the match itself.

Not imported from dota2_env/__init__.py on purpose: this subpackage needs pyyaml and httpx, and the
environments themselves stay installable with protobuf, gymnasium and numpy alone.

    from dota2_env.llm.config import load_match
    match = load_match('configs/match.example.yaml')
"""

import os
import re
from dataclasses import dataclass, field
from typing import Literal

import yaml

from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.game_text import load_records
from dota2_env.map_features import GameMode as Mode
from dota2_env.observation import TEAM_SIZE

Control = Literal['agent', 'builtin', 'idle']
Position = Literal['safe', 'mid', 'offlane', 'support', 'hard_support']

CONTROLS: tuple[Control, ...] = ('agent', 'builtin', 'idle')
MODES: tuple[Mode, ...] = ('mid1v1', 'allpick5v5')
POSITIONS: tuple[Position, ...] = ('safe', 'mid', 'offlane', 'support', 'hard_support')

RE_ENV = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


@dataclass(kw_only=True)
class GatewayConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    timeout_seconds: float = 8.0
    params: dict[str, object] = field(default_factory=dict)


@dataclass(kw_only=True)
class AgentConfig:
    nickname: str
    hero: str
    position: Position
    gateway: str
    decision_interval: float = 1.0  # game seconds between requests, not wall clock
    params: dict[str, object] = field(default_factory=dict)


@dataclass(kw_only=True)
class TeamConfig:
    team_id: int
    control: Control
    agents: list[AgentConfig]
    heroes: list[str]


@dataclass(kw_only=True)
class MatchConfig:
    mode: Mode = 'allpick5v5'
    timescale: float = 4.0
    ticks_per_observation: int = 6
    max_dota_time: float = 600.0
    spend_limit_usd: float = 2.0
    log_dir: str = 'logs'
    all_chat: bool = True
    show_reason: bool = True  # each hero's latest REASON shown over its health bar; only a game window shows it
    share_team_state: bool = True
    render: bool = False
    plan_length: int = 1  # actions per reply, carried out one per frame; 1 is a single action as before
    history_length: int = 6  # the agent's last commands, with where its hero stood, each prompt ends with
    log_prompts: bool = False  # also store every user message in the transcript, for debugging
    gateways: dict[str, GatewayConfig]
    radiant: TeamConfig
    dire: TeamConfig

    def team(self, team_id: int) -> TeamConfig:
        return self.radiant if team_id == TEAM_RADIANT else self.dire


def expand_env(value: object) -> object:
    """Replace every ${VAR} inside a parsed YAML tree with that environment variable."""
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if not isinstance(value, str):
        return value
    missing = [name for name in RE_ENV.findall(value) if name not in os.environ]
    if missing:
        raise ValueError(f'the config references unset environment variables: {", ".join(missing)}')
    return RE_ENV.sub(lambda match: os.environ[match.group(1)], value)


def build_gateway(name: str, data: dict[str, object]) -> GatewayConfig:
    price = data.get('price_per_mtok') or {}
    return GatewayConfig(
        name=name,
        base_url=str(data['base_url']).rstrip('/'),
        api_key=str(data['api_key']),
        model=str(data['model']),
        input_usd_per_mtok=float(price.get('input', 0.0)),
        output_usd_per_mtok=float(price.get('output', 0.0)),
        timeout_seconds=float(data.get('timeout_seconds', 8.0)),
        params=dict(data.get('params') or {}),
    )


def build_team(team_id: int, data: dict[str, object], gateways: dict[str, GatewayConfig], team_size: int) -> TeamConfig:
    control = str(data.get('control', 'builtin'))
    if control not in CONTROLS:
        raise ValueError(f'control must be one of {CONTROLS}, got {control!r}')
    agents = []
    for entry in data.get('agents') or []:
        position = str(entry['position'])
        if position not in POSITIONS:
            raise ValueError(f'position must be one of {POSITIONS}, got {position!r}')
        if entry['gateway'] not in gateways:
            raise ValueError(f'agent {entry["nickname"]!r} uses undefined gateway {entry["gateway"]!r}')
        agents.append(
            AgentConfig(
                nickname=str(entry['nickname']),
                hero=str(entry['hero']),
                position=position,
                gateway=str(entry['gateway']),
                decision_interval=float(entry.get('decision_interval', 1.0)),
                params=dict(entry.get('params') or {}),
            )
        )
    heroes = [agent.hero for agent in agents] if control == 'agent' else [str(hero) for hero in data['heroes']]
    unknown = [hero for hero in heroes if hero not in load_records('heroes')]
    if unknown:
        raise ValueError(f'no such hero: {", ".join(unknown)} (the unit name, such as npc_dota_hero_lina)')
    if len(heroes) != team_size:
        raise ValueError(f'team {team_id} needs {team_size} heroes, got {len(heroes)}')
    if control == 'agent' and len(agents) != team_size:
        raise ValueError(f'team {team_id} is agent-controlled and needs {team_size} agents, got {len(agents)}')
    return TeamConfig(team_id=team_id, control=control, agents=agents, heroes=heroes)


def load_match(path: str) -> MatchConfig:
    """Parse a match YAML file, expanding ${VAR} references against the environment."""
    with open(path) as f:
        raw = expand_env(yaml.safe_load(f))
    settings = dict(raw.get('match') or {})
    mode = str(settings.pop('mode', 'allpick5v5'))
    if mode not in MODES:
        raise ValueError(f'mode must be one of {MODES}, got {mode!r}')
    team_size = TEAM_SIZE if mode == 'allpick5v5' else 1
    gateways = {name: build_gateway(name, data) for name, data in (raw.get('gateways') or {}).items()}
    radiant = build_team(TEAM_RADIANT, raw.get('radiant') or {}, gateways, team_size)
    dire = build_team(TEAM_DIRE, raw.get('dire') or {}, gateways, team_size)
    if settings.get('plan_length', 1) < 1:
        raise ValueError('plan_length must be at least 1')
    if settings.get('history_length', 1) < 1:
        raise ValueError('history_length must be at least 1')
    if 'agent' not in (radiant.control, dire.control):
        raise ValueError('at least one team must have control: agent, otherwise no LLM plays')
    # All Pick will not hand the same hero to both sides, and a repeat inside one team is a wasted slot.
    lineup = radiant.heroes + dire.heroes
    if len(set(lineup)) != len(lineup):
        repeated = sorted({hero for hero in lineup if lineup.count(hero) > 1})
        raise ValueError(f'every hero in the match must be different, these repeat: {", ".join(repeated)}')
    return MatchConfig(mode=mode, gateways=gateways, radiant=radiant, dire=dire, **settings)
