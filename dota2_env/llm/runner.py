"""The decision loop: one LLM agent per hero, deciding while the game keeps running.

An agent has a per-second channel that writes actions and, when its config asks for one, a long think that writes
the plan the channel works to (think.py); each runs in a worker thread (channel.py), so neither ever holds up the
game or the other. Deliberately env-agnostic - it takes world states, per-hero action masks and player ids, and
returns one action dict per hero. The caller packs those into whichever action space its env uses, which is also
what lets a second team be driven by a second runner later. Every frame goes through sense() first (the env's
info['world_states']), then decide() acts on the newest.
"""

import json
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TextIO

import numpy as np

from dota2_env import actions
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dota2_env.llm import agent, memory
from dota2_env.llm.channel import Channel, Request, reply_record, work
from dota2_env.llm.config import AgentConfig, MatchConfig, TeamConfig
from dota2_env.llm.gateway import Gateway
from dota2_env.llm.think import CastSlots, LongThinks
from dota2_env.map_features import TreeTable
from dota2_env.observation import N_ABILITIES, build_team_observation, find_hero
from dota2_env.text import hero_blocks


@dataclass(kw_only=True)
class Slot:
    """One hero's per-second channel and what it keeps between turns. Written by the main thread only."""

    config: AgentConfig
    act: Channel
    history: deque[agent.Note] = field(default_factory=deque)  # its last commands, oldest first
    plan: list[str] = field(default_factory=list)  # steps left to carry out, one per frame
    plan_handles: list[int] = field(default_factory=list)  # unit rows of the frame the plan was made on
    plan_decided_at: float = 0.0  # dota_time of that frame, which ties a rejected step to its decision record
    parse_errors: int = 0
    steps: int = 0
    holds: int = 0


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
        self.nicknames = [config.nickname for config in team.agents]
        self.tracker = memory.MemoryTracker(team.team_id, len(team.agents))
        self.board = memory.Blackboard(directives=[None] * len(team.agents), intents=[None] * len(team.agents))
        self.player_ids: list[int] = []  # of the rows, as the last decide() was given them
        self.thinks = LongThinks(match, team, gateways, self.board, self.tracker, self.record, self.call)
        self.slots = [
            Slot(
                config=config,
                act=Channel(
                    name=config.nickname,
                    gateway=gateways[config.gateway],
                    system=agent.act_system_prompt(config, match, team.team_id),
                    params=dict(config.params),
                    interval=config.decision_interval,
                ),
                history=deque(maxlen=match.history_length),
            )
            for config in team.agents
        ]
        # Note (ruidu): what every request of an agent carries besides its user message, written once ahead of any
        # decision so a transcript alone shows what the models were told; scripts/prompt_debugger.py reads it.
        for slot, thinker in zip(self.slots, self.thinks.thinkers, strict=True):
            think = None
            if thinker is not None:
                channel = thinker.channel
                think = {
                    'gateway': slot.config.think.gateway,
                    'model': channel.gateway.config.model,
                    'params': {**channel.gateway.config.params, **channel.params},
                    'interval': channel.interval,
                    'system': channel.system,
                }
            self.record(
                'agent',
                nickname=slot.config.nickname,
                hero=slot.config.hero,
                position=slot.config.position,
                gateway=slot.config.gateway,
                model=slot.act.gateway.config.model,
                params={**slot.act.gateway.config.params, **slot.act.params},
                plan_length=match.plan_length,
                system=slot.act.system,
                think=think,
            )
        self.threads = [
            threading.Thread(target=work, args=(channel,), daemon=True)
            for channel in [slot.act for slot in self.slots] + self.thinks.channels
        ]
        for thread in self.threads:
            thread.start()

    @property
    def usd(self) -> float:
        return sum(slot.act.usd for slot in self.slots) + sum(channel.usd for channel in self.thinks.channels)

    def record(self, kind: str, **fields: object) -> None:
        if self.transcript is not None:
            self.transcript.write(json.dumps({'kind': kind, 'team': self.team_id, **fields}) + '\n')

    def sense(self, world_states: list[CMsgBotWorldState]) -> None:
        """Read every frame since the last call, oldest first: the facts they show and the long thinks they ask for."""
        for world_state in world_states:
            goals = [None if directive is None else directive.goal for directive in self.board.directives]
            for event in self.tracker.update(world_state, goals):
                self.record(
                    'event',
                    event=event.kind,
                    dota_time=round(event.dota_time, 2),
                    side=event.team,
                    name=event.name,
                    killer=event.killer,
                    position=event.position,
                )
                if event.kind == 'death' and event.player_id in self.tracker.ours:
                    dead = self.tracker.ours.index(event.player_id)
                    for row in range(len(self.slots)):
                        kind = 'death' if row == dead else 'teammate_death'
                        self.thinks.wake(
                            row, memory.Trigger(kind=kind, dota_time=event.dota_time, event=event, row=dead)
                        )
                elif event.kind in ('tower', 'roshan'):
                    for row in range(len(self.slots)):
                        self.thinks.wake(row, memory.Trigger(kind=event.kind, dota_time=event.dota_time, event=event))
            self.thinks.check_goals(world_state.dota_time)

    def call(self, row: int, text: str, dota_time: float) -> None:
        """Put row's CALL on the blackboard, and wake the long thinks of those it calls."""
        target, message = agent.read_call(text, self.nicknames)
        if not message or target == row:
            return
        expires = dota_time + self.match.call_seconds
        # Note (ruidu): a per-second channel repeats a call every turn it still means it; that only keeps it up.
        if not self.board.post(memory.Call(row=row, target=target, text=message, dota_time=dota_time, expires=expires)):
            return
        for other in range(len(self.slots)):
            if other != row and target in (None, other):
                self.thinks.wake(other, memory.Trigger(kind='call', dota_time=dota_time, row=row, text=message))

    def decide(
        self,
        world_state: CMsgBotWorldState,
        masks: list[dict[str, np.ndarray]],
        player_ids: list[int],
        cast_slots: CastSlots,
        trees: TreeTable | None = None,
    ) -> tuple[list[actions.Action], list[tuple[int, str]], list[tuple[int, str]]]:
        """One action dict per hero, and the (row, message) chats and (row, reason) labels to queue before stepping.

        cast_slots is the env's, keyed by player id: it names the skills and items in the prompt.
        trees is the env's too (env.trees); without it the prompt has no terrain, rune or landmark lines.
        A hero whose plan has run out keeps its last order, which NOOP does not disturb; a dead hero asks nothing
        of its per-second channel until it is back, while its long think plans what comes after.
        """
        self.player_ids = list(player_ids)
        observation = build_team_observation(world_state, self.team_id, player_ids)
        handles = [hero.unit_handles for hero in observation.heroes]
        dota_time = world_state.dota_time
        alive = {player.player_id: player.is_alive for player in world_state.players}
        hero_actions = [actions.NOOP_ACTION.copy() for _ in self.slots]
        chat, reasons, spots = [], [], []
        self.thinks.take(dota_time)

        for row, slot in enumerate(self.slots):
            said, reason = self.absorb(row, slot, dota_time)
            if said:
                chat.append((row, said))
            if reason:
                reasons.append((row, reason))
            hero = observation.heroes[row]
            spots.append((hero.hero.location.x, hero.hero.location.y) if hero.hero is not None else hero.origin)
            if not alive.get(player_ids[row], True):
                slot.plan = []
                continue
            if not slot.plan:
                slot.holds += 1
                continue
            step = slot.plan.pop(0)
            action, error = agent.resolve(step, masks[row], slot.plan_handles, handles[row], spots[row])
            hero_actions[row] = action
            slot.steps += 1
            # a NOOP leaves the hero at what it was doing, so the history keeps to the commands that changed it
            if error or action['type'] != actions.ActionType.NOOP:
                kind = 'rejected' if error else 'issued'
                note = agent.Note(kind=kind, dota_time=dota_time, position=spots[row], step=step, error=error)
                slot.history.append(note)
            if error:
                slot.parse_errors += 1
                self.record(
                    'rejected',
                    nickname=slot.config.nickname,
                    dota_time=dota_time,
                    decided_at=round(slot.plan_decided_at, 2),
                    step=step,
                    error=error,
                )

        due = [
            row
            for row, slot in enumerate(self.slots)
            if alive.get(player_ids[row], True)
            and slot.act.free(dota_time)
            and dota_time - slot.act.requested_dota_time >= slot.act.interval
        ]
        # Note (ruidu): only the heroes about to ask get a state block; rendering all five every time one asks is
        # the kind of work that makes the main loop fall behind the frames.
        rows = [player_ids[row] for row in due]
        blocks = hero_blocks(world_state, self.team_id, rows, cast_slots, trees, mode=self.match.mode) if due else []
        for row, block in zip(due, blocks, strict=True):
            slot = self.slots[row]
            items = [
                name for index, (name, _) in sorted(cast_slots.get(player_ids[row], {}).items()) if index >= N_ABILITIES
            ]
            teammates = [
                (self.agents[other], find_hero(world_state, self.team_id, player_id), self.board.intents[other])
                for other, player_id in enumerate(player_ids)
                if self.match.share_team_state and other != row
            ]
            directive = self.board.directives[row]
            aimed = directive is not None and directive.goal is not None and spots[row] is not None
            prompt = agent.act_user_prompt(
                block,
                items,
                teammates,
                slot.history,
                directive=directive,
                dota_time=dota_time,
                goal_distance=math.dist(spots[row], directive.goal) if aimed else None,
                calls=[(self.nicknames[call.row], call) for call in self.board.calls_for(row, dota_time)],
                intent=self.board.intents[row],
            )
            messages = [{'role': 'system', 'content': slot.act.system}, {'role': 'user', 'content': prompt}]
            request = Request(
                messages=messages, params=dict(slot.act.params), dota_time=dota_time, unit_handles=list(handles[row])
            )
            slot.act.submit(request)
        self.thinks.ask(world_state, self.player_ids, cast_slots)
        return hero_actions, chat, reasons

    def absorb(self, row: int, slot: Slot, dota_time: float) -> tuple[str | None, str | None]:
        """Take what has streamed in for this agent's per-second channel; returns the taunt and the reason it carried.

        A reply's first step replaces what is left of the plan before it, so the hero acts on the first
        line while the model is still writing the rest.
        """
        said = reason = None
        for reading, event in slot.act.events():
            request = reading.request
            if isinstance(event, str):
                kind, text = agent.read_line(event)
                if kind == 'step' and len(reading.steps) < self.match.plan_length:
                    if not reading.steps:
                        slot.plan, slot.plan_handles = [], list(request.unit_handles)
                        slot.plan_decided_at = request.dota_time
                    reading.steps.append(text)
                    slot.plan.append(text)
                elif kind == 'reason':
                    reading.reason = text
                    if self.match.show_reason:
                        reason = text
                elif kind == 'say' and self.match.all_chat and reading.said is None:
                    reading.said = said = text
                elif kind == 'intent' and text and reading.intent is None:
                    reading.intent = text
                    # an intent written again is the same one, still standing since it was first written
                    if self.board.intents[row] is None or self.board.intents[row][1] != text:
                        self.board.intents[row] = (request.dota_time, text)
                        self.thinks.remark(row, request.dota_time, 'intent', text)
                elif kind == 'call' and text:
                    reading.calls.append(text)
                    self.call(row, text, dota_time)
                elif kind == 'ask' and reading.ask is None:
                    reading.ask = text
                    self.thinks.wake(row, memory.Trigger(kind='ask', dota_time=dota_time, text=text))
                continue

            error = event.error
            if error is None and not reading.steps:
                error = 'reply carried no actions'
                slot.parse_errors += 1
            # Note (ruidu): a reply cut off after some of its steps still carries those out, into the history too.
            if not reading.steps:
                kind = 'unusable' if event.error is None else 'undelivered'
                slot.history.append(agent.Note(kind=kind, dota_time=dota_time, error=error))
            if reading.reason:
                self.thinks.remark(row, request.dota_time, 'reason', reading.reason)
            self.record(
                'decision',
                nickname=slot.config.nickname,
                dota_time=dota_time,
                **reply_record(reading, event, self.match.log_prompts),
                reply=event.text,
                reason=reading.reason,
                plan=reading.steps,
                say=reading.said,
                intent=reading.intent,
                calls=reading.calls,
                ask=reading.ask,
                error=error,
            )
        return said, reason

    def summary(self) -> list[dict[str, object]]:
        entries = []
        for slot, thinker in zip(self.slots, self.thinks.thinkers, strict=True):
            channels = [slot.act] if thinker is None else [slot.act, thinker.channel]
            entries.append(
                {
                    'nickname': slot.config.nickname,
                    'hero': slot.config.hero,
                    'position': slot.config.position,
                    'gateway': slot.config.gateway,
                    'requests': slot.act.requests,
                    'replies': slot.act.replies,
                    'gateway_errors': slot.act.errors,
                    'rejected_actions': slot.parse_errors,
                    'planned_steps': slot.steps,
                    'held_frames': slot.holds,
                    'input_tokens': sum(channel.input_tokens for channel in channels),
                    'output_tokens': sum(channel.output_tokens for channel in channels),
                    'usd': round(sum(channel.usd for channel in channels), 4),
                    'mean_latency': slot.act.mean_latency,
                    'thinks': 0 if thinker is None else thinker.channel.requests,
                    'think_errors': 0 if thinker is None else thinker.channel.errors,
                    'think_usd': 0.0 if thinker is None else round(thinker.channel.usd, 4),
                    'think_mean_latency': None if thinker is None else thinker.channel.mean_latency,
                }
            )
        return entries

    def close(self) -> None:
        for channel in [slot.act for slot in self.slots] + self.thinks.channels:
            channel.inbox.put(None)
