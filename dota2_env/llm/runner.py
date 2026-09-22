"""The decision loop: one LLM per hero, deciding while the game keeps running.

Deliberately env-agnostic - it takes a world state, per-hero action masks and player ids, and returns
one action dict per hero. The caller packs those into whichever action space its env uses, which is
also what lets a second team be driven by a second runner later.
"""

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import TextIO

import numpy as np

from dota2_env import actions
from dota2_env.llm import agent
from dota2_env.llm.config import AgentConfig, MatchConfig, TeamConfig
from dota2_env.llm.gateway import Gateway
from dota2_env.observation import build_team_observation
from dota2_env.text import hero_blocks

logger = logging.getLogger('dota2_env')

STUCK_TIMEOUT_FACTOR = 3.0  # warn once a worker has been in flight this many times its own timeout


@dataclass(kw_only=True)
class Request:
    """What crosses into the worker thread: immutable once submitted."""

    messages: list[dict[str, str]]
    params: dict[str, object]
    dota_time: float
    unit_handles: list[int]


@dataclass(kw_only=True)
class Slot:
    """One hero's agent. Every counter here is written by the main thread only.

    The worker owns nothing but its locals and the two queues, so there is no lock: inbox and outbox
    are thread-safe, busy is an Event, and the main thread is the single writer of everything else.
    """

    config: AgentConfig
    system: str
    gateway: Gateway
    inbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    outbox: queue.Queue = field(default_factory=queue.Queue)
    busy: threading.Event = field(default_factory=threading.Event)
    requested_dota_time: float = float('-inf')  # so every agent submits on the very first frame
    last_note: str = '你还没有下过命令'
    plan: list[dict] = field(default_factory=list)  # raw actions left to carry out, one per frame
    plan_handles: list[int] = field(default_factory=list)  # unit rows of the frame the plan was made on
    requests: int = 0
    replies: int = 0
    errors: int = 0
    parse_errors: int = 0
    steps: int = 0
    holds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_total: float = 0.0


def work(slot: Slot) -> None:
    """Worker body: block for a prompt, post the reply back, never touch anything else."""
    while True:
        request = slot.inbox.get()
        if request is None:
            return
        reply = slot.gateway.complete(request.messages, request.params)
        slot.outbox.put((request, reply))
        slot.busy.clear()


