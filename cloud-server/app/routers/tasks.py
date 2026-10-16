"""P3-1 提醒任务接口（家属端，M-APP-03 / FR-FL-05）。

路径（前缀 /api/v1/tasks）：
- POST    /api/v1/tasks        为绑定老人创建提醒任务
- GET     /api/v1/tasks        查询当前家属全部老人的任务（越权 0 次）
- DELETE  /api/v1/tasks/{id}   删除任务（仅本人老人）

鉴权 + 行级隔离：统一挂载 get_current_family，并对 elder 做归属校验（越权 → 403）。
复用数据层 services/storage.py，不再在路由层直接写 SQL。
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import User
from app.routers.deps import get_current_family
from app.services import storage

router = APIRouter()


class TaskCreate(BaseModel):
    elder_id: int
    task_type: str
    title: str
    content: str | None = None
    schedule_time: str | None = None


class TaskResponse(BaseModel):
    id: int
    elder_id: int
    task_type: str
    title: str
    content: str | None = None
    schedule_time: str | None = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


def _check_elder_owner(db: Session, elder_id: int, current_user: User):
    """校验 elder 存在且归属当前家属：不存在 → 404；非本人绑定 → 403（越权隔离）。"""
    elder = storage.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    if elder.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该老人数据")
    return elder


@router.get("", response_model=List[TaskResponse])
async def list_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """查询当前家属全部老人的提醒任务（仅本人数据，越权 0 次）。"""
    return storage.list_tasks_by_owner(db, current_user.id)


@router.post("", response_model=TaskResponse)
async def create_task(
    task: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """为绑定老人创建提醒任务（需归属校验，越权 → 403）。"""
    _check_elder_owner(db, task.elder_id, current_user)
    return storage.create_task(
        db,
        task.elder_id,
        task.task_type,
        task.title,
        task.content,
        task.schedule_time,
    )


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """删除任务（仅本人老人，越权 → 403；不存在 → 404）。"""
    task = storage.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _check_elder_owner(db, task.elder_id, current_user)
    storage.delete_task(db, task_id)
    return {"message": "任务已删除"}
