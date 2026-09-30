import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Check Postgres and Redis. Public on purpose: load balancers and Docker probe it."""
    checks = {
        "database": await _check("database", lambda: db.execute(text("SELECT 1"))),
        "redis": await _check("redis", redis.ping),
    }
    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "error", "checks": checks}


async def _check(name: str, probe: Callable[[], Awaitable[object]]) -> str:
    try:
        await probe()
    except Exception as exc:
        # Details go to the log only, so hostnames and credentials never reach the response.
        logger.warning("Health check failed for %s: %s", name, exc)
        return "error"
    return "ok"
