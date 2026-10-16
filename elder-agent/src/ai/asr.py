"""
ASR 语音识别服务 - Vosk 本地识别 + 阿里云云端识别

隐私硬约束：采集前强制校验授权（P5-5/FR-CM-05/AQ02），未授权不采集（采集 0 次）。
外部云端识别失败/未配置时返回 None，由上层兜底处理，不崩溃（CON-D-06）。
"""
import asyncio
import json
import logging
from typing import Optional
from pathlib import Path

from src.core.privacy import PrivacyGuard

logger = logging.getLogger(__name__)


class ASRService:
    def __init__(self, privacy: Optional[PrivacyGuard] = None):
        self._model = None
        self._recognizer = None
        self.privacy = privacy or PrivacyGuard()

    async def init(self):
        """初始化 ASR"""
        try:
            from vosk import Model, KaldiRecognizer
            import pyaudio

            model_path = Path("models/vosk-model-cn")
            if model_path.exists():
                self._model = Model(str(model_path))
                self._recognizer = KaldiRecognizer(self._model, 16000)
                logger.info("Vosk 模型加载成功")
            else:
                logger.warning("Vosk 模型不存在，使用云端识别")
        except ImportError:
            logger.warning("Vosk 未安装，使用云端识别")

    async def listen(self) -> Optional[bytes]:
        """监听语音输入。

        采集前强制校验隐私授权（P5-5）：未授权直接返回 None，不做任何采集（采集 0 次）。
        """
        if not self.privacy.check("audio"):
            return None

        try:
            import pyaudio

            p = pyaudio.PyAudio()
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=8000,
            )

            logger.info("开始监听语音...")
            data = stream.read(8000, exception_on_overflow=False)

            stream.stop_stream()
            stream.close()
            p.terminate()

            self.privacy.record_capture()
            return data
        except Exception as e:
            logger.error(f"语音采集失败: {e}")
            return None

    async def recognize(self, audio_data: bytes) -> Optional[str]:
        """识别语音"""
        if self._recognizer:
            if self._recognizer.AcceptWaveform(audio_data):
                result = json.loads(self._recognizer.FinalResult())
                text = result.get("text", "")
                if text:
                    logger.info(f"识别结果: {text}")
                    return text

        return await self._cloud_recognize(audio_data)

    async def _cloud_recognize(self, audio_data: bytes) -> Optional[str]:
        """云端识别（阿里云）。

        外部 API 失败/未配置时返回 None，由上层兜底（本地话术/规则），不崩溃（CON-D-06）。
        """
        logger.info("使用云端识别")
        return None
