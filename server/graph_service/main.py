from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

from graph_service.auth import require_api_key
from graph_service.config import get_settings
from graph_service.routers import ingest, retrieve
from graph_service.zep_graphiti import initialize_graphiti, shutdown_graphiti


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    await initialize_graphiti(settings)
    yield
    await shutdown_graphiti()


_AUTH = [Depends(require_api_key)]
_OPENAPI_PATH = '/openapi.json'

# FastAPI mounts /docs, /redoc and /openapi.json itself, and mounts them public. They
# are switched off here and re-declared below behind _AUTH instead, so the schema of
# every route is not readable by anyone who finds the URL. Turning them off at the
# constructor is the only way to reach them: routes FastAPI adds during setup() cannot
# have dependencies attached afterwards.
#
# title is set here rather than only on the docs pages below, so the schema they serve
# and the pages themselves agree on what this API is called.
app = FastAPI(
    title='Graphiti API',
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# Applied at the router rather than per-route, so a new endpoint added to either router
# is closed by default. Anything that should be public has to be declared on the app
# itself, below, which makes that an explicit decision rather than an omission.
app.include_router(retrieve.router, dependencies=_AUTH)
app.include_router(ingest.router, dependencies=_AUTH)


# Public: Render polls this to decide whether the service is live, and it exposes
# nothing about the graph. It is the only route that is not behind _AUTH, and
# tests/test_auth.py fails if that ever stops being true.
@app.get('/healthcheck')
async def healthcheck():
    return JSONResponse(content={'status': 'healthy'}, status_code=200)


# The API docs, restored with auth. include_in_schema=False keeps them out of the
# schema they serve, which is where FastAPI puts them by default.
#
# A browser cannot attach an Authorization header to a plain navigation, so on a
# deployment with a key set these are reachable by an HTTP client but not by clicking
# a link. That is deliberate, and it costs nothing locally: `docker compose` sets no
# GRAPHITI_API_KEY, auth is off, and /docs works in the browser as it always has.
@app.get(_OPENAPI_PATH, include_in_schema=False, dependencies=_AUTH)
async def openapi():
    return JSONResponse(app.openapi())


# The URL the docs pages fetch the schema from is built the way FastAPI builds it for its
# own built-ins: prefixed with root_path, so the pages keep working if this app is ever
# served under a path prefix by a proxy. Nothing does that today.
def _openapi_url(request: Request) -> str:
    return request.scope.get('root_path', '').rstrip('/') + _OPENAPI_PATH


@app.get('/docs', include_in_schema=False, dependencies=_AUTH)
async def swagger_ui(request: Request):
    return get_swagger_ui_html(openapi_url=_openapi_url(request), title=app.title)


@app.get('/redoc', include_in_schema=False, dependencies=_AUTH)
async def redoc(request: Request):
    return get_redoc_html(openapi_url=_openapi_url(request), title=app.title)
