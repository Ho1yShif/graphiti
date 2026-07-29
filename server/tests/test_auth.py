"""Auth tests for the graph endpoints.

Unlike tests/test_live_falkordb_int.py these need no OpenAI key and no database, so
they run in the default `make test`. Requests that pass auth are expected to fail
afterwards with a 500: the app is built without its lifespan, so no Graphiti client
exists and the handler raises. That 500 is the assertion — it means the request got
past require_api_key, which is the only thing under test here.

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

# Prefixes of every route that touches the graph. Anything matching one of these must
# carry the auth dependency; see test_every_graph_route_is_protected.
GRAPH_PREFIXES = (
    '/search',
    '/messages',
    # Singular, so it covers both DELETE /episode/{uuid} and GET /episodes/{group_id}.
    '/episode',
    '/entity-edge',
    '/entity-node',
    '/group',
    '/clear',
    '/get-memory',
)


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
    assert _search(app).status_code == 500


def test_configured_key_accepts_the_right_bearer_token(build_app):
    app = build_app(SECRET)
    assert _search(app, {'Authorization': f'Bearer {SECRET}'}).status_code == 500


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
    all and counts as unprotected — a graph path served by a Mount or a WebSocketRoute
    is exactly the case this test must not wave through.
    """
    if not isinstance(route, APIRoute):
        return False
    return any(dependency.call is require_api_key for dependency in route.dependant.dependencies)


def test_every_graph_route_is_protected(build_app):
    """A router included without the dependency would silently expose the graph."""
    app = build_app(SECRET)
    unprotected = {
        route.path
        for route in app.routes
        if route.path.startswith(GRAPH_PREFIXES) and not _is_protected(route)
    }
    assert not unprotected, f'graph routes missing auth: {sorted(unprotected)}'


def test_the_graph_prefix_list_has_not_drifted(build_app):
    """Guards the test above: a new route under a new prefix would otherwise be ignored.

    Every route is either a graph route, one of the public endpoints, or a docs route
    FastAPI adds itself. A path that is none of those means GRAPH_PREFIXES needs a new
    entry and the coverage check above is no longer complete.
    """
    public = {'/healthcheck', '/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc'}
    app = build_app(SECRET)
    unclassified = {
        route.path
        for route in app.routes
        if route.path not in public and not route.path.startswith(GRAPH_PREFIXES)
    }
    assert not unclassified, f'routes matching neither GRAPH_PREFIXES nor public: {unclassified}'
