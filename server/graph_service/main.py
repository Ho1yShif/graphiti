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


app = FastAPI(lifespan=lifespan)


# Applied at the router rather than per-route, so a new endpoint added to either router
# is closed by default. Anything that should be public has to be declared on the app
# itself, below, which makes that an explicit decision rather than an omission.
app.include_router(retrieve.router, dependencies=[Depends(require_api_key)])
app.include_router(ingest.router, dependencies=[Depends(require_api_key)])


# Public: Render polls this to decide whether the service is live, and it exposes
# nothing about the graph.
@app.get('/healthcheck')
async def healthcheck():
    return JSONResponse(content={'status': 'healthy'}, status_code=200)
