import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.core.logging import redact_url_path, setup_logging
from app.core.upload_limits import UploadBodyLimitMiddleware
from app.storage.s3 import get_storage

logger = logging.getLogger("sendrift.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager."""
    setup_logging(settings.LOG_LEVEL)
    logger.info("Sendrift API starting up (env=%s)...", settings.APP_ENV)
    await get_storage().ensure_bucket()
    yield
    logger.info("Sendrift API shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description="Sendrift - Minimalist temporary file sharing API",
    docs_url=None if settings.APP_ENV == "production" else "/docs",
    redoc_url=None if settings.APP_ENV == "production" else "/redoc",
    openapi_url=None if settings.APP_ENV == "production" else "/openapi.json",
    lifespan=lifespan,
)

# CORS Middleware with explicit origins from configuration
app.add_middleware(
    UploadBodyLimitMiddleware,
    upload_path=f"{settings.API_V1_PREFIX}/shares",
    max_body_bytes=settings.MAX_FILE_SIZE_BYTES + 5 * 1024 * 1024,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_and_sanitization_middleware(request: Request, call_next):
    """Logs incoming requests with URL tokens masked, timing the execution."""
    start_time = time.perf_counter()
    safe_path = redact_url_path(request.url.path)
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            "%s %s -> %s (%.2fms)",
            request.method,
            safe_path,
            response.status_code,
            process_time_ms,
        )
        return response
    except Exception as exc:
        process_time_ms = (time.perf_counter() - start_time) * 1000.0
        logger.error(
            "%s %s -> unhandled error %s (%.2fms)",
            request.method,
            safe_path,
            type(exc).__name__,
            process_time_ms,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )


# Global fallback exception handler ensuring DB/S3 internals never leak
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    safe_path = redact_url_path(request.url.path)
    logger.error("Unhandled exception on %s: %s", safe_path, type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected error occurred"},
    )


# Technical endpoints (/health/live, /health/ready)
app.include_router(health_router)

# Versioned API routes (/api/v1/...)
app.include_router(api_v1_router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Sendrift API",
        "status": "online",
        "documentation": "/docs" if settings.APP_ENV != "production" else "disabled",
    }
