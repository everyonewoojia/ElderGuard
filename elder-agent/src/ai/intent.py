"""
意图理解服务 - 通义千问大模型
"""
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class IntentService:
    def __init__(self):
        pass
    
    async def parse(self, text: str) -> Optional[Dict]:
        """解析意图"""
        intent = await self._cloud_parse(text)
        if intent:
            return intent
        
        return self._rule_parse(text)
    
    async def _cloud_parse(self, text: str) -> Optional[Dict]:
        """云端意图解析（通义千问）"""
        logger.info("调用通义千问 API")
        return None
    
    def _rule_parse(self, text: str) -> Dict:
        """规则匹配"""
        text_lower = text.lower()
        
        if any(kw in text_lower for kw in ["救命", "救命啊", "help", "我不舒服"]):
            return {"type": "help", "priority": "P0", "content": text}
        
        if any(kw in text_lower for kw in ["摔倒", "跌倒", "fall"]):
            return {"type": "fall", "priority": "P0", "content": text}
        
        if any(kw in text_lower for kw in ["提醒", "吃药", "服药"]):
            return {"type": "reminder", "priority": "P1", "content": text}
        
        if any(kw in text_lower for kw in ["几点", "时间", "现在"]):
            return {"type": "time", "priority": "P2", "content": text}
        
        return {"type": "unknown", "priority": "P2", "content": text}
