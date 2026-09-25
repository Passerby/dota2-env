"""Prompt debugger: browse LLM match transcripts in a browser and resend an edited prompt.

The page is scripts/prompt_debugger/, served on 127.0.0.1 only. With --config it also rebuilds system prompts from
the current templates and sends prompts through that config's gateways; the keys never leave this process.

    python scripts/prompt_debugger.py
    python scripts/prompt_debugger.py logs/20260924T182052Z.jsonl --config configs/openrouter_5v5.yaml
"""

import argparse
import contextlib
import dataclasses
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import jinja2
from dotenv import load_dotenv

from dota2_env.game_text import display_name, load_records
from dota2_env.llm import agent, review, think
from dota2_env.llm.config import MatchConfig, load_match
from dota2_env.llm.gateway import Gateway, Reply
from dota2_env.llm.memory import load_lessons
from dota2_env.text import TEAMS

PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompt_debugger')
CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8'}
# Note (ruidu): the page shows model replies, so nothing but this server's own files may run in it.
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'"


class DebuggerHandler(BaseHTTPRequestHandler):
    log_dir: ClassVar[str] = 'logs'
    selected: ClassVar[str | None] = None  # the transcript named on the command line
    config_path: ClassVar[str | None] = None
    match: ClassVar[MatchConfig | None] = None
    gateways: ClassVar[dict[str, Gateway]] = {}
    hosts: ClassVar[set[str]] = set()  # host:port this server answers to

    def log_request(self, code: int | str = '-', size: int | str = '-') -> None:
        pass  # a line per poll would bury the gateway warnings

    def allowed(self) -> bool:
        """Only this server's own page may call it: the Host check stops DNS rebinding, the Origin check other sites."""
        origin = self.headers.get('Origin')
        return self.headers.get('Host') in self.hosts and (
            origin is None or origin.removeprefix('http://') in self.hosts
        )

    def do_GET(self) -> None:
        if not self.allowed():
            self.send_error(403)
            return
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.path == '/api/logs':
            self.send_json({'logs': self.list_logs(), 'selected': self.selected})
        elif url.path == '/api/log':
            self.send_log(query.get('name', ''), int(query.get('offset', '0')))
        elif url.path == '/api/meta':
            self.send_json(self.meta())
        else:
            self.send_page(url.path.removeprefix('/') or 'index.html')

    def do_POST(self) -> None:
        if not self.allowed():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        match = self.match
        if match is None:
            self.send_json({'error': 'the debugger was started without --config'}, 409)
        elif path == '/api/system':
            self.send_system(match, request['team'], request['nickname'], request.get('channel', 'act'))
        elif path == '/api/send':
            self.send_reply(request['gateway'], request['params'], request['system'], request['user'])
        else:
            self.send_error(404)

    def send_body(self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self.send_body(status, 'application/json; charset=utf-8', json.dumps(payload, ensure_ascii=False).encode())

    def send_page(self, name: str) -> None:
        """A file of the page, read on every request; only names that are there, so a path cannot climb out."""
        suffix = os.path.splitext(name)[1]
        if name not in os.listdir(PAGE_DIR) or suffix not in CONTENT_TYPES:
            self.send_error(404)
            return
        with open(os.path.join(PAGE_DIR, name), 'rb') as f:
            self.send_body(200, CONTENT_TYPES[suffix], f.read())

    @classmethod
    def list_logs(cls) -> list[dict[str, str | float]]:
        """Every transcript in the log directory, the one written to last first."""
        logs = []
        for name in os.listdir(cls.log_dir):
            if name.endswith('.jsonl'):
                stat = os.stat(os.path.join(cls.log_dir, name))
                logs.append({'name': name, 'size': stat.st_size, 'mtime': stat.st_mtime})
        return sorted(logs, key=lambda log: log['mtime'], reverse=True)

    def send_log(self, name: str, offset: int) -> None:
        """The whole lines of a transcript from offset on; a record still being written waits for the next poll."""
        if name not in {log['name'] for log in self.list_logs()}:
            self.send_error(404)
            return
        with open(os.path.join(self.log_dir, name), 'rb') as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b'\n') + 1
        self.send_body(200, 'application/x-ndjson; charset=utf-8', chunk[:end], {'X-Offset': str(offset + end)})

    @classmethod
    def meta(cls) -> dict[str, object]:
        """The config without its keys, and the client's own names for teams, positions and heroes."""
        names = {
            'teams': {team_id: team['zh'] for team_id, team in TEAMS.items()},
            'positions': agent.POSITION_NAME,
            'heroes': {hero: display_name(hero) for hero in load_records('heroes')},
        }
        if cls.match is None:
            return {'config': None, **names}
        return {
            'config': cls.config_path,
            'plan_length': cls.match.plan_length,
            'gateways': [
                {
                    'name': gateway.name,
                    'base_url': gateway.base_url,
                    'model': gateway.model,
                    'params': gateway.params,
                    'timeout_seconds': gateway.timeout_seconds,
                }
                for gateway in cls.match.gateways.values()
            ],
            'agents': [
                {
                    'team': team.team_id,
                    'nickname': config.nickname,
                    'hero': config.hero,
                    'position': config.position,
                    'gateway': config.gateway,
                    'params': config.params,
                    'think': None
                    if config.think is None
                    else {'gateway': config.think.gateway, 'params': config.think.params},
                }
                for team in (cls.match.radiant, cls.match.dire)
                for config in team.agents
            ],
            **names,
        }

    def send_system(self, match: MatchConfig, team_id: int, nickname: str, channel: str) -> None:
        """The system prompt today's templates and the config give one agent's act, think or review channel."""
        team = match.team(team_id)
        config = next((config for config in team.agents if config.nickname == nickname), None)
        if config is None:
            self.send_json({'error': f'{nickname} is not an agent of team {team_id} in {self.config_path}'}, 404)
            return
        if channel == 'think' and config.think is None:
            self.send_json({'error': f'{nickname} has no think in {self.config_path}'}, 404)
            return
        # Note (ruidu): a template caught half edited is the one failure expected here, so it goes back to the page.
        try:
            if channel == 'think':
                # the lessons the runner would read for it now, which can be newer than the match's
                lessons = (
                    [] if match.memory_dir is None else load_lessons(match.memory_dir, config.hero, match.lessons_limit)
                )
                system = think.think_system_prompt(config, match, team, lessons)
            elif channel == 'review':
                system = review.review_system_prompt(config, match, team_id)
            else:
                system = agent.act_system_prompt(config, match, team_id)
        except jinja2.TemplateSyntaxError as e:
            self.send_json({'error': f'{e.name} line {e.lineno}: {e.message}'}, 400)
            return
        except jinja2.TemplateError as e:
            self.send_json({'error': f'{channel} template: {e.message}'}, 400)
            return
        self.send_json({'system': system})

    def send_reply(self, gateway: str, params: dict[str, object], system: str, user: str) -> None:
        """Stream one reply as JSON lines: each line the way the runner reads it, then the Reply as a whole."""

        def _write(payload: dict[str, object]) -> None:
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode() + b'\n')

        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        # Note (ruidu): HTTP/1.0 and no Content-Length, so closing the connection is what ends the stream. A page
        # that stops reading shows up as a broken pipe; closing the generator then ends the request to the gateway.
        with (
            contextlib.closing(self.gateways[gateway].stream(messages, params)) as events,
            contextlib.suppress(BrokenPipeError, ConnectionResetError),
        ):
            for event in events:
                if isinstance(event, Reply):
                    _write({'reply': dataclasses.asdict(event)})
                    continue
                kind, text = agent.read_line(event)
                try:
                    if kind == 'step':
                        agent.read_step(text)
                except ValueError as e:
                    error = str(e)
                else:
                    error = None
                _write({'line': event, 'kind': kind, 'text': text, 'error': error})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('logs', nargs='?', default='logs', help='a transcript directory, or one transcript in it')
    parser.add_argument('--config', help='a match YAML; lets the page resend prompts and rebuild system prompts')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    if os.path.isfile(args.logs):
        DebuggerHandler.log_dir, DebuggerHandler.selected = os.path.split(os.path.abspath(args.logs))
    elif os.path.isdir(args.logs):
        DebuggerHandler.log_dir = args.logs
    else:
        raise SystemExit(f'no such transcript or directory: {args.logs}')
    if args.config:
        load_dotenv()
        match = load_match(args.config)
        DebuggerHandler.match = match
        DebuggerHandler.config_path = args.config
        # Note (ruidu): the page sends every param itself, the gateway's own included, so the editor shows the whole
        # request; one client per gateway keeps its connections warm, as in a match.
        DebuggerHandler.gateways = {
            name: Gateway(dataclasses.replace(config, params={})) for name, config in match.gateways.items()
        }
    DebuggerHandler.hosts = {f'127.0.0.1:{args.port}', f'localhost:{args.port}'}

    with ThreadingHTTPServer(('127.0.0.1', args.port), DebuggerHandler) as server:
        resend = f'resend via {args.config}' if args.config else 'read only, pass --config to resend'
        print(f'prompt debugger on http://127.0.0.1:{args.port}  logs {DebuggerHandler.log_dir}  {resend}', flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == '__main__':
    main()
