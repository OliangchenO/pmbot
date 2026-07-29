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
            log.warning("could not append audit event: %s", e)
