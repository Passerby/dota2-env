"""One OpenAI-compatible chat endpoint: stream a completion line by line, count its tokens, price the call.

Every provider worth testing against speaks this shape (DeepSeek, OpenRouter, Qwen, Kimi, vLLM,
Ollama), so there is one client here and no per-provider adapters.
"""

import json
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass

import httpx

from dota2_env.llm.config import GatewayConfig

logger = logging.getLogger('dota2_env')


@dataclass(kw_only=True)
class Reply:
    text: str  # everything the model wrote, including a last line that was cut off
    input_tokens: int
    output_tokens: int
    usd: float
    latency: float  # wall clock seconds to the end of the reply, what decides whether the model can keep up
    first_line_latency: float | None  # wall clock seconds to the first line, which is when a hero can act on it
    error: str | None


class Gateway:
    """Blocking client for one gateway, shared by every agent that references it.

    httpx.Client is thread-safe and pools connections, which matters: ten agents reconnecting once a
    second would spend most of their latency budget on TLS handshakes.
    """

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={'Authorization': f'Bearer {config.api_key}'},
        )

    def stream(self, messages: list[dict[str, str]], params: dict[str, object]) -> Generator[str | Reply, None, None]:
        """Yield every line of the reply the moment its newline arrives, then the Reply as a whole.

        Blank lines are skipped. The last line needs no newline, but it is held back when the reply was
        cut off, because half a line can still read as a whole step: "MOVE, 11" of "MOVE, 1180, -1216".
        timeout_seconds bounds the whole reply, where httpx would only bound the wait for each chunk.
        """
        body = {
            'model': self.config.model,
            'messages': messages,
            # DeepSeek and most others only put usage in a stream when asked to; OpenRouter always does.
            'stream_options': {'include_usage': True},
            **self.config.params,
            **params,
            'stream': True,
        }
        start = time.time()
        text, sent, usage, finish_reason, error = '', 0, {}, None, None
        first_line_latency = None
        try:
            with self.client.stream('POST', '/chat/completions', json=body) as response:
                response.raise_for_status()
                for event in response.iter_lines():
                    if time.time() - start > self.config.timeout_seconds:
                        error = f'reply not finished within {self.config.timeout_seconds}s'
                        break
                    # blank lines end an event; OpenRouter keeps the connection alive with ": ..." comments
                    if not event.startswith('data:'):
                        continue
                    payload = event.removeprefix('data:').strip()
                    if payload == '[DONE]':
                        break
                    chunk = json.loads(payload)
                    # OpenRouter reports a failure after the 200 status as an event of its own.
                    if 'error' in chunk:
                        error = repr(chunk['error'])
                        break
                    usage = chunk.get('usage') or usage
                    for choice in chunk.get('choices') or []:
                        text += choice['delta'].get('content') or ''
                        finish_reason = choice.get('finish_reason') or finish_reason
                        # Note (ruidu): Kimi puts the usage inside its last choice rather than beside it.
                        usage = choice.get('usage') or usage
                    end = text.find('\n', sent)
                    while end >= 0:
                        if text[sent:end].strip():
                            if first_line_latency is None:
                                first_line_latency = time.time() - start
                            yield text[sent:end]
                        sent, end = end + 1, text.find('\n', end + 1)
        # A gateway can send an error body instead of choices, so the shape is not ours to trust.
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            error = repr(e)
        latency = time.time() - start
        if error is None and finish_reason in (None, 'stop') and text[sent:].strip():
            if first_line_latency is None:
                first_line_latency = latency
            yield text[sent:]
        if error is not None:
            logger.warning(f'{self.config.name} failed after {latency:.1f}s: {error}')
        input_tokens = int(usage.get('prompt_tokens', 0))
        output_tokens = int(usage.get('completion_tokens', 0))
        # Note (ruidu): a gateway that prices a prefix-cache hit apart from fresh input (OpenRouter reads a
        # cached token at a fiftieth of a fresh one, and these prompts hit 99%) cannot be repriced from two
        # flat rates, so where it bills the call itself that figure is the money and ours is an upper bound.
        if 'cost' in usage:
            usd = float(usage['cost'])
        else:
            usd = (
                input_tokens * self.config.input_usd_per_mtok + output_tokens * self.config.output_usd_per_mtok
            ) / 1e6
        yield Reply(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
            latency=latency,
            first_line_latency=first_line_latency,
            error=error,
        )

    def close(self) -> None:
        self.client.close()