class TeamRunner:
    def __init__(
        self,
        match: MatchConfig,
        team: TeamConfig,
        gateways: dict[str, Gateway],
        transcript: TextIO | None = None,
    ):
        self.team_id = team.team_id
        self.match = match
        self.agents = team.agents
        self.transcript = transcript
        self.slots = [
            Slot(
                config=config,
                system=agent.system_prompt(config, match, team.team_id),
                gateway=gateways[config.gateway],
            )
            for config in team.agents
        ]
        self.threads = [threading.Thread(target=work, args=(slot,), daemon=True) for slot in self.slots]
        for thread in self.threads:
            thread.start()

    @property
    def usd(self) -> float:
        return sum(slot.usd for slot in self.slots)

    def record(self, kind: str, **fields: object) -> None:
        if self.transcript is not None:
            self.transcript.write(json.dumps({'kind': kind, 'team': self.team_id, **fields}) + '\n')

    def decide(
        self,
        world_state,
        masks: list[dict[str, np.ndarray]],
        player_ids: list[int],
        cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]],
    ) -> tuple[list[dict[str, int]], list[tuple[int, str]]]:
        """One action dict per hero plus the (row, message) chat lines to queue before stepping.

        cast_slots is the env's, keyed by player id: it names the skills and items in the prompt.
        A hero whose plan has run out keeps its last order, which NOOP does not disturb.
        """
        observation = build_team_observation(world_state, self.team_id, player_ids)
        handles = [hero.unit_handles for hero in observation.heroes]
        dota_time = world_state.dota_time
        hero_actions = [dict(actions.NOOP_ACTION) for _ in self.slots]
        chat = []

        for row, slot in enumerate(self.slots):
            said = self.absorb(slot, dota_time)
            if said:
                chat.append((row, said))
            if not slot.plan:
                slot.holds += 1
                continue
            step = slot.plan.pop(0)
            action, error = agent.resolve(step, masks[row], slot.plan_handles, handles[row])
            hero_actions[row] = action
            slot.steps += 1
            slot.last_note = f'你上一条命令被拒绝了：{error}' if error else '你上一条命令已下达'
            if error:
                slot.parse_errors += 1
                self.record('rejected', nickname=slot.config.nickname, dota_time=dota_time, step=step, error=error)

        due = [row for row, slot in enumerate(self.slots) if self.is_due(slot, dota_time)]
        if due:
            blocks = hero_blocks(world_state, self.team_id, player_ids, cast_slots)
            for row in due:
                self.submit(row, world_state, blocks[row], handles[row], player_ids, dota_time)
        return hero_actions, chat

    def absorb(self, slot: Slot, dota_time: float) -> str | None:
        """Take the reply waiting for this agent, if any, and make it the plan it now works through.

        busy lets one request be in flight at a time, so at most one reply is ever waiting here.
        """
        try:
            request, reply = slot.outbox.get_nowait()
        except queue.Empty:
            return None
        slot.replies += 1
        slot.input_tokens += reply.input_tokens
        slot.output_tokens += reply.output_tokens
        slot.usd += reply.usd
        slot.latency_total += reply.latency
        record = {
            'nickname': slot.config.nickname,
            'dota_time': dota_time,
            'decided_at': round(request.dota_time, 2),
            'latency': round(reply.latency, 3),
            'input_tokens': reply.input_tokens,
            'output_tokens': reply.output_tokens,
            'usd': round(reply.usd, 6),
        }
        if self.match.log_prompts:
            record['prompt'] = request.messages[-1]['content']
        if reply.error is not None:
            slot.errors += 1
            slot.last_note = f'你上一条命令没能送达：{reply.error}'
            self.record('decision', **record, error=reply.error)
            return None
        plan, reason, said, error = agent.read_plan(reply.text, self.match.all_chat, self.match.plan_length)
        slot.plan, slot.plan_handles = plan, list(request.unit_handles)
        if error:
            slot.parse_errors += 1
            slot.last_note = f'你上一次的回复无法使用：{error}'
        self.record('decision', **record, reply=reply.text, reason=reason, plan=plan, say=said, error=error)
        return said

    def is_due(self, slot: Slot, dota_time: float) -> bool:
        if slot.busy.is_set():
            stuck = dota_time - slot.requested_dota_time
            if stuck > STUCK_TIMEOUT_FACTOR * slot.gateway.config.timeout_seconds:
                logger.warning(f'{slot.config.nickname} has been waiting {stuck:.0f} game seconds for a reply')
            return False
        return dota_time - slot.requested_dota_time >= slot.config.decision_interval

    def submit(
        self, row: int, world_state, block: str, unit_handles: list[int], player_ids: list[int], dota_time: float
    ) -> None:
        slot = self.slots[row]
        parts = [block]
        if self.match.share_team_state and len(player_ids) > 1:
            parts.append(agent.team_summary(world_state, self.team_id, player_ids, self.agents, row))
        parts.append(slot.last_note)
        request = Request(
            messages=[
                {'role': 'system', 'content': slot.system},
                {'role': 'user', 'content': '\n\n'.join(parts)},
            ],
            params=dict(slot.config.params),
            dota_time=dota_time,
            unit_handles=list(unit_handles),
        )
        slot.busy.set()
        slot.requested_dota_time = dota_time
        slot.requests += 1
        slot.inbox.put(request)

    def summary(self) -> list[dict[str, object]]:
        return [
            {
                'nickname': slot.config.nickname,
                'hero': slot.config.hero,
                'position': slot.config.position,
                'gateway': slot.config.gateway,
                'requests': slot.requests,
                'replies': slot.replies,
                'gateway_errors': slot.errors,
                'rejected_actions': slot.parse_errors,
                'planned_steps': slot.steps,
                'held_frames': slot.holds,
                'input_tokens': slot.input_tokens,
                'output_tokens': slot.output_tokens,
                'usd': round(slot.usd, 4),
                'mean_latency': round(slot.latency_total / slot.replies, 2) if slot.replies else None,
            }
            for slot in self.slots
        ]

    def close(self) -> None:
        for slot in self.slots:
            slot.inbox.put(None)
