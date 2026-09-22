from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.core.database import get_db
from app.models.models import ReminderTask

router = APIRouter()


class TaskCreate(BaseModel):
    elder_id: int
    task_type: str
    title: str
    content: str
    schedule_time: str


class TaskResponse(BaseModel):
    id: int
    elder_id: int
    task_type: str
    title: str
    content: str
    schedule_time: str
    is_active: bool

    class Config:
        from_attributes = True


@router.get("/{elder_id}", response_model=List[TaskResponse])
async def get_tasks(elder_id: int, db: Session = Depends(get_db)):
    tasks = db.query(ReminderTask).filter(ReminderTask.elder_id == elder_id).all()
    return tasks


@router.post("/", response_model=TaskResponse)
async def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    db_task = ReminderTask(**task.dict())
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


@router.delete("/{task_id}")
async def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(ReminderTask).filter(ReminderTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(task)
    db.commit()
    return {"message": "任务已删除"}
