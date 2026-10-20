from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager, contextmanager

from app.core.config import settings
from app.routers import auth, tasks, alerts, elder, privacy, edge, ws
from app.core import database
from app.services.alert_delivery import DeliveryService, DeliveryWorker, DeliveryError


@asynccontextmanager
async def lifespan(application):
    """No startup DDL; keep one worker and finish shutdown before fixture cleanup."""
    settings.family_signing_key()
    previous = getattr(application.state, "delivery_worker", None)
    if previous is not None and previous.task is not None:
        raise DeliveryError("notification_worker_already_owned")
    # Tests already override the REST Session generator with P0-12 checks.
    # Reuse that exact scope; no second engine/factory or weaker test path.
    override = application.dependency_overrides.get(database.get_db)
    scope = contextmanager(override) if override is not None else None
    worker = DeliveryWorker(DeliveryService(session_scope=scope))
    application.state.delivery_worker = worker
    try:
        await worker.start()
        yield
    finally:
        await worker.stop()

app = FastAPI(
    title="银发守护 Agent 中枢服务",
    description="空巢老人智能看护系统云端服务",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一错误码响应结构 {code, message, data}（P2-4）"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["认证"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["任务"])
app.include_router(alerts.router, prefix="/api/v1/alerts", tags=["告警"])
app.include_router(elder.router, prefix="/api/v1/elder", tags=["老人"])
app.include_router(privacy.router, prefix="/api/v1/privacy", tags=["隐私"])
app.include_router(edge.router, prefix="/api/v1/edge", tags=["边缘接入"])
app.include_router(ws.router, prefix="/api/v1", tags=["实时推送"])


@app.get("/health")
async def health_check():
    """Public readiness exposes no database, backlog, operator or error details."""
    worker = getattr(app.state, "delivery_worker", None)
    if worker is None:
        return JSONResponse(status_code=503, content={"status": "degraded"})
    health = worker.health()
    # A stale/absent completed-loop heartbeat is not evidence of readiness.
    from datetime import datetime, timezone
    heartbeat = health["last_loop_at"]
    stale = heartbeat is None or (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds() > 10
    if health["state"] != "running" or stale:
        return JSONResponse(status_code=503, content={"status": "degraded"})
    return {"status": "healthy"}
