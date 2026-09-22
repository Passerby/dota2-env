"""One OpenAI-compatible chat endpoint: post a completion, count its tokens, price the call.

Every provider worth testing against speaks this shape (DeepSeek, OpenRouter, Qwen, Kimi, vLLM,
Ollama), so there is one client here and no per-provider adapters.
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

from dota2_env.llm.config import GatewayConfig

logger = logging.getLogger('dota2_env')


@dataclass(kw_only=True)
class Reply:
    text: str  # empty when the call failed
    input_tokens: int
    output_tokens: int
    usd: float
    latency: float  # wall clock seconds, what decides whether the model can keep up
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

    def complete(self, messages: list[dict[str, str]], params: dict[str, object]) -> Reply:
        body = {'model': self.config.model, 'messages': messages, **self.config.params, **params}
        start = time.time()
        try:
            response = self.client.post('/chat/completions', json=body)
            response.raise_for_status()
            data = response.json()
            text = data['choices'][0]['message']['content'] or ''
            usage = data.get('usage') or {}
        # A gateway can answer 200 with an error body instead of choices, so the shape is not ours to trust.
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
            latency = time.time() - start
            logger.warning(f'{self.config.name} failed after {latency:.1f}s: {e!r}')
            return Reply(text='', input_tokens=0, output_tokens=0, usd=0.0, latency=latency, error=repr(e))
        input_tokens = int(usage.get('prompt_tokens', 0))
        output_tokens = int(usage.get('completion_tokens', 0))
        usd = (input_tokens * self.config.input_usd_per_mtok + output_tokens * self.config.output_usd_per_mtok) / 1e6
        return Reply(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
            latency=time.time() - start,
            error=None,
        )

    def close(self) -> None:
        self.client.close()
