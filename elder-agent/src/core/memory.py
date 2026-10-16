"""
记忆管理单元 - 管理短期会话记忆和长期用户档案
"""
import json
import logging
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
        """保存长期记忆"""
        with open(self.long_term_file, "w", encoding="utf-8") as f:
            json.dump(self.long_term, f, ensure_ascii=False, indent=2)
    
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
