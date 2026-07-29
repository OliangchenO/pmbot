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


def test_audit_logger_loads_latest_inventory_recovery_basis(tmp_path):
    """Restart recovery may reuse only the latest bot-recorded matching basis."""
    from pmbot.audit import AuditLogger

    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))
    logger.record({"event": "order_placed", "cid": "cid1", "path": "inventory_recovery",
                   "unpaired_cost": 0.61, "size": 10})
    logger.record({"event": "order_placed", "cid": "cid1", "path": "inventory_recovery",
                   "unpaired_cost": 0.65, "size": 15})
    logger.record({"event": "order_placed", "cid": "other", "path": "normal",
                   "unpaired_cost": 0.30, "size": 20})

    assert logger.recovery_bases() == {"cid1": (0.65, 15.0)}
