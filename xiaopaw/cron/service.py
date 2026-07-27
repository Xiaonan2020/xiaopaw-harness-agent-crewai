"""CronService — asyncio timer-based scheduler

读取 cron/tasks.json，精确调度定时任务，触发时构造 InboundMessage 进入 Runner 管道。
支持三种调度模式：at（一次性）、every（固定间隔）、cron（cron 表达式）。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
import uuid
import zoneinfo
from pathlib import Path
from typing import Any, Awaitable, Callable

from croniter import croniter

from xiaopaw.cron.models import CronJob, CronPayload, CronSchedule, CronState
from xiaopaw.cron.storage import CronStorage
from xiaopaw.models import InboundMessage
from xiaopaw.observability.metrics import record_cron_dlq, record_error

logger = logging.getLogger(__name__)

DispatchFn = Callable[[InboundMessage], Awaitable[None]]


def _now_ms() -> int:
    return int(time.time() * 1000)


class CronService:
    """读取 cron/tasks.json，精确调度定时任务。"""

    def __init__(
        self,
        data_dir: Path,
        dispatch_fn: DispatchFn,
        tick_interval: float = 0.05,
    ) -> None:
        self._data_dir = data_dir
        self._dispatch_fn = dispatch_fn
        self._tick_interval = tick_interval
        self._tasks_path = data_dir / "cron" / "tasks.json"
        self._dlq_path = data_dir / "cron" / "tasks.dlq.jsonl"
        self._storage = CronStorage(self._tasks_path)
        self._store_lock = asyncio.Lock()
        self._last_mtime: float = 0.0
        self._last_size: int = -1
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.jobs: list[CronJob] = []
        self._disabled_jobs_raw: list[dict] = []

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """加载 tasks.json 并启动 tick 循环。"""
        await self._load_store_async()
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止 tick 循环。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ── Main Loop ────────────────────────────────────────────

    async def _loop(self) -> None:
        """主 tick 循环：检测热重载 → 检查到期 job → 触发 → 持久化。"""
        while self._running:
            try:
                if self._check_mtime():
                    logger.info("tasks.json changed, reloading")
                    await self._load_store_async()

                now = _now_ms()
                fired_ids: list[str] = []

                for job in self.jobs:
                    if (
                        job.state.next_run_at_ms is not None
                        and job.state.next_run_at_ms <= now
                    ):
                        await self._fire(job)
                        fired_ids.append(job.id)

                if fired_ids:
                    self._post_fire(fired_ids, now)
                    await self._save_store_async()

                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("CronService tick error")
                record_error("cron", type(exc).__name__)
                await asyncio.sleep(self._tick_interval)

    # ── Fire ─────────────────────────────────────────────────

    async def _fire(self, job: CronJob) -> None:
        """触发一个 job：构造 InboundMessage 并 dispatch。"""
        msg_id = f"cron_{uuid.uuid4().hex[:12]}"
        ts_ms = _now_ms()
        logger.info(
            "Firing cron job %s (%s) -> %s",
            job.id,
            job.name,
            job.payload.routing_key,
        )

        inbound = InboundMessage(
            routing_key=job.payload.routing_key,
            content=job.payload.message + "\n[备注：回答时添加这是定时任务]",
            msg_id=msg_id,
            root_id=msg_id,
            sender_id="cron",
            ts=ts_ms,
            is_cron=True,
        )

        try:
            await self._dispatch_fn(inbound)
            job.state.last_status = "ok"
            job.state.last_error = None
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.exception("CronService fire error for job %s", job.id)
            record_error("cron", type(e).__name__)
            record_cron_dlq()
            await self.append_dlq(self._job_to_dict(job), str(e))

        job.state.last_run_at_ms = ts_ms

    def _post_fire(self, fired_ids: list[str], fired_at_ms: int) -> None:
        """触发后更新 job 状态：at 删除/禁用，every 重新计算 next_run。"""
        to_remove: list[str] = []

        for job in self.jobs:
            if job.id not in fired_ids:
                continue

            if job.schedule.kind == "at":
                if job.delete_after_run:
                    to_remove.append(job.id)
                else:
                    job.enabled = False
                    job.state.next_run_at_ms = None
            elif job.schedule.kind == "every" and job.schedule.every_ms:
                job.state.next_run_at_ms = fired_at_ms + job.schedule.every_ms
            elif job.schedule.kind == "cron" and job.schedule.expr:
                job.state.next_run_at_ms = self._next_cron_ms(job)

        self.jobs = [j for j in self.jobs if j.id not in to_remove]
        logger.info(
            "Post-fire update: removed=%s, remaining=%d",
            to_remove,
            len(self.jobs),
        )

    @staticmethod
    def _next_cron_ms(job: CronJob) -> int:
        """使用 croniter 计算 cron 表达式的下次触发时间。"""
        tz = zoneinfo.ZoneInfo(job.schedule.tz or "UTC")
        now_dt = datetime.datetime.now(tz)
        cron = croniter(job.schedule.expr, now_dt)
        next_dt = cron.get_next(datetime.datetime)
        return int(next_dt.timestamp() * 1000)

    # ── Persistence ──────────────────────────────────────────

    async def _load_store_async(self) -> None:
        """在文件锁保护下从 tasks.json 加载 enabled 的 jobs。"""
        if not self._tasks_path.exists():
            logger.warning("Cron tasks file not found: %s", self._tasks_path)

        async with self._store_lock:
            try:
                data, mtime, size = await self._storage.aload()
                self._apply_loaded(data)
                self._last_mtime = mtime
                self._last_size = size
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.error("Failed to parse %s: %s", self._tasks_path, e)
                self.jobs = []

    def _apply_loaded(self, data: dict[str, Any]) -> None:
        """把从文件读出的原始数据转换成内存中的 CronJob 列表。"""
        # 💡 兼容旧格式（直接是 list）和新格式（{"jobs": [...]}）
        if isinstance(data, list):
            jobs_raw = data
        else:
            jobs_raw = data.get("jobs", [])
        self.jobs = []
        self._disabled_jobs_raw = []
        for raw in jobs_raw:
            if not raw.get("enabled", True):
                self._disabled_jobs_raw.append(raw)
                continue
            self.jobs.append(
                CronJob(
                    id=raw["id"],
                    name=raw["name"],
                    enabled=raw["enabled"],
                    schedule=CronSchedule(**raw["schedule"]),
                    payload=CronPayload(**raw["payload"]),
                    state=CronState(**raw["state"]),
                    created_at_ms=raw.get("created_at_ms", 0),
                    updated_at_ms=raw.get("updated_at_ms", 0),
                    delete_after_run=raw.get("delete_after_run", False),
                )
            )
        logger.info(
            "Loaded cron tasks from %s: enabled=%d, disabled=%d",
            self._tasks_path,
            len(self.jobs),
            len(self._disabled_jobs_raw),
        )

        # 为首次加载、且尚未计算 next_run_at_ms 的 enabled 任务补全下一次触发时间。
        # 这样由外部（如 scheduler_mgr Skill）写入 tasks.json 且 state.next_run_at_ms 为空的任务
        # 也能被 CronService 正常调度。
        #
        # 同时，为了让“修改 cron 表达式”一类操作更加健壮，我们在每次加载时
        # 都会根据当前 cron 表达式重新计算 cron 任务的 next_run_at_ms，
        # 而不是盲信文件中持久化的值。这样即使外部工具只是更新了 schedule.expr
        # 却忘记清空 state.next_run_at_ms，新的 cron 规则也能立即生效。
        now_ms = _now_ms()
        for job in self.jobs:
            if not job.enabled:
                continue
            if job.schedule.kind == "cron" and job.schedule.expr:
                job.state.next_run_at_ms = self._next_cron_ms(job)
                continue
            if job.state.next_run_at_ms is not None:
                continue
            if job.schedule.kind == "at" and job.schedule.at_ms is not None:
                job.state.next_run_at_ms = job.schedule.at_ms
            elif job.schedule.kind == "every" and job.schedule.every_ms is not None:
                job.state.next_run_at_ms = now_ms + job.schedule.every_ms

    def _check_mtime(self) -> bool:
        """检测 tasks.json 是否被外部修改（包括被删除）。

        同时检查 mtime 和文件大小，避免在高精度不足的文件系统（如 tmpfs）上
        两次写入落在同一 mtime tick 内导致漏检。
        """
        if not self._tasks_path.exists():
            return self._last_mtime != 0.0
        try:
            st = self._tasks_path.stat()
            return st.st_mtime != self._last_mtime or st.st_size != self._last_size
        except OSError:
            return False

    async def _save_store_async(self) -> None:
        """write-then-replace 原子写入 tasks.json（保留 disabled jobs）。"""
        async with self._store_lock:
            enabled_raw = [self._job_to_dict(j) for j in self.jobs]
            output = {"version": 1, "jobs": enabled_raw + self._disabled_jobs_raw}

            mtime, size = await self._storage.awrite(output)
            self._last_mtime = mtime
            self._last_size = size
            logger.info(
                "Saved cron tasks to %s: enabled=%d, disabled=%d",
                self._tasks_path,
                len(self.jobs),
                len(self._disabled_jobs_raw),
            )

    @staticmethod
    def _job_to_dict(job: CronJob) -> dict:
        """CronJob → dict（用于 JSON 序列化）。"""
        return {
            "id": job.id,
            "name": job.name,
            "enabled": job.enabled,
            "schedule": {
                "kind": job.schedule.kind,
                "at_ms": job.schedule.at_ms,
                "every_ms": job.schedule.every_ms,
                "expr": job.schedule.expr,
                "tz": job.schedule.tz,
            },
            "payload": {
                "routing_key": job.payload.routing_key,
                "message": job.payload.message,
            },
            "state": {
                "next_run_at_ms": job.state.next_run_at_ms,
                "last_run_at_ms": job.state.last_run_at_ms,
                "last_status": job.state.last_status,
                "last_error": job.state.last_error,
            },
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
            "delete_after_run": job.delete_after_run,
        }

    async def append_dlq(self, job_dict: dict[str, Any], error: str) -> None:
        """把失败的 cron job 写入死信队列（append-only）。"""
        entry = {"job": job_dict, "error": error}
        await asyncio.to_thread(self._append_dlq_sync, entry)

    def _append_dlq_sync(self, entry: dict[str, Any]) -> None:
        self._dlq_path.parent.mkdir(parents=True, exist_ok=True)
        with self._dlq_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")