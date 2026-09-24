"""The OpenAI-compatible gateway on the wire, against a local server: no provider and no key needed."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from dota2_env.llm.config import GatewayConfig
from dota2_env.llm.gateway import Gateway

USAGE = {'prompt_tokens': 1234, 'completion_tokens': 56}
GATE, PAUSE = 'gate', 'pause'  # stream entries the handler acts on instead of sending
DONE = 'data: [DONE]\n\n'


def data(payload):
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


def delta(content, finish_reason=None):
    return data({'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': finish_reason}]})


# Content arrives in pieces that split lines the way tokens do.
STREAMS = {
    'tiny': [
        ': OPENROUTER PROCESSING\n\n',
        delta('MOVE, 4\nATT'),
        delta('ACK, 3\n\n'),
        delta('REASON, 兵线'),
        delta('在推', 'stop'),
        data({'choices': [], 'usage': USAGE}),
        DONE,
    ],
    # Kimi's last chunk carries the usage inside its choice.
    'kimi': [
        delta('MOVE, 4'),
        data({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop', 'usage': USAGE}]}),
        DONE,
    ],
    'cut': [delta('MOVE, 4\nMOVE, 11', 'length'), data({'choices': [], 'usage': USAGE}), DONE],
    # OpenRouter's failure after the 200 status: an error beside the fields, and finish_reason error.
    'weird': [
        delta('MOVE, 4\n'),
        data(
            {
                'error': {'code': 502, 'message': 'Provider disconnected'},
                'choices': [{'index': 0, 'delta': {'content': ''}, 'finish_reason': 'error'}],
            }
        ),
    ],
    'gated': [delta('MOVE, 4\n'), GATE, delta('REASON, 等', 'stop'), DONE],
    'slow': [delta('MOVE, 4\n')] + [': OPENROUTER PROCESSING\n\n', PAUSE] * 20 + [delta('STOP', 'stop'), DONE],
}


class ChatHandler(BaseHTTPRequestHandler):
    """Streams /chat/completions back the way an OpenAI-compatible provider does."""

    requests: ClassVar[list[dict]] = []
    gate: ClassVar[threading.Event] = threading.Event()

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
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            for entry in STREAMS[body['model']]:
                if entry == GATE:
                    ChatHandler.gate.wait(timeout=5)
                elif entry == PAUSE:
                    time.sleep(0.1)
                else:
                    self.wfile.write(entry.encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client stopped reading, which is what timeout_seconds is for


@pytest.fixture
def chat_server():
    ChatHandler.requests.clear()
    ChatHandler.gate.clear()
    server = ThreadingHTTPServer(('127.0.0.1', 0), ChatHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}/v1'
    ChatHandler.gate.set()
    server.shutdown()


def make_gateway(base_url, model, **fields):
    config = GatewayConfig(
        name='t',
        base_url=base_url,
        model=model,
        api_key='sk-abc',
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=2.0,
        **fields,
    )
    return Gateway(config)


def call_gateway(base_url, model, **fields):
    """The lines the gateway handed on, and the reply that ended them."""
    gateway = make_gateway(base_url, model, **fields)
    try:
        *lines, reply = gateway.stream([{'role': 'user', 'content': 'hi'}], {'temperature': 0.1})
    finally:
        gateway.close()
    return lines, reply


def test_a_completion_is_streamed_line_by_line_and_priced(chat_server):
    lines, reply = call_gateway(chat_server, 'tiny')
    assert lines == ['MOVE, 4', 'ATTACK, 3', 'REASON, 兵线在推']
    assert reply.text == 'MOVE, 4\nATTACK, 3\n\nREASON, 兵线在推' and reply.error is None
    assert (reply.input_tokens, reply.output_tokens) == (1234, 56)
    assert reply.usd == pytest.approx((1234 * 1.0 + 56 * 2.0) / 1e6)
    assert 0 < reply.first_line_latency <= reply.latency
    sent = ChatHandler.requests[0]
    assert sent['path'] == '/v1/chat/completions' and sent['auth'] == 'Bearer sk-abc'
    assert sent['body']['messages'] == [{'role': 'user', 'content': 'hi'}]
    assert sent['body']['temperature'] == 0.1 and sent['body']['model'] == 'tiny'
    assert sent['body']['stream'] is True and sent['body']['stream_options'] == {'include_usage': True}


def test_a_line_is_handed_on_before_the_reply_is_finished(chat_server):
    gateway = make_gateway(chat_server, 'gated')
    try:
        events = gateway.stream([{'role': 'user', 'content': 'hi'}], {})
        start = time.time()
        assert next(events) == 'MOVE, 4'
        assert time.time() - start < 2  # the server holds the rest back for 5s
        ChatHandler.gate.set()
        rest = list(events)
    finally:
        gateway.close()
    assert rest[0] == 'REASON, 等' and rest[1].error is None


def test_usage_is_read_where_kimi_puts_it(chat_server):
    lines, reply = call_gateway(chat_server, 'kimi')
    assert lines == ['MOVE, 4'] and (reply.input_tokens, reply.output_tokens) == (1234, 56)


def test_a_last_line_cut_off_by_max_tokens_is_held_back(chat_server):
    lines, reply = call_gateway(chat_server, 'cut')
    assert lines == ['MOVE, 4'] and reply.text.endswith('MOVE, 11') and reply.error is None


def test_an_error_mid_stream_keeps_the_lines_before_it(chat_server):
    lines, reply = call_gateway(chat_server, 'weird')
    assert lines == ['MOVE, 4'] and 'Provider disconnected' in reply.error


def test_timeout_seconds_bounds_the_whole_reply(chat_server):
    lines, reply = call_gateway(chat_server, 'slow', timeout_seconds=0.5)
    assert lines == ['MOVE, 4'] and 'not finished within 0.5s' in reply.error


def test_a_failed_request_is_reported_instead_of_raised(chat_server):
    lines, reply = call_gateway(chat_server, 'broken')
    assert lines == [] and reply.text == '' and reply.usd == 0.0 and reply.error is not None


def test_an_unreachable_gateway_is_reported_instead_of_raised():
    lines, reply = call_gateway('http://127.0.0.1:9/v1', 'tiny', timeout_seconds=0.3)
    assert lines == [] and reply.text == '' and reply.error is not None
