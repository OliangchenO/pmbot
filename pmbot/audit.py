"""Append-only JSONL audit stream for live trading evidence."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path


log = logging.getLogger("pmbot.audit")


class AuditLogger:
    """Durably append structured audit events without stopping the bot on I/O errors."""

    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: dict) -> None:
        if self._path is None:
            return
        row = {"ts": time.time(), **event}
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
        except OSError as e:
            log.warning("无法追加审计事件：%s", e)

    def recovery_bases(self) -> dict[str, tuple[float, float]]:
        """Load the latest verified recovery basis per market for restart safety."""
        if self._path is None or not self._path.exists():
            return {}
        bases: dict[str, tuple[float, float]] = {}
        try:
            with self._lock, self._path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if (row.get("event") != "order_placed"
                                or row.get("path") != "inventory_recovery"):
                            continue
                        cid = str(row.get("cid") or "")
                        basis = float(row.get("unpaired_cost"))
                        shares = float(row.get("size"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if cid and 0.0 < basis < 1.0 and shares > 0.0:
                        bases[cid] = (basis, shares)
        except OSError as e:
            log.warning("无法加载补仓成本基准审计缓存：%s", e)
        return bases
