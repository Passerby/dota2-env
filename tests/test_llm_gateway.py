"""The OpenAI-compatible gateway on the wire, against a local server: no provider and no key needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from dota2_env.llm.config import GatewayConfig
from dota2_env.llm.gateway import Gateway

MOVE_NORTH = '{"type": "MOVE", "move": 4}'


class ChatHandler(BaseHTTPRequestHandler):
    """Answers /chat/completions the way an OpenAI-compatible provider does."""

    requests: ClassVar[list[dict]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        ChatHandler.requests.append({'path': self.path, 'auth': self.headers['Authorization'], 'body': body})
        if body['model'] == 'broken':
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'kaboom')
            return
        # some proxies answer 200 with an error object instead of choices
        payload = (
            {'error': {'message': 'no credits'}}
            if body['model'] == 'weird'
            else {
                'choices': [{'message': {'content': MOVE_NORTH}}],
                'usage': {'prompt_tokens': 1234, 'completion_tokens': 56},
            }
        )
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def chat_server():
    ChatHandler.requests.clear()
    server = ThreadingHTTPServer(('127.0.0.1', 0), ChatHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}/v1'
    server.shutdown()


def call_gateway(base_url, model, **fields):
    config = GatewayConfig(
        name='t',
        base_url=base_url,
        model=model,
        api_key='sk-abc',
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=2.0,
        **fields,
    )
    gateway = Gateway(config)
    try:
        return gateway.complete([{'role': 'user', 'content': 'hi'}], {'temperature': 0.1})
    finally:
        gateway.close()


def test_a_completion_is_posted_and_priced(chat_server):
    reply = call_gateway(chat_server, 'tiny')
    assert reply.text == MOVE_NORTH and reply.error is None
    assert (reply.input_tokens, reply.output_tokens) == (1234, 56)
    assert reply.usd == pytest.approx((1234 * 1.0 + 56 * 2.0) / 1e6)
    sent = ChatHandler.requests[0]
    assert sent['path'] == '/v1/chat/completions' and sent['auth'] == 'Bearer sk-abc'
    assert sent['body']['messages'] == [{'role': 'user', 'content': 'hi'}]
    assert sent['body']['temperature'] == 0.1 and sent['body']['model'] == 'tiny'


@pytest.mark.parametrize('model', ['broken', 'weird'])
def test_a_bad_answer_is_reported_instead_of_raised(chat_server, model):
    reply = call_gateway(chat_server, model)
    assert reply.text == '' and reply.usd == 0.0 and reply.error is not None


def test_an_unreachable_gateway_is_reported_instead_of_raised():
    reply = call_gateway('http://127.0.0.1:9/v1', 'tiny', timeout_seconds=0.3)
    assert reply.text == '' and reply.error is not None
