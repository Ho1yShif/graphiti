from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
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


# /docs, /redoc and /openapi.json are left as FastAPI mounts them, which is public. The
# schema describes the routes, not the graph, and the README lists them all anyway — while
# hiding it would cost real usability, since a browser can't put an Authorization header on
# a plain navigation. The key on the routes below is the control that matters; Swagger's
# Authorize button picks it up from their dependency and Try it out works with it.
app = FastAPI(title='Graphiti API', lifespan=lifespan)


# Applied at the router rather than per-route, so an endpoint added to either router is
# closed by default. Anything public has to be declared on the app itself, below, which
# makes it an explicit decision rather than an omission — tests/test_auth.py holds the
# whole route table to that.
app.include_router(retrieve.router, dependencies=[Depends(require_api_key)])
app.include_router(ingest.router, dependencies=[Depends(require_api_key)])


# Public: Render polls this to decide whether the service is live.
@app.get('/healthcheck')
async def healthcheck():
    return JSONResponse(content={'status': 'healthy'}, status_code=200)
