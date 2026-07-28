import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from graph_service.config import get_settings
from graph_service.demo import DemoConfig, DemoModeMiddleware, DemoState
from graph_service.routers import ingest, retrieve
from graph_service.zep_graphiti import initialize_graphiti

# Without this the app's own INFO lines never reach the deploy logs, which makes a
# misconfigured DEMO flag invisible.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

demo_config = DemoConfig.from_env()
demo_state = DemoState(demo_config) if demo_config.enabled else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info('Starting Graphiti API with demo mode %s', 'on' if demo_config.enabled else 'off')

    settings = get_settings()
    if demo_state is not None and demo_config.model_name:
        # The demo runs on the deployer's key, so pin the cheaper model.
        settings.model_name = demo_config.model_name

    await initialize_graphiti(settings)

    sweeper = asyncio.create_task(demo_state.sweep_expired(settings)) if demo_state else None

    yield

    if sweeper is not None:
        # Await the cancellation so the sweeper's own cleanup runs before the loop closes.
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
    # Shutdown
    # No need to close Graphiti here, as it's handled per-request


app = FastAPI(lifespan=lifespan)

if demo_state is not None:
    app.add_middleware(DemoModeMiddleware, state=demo_state)


app.include_router(retrieve.router)
app.include_router(ingest.router)


@app.get('/healthcheck')
async def healthcheck():
    return JSONResponse(content={'status': 'healthy'}, status_code=200)
