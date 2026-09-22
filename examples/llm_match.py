"""Run a YAML-configured LLM match: one LLM per hero, deciding while the game keeps running.

Needs the optional extra: uv pip install -e ".[llm]". API keys come from the environment or from .env
at the repo root (cp .env.example .env).

    python examples/llm_match.py --config configs/match.example.yaml --dry-run
    python examples/llm_match.py --config configs/match.example.yaml --steps 3000
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import gymnasium as gym
import numpy as np
from dotenv import load_dotenv

import dota2_env  # noqa: F401  (registers the environments)
from dota2_env.llm.agent import system_prompt
from dota2_env.llm.config import load_match
from dota2_env.llm.gateway import Gateway
from dota2_env.llm.runner import TeamRunner
from dota2_env.observation import team_player_ids
from dota2_env.rewards import AllPick5v5Rules, Mid1v1Rules

ACTION_KEYS = ('type', 'move', 'target', 'ability')


def make_env(match, render_mode):
    """The agent team, and a gym env with the opposing team left to the client."""
    agent_team = match.radiant if match.radiant.control == 'agent' else match.dire
    other = match.dire if agent_team is match.radiant else match.radiant
    if other.control == 'agent':
        raise ValueError('two agent teams need a shared-game driver, which is not built yet; set one to builtin/idle')
    shared = {
        'render_mode': render_mode,
        'team_id': agent_team.team_id,
        'opponent': other.control,
        'timescale': match.timescale,
        'ticks_per_observation': match.ticks_per_observation,
    }
    if match.mode == 'allpick5v5':
        env = gym.make(
            'dota2_env/AllPick5v5-v0',
            heroes=tuple(agent_team.heroes),
            opponent_heroes=tuple(other.heroes),
            rules=AllPick5v5Rules(max_dota_time=match.max_dota_time),
            **shared,
        )
    else:
        env = gym.make(
            'dota2_env/Mid1v1-v0',
            hero=agent_team.heroes[0],
            opponent_hero=other.heroes[0],
            rules=Mid1v1Rules(max_dota_time=match.max_dota_time),
            **shared,
        )
    return env, agent_team


def hero_views(info, mode, team_id, rows):
    """Per-hero action masks and the player ids behind them, whichever env shape info came from."""
    if mode == 'allpick5v5':
        masks = [{key: values[row] for key, values in info['action_mask'].items()} for row in range(rows)]
        return masks, list(info['player_ids'])
    return [info['action_mask']], team_player_ids(info['world_state'], team_id)[:1]


def pack(hero_actions, mode):
    if mode == 'allpick5v5':
        return {key: np.array([action[key] for action in hero_actions], np.int64) for key in ACTION_KEYS}
    return hero_actions[0]


def say(env, mode, row, message):
    if mode == 'allpick5v5':
        env.unwrapped.queue_chat(row, message)
    else:
        env.unwrapped.queue_chat(message)


def dry_run(match):
    """Validate the config and show what every agent would be sent, without Dota and without spending."""
    print(f'mode {match.mode} timescale {match.timescale} ticks/obs {match.ticks_per_observation}')
    print(f'max_dota_time {match.max_dota_time:.0f}s  spend limit ${match.spend_limit_usd:.2f}')
    for name, gateway in match.gateways.items():
        key = gateway.api_key
        print(
            f'  gateway {name}: {gateway.base_url} model={gateway.model} '
            f'key={"set" if key else "MISSING"} ({len(key)} chars) timeout={gateway.timeout_seconds}s'
        )
    for team in (match.radiant, match.dire):
        print(f'\n--- team {team.team_id} control={team.control} ---')
        for row, config in enumerate(team.agents):
            prompt = system_prompt(config, match, team.team_id)
            print(
                f'  [{row}] {config.nickname} {config.hero} {config.position} via {config.gateway} '
                f'every {config.decision_interval}s  system prompt {len(prompt)} chars (~{len(prompt) // 3} tokens)'
            )
        if team.control != 'agent':
            print(f'  heroes: {", ".join(team.heroes)}')
    print('\n' + system_prompt(match.radiant.agents[0], match, match.radiant.team_id))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/match.example.yaml')
    parser.add_argument('--steps', type=int, default=3000)
    parser.add_argument('--dry-run', action='store_true', help='validate the config and print prompts, no Dota')
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()

    load_dotenv()
    match = load_match(args.config)
    if args.dry_run:
        dry_run(match)
        return

    os.makedirs(match.log_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    log_path = os.path.join(match.log_dir, f'{stamp}.jsonl')
    gateways = {name: Gateway(config) for name, config in match.gateways.items()}
    env, agent_team = make_env(match, 'human' if args.render or match.render else None)
    rows = len(agent_team.agents)

    with open(log_path, 'w') as transcript:
        runner = TeamRunner(match, agent_team, gateways, transcript)
        start = time.time()
        step = 0
        winner = None
        try:
            _, info = env.reset()
            for step in range(args.steps):
                masks, player_ids = hero_views(info, match.mode, agent_team.team_id, rows)
                slots = env.unwrapped.cast_slots()
                # the 1v1 env reports {index: slot}; hero_blocks wants it keyed by player id, as 5v5 reports it
                if match.mode == 'mid1v1':
                    slots = {player_ids[0]: slots}
                hero_actions, chat = runner.decide(info['world_state'], masks, player_ids, slots)
                for row, message in chat:
                    say(env, match.mode, row, message)
                    print(f'[{runner.slots[row].config.nickname}] {message}')
                _, _, terminated, truncated, info = env.step(pack(hero_actions, match.mode))
                if step % 100 == 0:
                    print(
                        'step {} t={:.0f} spent ${:.3f} {:.1f} steps/s'.format(
                            step, info['dota_time'], runner.usd, (step + 1) / (time.time() - start)
                        )
                    )
                if runner.usd >= match.spend_limit_usd:
                    print(f'spend limit ${match.spend_limit_usd:.2f} reached, ending the match')
                    break
                if terminated or truncated:
                    winner = info['winner']
                    print(f'match over at t={info["dota_time"]:.0f}, winner: {winner}')
                    break
        finally:
            runner.close()
            env.close()
            summary = runner.summary()
            transcript.write(json.dumps({'kind': 'summary', 'steps': step, 'winner': winner, 'agents': summary}) + '\n')

    for entry in summary:
        print(
            '{:20} {:>5} req {:>5} held {:>4} rejected {:>7} tok ${:.4f} {}s mean'.format(
                entry['nickname'],
                entry['requests'],
                entry['held_frames'],
                entry['rejected_actions'],
                entry['input_tokens'] + entry['output_tokens'],
                entry['usd'],
                entry['mean_latency'],
            )
        )
    print(f'total ${sum(entry["usd"] for entry in summary):.4f}  transcript {log_path}')


if __name__ == '__main__':
    main()
