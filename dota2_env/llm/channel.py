"""One stream of requests to one LLM gateway: a worker thread takes a prompt, and each line of the reply comes back
the moment it is written, then the reply as a whole.

Both channels of an agent run on it: the per-second one (runner.py) and the long think (think.py).
"""

import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from dota2_env.llm import memory
from dota2_env.llm.gateway import Gateway, Reply

logger = logging.getLogger('dota2_env')

STUCK_TIMEOUT_FACTOR = 3.0  # warn once a worker has been in flight this many times its own timeout


@dataclass(kw_only=True)
class Request:
    """What crosses into the worker thread: immutable once submitted."""

    messages: list[dict[str, str]]
    params: dict[str, object]
    dota_time: float
    unit_handles: list[int] = field(default_factory=list)  # the unit rows of the frame a per-second prompt showed
    triggers: list[memory.Trigger] = field(default_factory=list)  # why a long think was asked
    memos: list[memory.Memo] = field(default_factory=list)  # the notes a long think was shown, which FORGET numbers


@dataclass(kw_only=True)
class Reading:
    """What has streamed in so far of the reply to one request."""

    request: Request | None = None
    steps: list[str] = field(default_factory=list)  # at most plan_length
    reason: str = ''
    said: str | None = None
    intent: str | None = None
    ask: str | None = None
    calls: list[str] = field(default_factory=list)
    goal: tuple[float, float] | None = None  # a GOAL that came before its PLAN
    directive: memory.Directive | None = None
    notes: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    lessons: list[dict[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # tagged lines that could not be used, and why


@dataclass(kw_only=True)
class Channel:
    """One stream of requests to one gateway, run by its own worker thread with at most one request in flight.

    The worker owns nothing but its locals and the two queues, so there is no lock: inbox and outbox are
    thread-safe, busy is an Event, and the main thread is the single writer of everything else.
    """

    name: str  # who is waiting, for the log
    gateway: Gateway
    system: str
    params: dict[str, object]
    interval: float  # game seconds between requests at the most
    inbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    # each line of a reply as it streams in, then the reply as a whole
    outbox: queue.Queue[tuple[Request, str | Reply]] = field(default_factory=queue.Queue)
    busy: threading.Event = field(default_factory=threading.Event)
    requested_dota_time: float = float('-inf')  # so every channel submits on the very first frame
    reading: Reading = field(default_factory=Reading)
    requests: int = 0
    replies: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_total: float = 0.0

    def free(self, dota_time: float) -> bool:
        """No request is in flight; one that has been for much longer than its own timeout gets a warning."""
        if not self.busy.is_set():
            return True
        stuck = dota_time - self.requested_dota_time
        if stuck > STUCK_TIMEOUT_FACTOR * self.gateway.config.timeout_seconds:
            logger.warning(f'{self.name} has been waiting {stuck:.0f} game seconds for a reply')
        return False

    def submit(self, request: Request) -> None:
        self.busy.set()
        self.requested_dota_time = request.dota_time
        self.requests += 1
        self.inbox.put(request)

    def events(self) -> Iterator[tuple[Reading, str | Reply]]:
        """Each line and each whole reply streamed in since the last call, with the reading of its request.

        busy lets one request be in flight at a time, so the lines of two replies never interleave here.
        A reply's tokens, cost and latency are counted as it goes by.
        """
        while True:
            try:
                request, event = self.outbox.get_nowait()
            except queue.Empty:
                return
            if request is not self.reading.request:
                self.reading = Reading(request=request)
            if isinstance(event, Reply):
                self.replies += 1
                self.input_tokens += event.input_tokens
                self.output_tokens += event.output_tokens
                self.usd += event.usd
                self.latency_total += event.latency
                if event.error is not None:
                    self.errors += 1
            yield self.reading, event

    @property
    def mean_latency(self) -> float | None:
        return round(self.latency_total / self.replies, 2) if self.replies else None


def work(channel: Channel) -> None:
    """Worker body: block for a prompt, pass each line of the reply back as it streams in, then the reply."""
    while True:
        request = channel.inbox.get()
        if request is None:
            return
        for event in channel.gateway.stream(request.messages, request.params):
            channel.outbox.put((request, event))
        channel.busy.clear()


def reply_record(reading: Reading, event: Reply, log_prompts: bool) -> dict[str, object]:
    """The transcript fields every reply's record starts with: when it was asked, how long it took, what it cost."""
    first_line = event.first_line_latency
    record = {
        'decided_at': round(reading.request.dota_time, 2),
        'latency': round(event.latency, 3),
        'first_line_latency': None if first_line is None else round(first_line, 3),
        'input_tokens': event.input_tokens,
        'output_tokens': event.output_tokens,
        'usd': round(event.usd, 6),
    }
    if log_prompts:
        record['prompt'] = reading.request.messages[-1]['content']
    return record
