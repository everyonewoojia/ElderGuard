"""老人端云端集成模块 - 端云通信与断网重试（P6）。

- EdgeClient：老人端向云端上报告警、拉取任务/设置/授权（httpx，可注入 client 便于测试）。
- EdgeReporter：告警上报失败本地缓存 + 重试队列（重试 ≤3 次，KK01），失败有记录。

隐私硬约束：本模块只上传已授权采集产生的事件；授权联动见 P6-4。
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class EdgeClient:
    """老人端云端客户端。

    client 参数：可注入异步 http 客户端（暴露 .post/.get），默认懒加载 httpx.AsyncClient。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", client=None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    def _http(self):
        """懒加载默认 httpx 客户端"""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def report_alert(
        self,
        elder_id: int,
        alert_type: str,
        priority: str = "P1",
        content: Optional[str] = None,
    ) -> Optional[dict]:
        """上报告警到云端，成功返回 data 字典，失败（网络/非 2xx）返回 None。"""
        payload = {
            "elder_id": elder_id,
            "alert_type": alert_type,
            "priority": priority,
            "content": content,
        }
        try:
            r = await self._http().post(f"{self.base_url}/api/v1/edge/alerts", json=payload)
            if r.status_code in (200, 201):
                return r.json().get("data")
        except Exception as e:  # noqa: BLE001 - 网络异常视为失败
            logger.warning(f"上报告警失败: {e}")
        return None

    async def fetch_tasks(self, elder_id: int) -> List[dict]:
        """拉取该老人的激活提醒任务（FR-FL-06）。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/tasks", params={"elder_id": elder_id}
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取任务失败: {e}")
        return []

    async def fetch_settings(self, elder_id: int) -> Optional[dict]:
        """拉取老人设置（FR-FL-08）。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/settings", params={"elder_id": elder_id}
            )
            if r.status_code == 200:
                data = r.json().get("data")
                return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取设置失败: {e}")
        return None

    async def fetch_privacy(self, elder_id: int) -> Optional[bool]:
        """拉取隐私授权状态（FR-CM-05 授权联动），失败返回 None。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/privacy", params={"elder_id": elder_id}
            )
            if r.status_code == 200:
                data = r.json().get("data")
                if isinstance(data, dict) and "enabled" in data:
                    return bool(data["enabled"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取授权状态失败: {e}")
        return None


class EdgeReporter:
    """告警上报器：失败本地缓存 + 重试队列（重试 ≤3 次，KK01），最终失败有记录。"""

    MAX_RETRIES = 3

    def __init__(self, client: EdgeClient):
        self._client = client
        self._pending: List[dict] = []
        self._failures: List[dict] = []

    async def report(
        self,
        elder_id: int,
        alert_type: str,
        priority: str = "P1",
        content: Optional[str] = None,
    ) -> bool:
        """上报告警；失败入重试队列，返回是否成功。"""
        result = await self._client.report_alert(elder_id, alert_type, priority, content)
        if result is not None:
            return True
        await self._enqueue(elder_id, alert_type, priority, content, retries_left=self.MAX_RETRIES)
        return False

    async def _enqueue(
        self,
        elder_id: int,
        alert_type: str,
        priority: str,
        content: Optional[str],
        retries_left: int,
    ):
        """入重试队列；重试次数耗尽则记为最终失败。"""
        if retries_left > 0:
            self._pending.append(
                {
                    "elder_id": elder_id,
                    "alert_type": alert_type,
                    "priority": priority,
                    "content": content,
                    "retries_left": retries_left - 1,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.info(f"告警上报失败，已入重试队列（剩余 {retries_left - 1} 次）")
        else:
            self._failures.append(
                {
                    "elder_id": elder_id,
                    "alert_type": alert_type,
                    "priority": priority,
                    "content": content,
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.warning("告警重试次数耗尽，记为失败")

    async def flush(self) -> int:
        """重试队列：成功移除；失败递减重试次数，耗尽进 failures。返回累计失败数。"""
        remaining: List[dict] = []
        for item in self._pending:
            result = await self._client.report_alert(
                item["elder_id"], item["alert_type"], item["priority"], item["content"]
            )
            if result is not None:
                continue  # 重试成功，丢弃
            if item["retries_left"] > 0:
                item["retries_left"] -= 1
                remaining.append(item)
            else:
                self._failures.append(
                    {
                        "elder_id": item["elder_id"],
                        "alert_type": item["alert_type"],
                        "priority": item["priority"],
                        "content": item["content"],
                        "status": "failed",
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        self._pending = remaining
        return len(self._failures)

    @property
    def pending(self) -> List[dict]:
        """当前待重试队列"""
        return list(self._pending)

    @property
    def failures(self) -> List[dict]:
        """最终失败记录（重试耗尽）"""
        return list(self._failures)
