"""
记忆管理单元 - 管理短期会话记忆和长期用户档案
"""
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        self.short_term: list = []
        self.long_term_file = self.data_dir / "user_profile.json"
        self.long_term = self._load_long_term()
    
    def _load_long_term(self) -> dict:
        """加载长期记忆"""
        if self.long_term_file.exists():
            with open(self.long_term_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "preferences": {},
            "history": [],
            "alerts": [],
        }
    
    def _save_long_term(self):
        """Atomically replace the profile, including durable local revocations."""
        temporary = None
        identity = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.data_dir, prefix=".profile-",
                                             delete=False) as output:
                temporary = Path(output.name)
                info = os.fstat(output.fileno())
                identity = (info.st_dev, info.st_ino)
                json.dump(self.long_term, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.long_term_file)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                current = temporary.lstat()
                if (current.st_dev, current.st_ino) == identity and not temporary.is_symlink():
                    temporary.unlink()

    def get_local_privacy_revoked(self) -> bool:
        """Missing marker means no local veto, never permission to collect."""
        value = self.long_term["preferences"].get("privacy_local_revoked", False)
        if type(value) is not bool:
            raise ValueError("Local privacy record invalid")
        return value

    def set_local_privacy_revoked(self, revoked: bool) -> None:
        """Persist only the local veto; credentials and cloud grants are excluded."""
        if type(revoked) is not bool:
            raise ValueError("Local privacy record invalid")
        preferences = self.long_term["preferences"]
        missing = object()
        previous = preferences.get("privacy_local_revoked", missing)
        preferences["privacy_local_revoked"] = revoked
        try:
            self._save_long_term()
        except Exception:
            if previous is missing:
                preferences.pop("privacy_local_revoked", None)
            else:
                preferences["privacy_local_revoked"] = previous
            raise
    
    async def save(self, event: dict):
        """保存事件到记忆"""
        self.short_term.append(event)

        # 短期记忆硬上限：超过 50 条只保留最近 50 条（避免无界增长）
        if len(self.short_term) > 50:
            self.short_term = self.short_term[-50:]
        
        self.long_term["history"].append({
            "timestamp": event.get("timestamp"),
            "type": event.get("type"),
            "content": event.get("payload", {}).get("content", ""),
        })
        
        if len(self.long_term["history"]) > 1000:
            self.long_term["history"] = self.long_term["history"][-500:]
        
        self._save_long_term()
    
    async def get_context(self) -> dict:
        """获取当前上下文"""
        return {
            "short_term": self.short_term[-10:],
            "recent_history": self.long_term["history"][-10:],
            "preferences": self.long_term.get("preferences", {}),
        }
    
    async def update_preference(self, key: str, value):
        """更新用户偏好"""
        self.long_term["preferences"][key] = value
        self._save_long_term()
    
    async def get_preference(self, key: str) -> Optional:
        """获取用户偏好"""
        return self.long_term.get("preferences", {}).get(key)
