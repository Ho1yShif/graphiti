"""Auth tests for the graph endpoints.

Unlike tests/test_live_falkordb_int.py these need no OpenAI key and no database, so
they run in the default `make test`. Nothing here asserts a successful response: the
app is built without its lifespan, so a request that gets past auth reaches a handler
with no Graphiti client and fails. What is asserted is only whether require_api_key
let the request through — see _assert_passed_auth, which deliberately does not care
which error the handler then raised.

The real graph_service.main is reloaded per case rather than a stand-in app being
assembled, because the wiring in main is what these guard: the likeliest regression
is a future router included without the auth dependency, and only the real module
can catch that.
"""

import importlib

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from graph_service import config
from graph_service.auth import require_api_key

SECRET = 'test-api-key-6Yp2Qk'

# The allowlist the app is held to: every other route must carry the auth dependency.
# An allowlist rather than a list of protected prefixes, so a route added under a name
# nobody predicted is a failure by default instead of going unnoticed.
PUBLIC_PATHS = {'/healthcheck'}


@pytest.fixture
def build_app(monkeypatch):
    """Return a factory that builds the real app with GRAPHITI_API_KEY set to a given value."""

    def _build(graphiti_api_key: str | None):
        monkeypatch.setenv('OPENAI_API_KEY', 'unused-by-these-tests')
        if graphiti_api_key is None:
            monkeypatch.delenv('GRAPHITI_API_KEY', raising=False)
        else:
            monkeypatch.setenv('GRAPHITI_API_KEY', graphiti_api_key)
        # Settings are cached, and the app object is built at import time, so both have
        # to be discarded for a new GRAPHITI_API_KEY to take effect.
        config.get_settings.cache_clear()
        import graph_service.main as main

        return importlib.reload(main).app

    yield _build
    config.get_settings.cache_clear()


def _search(app, headers=None):
    client = TestClient(app, raise_server_exceptions=False)
    return client.post(
        '/search', json={'query': 'anything', 'group_ids': ['g']}, headers=headers or {}
    )


def _assert_passed_auth(response):
    """Assert only that require_api_key let the request through.

    Not `== 500`: what the handler does next is not this file's business, and pinning
    the exact failure would make these tests fail the day the app gains a lifespan in
    the fixture or returns a 503 instead. 401 is the one status that means auth itself
    rejected the request.
    """
    assert response.status_code != 401, response.text


@pytest.mark.parametrize(
    'graphiti_api_key', [None, '', '   '], ids=['unset', 'blank', 'whitespace']
)
def test_auth_is_off_when_no_key_is_configured(build_app, graphiti_api_key):
    """Local compose sets no GRAPHITI_API_KEY, and must keep working without a header.

    Blank and whitespace count as unset: config's OptionalStr validator normalises them
    to None, so a stray `GRAPHITI_API_KEY=` in a .env can't become a key equal to the empty string
    that every unauthenticated request would then have to guess.
    """
    app = build_app(graphiti_api_key)
    _assert_passed_auth(_search(app))


def test_configured_key_accepts_the_right_bearer_token(build_app):
    app = build_app(SECRET)
    _assert_passed_auth(_search(app, {'Authorization': f'Bearer {SECRET}'}))


@pytest.mark.parametrize(
    'headers',
    [
        pytest.param({}, id='no-header'),
        pytest.param({'Authorization': 'Bearer wrong-key'}, id='wrong-key'),
        pytest.param({'Authorization': 'Bearer '}, id='empty-token'),
        pytest.param({'Authorization': f'Basic {SECRET}'}, id='wrong-scheme'),
        pytest.param({'Authorization': SECRET}, id='bare-token-no-scheme'),
        pytest.param({'X-API-Key': SECRET}, id='wrong-header'),
        # The right value one byte short: guards the compare_digest call against being
        # swapped for a prefix or startswith comparison.
        pytest.param({'Authorization': f'Bearer {SECRET[:-1]}'}, id='truncated-key'),
    ],
)
def test_configured_key_rejects_everything_else(build_app, headers):
    app = build_app(SECRET)
    assert _search(app, headers).status_code == 401


def test_rejection_names_the_scheme_to_retry_with(build_app):
    """RFC 9110 requires WWW-Authenticate on a 401, and it tells clients what to send."""
    app = build_app(SECRET)
    assert _search(app).headers.get('WWW-Authenticate') == 'Bearer'


def test_healthcheck_stays_public(build_app):
    """Render polls /healthcheck to decide the service is live.

    If auth ever covers it, every deploy fails its health check and rolls back.
    """
    app = build_app(SECRET)
    response = TestClient(app).get('/healthcheck')
    assert response.status_code == 200
    assert response.json() == {'status': 'healthy'}


def _is_protected(route) -> bool:
    """Whether require_api_key runs before this route's handler.

    Identity, not a name comparison: a same-named dependency from somewhere else would
    otherwise satisfy the check. Anything that is not an APIRoute has no dependant at
    all and counts as unprotected — a path served by a Mount or a WebSocketRoute is
    exactly the case this test must not wave through.
    """
    if not isinstance(route, APIRoute):
        return False
    return any(dependency.call is require_api_key for dependency in route.dependant.dependencies)


def test_every_route_but_healthcheck_is_protected(build_app):
    """The whole surface, not a list of prefixes someone has to remember to extend.

    A router included without the dependency, or a new public endpoint added by
    accident, fails here. Adding a route to PUBLIC_PATHS is then a deliberate edit to
    a set named for what it means.
    """
    app = build_app(SECRET)
    unprotected = {
        route.path
        for route in app.routes
        if route.path not in PUBLIC_PATHS and not _is_protected(route)
    }
    assert not unprotected, f'routes missing auth: {sorted(unprotected)}'


@pytest.mark.parametrize('path', ['/openapi.json', '/docs', '/redoc'])
def test_the_api_docs_are_not_public(build_app, path):
    """FastAPI serves these to anyone by default, and they describe every route.

    main.py switches the built-ins off and re-declares them behind auth; if that ever
    regresses the schema becomes readable by anyone holding the URL.
    """
    app = build_app(SECRET)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': f'Bearer {SECRET}'}).status_code == 200


@pytest.mark.parametrize('path', ['/openapi.json', '/docs', '/redoc'])
def test_the_api_docs_stay_browsable_with_no_key_set(build_app, path):
    """Local compose sets no key, and losing the browser docs there would be a real cost."""
    app = build_app(None)
    assert TestClient(app).get(path).status_code == 200
