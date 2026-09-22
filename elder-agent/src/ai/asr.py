"""
ASR 语音识别服务 - Vosk 本地识别 + 阿里云云端识别
"""
import asyncio
import json
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ASRService:
    def __init__(self):
        self._model = None
        self._recognizer = None
    
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
        """监听语音输入"""
        try:
            import pyaudio
            
            p = pyaudio.PyAudio()
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=8000
            )
            
            logger.info("开始监听语音...")
            data = stream.read(8000, exception_on_overflow=False)
            
            stream.stop_stream()
            stream.close()
            p.terminate()
            
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
        """云端识别（阿里云）"""
        logger.info("使用云端识别")
        return None
