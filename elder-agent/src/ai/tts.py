"""
TTS 语音合成服务 - 阿里云智能语音合成

外部 API 失败/未配置时 speak 返回 False 且不崩溃（CON-D-06），由上层决定是否降级（本地话术）。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self):
        pass

    async def speak(self, text: str) -> bool:
        """语音播报。

        返回是否成功播报：合成失败/未配置时返回 False（不崩溃，供上层降级判断）。
        """
        logger.info("Speech synthesis requested")

        audio_data = await self._synthesize(text)
        if not audio_data:
            logger.warning("语音合成失败/未配置，跳过播报")
            return False

        await self._play(audio_data)
        return True

    async def _synthesize(self, text: str) -> Optional[bytes]:
        """合成语音（外部 TTS API）"""
        logger.info("调用 TTS API")
        return None

    async def _play(self, audio_data: bytes):
        """播放语音"""
        logger.info("播放语音")
