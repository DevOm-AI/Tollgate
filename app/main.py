from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.api.dashboard import router as dashboard_router
from app.api.demo import router as demo_router
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.http import http_client
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Close pooled connections so a restart doesn't leave them open on the server.
    await http_client.aclose()
    await redis_client.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Tollgate", debug=settings.debug, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(admin_router)
    app.include_router(dashboard_router)
    app.include_router(demo_router)
    app.include_router(chat_router)
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
        # So the dashboard and playground can read the limits off each response.
        expose_headers=[
            "Retry-After",
            *(
                f"X-RateLimit-{kind}-{limit}"
                for kind in ("Limit", "Remaining", "Reset")
                for limit in ("Requests", "Tokens")
            ),
        ],
    )
    return app


app = create_app()
