from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from datetime import datetime

from app.core.database import get_db
from app.models.models import AlertLog

router = APIRouter()


class AlertResponse(BaseModel):
    id: int
    elder_id: int
    alert_type: str
    priority: str
    content: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/{elder_id}", response_model=List[AlertResponse])
async def get_alerts(elder_id: int, db: Session = Depends(get_db)):
    alerts = db.query(AlertLog).filter(AlertLog.elder_id == elder_id).all()
    return alerts


@router.post("/{alert_id}/cancel")
async def cancel_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.query(AlertLog).filter(AlertLog.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    alert.status = "cancelled"
    db.commit()
    return {"message": "告警已取消"}
