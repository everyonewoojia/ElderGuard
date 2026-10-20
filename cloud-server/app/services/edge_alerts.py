"""Transactional device alert admission; no notification or independent commit helper."""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.models import AlertLog, Device, EdgeAlertReceipt, Elder


def _lookup_receipt(db: Session, device_id: str, event_id: str):
    """Look up only the authenticated device's namespace."""
    return db.query(EdgeAlertReceipt).filter_by(device_id=device_id, event_id=event_id).first()


def _existing_alert(db: Session, receipt: EdgeAlertReceipt, elder_id: int, digest: str):
    """Do not disclose a previous alert unless its full immutable payload matches."""
    if receipt.elder_id != elder_id or receipt.payload_hash != digest:
        raise HTTPException(status_code=409, detail="事件 ID 已用于不同告警")
    alert = db.get(AlertLog, receipt.alert_id)
    if alert is None or alert.elder_id != elder_id:
        raise HTTPException(status_code=503, detail="告警接收记录暂不可用，请稍后重试")
    return alert, receipt, False


def accept_alert(db: Session, device: Device, elder: Elder, data):
    """Call only after device authentication and live elder ownership verification.

    Auth dependencies already opened a read transaction; do not start a nested
    transaction or call storage.create_alert(), which commits independently.
    The database unique constraint is the final concurrent admission arbiter.
    """
    if elder.id != device.elder_id or elder.owner_id != device.owner_id:
        raise HTTPException(status_code=403, detail="无权访问该老人数据")
    normalized = {"elder_id": elder.id, "alert_type": data.alert_type,
                  "priority": data.priority, "content": data.content}
    digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    try:
        receipt = _lookup_receipt(db, device.device_id, data.event_id)
        if receipt is not None:
            return _existing_alert(db, receipt, elder.id, digest)
        alert = AlertLog(**normalized, status="pending")
        db.add(alert)
        db.flush()  # Allocate ID without committing an orphan alert.
        receipt = EdgeAlertReceipt(device_id=device.device_id, event_id=data.event_id,
                                   elder_id=elder.id, payload_hash=digest, alert_id=alert.id,
                                   notification_state="pending", notification_attempts=0)
        db.add(receipt)
        db.commit()
        db.refresh(alert)
        db.refresh(receipt)
        return alert, receipt, True
    except IntegrityError:
        db.rollback()  # Includes this loser's uncommitted AlertLog.
        try:
            winner = _lookup_receipt(db, device.device_id, data.event_id)
            if winner is not None:
                return _existing_alert(db, winner, elder.id, digest)
        except SQLAlchemyError:
            db.rollback()
        raise HTTPException(status_code=503, detail="告警接收暂不可用，请使用原事件 ID 重试") from None
    except SQLAlchemyError:
        # Commit may have succeeded remotely. Never invent an acknowledgement;
        # a retry with the same key will resolve its durable outcome.
        db.rollback()
        raise HTTPException(status_code=503, detail="告警接收暂不可用，请使用原事件 ID 重试") from None
