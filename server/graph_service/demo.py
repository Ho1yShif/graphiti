"""Opt-in demo mode for the public Graphiti template.

`DEMO` is off by default, so a fork gets the ordinary API with no restrictions. When
`DEMO=true` the service becomes a public, no-signup showcase running on the deployer's
OpenAI key, so it is locked down instead:

* every visitor is pinned to their own `group_id`, so graphs never leak between visitors
* destructive routes are refused
* requests and ingested episodes are capped
* session graphs are swept away once they expire, so no history accumulates

All of it lives in one ASGI middleware so the routers stay untouched and the normal
(non-demo) request path is unchanged.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

from graph_service.config import Settings
from graph_service.zep_graphiti import create_graphiti_client

logger = logging.getLogger(__name__)

SESSION_COOKIE = 'graphiti_demo_session'
# API clients (curl, scripts) can't hold a cookie, so the session is also readable and
# settable through a header. Either way it is an unguessable UUID minted server side.
SESSION_HEADER = 'X-Demo-Session'
LIMIT_MESSAGE = 'Demo limit reached — deploy your own Graphiti to keep going.'
LOCKED_MESSAGE = 'This route is disabled in demo mode. Deploy your own Graphiti to use it.'

# Routes that write destructively or reach beyond a single session. Matched as
# (method, path prefix) against the incoming request.
LOCKED_ROUTES: tuple[tuple[str, str], ...] = (
    ('DELETE', '/entity-edge/'),
    ('DELETE', '/group/'),
    ('DELETE', '/episode/'),
    ('POST', '/clear'),
)

# Never gated: Render's health check has to answer even when a visitor is rate limited.
UNGATED_PATHS = frozenset({'/healthcheck'})

# Read routes whose group is optional in the request body. They default to searching every
# group, so the session has to be pinned onto them even when the caller sends no group.
SCOPED_SEARCH_PATHS = frozenset({'/search'})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {'true', '1', 'yes', 'on'}


def _env_limit(name: str, default: int) -> int:
    """Resolve a limit. Missing or nonsensical falls back to the default; 0 means unlimited."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning('Ignoring invalid %s=%r; using default %d', name, raw, default)
        return default
    if value < 0:
        logger.warning('Ignoring negative %s=%d; using default %d', name, value, default)
        return default
    return value


@dataclass
class DemoConfig:
    enabled: bool = False
    rate_limit_per_minute: int = 10
    max_episodes_per_session: int = 30
    session_ttl_minutes: int = 60
    model_name: str | None = None

    @classmethod
    def from_env(cls) -> 'DemoConfig':
        return cls(
            enabled=_env_flag('DEMO'),
            rate_limit_per_minute=_env_limit('DEMO_RATE_LIMIT_PER_MINUTE', 10),
            max_episodes_per_session=_env_limit('DEMO_MAX_EPISODES_PER_SESSION', 30),
            session_ttl_minutes=_env_limit('DEMO_SESSION_TTL_MINUTES', 60),
            model_name=os.environ.get('DEMO_MODEL_NAME') or None,
        )


@dataclass
class _Session:
    last_seen: float
    episodes: int = 0


@dataclass
class _RateLimiter:
    """Sliding window of request timestamps, keyed by client IP."""

    per_minute: int
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, key: str) -> bool:
        if self.per_minute == 0:
            return True
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True


class DemoState:
    """Live demo state, shared by the middleware and the background sweeper."""

    def __init__(self, config: DemoConfig):
        self.config = config
        self.sessions: dict[str, _Session] = {}
        self.rate_limiter = _RateLimiter(config.rate_limit_per_minute)

    def touch(self, session_id: str) -> _Session:
        now = time.monotonic()
        session = self.sessions.setdefault(session_id, _Session(last_seen=now))
        session.last_seen = now
        return session

    async def sweep_expired(self, settings: Settings) -> None:
        """Drop session graphs past their TTL so demo history never accumulates."""
        ttl_seconds = self.config.session_ttl_minutes * 60
        if not ttl_seconds:
            return

        while True:
            await asyncio.sleep(min(ttl_seconds, 300))
            cutoff = time.monotonic() - ttl_seconds
            expired = [sid for sid, s in self.sessions.items() if s.last_seen < cutoff]
            if not expired:
                continue

            client = create_graphiti_client(settings)
            try:
                for session_id in expired:
                    try:
                        await client.delete_group(session_id)
                    except Exception:
                        logger.exception('Failed to sweep demo session %s', session_id)
                    else:
                        self.sessions.pop(session_id, None)
            finally:
                await client.close()

            logger.info('Swept %d expired demo session(s)', len(expired))


