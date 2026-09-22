"""
银发守护 - 老人端 Agent 主入口
"""
import asyncio
import logging
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
