"""
银发守护 - 老人端 Agent 主入口
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

# 保证以 `python src/main.py` 或 `python -m src.main` 启动时均能 import src.* 包：
# 直接把 src 目录当顶层（sys.path[0]=src）会找不到 `src` 模块，需将项目根(elder-agent)加入 sys.path。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from src.core.agent import ElderAgent
from src.core.memory import MemoryManager
from src.core.privacy import PrivacyGuard
from src.core.alert_outbox import AlertOutbox
from src.integration.edge import EdgeClient, EdgeCredentialError, EdgeReporter

logging.basicConfig(level=logging.INFO)
# HTTPX INFO includes full business URLs. Keep application status/error events.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def create_agent() -> ElderAgent:
    """Build a closed guard and a device client; never initialize audio here.

    EDGE_BASE_URL and EDGE_ELDER_ID identify the configured cloud/elder.
    EDGE_DEVICE_TOKEN_FILE is consumed by EdgeClient, without copying credentials
    into the profile. Invalid configuration permits only non-sensing operation.
    """
    data_dir = os.environ.get("ELDER_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))
    memory = MemoryManager(data_dir)
    privacy = PrivacyGuard(memory=memory)
    client = None
    elder_id = None
    outbox = None
    base_url = os.environ.get("EDGE_BASE_URL", "http://127.0.0.1:8000")
    device_id = os.environ.get("EDGE_DEVICE_ID")
    if device_id is not None:
        # Public binding is explicit, never inferred from the device JWT.
        # Acquire/load before client creation; invalid storage fails closed.
        try:
            configured_elder = int(os.environ.get("EDGE_ELDER_ID", ""))
        except ValueError:
            configured_elder = None
        outbox = AlertOutbox(data_dir, device_id=device_id,
                             elder_id=configured_elder, cloud_url=base_url)
    else:
        logger.warning("Alert persistence unavailable: public device binding missing")
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("Invalid cloud endpoint")
        elder_id = int(os.environ.get("EDGE_ELDER_ID", ""))
        if elder_id <= 0:
            raise ValueError("Invalid elder configuration")
        client = EdgeClient(base_url=base_url)
    except (ValueError, EdgeCredentialError):
        logger.warning("Device configuration unavailable; sensing disabled")
    except BaseException:
        if outbox is not None:
            outbox.close()
        raise
    try:
        reporter = EdgeReporter(client, outbox=outbox) if client is not None and outbox is not None else None
        return ElderAgent(memory=memory, privacy=privacy, edge_client=client,
                          elder_id=elder_id, owns_edge_client=True,
                          alert_outbox=outbox, owns_alert_outbox=True, reporter=reporter)
    except BaseException:
        if outbox is not None:
            outbox.close()
        raise


async def main():
    """Load configuration before construction and always stop on cancellation."""
    load_dotenv()
    agent = create_agent()
    try:
        await agent.start()
        logger.info("老人端 Agent 已启动，采集状态: %s", agent.capture_status)
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("正在停止...")
    finally:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
