"""Auth tests for the graph endpoints.

Unlike tests/test_live_falkordb_int.py these need no OpenAI key and no database, so they
run in the default `make test`. Nothing here asserts a successful response: the app is
built without its lifespan, so a request that gets past auth reaches a handler with no
Graphiti client and fails. What is asserted is only whether require_api_key let the request
through — see _assert_passed_auth.

The real graph_service.main is reloaded per case rather than a stand-in app assembled,
because the wiring in main is what these guard: the likeliest regression is a future router
included without the auth dependency, and only the real module can catch that.
"""

import importlib

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from graph_service import config
from graph_service.auth import require_api_key

SECRET = 'test-api-key-6Yp2Qk'

# The allowlist the app is held to: every other route must carry the auth dependency. An
# allowlist rather than a list of protected prefixes, so a route added under a name nobody
# predicted is a failure by default. The docs are deliberately public — see main.py.
PUBLIC_PATHS = {'/healthcheck', '/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json'}
DOCS_PATHS = ['/openapi.json', '/docs', '/redoc']


@pytest.fixture
def build_app(monkeypatch):
    """Return a factory that builds the real app with GRAPHITI_API_KEY set to a given value."""

    def _build(graphiti_api_key: str | None):
        monkeypatch.setenv('OPENAI_API_KEY', 'unused-by-these-tests')
        if graphiti_api_key is None:
            monkeypatch.delenv('GRAPHITI_API_KEY', raising=False)
        else:
            monkeypatch.setenv('GRAPHITI_API_KEY', graphiti_api_key)
        # Settings are cached and the app is built at import time, so both have to be
        # discarded for a new key to take effect.
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

    Not `== 500`: what the handler does next is not this file's business, and pinning the
    exact failure would break the day the fixture gains a lifespan. 401 is the one status
    that means auth itself rejected the request.
    """
    assert response.status_code != 401, response.text


@pytest.mark.parametrize('graphiti_api_key', [None, '', '   '], ids=['unset', 'blank', 'space'])
def test_no_key_means_no_service(build_app, graphiti_api_key):
    """Auth is mandatory, so a missing key is a startup error and not an open API.

    There is no way to opt out, which is the point: a service that boots without a key and a
    service that 401s every request both look healthy to Render, and only one of them is
    safe. Blank and whitespace count as missing — a stray `GRAPHITI_API_KEY=` in a .env must
    not become a key of '' or ' ' that the deployment then accepts.

    Asserted against the app's lifespan rather than get_settings() alone, because that is
    what makes it a startup failure: uvicorn runs it before serving, so the process exits
    instead of coming up. Entering it is enough — settings are read on the first line.
    """
    app = build_app(graphiti_api_key)
    with pytest.raises(ValidationError, match='graphiti_api_key'), TestClient(app):
        pass  # never reached: entering the lifespan is what raises


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
        # The right value one byte short: guards compare_digest against being swapped for a
        # prefix comparison.
        pytest.param({'Authorization': f'Bearer {SECRET[:-1]}'}, id='truncated-key'),
        # Raw bytes, because httpx refuses to encode a non-ASCII str header: Starlette
        # decodes headers as latin-1, so this arrives as a non-ASCII str, which
        # compare_digest refuses to compare as str. Must be a 401, not a 500 for anyone who
        # sends a stray high byte.
        pytest.param({b'Authorization': b'Bearer \xff'}, id='non-ascii-token'),
    ],
)
def test_configured_key_rejects_everything_else(build_app, headers):
    app = build_app(SECRET)
    assert _search(app, headers).status_code == 401


def test_a_non_ascii_configured_key_still_only_rejects(build_app):
    """A key with an accent in it must not take the whole service down.

    Render generates an ASCII key, but rotating it is a manual edit and nothing there
    rejects a passphrase. Compared as str, such a key raised TypeError on every request and
    returned 500 — the service answered nothing at all, rather than rejecting the requests
    that were wrong. It still can't be relied on to authenticate, whatever the client sends,
    which is why auth.py says to keep the key ASCII; what is guaranteed here is a clean 401
    instead of a crash.
    """
    app = build_app('clé-secrète')
    assert _search(app, {'Authorization': 'Bearer wrong-key'}).status_code == 401
    assert _search(app, {b'Authorization': 'Bearer clé-secrète'.encode()}).status_code == 401


def test_rejection_names_the_scheme_to_retry_with(build_app):
    """RFC 9110 requires WWW-Authenticate on a 401, and it tells clients what to send."""
    app = build_app(SECRET)
    assert _search(app).headers.get('WWW-Authenticate') == 'Bearer'


def test_healthcheck_stays_public(build_app):
    """If auth ever covers it, every deploy fails its health check and rolls back."""
    app = build_app(SECRET)
    response = TestClient(app).get('/healthcheck')
    assert response.status_code == 200
    assert response.json() == {'status': 'healthy'}


@pytest.mark.parametrize('path', DOCS_PATHS)
def test_the_docs_are_browsable(build_app, path):
    """Deliberately public, and worth a test because it is a judgement call, not a default.

    A browser can't attach a bearer header to a navigation, so protecting these would make
    them unreachable by clicking a link on any deployment that has a key.
    """
    app = build_app(SECRET)
    assert TestClient(app).get(path).status_code == 200


def test_the_docs_offer_the_bearer_scheme(build_app):
    """Swagger's Authorize button, which keeps the docs usable against a deployment.

    It comes from the routers' dependency, which is easy to lose by protecting the routes
    some other way, and its absence would leave Try it out silently 401ing.
    """
    schema = TestClient(build_app(SECRET)).get('/openapi.json').json()
    assert schema['components']['securitySchemes']['HTTPBearer']['scheme'] == 'bearer'
    assert schema['paths']['/search']['post']['security'] == [{'HTTPBearer': []}]


def _is_protected(route) -> bool:
    """Whether require_api_key runs before this route's handler.

    Identity, not a name comparison: a same-named dependency from somewhere else would
    otherwise satisfy the check. Anything that is not an APIRoute has no dependant and
    counts as unprotected — a path served by a Mount or a WebSocketRoute is exactly the case
    this must not wave through.
    """
    if not isinstance(route, APIRoute):
        return False
    return any(dependency.call is require_api_key for dependency in route.dependant.dependencies)


def test_every_route_but_the_public_ones_is_protected(build_app):
    """The whole surface, not a list of prefixes someone has to remember to extend.

    A router included without the dependency, or a new public endpoint added by accident,
    fails here. Adding a route to PUBLIC_PATHS is then a deliberate edit.
    """
    app = build_app(SECRET)
    unprotected = {
        route.path
        for route in app.routes
        if route.path not in PUBLIC_PATHS and not _is_protected(route)
    }
    assert not unprotected, f'routes missing auth: {sorted(unprotected)}'
