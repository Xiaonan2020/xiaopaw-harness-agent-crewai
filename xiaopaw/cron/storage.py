"""Cron 跨进程存储封装。

`tasks.json` 会被 CronService（主进程）和 scheduler_mgr Skill（沙箱子进程）同时读写，
因此使用 `filelock.FileLock` 做跨进程互斥；所有加锁操作都通过 `asyncio.to_thread`
跑在线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from xiaopaw.observability.metrics import record_error

logger = logging.getLogger(__name__)


class CronStorage:
    """跨进程安全的 `tasks.json` 读写封装。"""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._timeout = timeout
        self._lock = FileLock(str(self._lock_path), timeout=timeout)

    # ── 同步底层：真正持有 filelock 的读写 ─────────────────────────

    def read(self) -> tuple[dict[str, Any], float, int]:
        """在文件锁保护下读取 tasks.json。

        返回 (data, mtime, size)。mtime/size 用于 `_check_mtime()` 热重载检测。
        """
        with self._lock:
            if not self._path.exists():
                return {"version": 1, "jobs": []}, 0.0, -1
            data = json.loads(self._path.read_text(encoding="utf-8"))
            st = self._path.stat()
            return data, st.st_mtime, st.st_size

    def write(self, data: dict[str, Any]) -> tuple[float, int]:
        """在文件锁保护下原子写入 tasks.json。

        使用 write-then-replace 保证目标文件始终完整。
        返回写入后的 (mtime, size)。
        """
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
            st = self._path.stat()
            return st.st_mtime, st.st_size

    # ── async 包装：不阻塞事件循环，并做超时重试 ───────────────────

    async def aload(self) -> tuple[dict[str, Any], float, int]:
        return await self._with_timeout_retry(self.read)

    async def awrite(self, data: dict[str, Any]) -> tuple[float, int]:
        return await self._with_timeout_retry(self.write, data)

    async def _with_timeout_retry(self, fn, *args):
        """先尝试一次，若获取文件锁超时则重试一次并记录 metric。"""
        try:
            return await asyncio.to_thread(fn, *args)
        except Timeout:
            logger.warning(
                "CronStorage lock timeout on %s, retry once",
                self._path,
            )
            record_error("cron", "storage_lock_timeout")
            return await asyncio.to_thread(fn, *args)
