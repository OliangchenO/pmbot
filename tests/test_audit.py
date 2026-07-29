"""Tests for the durable live-trading audit stream."""

import json


def test_audit_logger_appends_complete_json_event(tmp_path):
    from pmbot.audit import AuditLogger

    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))

    logger.record({"event": "order_placed", "order_id": "order-1"})

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "order_placed"
    assert rows[0]["order_id"] == "order-1"
    assert isinstance(rows[0]["ts"], float)
