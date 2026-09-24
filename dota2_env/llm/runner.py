"""The decision loop: one LLM per hero, deciding while the game keeps running.

Deliberately env-agnostic - it takes a world state, per-hero action masks and player ids, and returns
one action dict per hero. The caller packs those into whichever action space its env uses, which is
also what lets a second team be driven by a second runner later.
"""

import json
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TextIO

import numpy as np

from dota2_env import actions
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.llm import agent
from dota2_env.llm.config import AgentConfig, MatchConfig, TeamConfig
from dota2_env.llm.gateway import Gateway, Reply
from dota2_env.map_features import TreeTable
from dota2_env.observation import N_ABILITIES, build_team_observation, find_hero
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
class Reading:
    """What has streamed in so far of the reply to one request."""

    request: Request | None = None
    steps: list[str] = field(default_factory=list)  # at most plan_length
    reason: str = ''
    said: str | None = None


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
    # each line of a reply as it streams in, then the reply as a whole
    outbox: queue.Queue[tuple[Request, str | Reply]] = field(default_factory=queue.Queue)
    busy: threading.Event = field(default_factory=threading.Event)
    requested_dota_time: float = float('-inf')  # so every agent submits on the very first frame
    history: deque[agent.Note] = field(default_factory=deque)  # its last commands, oldest first
    reading: Reading = field(default_factory=Reading)
    plan: list[str] = field(default_factory=list)  # steps left to carry out, one per frame
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
    """Worker body: block for a prompt, pass each line of the reply back as it streams in, then the reply."""
    while True:
        request = slot.inbox.get()
        if request is None:
            return
        for event in slot.gateway.stream(request.messages, request.params):
            slot.outbox.put((request, event))
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
                history=deque(maxlen=match.history_length),
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
        world_state: CMsgBotWorldState,
        masks: list[dict[str, np.ndarray]],
        player_ids: list[int],
        cast_slots: dict[int, dict[int, tuple[str, tuple[str, ...]]]],
        trees: TreeTable | None = None,
    ) -> tuple[list[actions.Action], list[tuple[int, str]], list[tuple[int, str]]]:
        """One action dict per hero, and the (row, message) chats and (row, reason) labels to queue before stepping.

        cast_slots is the env's, keyed by player id: it names the skills and items in the prompt.
        trees is the env's too (env.trees); without it the prompt has no terrain, rune or landmark lines.
        A hero whose plan has run out keeps its last order, which NOOP does not disturb.
        """
        observation = build_team_observation(world_state, self.team_id, player_ids)
        handles = [hero.unit_handles for hero in observation.heroes]
        dota_time = world_state.dota_time
        hero_actions = [actions.NOOP_ACTION.copy() for _ in self.slots]
        chat, reasons = [], []

        for row, slot in enumerate(self.slots):
            said, reason = self.absorb(slot, dota_time)
            if said:
                chat.append((row, said))
            if reason:
                reasons.append((row, reason))
            if not slot.plan:
                slot.holds += 1
                continue
            step = slot.plan.pop(0)
            hero = observation.heroes[row]
            spot = (hero.hero.location.x, hero.hero.location.y) if hero.hero is not None else hero.origin
            action, error = agent.resolve(step, masks[row], slot.plan_handles, handles[row], spot)
            hero_actions[row] = action
            slot.steps += 1
            # a NOOP leaves the hero at what it was doing, so the history keeps to the commands that changed it
            if error or action['type'] != actions.ActionType.NOOP:
                kind = 'rejected' if error else 'issued'
                slot.history.append(agent.Note(kind=kind, dota_time=dota_time, position=spot, step=step, error=error))
            if error:
                slot.parse_errors += 1
                self.record('rejected', nickname=slot.config.nickname, dota_time=dota_time, step=step, error=error)

        due = [row for row, slot in enumerate(self.slots) if self.is_due(slot, dota_time)]
        if due:
            blocks = hero_blocks(world_state, self.team_id, player_ids, cast_slots, trees)
            for row in due:
                items = [
                    name
                    for index, (name, _) in sorted(cast_slots.get(player_ids[row], {}).items())
                    if index >= N_ABILITIES
                ]
                teammates = [
                    (self.agents[other], find_hero(world_state, self.team_id, player_id))
                    for other, player_id in enumerate(player_ids)
                    if self.match.share_team_state and other != row
                ]
                prompt = agent.user_prompt(blocks[row], items, teammates, self.slots[row].history)
                self.submit(row, prompt, handles[row], dota_time)
        return hero_actions, chat, reasons

    def absorb(self, slot: Slot, dota_time: float) -> tuple[str | None, str | None]:
        """Take what has streamed in for this agent since the last frame; returns the taunt and the reason it carried.

        A reply's first step replaces what is left of the plan before it, so the hero acts on the first
        line while the model is still writing the rest. busy lets one request be in flight at a time,
        so the lines of two replies never interleave here.
        """
        said = reason = None
        while True:
            try:
                request, event = slot.outbox.get_nowait()
            except queue.Empty:
                return said, reason
            if request is not slot.reading.request:
                slot.reading = Reading(request=request)
            reading = slot.reading
            if isinstance(event, str):
                kind, text = agent.read_line(event)
                if kind == 'step' and len(reading.steps) < self.match.plan_length:
                    if not reading.steps:
                        slot.plan, slot.plan_handles = [], list(request.unit_handles)
                    reading.steps.append(text)
                    slot.plan.append(text)
                elif kind == 'reason':
                    reading.reason = text
                    if self.match.show_reason:
                        reason = text
                elif kind == 'say' and self.match.all_chat and reading.said is None:
                    reading.said = said = text
                continue

            slot.replies += 1
            slot.input_tokens += event.input_tokens
            slot.output_tokens += event.output_tokens
            slot.usd += event.usd
            slot.latency_total += event.latency
            error = event.error
            if error is not None:
                slot.errors += 1
            elif not reading.steps:
                error = 'reply carried no actions'
                slot.parse_errors += 1
            # Note (ruidu): a reply cut off after some of its steps still carries those out, into the history too.
            if not reading.steps:
                kind = 'unusable' if event.error is None else 'undelivered'
                slot.history.append(agent.Note(kind=kind, dota_time=dota_time, error=error))
            first_line = event.first_line_latency
            record = {
                'nickname': slot.config.nickname,
                'dota_time': dota_time,
                'decided_at': round(request.dota_time, 2),
                'latency': round(event.latency, 3),
                'first_line_latency': None if first_line is None else round(first_line, 3),
                'input_tokens': event.input_tokens,
                'output_tokens': event.output_tokens,
                'usd': round(event.usd, 6),
            }
            if self.match.log_prompts:
                record['prompt'] = request.messages[-1]['content']
            self.record(
                'decision',
                **record,
                reply=event.text,
                reason=reading.reason,
                plan=reading.steps,
                say=reading.said,
                error=error,
            )

    def is_due(self, slot: Slot, dota_time: float) -> bool:
        if slot.busy.is_set():
            stuck = dota_time - slot.requested_dota_time
            if stuck > STUCK_TIMEOUT_FACTOR * slot.gateway.config.timeout_seconds:
                logger.warning(f'{slot.config.nickname} has been waiting {stuck:.0f} game seconds for a reply')
            return False
        return dota_time - slot.requested_dota_time >= slot.config.decision_interval

    def submit(self, row: int, prompt: str, unit_handles: list[int], dota_time: float) -> None:
        slot = self.slots[row]
        request = Request(
            messages=[
                {'role': 'system', 'content': slot.system},
                {'role': 'user', 'content': prompt},
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
