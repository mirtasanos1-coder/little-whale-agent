"""极简事件总线：给 SSE 推送 / 调试日志用（不引第三方库）。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, maxlen: int = 400) -> None:
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = asyncio.Lock()

    def publish(self, kind: str, **data: Any) -> dict:
        self._seq += 1
        item = {"seq": self._seq, "ts": round(time.time(), 3), "kind": kind, **data}
        self._items.append(item)
        return item

    def recent(self, limit: int = 50, since: int = 0) -> list[dict]:
        if since:
            return [i for i in self._items if i["seq"] > since][-limit:]
        return list(self._items)[-limit:]

    @property
    def last_seq(self) -> int:
        return self._seq

    async def wait_after(self, seq: int, timeout: float = 15.0) -> list[dict]:
        """长轮询：等到有新事件或超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = [i for i in self._items if i["seq"] > seq]
            if items:
                return items
            await asyncio.sleep(0.15)
        return []


BUS = EventBus()
