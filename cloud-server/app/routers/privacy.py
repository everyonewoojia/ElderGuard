from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.models.models import Elder

router = APIRouter()


class PrivacyUpdate(BaseModel):
    elder_id: int
    enabled: bool


@router.post("/auth")
async def update_privacy(data: PrivacyUpdate, db: Session = Depends(get_db)):
    elder = db.query(Elder).filter(Elder.id == data.elder_id).first()
    if not elder:
        raise HTTPException(status_code=404, detail="老人不存在")
    elder.privacy_enabled = data.enabled
    db.commit()
    return {"message": "隐私授权已更新", "enabled": elder.privacy_enabled}
