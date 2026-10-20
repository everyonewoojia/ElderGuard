"""老人端云端集成模块 - 端云通信与断网重试（P6）。

- EdgeClient：老人端向云端上报告警、拉取任务/设置/授权（httpx，可注入 client 便于测试）。
- EdgeReporter：默认生产装配使用唯一持久 Outbox；旧直接调用保留兼容。

隐私硬约束：本模块只上传已授权采集产生的事件；授权联动见 P6-4。
"""
import logging
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from src.core.alert_outbox import AlertOutbox, OutboxError, _cloud_origin

logger = logging.getLogger(__name__)


class EdgeCredentialError(ValueError):
    """Missing or unusable device configuration, without credential details."""


class ReceiptQueryError(ValueError):
    """Only controlled categories; never confuse query failure with not_found."""


@dataclass(frozen=True, repr=False)
class AlertOutcome:
    """Controlled transport evidence; never includes exceptions or credentials."""

    status: Optional[int] = None
    data: Optional[dict] = None
    error: Optional[str] = None


def _require_event_id(value: str) -> str:
    """Only the caller knows the durable identity; never generate one per send."""
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid event identity") from None
    return value


def _read_token_file(value) -> str:
    """Read the raw UTF-8 JWT produced by manage_edge_devices --output.

    The operator supplies an absolute path in a controlled directory. Reject
    links, non-files, oversized content and replacement during opening. This
    does not replace operating-system ACLs or server-side JWT verification.
    """
    try:
        path = Path(value)
        if (not path.is_absolute() or path.resolve() != path
                or any(p.is_symlink() or p.is_junction()
                       for p in [path, *path.parents])):
            raise ValueError
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16384:
            raise ValueError
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (not opened.st_ino or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)):
                raise ValueError
            content = source.read(16385)
        if len(content) > 16384:
            raise ValueError
        return content.decode("utf-8").strip()
    except Exception:
        raise EdgeCredentialError("Device credential file unavailable") from None