class DemoModeMiddleware:
    """Confines each visitor to their own session graph and enforces the demo caps."""

    def __init__(self, app, state: DemoState):
        self.app = app
        self.state = state
        self.config = state.config

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] in UNGATED_PATHS:
            await self.app(scope, receive, send)
            return

        method, path = scope['method'], scope['path']

        if any(method == m and path.startswith(p) for m, p in LOCKED_ROUTES):
            await _send_json(send, 403, LOCKED_MESSAGE)
            return

        if not self.state.rate_limiter.allow(_client_ip(scope)):
            await _send_json(send, 429, LIMIT_MESSAGE)
            return

        session_id = _session_id(scope) or str(uuid.uuid4())
        session = self.state.touch(session_id)

        body = await _read_body(receive)

        if path == '/messages' and method == 'POST':
            requested = _count_messages(body)
            cap = self.config.max_episodes_per_session
            if cap and session.episodes + requested > cap:
                await _send_json(send, 429, LIMIT_MESSAGE)
                return
            session.episodes += requested

        scope = _scope_with_session(scope, session_id)
        body = _force_group(body, session_id, path=path)

        await self.app(
            scope,
            _replay_body(body),
            _send_with_session_cookie(send, session_id),
        )


def _client_ip(scope) -> str:
    for name, value in scope.get('headers', []):
        if name == b'x-forwarded-for':
            # Render appends the real client IP to whatever the caller sent, so only the
            # rightmost hop is trustworthy. Reading the leftmost lets a visitor mint a
            # fresh rate-limit bucket per request just by sending their own header.
            return value.decode('latin-1').split(',')[-1].strip()
    client = scope.get('client')
    return client[0] if client else 'unknown'


def _session_id(scope) -> str | None:
    """Read the caller's session from the header first, then the cookie."""
    header_name = SESSION_HEADER.lower().encode()
    cookie_value = None

    for name, value in scope.get('headers', []):
        if name == header_name:
            candidate = value.decode('latin-1').strip()
            if _is_session_id(candidate):
                return candidate
        elif name == b'cookie' and cookie_value is None:
            cookie = SimpleCookie()
            cookie.load(value.decode('latin-1'))
            morsel = cookie.get(SESSION_COOKIE)
            if morsel and _is_session_id(morsel.value):
                cookie_value = morsel.value

    return cookie_value


def _is_session_id(value: str) -> bool:
    """Only accept ids we could have minted, so a caller can't name their own group."""
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


async def _read_body(receive) -> bytes:
    body = b''
    while True:
        message = await receive()
        if message['type'] != 'http.request':
            break
        body += message.get('body', b'')
        if not message.get('more_body', False):
            break
    return body


def _replay_body(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return receive


def _count_messages(body: bytes) -> int:
    payload = _load_json_object(body)
    messages = payload.get('messages') if payload else None
    return len(messages) if isinstance(messages, list) else 1


def _force_group(body: bytes, session_id: str, *, path: str) -> bytes:
    """Overwrite any caller-supplied group so a visitor can only touch their own graph."""
    payload = _load_json_object(body)
    if payload is None:
        return body
    if 'group_id' in payload:
        payload['group_id'] = session_id
    # An absent group_ids means "every group" to the search router, so it has to be
    # pinned whether or not the caller sent it — otherwise omitting the key reads
    # across every visitor's graph.
    if 'group_ids' in payload or path in SCOPED_SEARCH_PATHS:
        payload['group_ids'] = [session_id]
    return json.dumps(payload).encode('utf-8')


def _load_json_object(body: bytes) -> dict | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _scope_with_session(scope, session_id: str):
    """Rewrite `/episodes/{group_id}`, the one route that carries a group in its path."""
    if not scope['path'].startswith('/episodes/'):
        return scope

    scope = dict(scope)
    scope['path'] = f'/episodes/{session_id}'
    scope['raw_path'] = scope['path'].encode('utf-8')
    return scope


def _send_with_session_cookie(send, session_id: str):
    async def wrapped(message):
        if message['type'] == 'http.response.start':
            message = dict(message)
            cookie = f'{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Secure'
            message['headers'] = [
                *message.get('headers', []),
                (b'set-cookie', cookie.encode()),
                (SESSION_HEADER.lower().encode(), session_id.encode()),
            ]
        await send(message)

    return wrapped


async def _send_json(send, status_code: int, detail: str) -> None:
    body = json.dumps({'detail': detail}).encode('utf-8')
    await send(
        {
            'type': 'http.response.start',
            'status': status_code,
            'headers': [
                (b'content-type', b'application/json'),
                (b'content-length', str(len(body)).encode()),
            ],
        }
    )
    await send({'type': 'http.response.body', 'body': body})
