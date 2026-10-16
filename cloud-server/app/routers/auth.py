from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import timedelta
from typing import Optional
from pydantic import BaseModel

from app.core.database import get_db
from app.core.config import settings
from app.models.models import User
from app.routers.deps import get_current_user
from app.services.auth import verify_password, get_password_hash, create_access_token
from app.services.storage import create_user

router = APIRouter()


class RegisterRequest(BaseModel):
    """家属注册请求"""
    username: str
    password: str
    phone: Optional[str] = None
    role: str = "family"


@router.post("/register")
async def register(data: RegisterRequest, db: Session = Depends(get_db)):
    """家属注册：密码经 bcrypt 哈希落库，不存明文。"""
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    if data.phone and db.query(User).filter(User.phone == data.phone).first():
        raise HTTPException(status_code=400, detail="手机号已注册")
    user = create_user(
        db,
        username=data.username,
        password_hash=get_password_hash(data.password),
        role=data.role,
        phone=data.phone,
    )
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    """返回当前登录用户信息（复用统一鉴权依赖）"""
    return {"id": current_user.id, "username": current_user.username, "role": current_user.role}