class EdgeClient:
    """老人端云端客户端。

    client 参数：可注入异步 http 客户端（暴露 .post/.get），默认懒加载 httpx.AsyncClient。
    凭证优先级：显式 device_token；显式 token_file；EDGE_DEVICE_TOKEN_FILE。
    文件为本地管理 CLI 输出的原始 UTF-8 JWT，轮换后需重新创建客户端。
    此处只验证传输格式，签名、有效期和绑定由云端验证。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", client=None,
                 *, device_token: Optional[str] = None, token_file=None):
        if device_token is None:
            source = token_file if token_file is not None else os.environ.get("EDGE_DEVICE_TOKEN_FILE")
            if not source:
                raise EdgeCredentialError("Device credentials required")
            device_token = _read_token_file(source)
        if (not isinstance(device_token, str) or len(device_token) > 16384
                or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                                device_token) is None):
            raise EdgeCredentialError("Device credential format invalid")
        self._device_token = device_token
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:
        """Never include credentials, injected client details or request URLs."""
        return "EdgeClient(credentials=<redacted>)"

    def _headers(self) -> Dict[str, str]:
        """Build per-request authentication separately from business payloads."""
        return {"Authorization": f"Bearer {self._device_token}"}

    @staticmethod
    def _log_status(status) -> None:
        """Only log a numeric status, never server response bodies."""
        if type(status) is int:
            logger.warning("edge request rejected: status=%d", status)
        else:
            logger.warning("edge response failed: invalid_status")

    def _http(self):
        """懒加载默认 httpx 客户端"""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def aclose(self) -> None:
        """Close only a lazily created HTTP client; injected clients are caller-owned."""
        if self._owns_client and self._client is not None:
            client = self._client
            self._client = None
            await client.aclose()

    async def report_alert(
        self,
        elder_id: int,
        alert_type: str,
        priority: str = "P1",
        content: Optional[str] = None,
        *,
        event_id: str,
        with_outcome: bool = False,
    ):
        """上报告警到云端，成功返回 data 字典，失败（网络/非 2xx）返回 None。"""
        payload = {
            "event_id": _require_event_id(event_id),
            "elder_id": elder_id,
            "alert_type": alert_type,
            "priority": priority,
            "content": content,
        }
        try:
            r = await self._http().post(f"{self.base_url}/api/v1/edge/alerts",
                                        json=payload, headers=self._headers())
            if with_outcome:
                status = r.status_code
                if type(status) is not int:
                    return AlertOutcome(error="invalid_response")
                if status in (200, 201):
                    try:
                        body = r.json()
                    except (ValueError, TypeError):
                        return AlertOutcome(status=status, error="invalid_response")
                    if (type(body) is not dict or type(body.get("code")) is not int
                            or body["code"] != 200 or type(body.get("message")) is not str
                            or type(body.get("data")) is not dict):
                        return AlertOutcome(status=status, error="invalid_response")
                    return AlertOutcome(status=status, data=body["data"])
                error = ("authentication" if status in (401, 403) else "conflict" if status == 409 else
                         "invalid_payload" if status == 422 else "request_rejected" if 400 <= status < 500 else
                         "server_error" if 500 <= status < 600 else "invalid_response")
                self._log_status(status)
                return AlertOutcome(status=status if 100 <= status <= 599 else None, error=error)
            if r.status_code in (200, 201):
                return r.json().get("data")
            self._log_status(r.status_code)
        except Exception:  # noqa: BLE001 - 不记录可能含凭证的异常详情
            logger.warning("edge request failed: transport_or_response")
            if with_outcome:
                return AlertOutcome(error="transport")
        return None

    async def fetch_tasks(self, elder_id: int) -> List[dict]:
        """拉取该老人的激活提醒任务（FR-FL-06）。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/tasks", params={"elder_id": elder_id},
                headers=self._headers(),
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                return data if isinstance(data, list) else []
            self._log_status(r.status_code)
        except Exception:  # noqa: BLE001
            logger.warning("edge request failed: transport_or_response")
        return []

    async def fetch_receipt(self, elder_id: int, event_id: str, *, device_id: str) -> dict:
        """Authenticated GET only; a failure never authorizes a retry or a POST."""
        _require_event_id(event_id)
        try:
            response = await self._http().get(f"{self.base_url}/api/v1/edge/alerts/receipts/{event_id}",
                params={"elder_id": elder_id}, headers=self._headers())
            if response.status_code in (401, 403):
                raise ReceiptQueryError("query_authentication")
            if type(response.status_code) is not int or response.status_code != 200:
                raise ReceiptQueryError("query_unavailable")
            body = response.json()
            if (type(body) is not dict or type(body.get("code")) is not int or body["code"] != 200
                    or type(body.get("message")) is not str or type(body.get("data")) is not dict):
                raise ReceiptQueryError("query_invalid")
            data = body["data"]
            absent = {"event_id": event_id, "device_id": device_id, "receipt_status": "not_found"}
            if data == absent:
                return absent
            AlertOutbox.validate_receipt(data, event_id=event_id, device_id=device_id)
            if not isinstance(data.get("payload_hash"), str) or re.fullmatch(r"[0-9a-f]{64}", data["payload_hash"]) is None:
                raise ReceiptQueryError("query_invalid")
            return {key: data[key] for key in ("event_id", "device_id", "receipt_status", "alert_id", "payload_hash", "status",
                    "notification", "notification_attempts", "notification_send_count", "notification_last_result", "confirmation")}
        except ReceiptQueryError:
            raise
        except Exception:
            raise ReceiptQueryError("query_unavailable_or_invalid") from None

    async def fetch_settings(self, elder_id: int) -> Optional[dict]:
        """拉取老人设置（FR-FL-08）。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/settings", params={"elder_id": elder_id},
                headers=self._headers(),
            )
            if r.status_code == 200:
                data = r.json().get("data")
                return data if isinstance(data, dict) else None
            self._log_status(r.status_code)
        except Exception:  # noqa: BLE001
            logger.warning("edge request failed: transport_or_response")
        return None

    async def fetch_privacy(self, elder_id: int) -> Optional[bool]:
        """拉取隐私授权状态（FR-CM-05 授权联动），失败返回 None。"""
        try:
            r = await self._http().get(
                f"{self.base_url}/api/v1/edge/privacy", params={"elder_id": elder_id},
                headers=self._headers(),
            )
            if r.status_code == 200:
                body = r.json()
                if (type(body) is dict and type(body.get("code")) is int
                        and body["code"] == 200 and type(body.get("message")) is str):
                    data = body.get("data")
                    if (type(data) is dict and type(data.get("elder_id")) is int
                            and data["elder_id"] == elder_id
                            and type(data.get("enabled")) is bool):
                        return data["enabled"]
                logger.warning("edge privacy response invalid")
                return None
            self._log_status(r.status_code)
        except Exception:  # noqa: BLE001
            logger.warning("edge request failed: transport_or_response")
        return None


class EdgeReporter:
    """One durable production sender; no-outbox direct calls are legacy only."""

    MAX_RETRIES = 3

    def __init__(self, client: EdgeClient, *, outbox=None, poll_interval=0.25, shutdown_timeout=1):
        self._client = client
        self._outbox = outbox
        self.task = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.poll_interval, self.shutdown_timeout = poll_interval, shutdown_timeout
        if outbox is None:
            # Compatibility for old direct callers only; production always
            # supplies Outbox and never allocates either in-memory queue.
            self._pending: List[dict] = []
            self._failures: List[dict] = []
        else:
            self._validate_binding()

    def _validate_binding(self) -> None:
        """Fail before HTTP if the client endpoint differs from the owned queue."""
        try:
            if self._outbox is None or _cloud_origin(self._client.base_url) != self._outbox.binding["cloud_origin"]:
                raise OutboxError("binding_mismatch")
        except OutboxError:
            raise
        except Exception:
            raise OutboxError("binding_mismatch") from None
        self._outbox.records()  # Recheck directory, lock, snapshot identity.

    @property
    def outbox(self):
        """Expose the sole queue identity to the lifecycle owner, not a copy."""
        return self._outbox

    def wake(self) -> None:
        """No per-event task or payload retained here."""
        self._wake.set()

    async def start(self) -> None:
        """One retained production sender; no module-import side effects."""
        if self.task is not None:
            if self.task.done():
                self.task.result()
            return
        self._validate_binding()
        self._stop.clear()
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._observe)

    def _observe(self, task) -> None:
        """Retrieve failures without printing raw event, HTTP or exception details."""
        if not task.cancelled() and task.exception() is not None:
            logger.error("Alert reporter stopped: worker_failed")

    async def _run(self) -> None:
        """Startup recovery and periodic retry scan share one task."""
        while not self._stop.is_set():
            self._wake.clear()
            await self.run_once()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        """Claim from the sole durable queue, send, then atomically save evidence."""
        async with self._send_lock:
            self._validate_binding()
            records = sorted(self._outbox.records(), key=lambda r: ({"P0": 0, "P1": 1, "P2": 2}[r["priority"]], r["occurred_at"]))
            for candidate in records:
                record = self._outbox.claim(candidate["event_id"])
                if record is None:
                    continue
                try:
                    result = await self._client.report_alert(record["elder_id"], record["alert_type"],
                        record["priority"], record["content"], event_id=record["event_id"], with_outcome=True)
                except Exception:
                    result = AlertOutcome(error="transport")
                error = result.error if isinstance(result, AlertOutcome) else "invalid_response"
                if error is None:
                    try:
                        self._outbox.validate_receipt(result.data, event_id=record["event_id"], device_id=record["device_id"])
                    except (OutboxError, TypeError, ValueError):
                        error = "invalid_response"
                if error is not None:
                    self._outbox.record_failure(record["event_id"], expected_attempts=record["attempts"],
                        error=error, http_status=result.status if isinstance(result, AlertOutcome) else None)
                    return False
                self._outbox.mark_received(record["event_id"], result.data,
                    expected_attempts=record["attempts"], http_status=result.status)
                return True
            return False

    async def stop(self) -> None:
        """Bounded close; a non-cooperative sender keeps its real task handle."""
        self._stop.set()
        self._wake.set()
        task = self.task
        if task is None:
            return
        try:
            done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
            if not done:
                raise RuntimeError("reporter_shutdown_timeout")
            if not task.cancelled():
                task.result()
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=self.shutdown_timeout)
            raise
        finally:
            if task.done():
                self.task = None

    async def report(
        self,
        elder_id: int,
        alert_type: str,
        priority: str = "P1",
        content: Optional[str] = None,
        *,
        event_id: str,
    ) -> bool:
        """上报告警；失败入重试队列，返回是否成功。"""
        _require_event_id(event_id)
        if self._outbox is not None:
            record = self._outbox.get(event_id)
            if record is None or (record["elder_id"], record["alert_type"], record["priority"], record["content"]) != (elder_id, alert_type, priority, content):
                raise OutboxError("event_conflict")
            await self.run_once()
            return self._outbox.get(event_id)["state"] == "received"
        result = await self._client.report_alert(elder_id, alert_type, priority, content, event_id=event_id)
        if result is not None:
            return True
        await self._enqueue(elder_id, alert_type, priority, content, retries_left=self.MAX_RETRIES, event_id=event_id)
        return False

    async def _enqueue(
        self,
        elder_id: int,
        alert_type: str,
        priority: str,
        content: Optional[str],
        retries_left: int,
        event_id: str,
    ):
        """入重试队列；重试次数耗尽则记为最终失败。"""
        if retries_left > 0:
            self._pending.append(
                {
                    "elder_id": elder_id,
                    "event_id": event_id,
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
                    "event_id": event_id,
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
        if self._outbox is not None:
            await self.run_once()
            return len(self.failures)
        remaining: List[dict] = []
        for item in self._pending:
            result = await self._client.report_alert(
                item["elder_id"], item["alert_type"], item["priority"], item["content"],
                event_id=item["event_id"],
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
                        "event_id": item["event_id"],
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
        if self._outbox is not None:
            return [r for r in self._outbox.records() if r["state"] in ("pending", "sending")]
        return list(self._pending)

    @property
    def failures(self) -> List[dict]:
        """最终失败记录（重试耗尽）"""
        if self._outbox is not None:
            return [r for r in self._outbox.records() if r["state"] in ("blocked", "failed", "exhausted")]
        return list(self._failures)
