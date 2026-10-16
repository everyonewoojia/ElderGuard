from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.routers import auth, tasks, alerts, elder, privacy

app = FastAPI(
    title="银发守护 Agent 中枢服务",
    description="空巢老人智能看护系统云端服务",
    version="0.1.0"
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


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
