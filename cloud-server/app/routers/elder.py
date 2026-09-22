from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.core.database import get_db
from app.models.models import Elder, MonitorLog

router = APIRouter()


class ElderResponse(BaseModel):
    id: int
    name: int
    age: int
    phone: str
    address: str
    privacy_enabled: bool

    class Config:
        from_attributes = True


class LogResponse(BaseModel):
    id: int
    event_type: str
    content: str
    created_at: str

    class Config:
        from_attributes = True


@router.get("/{elder_id}", response_model=ElderResponse)
async def get_elder(elder_id: int, db: Session = Depends(get_db)):
    elder = db.query(Elder).filter(Elder.id == elder_id).first()
    if not elder:
        raise HTTPException(status_code=404, detail="老人不存在")
    return elder


@router.get("/{elder_id}/logs", response_model=List[LogResponse])
async def get_elder_logs(elder_id: int, db: Session = Depends(get_db)):
    logs = db.query(MonitorLog).filter(MonitorLog.elder_id == elder_id).all()
    return logs
