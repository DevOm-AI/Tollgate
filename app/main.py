from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Close pooled connections so a restart doesn't leave them open on the server.
    await redis_client.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Tollgate", debug=settings.debug, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(admin_router)
    app.include_router(chat_router)
    install_error_handlers(app)
    return app


app = create_app()
