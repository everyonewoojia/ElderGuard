"""
银发守护 - 老人端 Agent 主入口
"""
import asyncio
import logging
import os
import sys

# 保证以 `python src/main.py` 或 `python -m src.main` 启动时均能 import src.* 包：
# 直接把 src 目录当顶层（sys.path[0]=src）会找不到 `src` 模块，需将项目根(elder-agent)加入 sys.path。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from src.core.agent import ElderAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    load_dotenv()
    
    agent = ElderAgent()
    await agent.start()
    
    logger.info("老人端 Agent 已启动")
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("正在停止...")
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
