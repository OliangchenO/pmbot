"""Structured metrics: SQLite logging, uptime tracking, PnL decomposition."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("pmbot.metrics")


class MetricsStore:
    def __init__(self, db_path: str = "data/metrics.db",
                 trades_log: str | None = None,
                 inception_date: str | None = None,
                 read_only: bool = False):
        self.path = Path(db_path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(exist_ok=True)
        self._trades_log = Path(trades_log) if trades_log else None
        if self._trades_log:
            self._trades_log.parent.mkdir(exist_ok=True)
        # Reports reflect bot activity only: drop/refuse anything before this
        # UTC date (earlier rows were manual testing).
        self.inception_date = inception_date or None
        if read_only:
            self._conn = sqlite3.connect(
                f"file:{self.path.resolve().as_posix()}?mode=ro",
                uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Tolerate brief contention from a concurrent reader/backfill instead of
        # raising "database is locked" immediately.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Order ops run concurrently in worker threads and all record metrics
        # through this single connection — serialize writes.
        self._lock = threading.Lock()
        if not read_only:
            self._init_schema()
            self._prune_before_inception()
        self._uptime_samples: dict[str, list[bool]] = {}
        self._last_uptime_minute: int = 0
        self._session_start = time.time()

    def _inception_ts(self) -> float | None:
        if not self.inception_date:
            return None
        try:
            return datetime.strptime(self.inception_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            log.warning("metrics.inception_date 无效：%r，已忽略",
                        self.inception_date)
            return None

    def _prune_before_inception(self) -> None:
        """Delete rows that predate the inception date (manual-testing data)."""
        ts = self._inception_ts()
        if ts is None:
            return
        with self._lock:
            for tbl in ("fills", "hedges", "merges", "equity", "markouts",
                        "quotes", "inventory_snapshots", "inventory_events",
                        "market_rewards", "guard_events", "pause_day_events",
                        "quote_risk_decisions"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE ts < ?", (ts,))
            self._conn.execute("DELETE FROM uptime WHERE minute_ts < ?",
                               (int(ts) // 60,))
            self._conn.execute("DELETE FROM reward_samples WHERE minute_ts < ?",
                               (int(ts) // 60,))
            self._conn.execute("DELETE FROM rewards WHERE date < ?",
                               (self.inception_date,))
            self._conn.commit()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, market TEXT, side TEXT,
                token TEXT, price REAL, size REAL,
                taker INTEGER DEFAULT 0, exit INTEGER DEFAULT 0, merged REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, quotes_json TEXT
            );
            CREATE TABLE IF NOT EXISTS hedges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, price REAL, size REAL
            );
            CREATE TABLE IF NOT EXISTS merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, pairs REAL
            );
            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, equity REAL, inventory_usd REAL
            );
            CREATE TABLE IF NOT EXISTS uptime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                minute_ts INTEGER, cid TEXT, in_band INTEGER
            );
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, date TEXT, estimated REAL DEFAULT 0,
                realized REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS market_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, date TEXT, cid TEXT, realized REAL, source TEXT,
                UNIQUE(date, cid)
            );
            CREATE INDEX IF NOT EXISTS idx_market_rewards_date_cid
                ON market_rewards (date, cid);
            CREATE TABLE IF NOT EXISTS reward_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                minute_ts INTEGER, cid TEXT, est_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_reward_samples_minute
                ON reward_samples (minute_ts);
            CREATE TABLE IF NOT EXISTS markouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, fill_ts REAL, cid TEXT, market TEXT,
                horizon REAL, markout REAL
            );
            CREATE TABLE IF NOT EXISTS recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, event TEXT, reason TEXT,
                unpaired REAL, recovery_path TEXT,
                quote_price REAL, pair_cap REAL, proposed_price REAL,
                cost_basis REAL, fee_per_share REAL,
                expected_pair_pnl REAL, hard_cap REAL,
                complement_ask REAL, original_bid REAL,
                mkt_fee_bps INTEGER, mkt_fee_exponent REAL
            );
            CREATE TABLE IF NOT EXISTS inventory_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, market TEXT,
                unpaired_shares REAL, cost_basis REAL,
                exposure_usd REAL, status TEXT,
                yes_mid REAL, yes_shares REAL, no_shares REAL,
                book_updated_ts REAL
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_cid_ts
                ON inventory_snapshots (cid, ts);
            CREATE TABLE IF NOT EXISTS inventory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, event TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_events_cid_ts
                ON inventory_events (cid, ts);
            CREATE TABLE IF NOT EXISTS guard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, market TEXT, scope TEXT, reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_guard_events_cid_ts
                ON guard_events (cid, ts);
            CREATE TABLE IF NOT EXISTS recovery_episodes (
                cid TEXT NOT NULL,
                started_ts REAL NOT NULL,
                initial_unpaired REAL NOT NULL,
                peak_abs_exposure_usd REAL NOT NULL DEFAULT 0.0,
                stage TEXT NOT NULL DEFAULT 'passive',
                chosen_path TEXT,
                expected_loss_usd REAL,
                actual_loss_usd REAL,
                sell_reserved_loss_usd REAL DEFAULT 0.0,
                is_closed INTEGER NOT NULL DEFAULT 0,
                closed_ts REAL,
                closed_reason TEXT,
                PRIMARY KEY (cid, started_ts)
            );
            CREATE TABLE IF NOT EXISTS pause_day_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, event TEXT, reason TEXT, equity REAL,
                smoothed_equity REAL, day_loss REAL, inventory_usd REAL
            );
            CREATE TABLE IF NOT EXISTS net_shadow_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, top_n INTEGER NOT NULL, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quote_risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, mode TEXT,
                market TEXT,
                yes_action TEXT, no_action TEXT,
                yes_widen REAL, no_widen REAL,
                score REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS net_shadow_candidates (
                scan_id INTEGER NOT NULL, cid TEXT NOT NULL, market TEXT,
                legacy_score REAL NOT NULL, shadow_score REAL NOT NULL,
                legacy_rank INTEGER NOT NULL, shadow_rank INTEGER NOT NULL,
                inputs_json TEXT NOT NULL,
                PRIMARY KEY (scan_id, cid)
            );
            CREATE INDEX IF NOT EXISTS idx_net_shadow_scans_ts ON net_shadow_scans (ts);
            CREATE INDEX IF NOT EXISTS idx_net_shadow_candidates_scan_rank
                ON net_shadow_candidates (scan_id, shadow_rank);
        """)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(fills)")}
        if "fee" not in cols:
            self._conn.execute("ALTER TABLE fills ADD COLUMN fee REAL DEFAULT 0")
        qrd_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(quote_risk_decisions)")}
        if "market" not in qrd_cols:
            self._conn.execute("ALTER TABLE quote_risk_decisions ADD COLUMN market TEXT DEFAULT ''")
        rec_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(recovery_events)")}
        if "quote_price" not in rec_cols:
            self._conn.execute("ALTER TABLE recovery_events ADD COLUMN quote_price REAL")
        if "pair_cap" not in rec_cols:
            self._conn.execute("ALTER TABLE recovery_events ADD COLUMN pair_cap REAL")
        for column in ("proposed_price", "cost_basis", "fee_per_share",
                       "expected_pair_pnl", "hard_cap",
                       "complement_ask", "original_bid",
                       "mkt_fee_bps", "mkt_fee_exponent"):
            if column not in rec_cols:
                self._conn.execute(
                    f"ALTER TABLE recovery_events ADD COLUMN {column} REAL")
        ep_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(recovery_episodes)")}
        if "sell_reserved_loss_usd" not in ep_cols:
            self._conn.execute(
                "ALTER TABLE recovery_episodes ADD COLUMN sell_reserved_loss_usd REAL DEFAULT 0.0")
        self._conn.commit()
        inv_cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(inventory_snapshots)")}
        if "yes_mid" not in inv_cols:
            self._conn.execute(
                "ALTER TABLE inventory_snapshots ADD COLUMN yes_mid REAL")
        for column in ("yes_shares", "no_shares", "book_updated_ts"):
            if column not in inv_cols:
                self._conn.execute(
                    f"ALTER TABLE inventory_snapshots ADD COLUMN {column} REAL")
        self._conn.commit()
        # Migration: guard_events gained a market column
        ge_cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(guard_events)")}
        if "market" not in ge_cols:
            self._conn.execute(
                "ALTER TABLE guard_events ADD COLUMN market TEXT DEFAULT ''")
        self._conn.commit()

    def net_shadow_inputs(self, lookback_hours: float,
                          now: float | None = None) -> dict[str, dict[str, float | int]]:
        """Aggregate bounded, market-specific observations for P2.1 scoring."""
        now = time.time() if now is None else now
        hours = max(float(lookback_hours), 1e-6)
        cutoff = now - hours * 3600.0
        cids: set[str] = set()
        for table, column, predicate in (
            ("fills", "ts", ""), ("markouts", "ts", ""),
            ("recovery_events", "ts", ""),
        ):
            cids.update(r[0] for r in self._conn.execute(
                f"SELECT DISTINCT cid FROM {table} WHERE {column}>=? AND cid IS NOT NULL{predicate}",
                (cutoff,)))
        cids.update(r[0] for r in self._conn.execute(
            "SELECT DISTINCT cid FROM uptime WHERE minute_ts>=? AND cid IS NOT NULL",
            (int(cutoff) // 60,)))
        cids.update(r[0] for r in self._conn.execute(
            "SELECT DISTINCT cid FROM reward_samples WHERE minute_ts>=? AND cid IS NOT NULL",
            (int(cutoff) // 60,)))
        reward_dates = sorted({
            datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            for ts in (cutoff, now)
        })
        out: dict[str, dict[str, float | int]] = {}
        for cid in cids:
            maker_count = self._conn.execute(
                "SELECT COUNT(*) FROM fills WHERE cid=? AND ts>=? AND taker=0 AND exit=0",
                (cid, cutoff)).fetchone()[0]
            fee_total, fee_samples = self._conn.execute(
                "SELECT COALESCE(SUM(fee),0), COUNT(*) FROM fills "
                "WHERE cid=? AND ts>=? AND taker=1", (cid, cutoff)).fetchone()
            uptime_sum, uptime_samples = self._conn.execute(
                "SELECT COALESCE(SUM(in_band),0), COUNT(*) FROM uptime "
                "WHERE cid=? AND minute_ts>=?", (cid, int(cutoff) // 60)).fetchone()
            rows = self._conn.execute(
                "SELECT markout, horizon FROM markouts WHERE cid=? AND ts>=?",
                (cid, cutoff)).fetchall()
            highest_horizon = max((r[1] for r in rows), default=None)
            marks = [float(r[0]) for r in rows if r[1] == highest_horizon]
            avg_markout = sum(marks) / len(marks) if marks else 0.0
            recovery_total, recovery_samples = self._conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN expected_pair_pnl < 0 "
                "THEN -expected_pair_pnl ELSE 0 END),0), "
                "COUNT(expected_pair_pnl) FROM recovery_events WHERE cid=? AND ts>=?",
                (cid, cutoff)).fetchone()
            estimate, reward_samples = self._conn.execute(
                "SELECT COALESCE(SUM(est_usd),0), COUNT(*) FROM reward_samples "
                "WHERE cid=? AND minute_ts>=?", (cid, int(cutoff) // 60)).fetchone()
            date_placeholders = ",".join("?" for _ in reward_dates)
            realized, realized_samples = self._conn.execute(
                f"SELECT COALESCE(SUM(realized),0), COUNT(*) FROM market_rewards "
                f"WHERE cid=? AND date IN ({date_placeholders})",
                (cid, *reward_dates)).fetchone()
            out[cid] = {
                "maker_fill_count": int(maker_count),
                "uptime_ratio": float(uptime_sum) / uptime_samples if uptime_samples else 0.0,
                "uptime_samples": int(uptime_samples),
                "markout_cost_per_hour": max(0.0, -avg_markout) * maker_count / hours,
                "markout_samples": len(marks),
                "recovery_cost_per_hour": float(recovery_total) / hours,
                "recovery_samples": int(recovery_samples),
                "taker_fee_per_hour": float(fee_total) / hours,
                "taker_fee_samples": int(fee_samples),
                "reward_realization": (float(realized) / float(estimate)
                                       if estimate and realized_samples else 0.0),
                "reward_samples": int(reward_samples) if realized_samples else 0,
            }
        # ── P2.2 closed-cycle data ──
        # A "closed cycle" = one UTC day where a market had fills AND ended
        # with zero unpaired inventory (fully closed).  Each such day is one
        # observation of the per-hour net result.  We count distinct closed
        # UTC days per cid and compute the average net/hour across them.
        #
        # CRITICAL: closed-cycle scoring needs enough samples to be meaningful.
        # The gate uses a 14-day window; we match that here with a dedicated
        # lookback that is the larger of lookback_hours and the gate's effective
        # observation window (14 days). Component inputs (uptime, markout, etc.)
        # still use the shorter lookback — closed cycles are the slow signal.
        closed_by_cid: dict[str, dict[str, float | int]] = {}
        try:
            from .outcomes import build_market_outcomes as _build
            # Use at least 14 calendar days for cycle counting so min_closed_samples
            # (default 3) can actually be reached. Component lookback (24h default)
            # is too short — a market rarely closes a full cycle in one day.
            closed_lookback_hours = max(hours, 14.0 * 24.0)
            closed_cutoff = now - closed_lookback_hours * 3600.0
            # Process each UTC calendar day separately to count cycles.
            # Walk backwards from today's midnight UTC to the cutoff date,
            # building per-day outcomes. Each "realized" outcome for a market
            # is one closed cycle.
            per_cid_nets: dict[str, list[float]] = {}
            now_dt = datetime.fromtimestamp(now, timezone.utc)
            cutoff_dt = datetime.fromtimestamp(closed_cutoff, timezone.utc)
            day_end = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            while day_end > cutoff_dt:
                day_start = max(day_end - timedelta(days=1), cutoff_dt)
                d_start_ts = day_start.timestamp()
                d_end_ts = day_end.timestamp()
                outcomes = _build(self._conn, d_start_ts, d_end_ts,
                                  snapshot_max_age_seconds=43200.0)  # 12h freshness
                day_hours = max((d_end_ts - d_start_ts) / 3600.0, 0.01)
                for o in outcomes:
                    if o.state == "realized" and o.net_outcome_usd is not None:
                        per_cid_nets.setdefault(o.cid, []).append(
                            o.net_outcome_usd / day_hours)
                day_end = day_start
            for cid, nets in per_cid_nets.items():
                if nets:
                    closed_by_cid[cid] = {
                        "closed_cycle_net_per_hour": sum(nets) / len(nets),
                        "closed_cycle_samples": len(nets),
                    }
        except Exception:
            pass
        for cid in closed_by_cid:
            if cid in out:
                out[cid] = {**out[cid], **closed_by_cid[cid]}
        return out

    def record_net_shadow_snapshot(self, markets, scanned_at: float,
                                   config: dict) -> None:
        """Persist one passive scan with both rankings for later comparison."""
        rows = list(markets)
        legacy = sorted(rows, key=lambda m: (-m.score, m.condition_id))
        shadow = sorted(rows, key=lambda m: (-m.net_shadow_score, m.condition_id))
        legacy_rank = {m.condition_id: i + 1 for i, m in enumerate(legacy)}
        shadow_rank = {m.condition_id: i + 1 for i, m in enumerate(shadow)}
        top_n = int(config.get("top_n", len(rows)))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO net_shadow_scans (ts,top_n,config_json) VALUES (?,?,?)",
                (scanned_at, top_n, json.dumps(config, sort_keys=True)),
            )
            scan_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO net_shadow_candidates "
                "(scan_id,cid,market,legacy_score,shadow_score,legacy_rank,shadow_rank,inputs_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(scan_id, m.condition_id, m.question, m.score, m.net_shadow_score,
                  legacy_rank[m.condition_id], shadow_rank[m.condition_id],
                  json.dumps(m.net_shadow_inputs, sort_keys=True)) for m in rows],
            )
            self._conn.commit()

    def net_shadow_report(self, date: str) -> dict:
        """Return the latest passive shadow scan for one UTC date."""
        start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        scan = self._conn.execute(
            "SELECT id,ts,top_n FROM net_shadow_scans WHERE ts>=? AND ts<? "
            "ORDER BY ts DESC,id DESC LIMIT 1", (start, start + 86400)).fetchone()
        if scan is None:
            return {"status": "no_shadow_scan_data", "legacy_top": [], "shadow_top": []}
        scan_id, ts, top_n = scan
        rows = self._conn.execute(
            "SELECT cid,market,legacy_score,shadow_score,legacy_rank,shadow_rank,inputs_json "
            "FROM net_shadow_candidates WHERE scan_id=?", (scan_id,)).fetchall()
        data = [{"cid": r[0], "market": r[1], "legacy_score": r[2],
                 "net_shadow_score": r[3], "legacy_rank": r[4],
                 "shadow_rank": r[5], "inputs": json.loads(r[6])} for r in rows]
        return {"status": "ok", "ts": ts, "legacy_top": sorted(
            data, key=lambda r: r["legacy_rank"])[:top_n], "shadow_top": sorted(
            data, key=lambda r: r["shadow_rank"])[:top_n]}

    def _append_trades_log(self, entry: dict) -> None:
        if self._trades_log is None:
            return
        try:
            with self._trades_log.open("a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as e:
            log.warning("无法追加成交日志：%s", e)

    def record_fill(self, entry: dict) -> None:
        ts = entry.get("ts", time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO fills (ts,cid,market,side,token,price,size,taker,exit,merged,fee) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, entry.get("cid"), entry.get("market"),
                 entry.get("side"), entry.get("token"), entry.get("price", 0),
                 entry.get("size", 0), int(bool(entry.get("taker"))),
                 int(bool(entry.get("exit"))), entry.get("merged", 0),
                 entry.get("fee", 0)),
            )
            self._conn.execute(
                "INSERT INTO inventory_events (ts,cid,event) VALUES (?,?,?)",
                (ts, entry.get("cid"), "exit" if entry.get("exit") else "fill"),
            )
            self._conn.commit()
        self._append_trades_log(entry)

    def record_markout(self, entry: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO markouts (ts,fill_ts,cid,market,horizon,markout) "
                "VALUES (?,?,?,?,?,?)",
                (entry.get("ts", time.time()), entry.get("fill_ts"),
                 entry.get("cid"), entry.get("market"),
                 entry.get("horizon"), entry.get("markout")),
            )
            self._conn.commit()

    def record_quotes(self, cid: str, quotes: list) -> None:
        data = [{"token": q.token_id, "price": q.price, "size": q.size} for q in quotes]
        with self._lock:
            self._conn.execute(
                "INSERT INTO quotes (ts, cid, quotes_json) VALUES (?,?,?)",
                (time.time(), cid, json.dumps(data)),
            )
            self._conn.commit()

    def record_hedge(self, cid: str, price: float, size: float,
                     ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO hedges (ts, cid, price, size) VALUES (?,?,?,?)",
                (ts, cid, price, size),
            )
            self._conn.execute(
                "INSERT INTO inventory_events (ts,cid,event) VALUES (?,?,?)",
                (ts, cid, "hedge"),
            )
            self._conn.commit()

    def record_merge(self, cid: str, pairs: float,
                     ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO merges (ts, cid, pairs) VALUES (?,?,?)",
                (ts, cid, pairs),
            )
            self._conn.execute(
                "INSERT INTO inventory_events (ts,cid,event) VALUES (?,?,?)",
                (ts, cid, "merge"),
            )
            self._conn.commit()

    def record_recovery_event(self, cid: str, event: str, unpaired: float,
                              reason: str | None = None,
                              recovery_path: str | None = None,
                              quote_price: float | None = None,
                              pair_cap: float | None = None,
                              proposed_price: float | None = None,
                              cost_basis: float | None = None,
                              fee_per_share: float | None = None,
                              expected_pair_pnl: float | None = None,
                              hard_cap: float | None = None,
                              complement_ask: float | None = None,
                              original_bid: float | None = None,
                              mkt_fee_bps: int | None = None,
                              mkt_fee_exponent: float | None = None,
                              ts: float | None = None) -> None:
        """Log one inventory-recovery lifecycle event for monitoring.

        ``event`` is one of ``'skip'``, ``'quote_placed'``, or
        ``'forced_hedge'``.  ``reason`` is the skip reason (only used when
        ``event='skip'``).  ``recovery_path`` records the current recovery
        stage so the stats query can group by phase.  ``quote_price`` and
            ``pair_cap`` records the effective cap and ``hard_cap`` the strict
            fee-inclusive break-even cap. The remaining economic inputs retain
            the decision facts needed to audit a future fill or refusal.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO recovery_events (ts,cid,event,reason,unpaired,recovery_path,"
                "quote_price,pair_cap,proposed_price,cost_basis,fee_per_share,"
                "expected_pair_pnl,hard_cap,complement_ask,original_bid,"
                "mkt_fee_bps,mkt_fee_exponent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time() if ts is None else ts, cid, event, reason, unpaired,
                 recovery_path, quote_price, pair_cap, proposed_price, cost_basis,
                 fee_per_share, expected_pair_pnl, hard_cap,
                 complement_ask, original_bid,
                 mkt_fee_bps, mkt_fee_exponent),
            )
            self._conn.commit()

    # ── P1 recovery episode lifecycle ──

    def open_recovery_episode(
            self, *, cid: str, started_ts: float, initial_unpaired: float,
            peak_abs_exposure_usd: float, stage: str,
    ) -> None:
        """Create or refresh the single open episode for *cid*.

        If an open episode already exists for this CID, update its
        peak_exposure and stage without creating a second row.
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT started_ts FROM recovery_episodes "
                "WHERE cid=? AND is_closed=0", (cid,),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE recovery_episodes SET "
                    "peak_abs_exposure_usd=MAX(peak_abs_exposure_usd,?), "
                    "stage=? WHERE cid=? AND is_closed=0",
                    (peak_abs_exposure_usd, stage, cid),
                )
            else:
                # INSERT … ON CONFLICT handles the restart case where a
                # previously-closed episode for the same (cid, started_ts)
                # already exists.  Re-open it with fresh values instead of
                # crashing on the UNIQUE constraint.
                self._conn.execute(
                    "INSERT INTO recovery_episodes "
                    "(cid,started_ts,initial_unpaired,peak_abs_exposure_usd,stage) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(cid, started_ts) DO UPDATE SET "
                    "initial_unpaired=excluded.initial_unpaired, "
                    "peak_abs_exposure_usd=MAX(peak_abs_exposure_usd, excluded.peak_abs_exposure_usd), "
                    "stage=excluded.stage, "
                    "is_closed=0, "
                    "closed_ts=NULL, "
                    "closed_reason=NULL, "
                    "chosen_path=NULL, "
                    "expected_loss_usd=NULL, "
                    "actual_loss_usd=NULL, "
                    "sell_reserved_loss_usd=0.0",
                    (cid, started_ts, initial_unpaired, peak_abs_exposure_usd, stage),
                )
            self._conn.commit()

    def update_recovery_episode(
            self, *, cid: str, peak_abs_exposure_usd: float | None = None,
            stage: str | None = None,
            chosen_path: str | None = None,
            expected_loss_usd: float | None = None,
            sell_reserved_loss_usd: float | None = None,
    ) -> None:
        """Update fields on the open episode for *cid*.

        peak_abs_exposure_usd is ratcheted via MAX in SQL.  Other fields
        are overwritten with the latest value — the caller is expected to
        pass the current decision on every tick.
        """
        with self._lock:
            sets: list[str] = []
            params: list = []
            if peak_abs_exposure_usd is not None:
                sets.append(
                    "peak_abs_exposure_usd=MAX(peak_abs_exposure_usd,?)")
                params.append(peak_abs_exposure_usd)
            if stage is not None:
                sets.append("stage=?")
                params.append(stage)
            if chosen_path is not None:
                sets.append("chosen_path=?")
                params.append(chosen_path)
            if expected_loss_usd is not None:
                sets.append("expected_loss_usd=?")
                params.append(expected_loss_usd)
            if sell_reserved_loss_usd is not None:
                sets.append("sell_reserved_loss_usd=?")
                params.append(sell_reserved_loss_usd)
            if not sets:
                return
            params.append(cid)
            self._conn.execute(
                f"UPDATE recovery_episodes SET {', '.join(sets)} "
                "WHERE cid=? AND is_closed=0", params,
            )
            self._conn.commit()

    def close_recovery_episode(
            self, *, cid: str, closed_ts: float, chosen_path: str | None = None,
            expected_loss_usd: float | None = None,
            actual_loss_usd: float | None = None,
            reason: str | None = None,
    ) -> None:
        """Mark the open episode for *cid* as closed with outcome data."""
        with self._lock:
            self._conn.execute(
                "UPDATE recovery_episodes SET is_closed=1, closed_ts=?, "
                "chosen_path=?, expected_loss_usd=?, actual_loss_usd=?, "
                "closed_reason=? "
                "WHERE cid=? AND is_closed=0",
                (closed_ts, chosen_path, expected_loss_usd, actual_loss_usd,
                 reason, cid),
            )
            self._conn.commit()

    def get_open_episode(self, cid: str) -> dict | None:
        """Return the open episode for *cid*, or None."""
        row = self._conn.execute(
            "SELECT cid, started_ts, initial_unpaired, peak_abs_exposure_usd, "
            "stage, chosen_path, expected_loss_usd, actual_loss_usd, "
            "sell_reserved_loss_usd, "
            "is_closed, closed_ts, closed_reason "
            "FROM recovery_episodes WHERE cid=? AND is_closed=0",
            (cid,),
        ).fetchone()
        if row is None:
            return None
        return {
            "cid": row[0], "started_ts": row[1], "initial_unpaired": row[2],
            "peak_abs_exposure_usd": row[3], "stage": row[4],
            "chosen_path": row[5], "expected_loss_usd": row[6],
            "actual_loss_usd": row[7], "sell_reserved_loss_usd": row[8],
            "is_closed": row[9], "closed_ts": row[10], "closed_reason": row[11],
        }

    def recovery_episode_summary(self) -> dict:
        """Read-only aggregate of all episodes."""
        total = self._conn.execute(
            "SELECT COUNT(*) FROM recovery_episodes").fetchone()[0]
        open_count = self._conn.execute(
            "SELECT COUNT(*) FROM recovery_episodes WHERE is_closed=0"
        ).fetchone()[0]
        closed_count = self._conn.execute(
            "SELECT COUNT(*) FROM recovery_episodes WHERE is_closed=1"
        ).fetchone()[0]
        return {
            "total_episodes": total,
            "open_episodes": open_count,
            "closed_episodes": closed_count,
        }

    def recovery_cumulative_loss(self, cid: str) -> float:
        """Total actual_loss_usd across all episodes (open + closed) for *cid*.

        This is the realised cost of all recovery actions for this market,
        irrespective of episode boundaries.  When the cumulative loss exceeds
        ``recovery_loss_ban_threshold_usd`` the market should be banned so it
        cannot re-enter the quote set — even after a restart the persistent
        ban stays until the operator manually clears it.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(actual_loss_usd), 0) FROM recovery_episodes "
            "WHERE cid=?",
            (cid,),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def add_actual_loss(self, cid: str, delta: float) -> None:
        """Atomically add *delta* to actual_loss_usd for the open episode."""
        with self._lock:
            self._conn.execute(
                "UPDATE recovery_episodes SET actual_loss_usd="
                "COALESCE(actual_loss_usd,0)+? "
                "WHERE cid=? AND is_closed=0",
                (delta, cid),
            )
            self._conn.commit()

    def set_sell_reserved_loss(self, cid: str, value: float) -> None:
        """Atomically set sell_reserved_loss_usd for the open episode.

        Updates the reservation when *value* exceeds the current stored value
        (e.g. the ask has widened and expected_loss_usd has grown).  Never
        decreases — a shrinking expected loss is fine because the actual loss
        can only be less than the reservation, not more.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT sell_reserved_loss_usd FROM recovery_episodes "
                "WHERE cid=? AND is_closed=0",
                (cid,),
            ).fetchone()
            prev = row[0] if row and row[0] is not None else 0.0
            if value > prev:
                self._conn.execute(
                    "UPDATE recovery_episodes SET sell_reserved_loss_usd=? "
                    "WHERE cid=? AND is_closed=0",
                    (value, cid),
                )
                self._conn.commit()

    def recovery_event_cost_basis(self, cid: str,
                                   since_ts: float) -> float | None:
        """Return the last known cost_basis for *cid* from events >= *since_ts*.

        Used when the position has gone flat and the broker can no longer
        supply an unpaired_cost_basis — the last recorded event still has
        the basis that was used for the episode's decisions.
        """
        row = self._conn.execute(
            "SELECT cost_basis FROM recovery_events "
            "WHERE cid=? AND ts>=? AND cost_basis IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1",
            (cid, since_ts),
        ).fetchone()
        return row[0] if row else None

    def list_recovery_episodes(
            self, *, limit: int = 100, offset: int = 0,
            open_only: bool = False, closed_only: bool = False,
    ) -> list[dict]:
        """Read-only listing of all episodes sorted by started_ts DESC."""
        where: list[str] = []
        params: list = []
        if open_only:
            where.append("is_closed=0")
        elif closed_only:
            where.append("is_closed=1")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"SELECT cid, started_ts, initial_unpaired, peak_abs_exposure_usd, "
            f"stage, chosen_path, expected_loss_usd, actual_loss_usd, "
            f"is_closed, closed_ts, closed_reason "
            f"FROM recovery_episodes {clause} "
            f"ORDER BY started_ts DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [
            {
                "cid": r[0], "started_ts": r[1], "initial_unpaired": r[2],
                "peak_abs_exposure_usd": r[3], "stage": r[4],
                "chosen_path": r[5], "expected_loss_usd": r[6],
                "actual_loss_usd": r[7], "is_closed": r[8],
                "closed_ts": r[9], "closed_reason": r[10],
                "duration_secs": (r[9] - r[1]) if r[9] is not None else None,
            }
            for r in rows
        ]

    def replay_old_recovery_events(
            self, gap_secs: float = 1800.0,
            market_hints: dict[str, object] | None = None,
    ) -> list[dict]:
        """Fold old recovery_events into per-episode summaries.

        Events are split into episodes by a *gap_secs* quiet period
        (default 30 min).  Within each episode the earliest/latest timestamps,
        peak exposure, and fill outcome are tracked independently.

        When complement_ask + original_bid + cost_basis are present on at
        least one event, the replay re-runs choose_recovery_action() to
        compare the old-strategy result against the new decision engine.
        *market_hints* is an optional dict mapping cid→Market-like object
        (needed for fee calculation); without it the comparison column
        shows "no market hints".
        """
        rows = self._conn.execute(
            "SELECT cid, ts, event, reason, unpaired, recovery_path, "
            "quote_price, proposed_price, cost_basis, expected_pair_pnl, "
            "complement_ask, original_bid, mkt_fee_bps, mkt_fee_exponent "
            "FROM recovery_events ORDER BY cid, ts, id"
        ).fetchall()

        episodes: list[dict] = []
        current: dict | None = None

        for r in rows:
            cid = r[0]
            ts = r[1]
            unpaired = r[4]
            event = r[2]
            proposed_price = r[7]
            cost_basis = r[8]
            complement_ask = r[10] if len(r) > 10 else None
            original_bid = r[11] if len(r) > 11 else None
            mkt_fee_bps = r[12] if len(r) > 12 else None
            mkt_fee_exponent = r[13] if len(r) > 13 else None

            # Split into a new episode when CID changes or a long gap
            # appears between consecutive events for the same market.
            start_new = False
            if current is None:
                start_new = True
            elif current["cid"] != cid:
                start_new = True
            elif ts - current["latest_ts"] > gap_secs:
                start_new = True

            if start_new:
                if current is not None:
                    episodes.append(self._finalize_replay_episode(
                        current, market_hints))
                current = {
                    "cid": cid,
                    "earliest_ts": ts,
                    "latest_ts": ts,
                    "event_count": 0,
                    "quote_placed_count": 0,
                    "max_abs_unpaired": 0.0,
                    "has_proposed_price": False,
                    "has_dual_prices": False,
                    "filled": False,
                    "fill_event": None,
                    "insufficient_evidence": True,
                    "sample_quotes": [],
                    "mkt_fee_bps": mkt_fee_bps,
                    "mkt_fee_exponent": mkt_fee_exponent,
                    # Best dual-path snapshot for comparison
                    "dual_path_snapshot": None,
                }

            assert current is not None
            current["earliest_ts"] = min(current["earliest_ts"], ts)
            current["latest_ts"] = max(current["latest_ts"], ts)
            current["event_count"] += 1
            current["max_abs_unpaired"] = max(
                current["max_abs_unpaired"], abs(unpaired))
            if proposed_price is not None:
                current["has_proposed_price"] = True
                if len(current["sample_quotes"]) < 5:
                    current["sample_quotes"].append({
                        "ts": ts, "proposed_price": proposed_price,
                        "cost_basis": cost_basis, "unpaired": unpaired,
                    })
            # Only clear insufficient_evidence when *both* sides of the
            # order book are present — a single proposed_price without the
            # complementary ask/original-bid pair cannot produce a
            # meaningful buy-vs-sell comparison.
            if complement_ask is not None and original_bid is not None:
                current["has_dual_prices"] = True
                current["insufficient_evidence"] = False
                current["dual_path_snapshot"] = {
                    "ts": ts,
                    "unpaired": unpaired,
                    "cost_basis": cost_basis,
                    "complement_ask": complement_ask,
                    "original_bid": original_bid,
                    "mkt_fee_bps": mkt_fee_bps,
                    "mkt_fee_exponent": mkt_fee_exponent,
                }
            if event == "quote_placed":
                current["quote_placed_count"] += 1
            if event in ("forced_hedge_filled",):
                current["filled"] = True
                current["fill_event"] = {
                    "ts": ts, "event": event, "reason": r[3],
                    "quote_price": r[6], "expected_pair_pnl": r[9],
                }

        if current is not None:
            episodes.append(self._finalize_replay_episode(current, market_hints))

        return episodes

    @staticmethod
    def _finalize_replay_episode(ep: dict,
                                 market_hints: dict | None = None) -> dict:
        """Convert internal dict to the public per-episode summary shape.

        When dual-path prices (complement_ask + original_bid) and
        market_hints are available, re-run choose_recovery_action() to
        compare the old-strategy result against the new decision engine.
        """
        result: dict = {
            "cid": ep["cid"],
            "duration_secs": ep["latest_ts"] - ep["earliest_ts"],
            "event_count": ep["event_count"],
            "quote_placed_count": ep["quote_placed_count"],
            "max_abs_unpaired": ep["max_abs_unpaired"],
            "filled": ep["filled"],
            "insufficient_evidence": ep["insufficient_evidence"],
            "sample_quotes": ep["sample_quotes"][:5],
            "fill_event": ep["fill_event"],
        }

        # Replay comparison: re-run the new decision engine on the old
        # event data to surface whether it would have chosen differently.
        snap = ep.get("dual_path_snapshot")
        result["comparison"] = None
        if snap is not None:
            from pmbot.recovery import choose_recovery_action  # noqa: F811

            # Build a minimal Market-like object from persisted metadata
            # so historical CID comparison works even when gamma.scan()
            # no longer returns the market.
            mkt_fee_bps_val = snap.get("mkt_fee_bps")
            mkt_fee_exp_val = snap.get("mkt_fee_exponent")
            if mkt_fee_bps_val is not None and mkt_fee_exp_val is not None:
                class _ReplayMarket:
                    fee_bps: int = int(mkt_fee_bps_val)
                    fee_exponent: float = float(mkt_fee_exp_val)

                replay_market = _ReplayMarket()
            elif market_hints is not None:
                replay_market = market_hints.get(ep["cid"])
            else:
                replay_market = None

            if replay_market is not None:
                max_loss_usd = float(
                    (market_hints or {}).get("_max_loss_usd", 3.0))
                old_quote = choose_recovery_action(
                    market=replay_market,
                    unpaired=snap["unpaired"],
                    basis=snap["cost_basis"],
                    complement_ask=snap["complement_ask"],
                    original_bid=snap["original_bid"],
                    elapsed_secs=0.0,  # no elapsed info in old events
                    max_loss_usd=max_loss_usd,
                )
                result["comparison"] = {
                    "path": old_quote.path,
                    "expected_loss_usd": old_quote.expected_loss_usd,
                    "token_id": old_quote.token_id,
                    "price": old_quote.price,
                    "reason": old_quote.reason,
                }
            else:
                result["comparison"] = {
                    "status": "no_market_hints",
                    "detail": "dual prices available but no fee metadata "
                              "persisted and no market hints provided",
                }
        return result

    def record_equity(self, equity: float, inventory_usd: float) -> None:
        if equity != equity:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity (ts, equity, inventory_usd) VALUES (?,?,?)",
                (time.time(), equity, inventory_usd),
            )
            self._conn.commit()

    def record_inventory_snapshot(
            self, cid: str, market: str, *, unpaired_shares: float,
            cost_basis: float | None, exposure_usd: float, status: str,
            ts: float | None = None, yes_mid: float | None = None,
            position_shares: tuple[float, float] | None = None,
            book_updated_ts: float | None = None) -> None:
        """Persist a read-only per-market inventory observation.

        ``status`` describes only the observed balance: ``unpaired`` or
        ``flat``. It never asserts whether a flat balance was merged, sold,
        redeemed, or reconciled by the exchange.

        ``yes_mid`` is the independently observed order-book YES mid at
        snapshot time.  When None the mid was unavailable (no live book).

        ``position_shares`` is (yes_shares, no_shares) — the total position
        including paired shares.  Defaults to (0, 0) when None.

        ``book_updated_ts`` is the Book.updated_ts of the YES order book
        at snapshot time.  Used for mid freshness validation — a mid value
        from a stale book (last updated long ago) is not a valid MTM anchor.
        """
        yes_s, no_s = position_shares or (0.0, 0.0)
        with self._lock:
            self._conn.execute(
                "INSERT INTO inventory_snapshots "
                "(ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,yes_shares,no_shares,book_updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time() if ts is None else ts, cid, market,
                 unpaired_shares, cost_basis, exposure_usd, status, yes_mid,
                 yes_s, no_s, book_updated_ts),
            )
            self._conn.commit()

    def record_guard_event(self, cid: str, scope: str, reason: str,
                           ts: float | None = None, market: str = "") -> None:
        """Persist a quote interruption caused by a risk guard.

        ``reason`` names the observable action (for example
        ``market_guard_pull``); it does not invent the risk rule that tripped.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO guard_events (ts,cid,market,scope,reason) VALUES (?,?,?,?,?)",
                (time.time() if ts is None else ts, cid, market, scope, reason),
            )
            self._conn.commit()

    def record_quote_risk_decision(
            self, cid: str, mode: str, decision,
            ts: float | None = None, market: str = "") -> None:
        """Persist one per-loop adverse-selection decision for audit.

        ``decision`` is a ``QuoteRiskDecision``; SQLite write failures
        must NEVER abort the quote loop — catch and warn only.
        """
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO quote_risk_decisions "
                    "(ts, cid, mode, market, yes_action, no_action, yes_widen, "
                    "no_widen, score, reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (time.time() if ts is None else ts,
                     cid, mode, market,
                     decision.yes_action, decision.no_action,
                     decision.yes_widen, decision.no_widen,
                     decision.score, decision.reason),
                )
                self._conn.commit()
                # Probabilistic cleanup: ~5% chance each write, delete rows
                # older than 10 hours to keep the table lean.
                self._prune_old_decisions(self._conn)
        except Exception:
            log.warning("quote_risk_decision 持久化失败（cid=%s）", cid, exc_info=True)

    @staticmethod
    def _prune_old_decisions(conn: sqlite3.Connection, keep_hours: int = 10) -> None:
        """Delete quote_risk_decisions rows older than *keep_hours*, ~5% of calls."""
        import random
        if random.random() > 0.05:
            return
        cutoff = time.time() - keep_hours * 3600
        try:
            cur = conn.execute(
                "DELETE FROM quote_risk_decisions WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted > 0:
                conn.commit()
                log.info("P0 guard 历史清理: 删除 %d 条 (%d 小时前的决策)",
                         deleted, keep_hours)
        except Exception:
            # Best-effort; never let cleanup break the write path.
            pass

    def quote_risk_report(self, cid: str | None = None,
                          since_ts: float | None = None,
                          until_ts: float | None = None,
                          mode: str | None = None) -> dict:
        """Read-only summary for shadow/active guard effect measurement.

        The minimum viable audit surface per the design spec:

        * intercepted vs allow-group markout at 30s and 300s horizons
          (paired via the nearest decision → markout sample within 120s).
        * negative-markout hit rate (what fraction of intercepted decisions
          had a subsequent negative markout).
        * false-positive rate (allow-group decisions that coincidentally
          had a negative markout).

        ``since_ts`` and ``until_ts`` define a UTC timestamp window; without
        them, the full dataset is used (up to 10 000 decisions).
        """
        # Build two parallel clause lists:
        #  - unqualified (no table alias) for single-table queries
        #  - qualified with "qrd." for the markout JOIN subqueries.
        clauses_u, params_u = [], []
        clauses_q, params_q = [], []
        if cid:
            clauses_u.append("cid = ?")
            clauses_q.append("qrd.cid = ?")
            params_u.append(cid)
            params_q.append(cid)
        if since_ts is not None:
            clauses_u.append("ts >= ?")
            clauses_q.append("qrd.ts >= ?")
            params_u.append(since_ts)
            params_q.append(since_ts)
        if until_ts is not None:
            clauses_u.append("ts < ?")
            clauses_q.append("qrd.ts < ?")
            params_u.append(until_ts)
            params_q.append(until_ts)
        if mode:
            clauses_u.append("mode = ?")
            clauses_q.append("qrd.mode = ?")
            params_u.append(mode)
            params_q.append(mode)
        uwhere = f"WHERE {' AND '.join(clauses_u)}" if clauses_u else "WHERE 1=1"
        qwhere = f"WHERE {' AND '.join(clauses_q)}" if clauses_q else "WHERE 1=1"

        # ── Action distribution ──
        dist_rows = self._conn.execute(
            f"SELECT yes_action, no_action, COUNT(*) FROM quote_risk_decisions "
            f"{uwhere} GROUP BY yes_action, no_action",
            params_u,
        ).fetchall()
        action_dist: dict[str, int] = {}
        for ya, na, cnt in dist_rows:
            action_dist[ya] = action_dist.get(ya, 0) + cnt
            action_dist[na] = action_dist.get(na, 0) + cnt

        shadow_intercepted = self._conn.execute(
            f"SELECT COUNT(*) FROM quote_risk_decisions {uwhere} "
            f"AND mode='shadow' AND (yes_action!='allow' OR no_action!='allow')",
            params_u,
        ).fetchone()[0]

        active_intercepted = self._conn.execute(
            f"SELECT COUNT(*) FROM quote_risk_decisions {uwhere} "
            f"AND mode='active' AND (yes_action!='allow' OR no_action!='allow')",
            params_u,
        ).fetchone()[0]

        avg_score = self._conn.execute(
            f"SELECT AVG(score) FROM quote_risk_decisions {uwhere}",
            params_u,
        ).fetchone()[0]

        avg_score_intercepted = self._conn.execute(
            f"SELECT AVG(score) FROM quote_risk_decisions {uwhere} "
            f"AND (yes_action!='allow' OR no_action!='allow')",
            params_u,
        ).fetchone()[0]

        # ── Markout-matched validation ──
        # Correlation is markout → decision (one-to-one).  Each markout row
        # belongs to the single most recent quote_risk_decision on the same
        # CID whose ts ≤ m.fill_ts.  This prevents one markout from being
        # counted by two overlapping decisions.
        #
        # Window: the decision must have been issued within
        # ``horizon + 120 s slack`` before fill_ts.
        # Build separate WHERE clauses for the markout table itself
        # (m.cid / m.ts) so they don't collide with qrd columns in the JOIN.
        m_clauses, m_params = [], []
        if cid:
            m_clauses.append("m.cid = ?")
            m_params.append(cid)
        if since_ts is not None:
            m_clauses.append("m.ts >= ?")
            m_params.append(since_ts)
        if until_ts is not None:
            m_clauses.append("m.ts < ?")
            m_params.append(until_ts)
        mwhere = f"WHERE {' AND '.join(m_clauses)}" if m_clauses else "WHERE 1=1"
        mode_2 = " AND qrd2.mode = ?" if mode else ""
        mode_3 = " AND qrd3.mode = ?" if mode else ""
        mode_qrd = " AND qrd.mode = ?" if mode else ""
        paired_params = ([mode] + m_params + [mode, mode]) if mode else m_params

        paired_30 = self._conn.execute(f"""
            SELECT
              (qrd.yes_action != 'allow' OR qrd.no_action != 'allow') AS intercepted,
              m.markout
            FROM markouts m
            INNER JOIN quote_risk_decisions qrd ON qrd.cid = m.cid
             AND qrd.ts = (
               SELECT MAX(qrd2.ts) FROM quote_risk_decisions qrd2
               WHERE qrd2.cid = m.cid
                 AND qrd2.ts <= m.fill_ts
                 AND qrd2.ts >= m.fill_ts - 150.0
                 {mode_2}
             )
            {mwhere}
              AND m.horizon = 30.0
              {mode_qrd}
              AND EXISTS (
                SELECT 1 FROM quote_risk_decisions qrd3
                WHERE qrd3.cid = m.cid
                  AND qrd3.ts <= m.fill_ts
                  AND qrd3.ts >= m.fill_ts - 150.0
                  {mode_3}
              )
            ORDER BY qrd.ts
        """, paired_params).fetchall()

        paired_300 = self._conn.execute(f"""
            SELECT
              (qrd.yes_action != 'allow' OR qrd.no_action != 'allow') AS intercepted,
              m.markout
            FROM markouts m
            INNER JOIN quote_risk_decisions qrd ON qrd.cid = m.cid
             AND qrd.ts = (
               SELECT MAX(qrd2.ts) FROM quote_risk_decisions qrd2
               WHERE qrd2.cid = m.cid
                 AND qrd2.ts <= m.fill_ts
                 AND qrd2.ts >= m.fill_ts - 420.0
                 {mode_2}
             )
            {mwhere}
              AND m.horizon >= 300.0
              {mode_qrd}
              AND EXISTS (
                SELECT 1 FROM quote_risk_decisions qrd3
                WHERE qrd3.cid = m.cid
                  AND qrd3.ts <= m.fill_ts
                  AND qrd3.ts >= m.fill_ts - 420.0
                  {mode_3}
              )
            ORDER BY qrd.ts
        """, paired_params).fetchall()

        def _markout_stats(rows: list) -> dict:
            intercepted_marks = [r[1] for r in rows if r[0]]
            allow_marks = [r[1] for r in rows if not r[0]]
            intercepted_neg = [m for m in intercepted_marks if m < 0]
            allow_neg = [m for m in allow_marks if m < 0]
            return {
                "intercepted_avg_cents": round(
                    sum(intercepted_marks) / len(intercepted_marks) * 100, 2
                ) if intercepted_marks else None,
                "allow_avg_cents": round(
                    sum(allow_marks) / len(allow_marks) * 100, 2
                ) if allow_marks else None,
                "intercepted_neg_hit_rate": round(
                    len(intercepted_neg) / len(intercepted_marks), 4
                ) if intercepted_marks else None,
                "allow_neg_rate": round(
                    len(allow_neg) / len(allow_marks), 4
                ) if allow_marks else None,
                "intercepted_paired_samples": len(intercepted_marks),
                "allow_paired_samples": len(allow_marks),
            }

        decisions = self._conn.execute(
            f"SELECT qrd.ts, qrd.cid, qrd.mode, qrd.yes_action, qrd.no_action, "
            f"qrd.yes_widen, qrd.no_widen, qrd.score, qrd.reason, "
            f"(SELECT f.market FROM fills f WHERE f.cid=qrd.cid "
            f"AND f.market IS NOT NULL AND f.market!='' "
            f"ORDER BY f.ts DESC LIMIT 1) "
            f"FROM quote_risk_decisions qrd {qwhere} "
            f"ORDER BY qrd.ts ASC LIMIT 200",
            params_q,
        ).fetchall()

        return {
            "shadow_intercepted": shadow_intercepted,
            "active_intercepted": active_intercepted,
            "action_distribution": action_dist,
            "avg_score": round(avg_score, 4) if avg_score is not None else None,
            "avg_score_intercepted": (round(avg_score_intercepted, 4)
                                      if avg_score_intercepted is not None else None),
            "markout_30s": _markout_stats(paired_30),
            "markout_300s": _markout_stats(paired_300),
            "decisions": [
                {
                    "ts": r[0], "cid": r[1], "mode": r[2],
                    "yes_action": r[3], "no_action": r[4],
                    "yes_widen": r[5], "no_widen": r[6],
                    "score": r[7], "reason": r[8], "market": r[9],
                }
                for r in decisions
            ],
        }

    def record_pause_day_event(
            self, event: str, *, reason: str, equity: float,
            smoothed_equity: float, day_loss: float, inventory_usd: float,
            ts: float | None = None) -> None:
        """Persist the facts that caused or ended a daily-loss pause."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO pause_day_events "
                "(ts,event,reason,equity,smoothed_equity,day_loss,inventory_usd) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time() if ts is None else ts, event, reason, equity,
                 smoothed_equity, day_loss, inventory_usd),
            )
            self._conn.commit()

    def record_est_reward(self, usd: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT id, estimated FROM rewards WHERE date=? ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE rewards SET estimated=estimated+? WHERE id=?",
                    (usd, row[0]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO rewards (ts, date, estimated) VALUES (?,?,?)",
                    (time.time(), today, usd),
                )
            self._conn.commit()

    def record_reward_sample(self, cid: str, usd: float) -> None:
        """Per-minute, per-market estimated reward accrual (USD for that minute).

        The daily ``rewards`` table only keeps a running daily total, which
        makes the reward *rate* invisible — diagnosing intra-session decay
        meant inferring it from uptime and churn. This table keeps the raw
        time series (one row per market per ~minute) so the rate is directly
        observable and changes can be proven rather than inferred.
        """
        if usd != usd:  # NaN guard
            return
        minute = int(time.time()) // 60
        with self._lock:
            self._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) "
                "VALUES (?,?,?)",
                (minute, cid, usd),
            )
            self._conn.commit()

    def reward_rate_recent(self, minutes: int = 60) -> dict:
        """Estimated reward accrual over the last ``minutes``, as a rate.

        Each sample is the USD accrued in its minute, so summing the window
        gives the dollars accrued in it. ``usd_per_hr`` divides that by the
        number of *distinct sampled minutes* (not wall-clock), so a bot that
        only ran part of the window isn't penalised for the idle stretch.
        """
        cutoff = int(time.time()) // 60 - int(minutes)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(est_usd),0), COUNT(DISTINCT minute_ts) "
            "FROM reward_samples WHERE minute_ts > ?",
            (cutoff,),
        ).fetchone()
        usd, n = (row[0] or 0.0), (row[1] or 0)
        usd_per_hr = (usd / (n / 60.0)) if n else 0.0
        return {"usd": usd, "minutes": n, "usd_per_hr": usd_per_hr}

    def reward_rate_by_market(self, since_minute: int) -> dict[str, dict]:
        """Per-market estimated accrual since ``since_minute`` (a minute_ts).

        Returns ``{cid: {"usd": total, "minutes": n, "usd_per_hr": rate}}`` so
        you can see which held markets are actually carrying the reward rate.
        """
        rows = self._conn.execute(
            "SELECT cid, COALESCE(SUM(est_usd),0), COUNT(DISTINCT minute_ts) "
            "FROM reward_samples WHERE minute_ts >= ? GROUP BY cid",
            (int(since_minute),),
        ).fetchall()
        out: dict[str, dict] = {}
        for cid, usd, n in rows:
            out[cid] = {
                "usd": usd or 0.0,
                "minutes": n or 0,
                "usd_per_hr": ((usd or 0.0) / (n / 60.0)) if n else 0.0,
            }
        return out

    def reward_calibration_report(self, days: int = 7,
                                  end_date: str | None = None) -> dict:
        """Read-only market/day reward calibration from explicitly attributed facts.

        ``market_rewards`` must contain a source-specific market amount before
        a ratio is emitted.  An account-level daily reward is intentionally not
        allocated to market rows, so missing attribution remains inconclusive.
        """
        if days < 1:
            raise ValueError("days must be positive")
        end = (datetime.strptime(end_date, "%Y-%m-%d").date()
               if end_date else datetime.now(timezone.utc).date())
        start = end - timedelta(days=days - 1)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")
        start_minute = int(datetime.combine(start, datetime.min.time(),
                                            tzinfo=timezone.utc).timestamp()) // 60
        end_minute = start_minute + days * 1440

        rows: dict[tuple[str, str], dict] = {}

        def _row(day: str, cid: str) -> dict:
            return rows.setdefault((day, cid), {
                "date": day, "cid": cid, "estimated_usd": 0.0,
                "realized_usd": None, "calibration_ratio": None,
                "status": "unattributed", "uptime_samples": 0,
                "uptime_pct": None, "recovery_skips": 0,
                "recovery_skip_reasons": {},
                "guard_interruptions_status": "not_recorded",
                "guard_interruptions": 0,
                "guard_interruption_reasons": {},
            })

        for day, cid, estimated in self._conn.execute(
            "SELECT strftime('%Y-%m-%d', minute_ts*60, 'unixepoch'), cid, "
            "COALESCE(SUM(est_usd),0) FROM reward_samples "
            "WHERE minute_ts>=? AND minute_ts<? GROUP BY 1, cid",
            (start_minute, end_minute),
        ):
            _row(day, cid)["estimated_usd"] = estimated or 0.0
        for day, cid, realized, source in self._conn.execute(
            "SELECT date,cid,realized,source FROM market_rewards "
            "WHERE date>=? AND date<=?",
            (start_date, end_date),
        ):
            row = _row(day, cid)
            if row["estimated_usd"] <= 0:
                row["status"] = "missing_estimate"
            row["realized_usd"] = realized
            row["source"] = source
            if row["estimated_usd"] > 0:
                row["calibration_ratio"] = realized / row["estimated_usd"]
                row["status"] = "calibrated"

        for day, cid, n, in_band in self._conn.execute(
            "SELECT strftime('%Y-%m-%d', minute_ts*60, 'unixepoch'), cid, "
            "COUNT(*), COALESCE(SUM(in_band),0) FROM uptime "
            "WHERE minute_ts>=? AND minute_ts<? GROUP BY 1, cid",
            (start_minute, end_minute),
        ):
            row = _row(day, cid)
            row["uptime_samples"] = n
            row["uptime_pct"] = (in_band / n * 100.0) if n else None

        for day, cid, reason, n in self._conn.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch'), cid, reason, COUNT(*) "
            "FROM recovery_events WHERE ts>=? AND ts<? AND event='skip' "
            "GROUP BY 1, cid, reason",
            (start_minute * 60, end_minute * 60),
        ):
            row = _row(day, cid)
            row["recovery_skips"] += n
            row["recovery_skip_reasons"][reason or "unknown"] = n

        for day, cid, reason, n in self._conn.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch'), cid, reason, COUNT(*) "
            "FROM guard_events WHERE ts>=? AND ts<? GROUP BY 1, cid, reason",
            (start_minute * 60, end_minute * 60),
        ):
            row = _row(day, cid)
            row["guard_interruptions_status"] = "recorded"
            row["guard_interruptions"] += n
            row["guard_interruption_reasons"][reason or "unknown"] = n

        market_days = sorted(rows.values(), key=lambda r: (r["date"], r["cid"]))
        calibrated = [r for r in market_days if r["status"] == "calibrated"]
        return {
            "start_date": start_date,
            "end_date": end_date,
            "market_days": market_days,
            "summary": {
                "market_days": len(market_days),
                "calibrated_market_days": len(calibrated),
                "unattributed_market_days": sum(
                    r["status"] == "unattributed" for r in market_days),
            },
        }

    def record_realized_reward(self, date: str, usd: float) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM rewards WHERE date=? ORDER BY id DESC LIMIT 1", (date,),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE rewards SET realized=? WHERE id=?", (usd, row[0]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO rewards (ts, date, realized) VALUES (?,?,?)",
                    (time.time(), date, usd),
                )
            self._conn.commit()

    def record_market_realized_reward(self, date: str, cid: str, usd: float,
                                      source: str) -> None:
        """Upsert one explicitly attributed realized reward amount.

        This is intentionally separate from the CLOB account-day total in
        ``rewards``.  A caller must supply a source that actually identifies
        the market; no estimated or prorated allocation is accepted here.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO market_rewards (ts,date,cid,realized,source) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(date,cid) DO UPDATE SET "
                "ts=excluded.ts, realized=excluded.realized, source=excluded.source",
                (time.time(), date, cid, usd, source),
            )
            self._conn.commit()

    def fetch_market_realized_rewards(self, client, date: str | None = None) -> int:
        """Import official, condition-id-attributed reward rows for one UTC day.

        The account-total endpoint deliberately remains the source of the daily
        total.  This companion import only records rows that the official CLOB
        response identifies with ``condition_id``; it never allocates a wallet
        total across markets.
        """
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.inception_date and date < self.inception_date:
            return 0
        fetch = getattr(client, "get_earnings_for_user_for_day", None)
        if fetch is None:
            log.debug("市场奖励接口不可用（%s）", date)
            return 0
        try:
            rows = fetch(date)
        except Exception as e:  # noqa: BLE001
            log.debug("获取市场奖励失败（%s）：%s", date, e)
            return 0
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("earnings") or []
        imported = 0
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            cid = item.get("condition_id") or item.get("conditionId")
            if not cid:
                continue
            try:
                earnings = float(item.get("earnings") or item.get("amount")
                                 or item.get("reward") or 0.0)
                rate = float(item.get("asset_rate") or 1.0)
            except (TypeError, ValueError):
                continue
            self.record_market_realized_reward(
                date, str(cid), earnings * rate, "clob_rewards_user")
            imported += 1
        return imported

    def sample_uptime(self, cid: str, in_band: bool) -> None:
        self._uptime_samples.setdefault(cid, []).append(in_band)
        minute = int(time.time()) // 60
        if minute != self._last_uptime_minute:
            self._flush_uptime(minute)
            self._last_uptime_minute = minute

    def _flush_uptime(self, minute: int) -> None:
        with self._lock:
            for cid, samples in self._uptime_samples.items():
                if not samples:
                    continue
                in_band = sum(samples) / len(samples) >= 0.5
                self._conn.execute(
                    "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
                    (minute, cid, int(in_band)),
                )
                self._uptime_samples[cid] = []
            self._conn.commit()

    def session_uptime_pct(self) -> float:
        rows = self._conn.execute(
            "SELECT in_band FROM uptime WHERE minute_ts >= ?",
            (int(self._session_start) // 60,),
        ).fetchall()
        if not rows:
            return 0.0
        return sum(r[0] for r in rows) / len(rows) * 100

    def uptime_pct_by_market(self, since_minute: int,
                             min_samples: int = 10) -> dict[str, float]:
        """In-band uptime % per market over the recent window.

        Used by sticky market selection to decide whether a held market is
        actually farming rewards (high in-band %) or underperforming. Markets
        with fewer than ``min_samples`` minutes of history are omitted, so a
        freshly-entered market is treated as 'performing' (protected) until it
        has a real track record rather than being evicted on thin data.
        """
        rows = self._conn.execute(
            "SELECT cid, AVG(in_band) * 100.0, COUNT(*) FROM uptime "
            "WHERE minute_ts >= ? GROUP BY cid",
            (int(since_minute),),
        ).fetchall()
        return {cid: pct for cid, pct, n in rows if n >= min_samples}

    @staticmethod
    def _sum_earnings(rows) -> float:
        """USD realized rewards from a /rewards/user[/total] payload.

        The CLOB returns one row per collateral asset:
        {date, asset_address, maker_address, earnings, asset_rate}. `earnings`
        is denominated in the asset (pUSD/USDC) and `asset_rate` is its USD
        price (~1.0), so USD = sum(earnings * asset_rate). Some deployments wrap
        the list in {"data": [...]}; handle both.
        """
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("earnings") or []
        total = 0.0
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            earnings = float(item.get("earnings") or item.get("amount")
                             or item.get("reward") or 0.0)
            rate = float(item.get("asset_rate") or 1.0)
            total += earnings * rate
        return total

    def fetch_realized_rewards(self, client, date: str | None = None) -> float:
        """Realized liquidity rewards (USD) for a UTC day, from the CLOB.

        Uses the authenticated `/rewards/user/total` endpoint via the official
        client method, which signs the request correctly (L2 HMAC over the bare
        path), passes the wallet `signature_type`, and handles pagination — the
        previous hand-rolled call signed the path WITH its query string and
        omitted signature_type, so it 401'd and silently recorded $0.

        Records the day's realized total so report/backtest can compare it to
        the estimate. Best-effort: on a transient fetch error we leave any
        previously recorded value intact (return without overwriting).
        """
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.inception_date and date < self.inception_date:
            return 0.0
        try:
            rows = client.get_total_earnings_for_user_for_day(date)
        except Exception as e:  # noqa: BLE001
            log.debug("获取已实现奖励失败（%s）：%s", date, e)
            return 0.0
        total = self._sum_earnings(rows)
        self.record_realized_reward(date, total)
        self.fetch_market_realized_rewards(client, date)
        return total

    def backfill_realized_rewards(self, client, days: int = 7) -> dict[str, float]:
        """Re-fetch and record realized rewards for the last `days` UTC dates.

        Lets recently-finalized days (rewards post shortly after UTC midnight)
        and any rows left at $0 by the old bug self-heal. Returns {date: usd}.
        """
        out: dict[str, float] = {}
        today = datetime.now(timezone.utc).date()
        for d in range(days):
            date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
            if self.inception_date and date < self.inception_date:
                continue
            out[date] = self.fetch_realized_rewards(client, date)
        return out

    def daily_report(self, date: str | None = None) -> dict:
        """PnL decomposition for a UTC day."""
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        day_end = day_start + 86400

        merges = self._conn.execute(
            "SELECT COALESCE(SUM(pairs),0) FROM merges WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        hedge_cost = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM hedges WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        fees = self._conn.execute(
            "SELECT COALESCE(SUM(fee),0) FROM fills WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        # Net trading P&L from the cash ledger (merges + sells - buys - fees) —
        # the ground truth, vs the gross component figures above.
        buys = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM fills "
            "WHERE ts>=? AND ts<? AND exit=0",
            (day_start, day_end),
        ).fetchone()[0]
        sells = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM fills "
            "WHERE ts>=? AND ts<? AND exit=1",
            (day_start, day_end),
        ).fetchone()[0]
        trading_pnl = merges + sells - buys - fees

        est_rewards = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards WHERE date=?",
            (date,),
        ).fetchone()[0]

        realized_rewards = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards WHERE date=?",
            (date,),
        ).fetchone()[0]

        fill_count = self._conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts>=? AND ts<? AND taker=0 AND exit=0",
            (day_start, day_end),
        ).fetchone()[0]

        equity_rows = self._conn.execute(
            "SELECT equity FROM equity WHERE ts>=? AND ts<? ORDER BY ts",
            (day_start, day_end),
        ).fetchall()
        equity_pnl = 0.0
        if len(equity_rows) >= 2:
            equity_pnl = equity_rows[-1][0] - equity_rows[0][0]

        uptime_rows = self._conn.execute(
            "SELECT in_band FROM uptime WHERE minute_ts>=? AND minute_ts<?",
            (int(day_start) // 60, int(day_end) // 60),
        ).fetchall()
        uptime_pct = (sum(r[0] for r in uptime_rows) / len(uptime_rows) * 100
                      if uptime_rows else 0.0)

        # ── recovery events ──
        rec_skips = self._conn.execute(
            "SELECT reason, COUNT(*) FROM recovery_events "
            "WHERE ts>=? AND ts<? AND event='skip' GROUP BY reason",
            (day_start, day_end),
        ).fetchall()
        total_skips = sum(n for _, n in rec_skips)
        quotes_placed = self._conn.execute(
            "SELECT COUNT(*) FROM recovery_events "
            "WHERE ts>=? AND ts<? AND event='quote_placed'",
            (day_start, day_end),
        ).fetchone()[0]
        forced_hedges = self._conn.execute(
            "SELECT COUNT(*) FROM recovery_events "
            "WHERE ts>=? AND ts<? AND event='forced_hedge'",
            (day_start, day_end),
        ).fetchone()[0]
        recovery_attempts = quotes_placed + forced_hedges
        hedge_success_rate = (quotes_placed / recovery_attempts
                              if recovery_attempts > 0 else None)
        # Average premium paid: (quote_price - pair_cap) on quotes placed
        # above the break-even cap.  Positive = we paid extra.
        premium_avg = self._conn.execute(
            "SELECT AVG(quote_price - pair_cap) FROM recovery_events "
            "WHERE ts>=? AND ts<? AND event='quote_placed' "
            "AND quote_price IS NOT NULL AND pair_cap IS NOT NULL",
            (day_start, day_end),
        ).fetchone()[0]
        premium_max = self._conn.execute(
            "SELECT MAX(quote_price - pair_cap) FROM recovery_events "
            "WHERE ts>=? AND ts<? AND event='quote_placed' "
            "AND quote_price IS NOT NULL AND pair_cap IS NOT NULL",
            (day_start, day_end),
        ).fetchone()[0]

        return {
            "date": date,
            "merge_proceeds_usd": merges,
            "buys_usd": buys,
            "sells_usd": sells,
            "trading_pnl_usd": trading_pnl,
            "spread_capture_usd": merges,
            "hedge_cost_usd": hedge_cost,
            "fees_usd": -fees,
            "est_rewards_usd": est_rewards,
            "realized_rewards_usd": realized_rewards,
            "equity_pnl_usd": equity_pnl,
            "maker_fills": fill_count,
            "uptime_pct": uptime_pct,
            "recovery": {
                "total_skips": total_skips,
                "quotes_placed": quotes_placed,
                "forced_hedges": forced_hedges,
                "skips_by_reason": {r: n for r, n in rec_skips},
                "hedge_success_rate": hedge_success_rate,
                "premium_avg_cents": (premium_avg * 100.0) if premium_avg is not None else None,
                "premium_max_cents": (premium_max * 100.0) if premium_max is not None else None,
            },
        }

    def reward_totals(self, date: str | None = None) -> dict:
        """Realized/estimated rewards, all-time and rolling last 24h.

        Realized rewards are stored per UTC day (the CLOB finalizes them
        daily), so the 24h figure is the sum over days whose record timestamp
        falls in the last 24h — in practice today's (and possibly yesterday's
        just-finalized) total.
        """
        realized_total = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards").fetchone()[0]
        est_total = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards").fetchone()[0]
        report_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_24h = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards WHERE date=?",
            (report_date,)).fetchone()[0]
        est_24h = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards WHERE date=?",
            (report_date,)).fetchone()[0]
        return {
            "realized_total": realized_total, "realized_24h": realized_24h,
            "est_total": est_total, "est_24h": est_24h,
        }

    def hedge_pnl_totals(self) -> dict:
        """Estimated P&L of forced hedges, all-time and rolling last 24h.

        A forced hedge taker-buys the complement of a maker leg we already hold;
        the completed pair then merges back to $1. The realized P&L on a
        completed pair is

            (1 - hedge_price - basis)

        where `basis` is our cost for the held leg, approximated by the average
        maker fill price of the *majority* maker side in that market (the side
        we accumulate is the one that needs hedging). Crucially we cap the
        number of loss-bearing pairs at the maker shares we actually held —
        hedge shares beyond that were open taker buys that merged against other
        flow or resolved, and aren't a realized pairing loss. Hedges in markets
        with no recorded maker leg are skipped (no basis to estimate from).

        This is an ESTIMATE — lot-level pairing isn't logged, so treat it as
        indicative, not audited. Realized rewards are exact; for the true
        bottom line compare deposits vs wallet balance. `spend_*` is the gross
        taker dollars for reference.
        """
        maker: dict[str, dict[str, tuple[float, float]]] = {}
        for cid, side, notional, shares in self._conn.execute(
            "SELECT cid, side, COALESCE(SUM(price*size),0), COALESCE(SUM(size),0) "
            "FROM fills WHERE taker=0 AND exit=0 GROUP BY cid, side"
        ):
            avg = (notional / shares) if shares else 0.0
            maker.setdefault(cid, {})[side] = (avg, shares)

        cutoff = time.time() - 86400
        agg: dict[str, dict[str, float]] = {}
        for cid, ts, price, size in self._conn.execute(
            "SELECT cid, ts, price, size FROM hedges"
        ):
            a = agg.setdefault(cid, {"pv": 0.0, "sz": 0.0,
                                     "pv24": 0.0, "sz24": 0.0})
            a["pv"] += price * size
            a["sz"] += size
            if ts >= cutoff:
                a["pv24"] += price * size
                a["sz24"] += size

        out = {"pnl_total": 0.0, "pnl_24h": 0.0, "spend_total": 0.0,
               "spend_24h": 0.0, "shares_total": 0.0, "shares_24h": 0.0}
        for cid, a in agg.items():
            sides = maker.get(cid, {})
            y_px, y_sz = sides.get("YES", (0.0, 0.0))
            n_px, n_sz = sides.get("NO", (0.0, 0.0))
            # The leg we hold (and must hedge) is the majority maker side.
            basis, held = (y_px, y_sz) if y_sz >= n_sz else (n_px, n_sz)
            out["spend_total"] += a["pv"]
            out["shares_total"] += a["sz"]
            out["spend_24h"] += a["pv24"]
            out["shares_24h"] += a["sz24"]
            if held <= 0:
                continue
            if a["sz"] > 0:
                hpx = a["pv"] / a["sz"]
                out["pnl_total"] += min(a["sz"], held) * (1.0 - hpx - basis)
            if a["sz24"] > 0:
                hpx24 = a["pv24"] / a["sz24"]
                out["pnl_24h"] += min(a["sz24"], held) * (1.0 - hpx24 - basis)
        return out

    def trading_pnl_ledger(self, date: str | None = None) -> dict:
        """Ground-truth realized trading P&L from the cash ledger.

        Reconciles the ACTUAL logged cashflows the way the Polymarket trade
        history does, rather than estimating forced-hedge pairing loss like
        ``hedge_pnl_totals`` (which assumes a basis and caps loss-bearing
        pairs, and was found to understate the real loss ~2x):

            realized = merges($1/pair) + exits(sells) - buys - fees

        Every fill with ``exit=0`` is cash OUT (maker reward quotes AND taker
        forced hedges); every ``exit=1`` fill is cash IN; each merged pair
        returns $1. Rewards and deposits are EXCLUDED — they are not trading
        P&L. ``mtm_total`` adds the latest inventory mark so open (bought but
        not-yet-merged) pairs aren't counted as pure loss; ``realized_*`` treat
        held inventory as sunk, so short windows read low until those pairs
        merge/resolve. This is the apples-to-apples match for your Polymarket
        history (sum of +/- trades - deposits - rewards).

        Caveat: positions that resolved and redeemed at $1 without a logged
        merge are not captured here, which would make the ledger look slightly
        worse than reality; with ``merge_enabled`` this should be small.
        """
        def _cash(since: float | None, until: float | None = None) -> float:
            clauses, args = [], []
            if since is not None:
                clauses.append("ts >= ?")
                args.append(since)
            if until is not None:
                clauses.append("ts < ?")
                args.append(until)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            buys = self._conn.execute(
                "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=0"
                + (f" AND {' AND '.join(clauses)}" if clauses else ""),
                args).fetchone()[0] or 0.0
            sells = self._conn.execute(
                "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=1"
                + (f" AND {' AND '.join(clauses)}" if clauses else ""),
                args).fetchone()[0] or 0.0
            fees = self._conn.execute(
                "SELECT COALESCE(SUM(fee),0) FROM fills" + where,
                args).fetchone()[0] or 0.0
            merge_cash = self._conn.execute(
                "SELECT COALESCE(SUM(pairs),0) FROM merges" + where,
                args).fetchone()[0] or 0.0
            return merge_cash + sells - buys - fees

        inv_row = self._conn.execute(
            "SELECT inventory_usd FROM equity ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        inv = float(inv_row[0]) if inv_row and inv_row[0] is not None else 0.0
        realized_total = _cash(None)
        if date is None:
            realized_24h = _cash(time.time() - 86400)
        else:
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
            realized_24h = _cash(day_start, day_start + 86400)
        return {
            "realized_total": realized_total,
            "realized_24h": realized_24h,
            "inventory_usd": inv,
            "mtm_total": realized_total + inv,
        }

    def recent_fills(self, limit: int = 50, since_ts: float | None = None,
                     cid: str | None = None) -> list[dict]:
        """Return recent fills newest-first."""
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if cid:
            clauses.append("cid = ?")
            params.append(cid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT ts,cid,market,side,token,price,size,taker,exit,merged,fee "
            f"FROM fills {where} ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "ts": r[0], "cid": r[1], "market": r[2], "side": r[3],
                "token": r[4], "price": r[5], "size": r[6],
                "taker": bool(r[7]), "exit": bool(r[8]),
                "merged": r[9], "fee": r[10],
            }
            for r in rows
        ]

    def recovery_history(self, cid: str) -> dict:
        """Return one market's recovery decision timeline without side effects."""
        rows = self._conn.execute(
            "SELECT ts,event,reason,unpaired,recovery_path,quote_price,pair_cap,"
            "proposed_price,cost_basis,fee_per_share,expected_pair_pnl,hard_cap "
            "FROM recovery_events "
            "WHERE cid=? ORDER BY ts,id", (cid,)).fetchall()
        events = [
            {
                "ts": r[0], "event": r[1], "reason": r[2], "unpaired": r[3],
                "recovery_path": r[4], "quote_price": r[5], "pair_cap": r[6],
                "proposed_price": r[7], "cost_basis": r[8],
                "fee_per_share": r[9], "expected_pair_pnl": r[10], "hard_cap": r[11],
            }
            for r in rows
        ]
        inventory_row = self._conn.execute(
            "SELECT market,unpaired_shares,cost_basis,exposure_usd,status,ts "
            "FROM inventory_snapshots WHERE cid=? ORDER BY ts DESC,id DESC LIMIT 1",
            (cid,)).fetchone()
        inventory = None if inventory_row is None else {
            "market": inventory_row[0], "unpaired_shares": inventory_row[1],
            "cost_basis": inventory_row[2], "exposure_usd": inventory_row[3],
            "status": inventory_row[4], "ts": inventory_row[5],
        }
        return {"cid": cid, "events": events, "inventory": inventory}

    def performance_report(self, date: str | None = None) -> dict:
        """Per-market breakdown for tuning: fills, merges, hedges, markouts, uptime."""
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        day_end = day_start + 86400
        minute_start = int(day_start) // 60
        minute_end = int(day_end) // 60

        markets: dict[str, dict] = {}

        def _ensure(cid: str, name: str = "") -> dict:
            if cid not in markets:
                markets[cid] = {
                    "cid": cid,
                    "market": name,
                    "maker_fills": 0,
                    "taker_fills": 0,
                    "exits": 0,
                    "merged_pairs": 0.0,
                    "buy_cost_usd": 0.0,
                    "exit_proceeds_usd": 0.0,
                    "merge_proceeds_usd": 0.0,
                    "hedge_cost_usd": 0.0,
                    "fees_usd": 0.0,
                    "trading_pnl_usd": 0.0,
                    "selection_cashflow_usd": None,
                    "cashflow_attribution_status": None,
                    "carry_in_yes_shares": 0.0,
                    "carry_in_no_shares": 0.0,
                    "carry_in_paired_shares": 0.0,
                    "cross_day_merge_pairs_upper_bound": 0.0,
                    "cross_day_exit_shares_upper_bound": 0.0,
                    "est_rewards_usd": 0.0,
                    "realized_rewards_usd": None,
                    "reward_attribution_status": None,
                    "reward_calibration_ratio": None,
                    "net_pnl_est_usd": 0.0,
                    "inventory_status": None,
                    "unpaired_shares": None,
                    "unpaired_cost_basis": None,
                    "inventory_exposure_usd": None,
                    "unpaired_inventory_mtm_usd": None,
                    "net_pnl_with_unpaired_mtm_est_usd": None,
                    "completed_pair_count": 0.0,
                    "cashflow_per_completed_pair_usd": None,
                    "trade_event_count": 0,
                    "last_inventory_event": None,
                    "inventory_terminal_status": None,
                    "inventory_terminal_evidence": None,
                    "markout_cents": None,
                    "markout_n": 0,
                    "uptime_pct": 0.0,
                    "recovery_skips": 0,
                    "recovery_quotes": 0,
                    "forced_hedges": 0,
                }
            elif name and not markets[cid]["market"]:
                markets[cid]["market"] = name
            return markets[cid]

        day_exit_shares: dict[str, dict[str, float]] = {}
        for row in self._conn.execute(
            "SELECT cid, market, side, taker, exit, merged, fee, price, size FROM fills "
            "WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ):
            cid, name, side, taker, exit_, merged, fee, price, size = row
            m = _ensure(cid, name or "")
            if exit_:
                m["exits"] += 1
                m["exit_proceeds_usd"] += (price or 0.0) * (size or 0.0)
                by_side = day_exit_shares.setdefault(cid, {"YES": 0.0, "NO": 0.0})
                by_side["YES" if str(side).upper() == "YES" else "NO"] += size or 0.0
            elif taker:
                m["taker_fills"] += 1
                m["buy_cost_usd"] += (price or 0.0) * (size or 0.0)
            else:
                m["maker_fills"] += 1
                m["buy_cost_usd"] += (price or 0.0) * (size or 0.0)
            m["merged_pairs"] += merged or 0
            m["fees_usd"] += fee or 0

        for cid, proceeds in self._conn.execute(
            "SELECT cid, COALESCE(SUM(pairs),0) FROM merges "
            "WHERE ts>=? AND ts<? GROUP BY cid",
            (day_start, day_end),
        ):
            _ensure(cid)["merge_proceeds_usd"] = proceeds

        for cid, cost in self._conn.execute(
            "SELECT cid, COALESCE(SUM(price*size),0) FROM hedges "
            "WHERE ts>=? AND ts<? GROUP BY cid",
            (day_start, day_end),
        ):
            _ensure(cid)["hedge_cost_usd"] = cost

        markout_rows = self._conn.execute(
            "SELECT cid, market, markout, horizon FROM markouts "
            "WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchall()
        if markout_rows:
            max_h = max(r[3] for r in markout_rows)
            by_cid: dict[str, list[float]] = {}
            for cid, name, markout, horizon in markout_rows:
                if horizon != max_h:
                    continue
                _ensure(cid, name or "")
                by_cid.setdefault(cid, []).append(markout)
            for cid, vals in by_cid.items():
                m = markets[cid]
                m["markout_n"] = len(vals)
                m["markout_cents"] = sum(vals) / len(vals) * 100

        for cid, total, in_band in self._conn.execute(
            "SELECT cid, COUNT(*), COALESCE(SUM(in_band),0) FROM uptime "
            "WHERE minute_ts>=? AND minute_ts<? GROUP BY cid",
            (minute_start, minute_end),
        ):
            m = _ensure(cid)
            m["uptime_pct"] = (in_band / total * 100) if total else 0.0

        for cid, event, n in self._conn.execute(
            "SELECT cid, event, COUNT(*) FROM recovery_events "
            "WHERE ts>=? AND ts<? GROUP BY cid, event",
            (day_start, day_end),
        ):
            m = _ensure(cid)
            if event == "skip":
                m["recovery_skips"] = n
            elif event == "quote_placed":
                m["recovery_quotes"] = n
            elif event == "forced_hedge":
                m["forced_hedges"] = n

        for cid, estimated in self._conn.execute(
            "SELECT cid, COALESCE(SUM(est_usd),0) FROM reward_samples "
            "WHERE minute_ts>=? AND minute_ts<? GROUP BY cid",
            (minute_start, minute_end),
        ):
            _ensure(cid)["est_rewards_usd"] = estimated

        for cid, realized in self._conn.execute(
            "SELECT cid, realized FROM market_rewards WHERE date=?",
            (date,),
        ):
            m = _ensure(cid)
            m["realized_rewards_usd"] = realized
            m["reward_attribution_status"] = "attributed"

        latest_snapshots: dict[str, tuple[float, str]] = {}
        for row in self._conn.execute(
            "SELECT s.cid, s.market, s.unpaired_shares, s.cost_basis, "
            "s.exposure_usd, s.status, s.ts "
            "FROM inventory_snapshots s "
            "JOIN ("
            "  SELECT cid, MAX(id) AS latest_id FROM inventory_snapshots "
            "  WHERE ts>=? AND ts<? GROUP BY cid"
            ") latest ON latest.latest_id=s.id",
            (day_start, day_end),
        ):
            cid, name, unpaired, basis, exposure, status, snapshot_ts = row
            m = _ensure(cid, name or "")
            m["inventory_status"] = status
            m["unpaired_shares"] = unpaired
            m["unpaired_cost_basis"] = basis
            m["inventory_exposure_usd"] = exposure
            m["unpaired_inventory_mtm_usd"] = exposure
            latest_snapshots[cid] = (snapshot_ts, status)

        for cid, event in self._conn.execute(
            "SELECT e.cid, e.event FROM inventory_events e "
            "JOIN ("
            "  SELECT cid, MAX(id) AS latest_id FROM inventory_events "
            "  WHERE ts>=? AND ts<? GROUP BY cid"
            ") latest ON latest.latest_id=e.id",
            (day_start, day_end),
        ):
            _ensure(cid)["last_inventory_event"] = event

        for cid, (flat_ts, status) in latest_snapshots.items():
            m = markets[cid]
            if status != "flat":
                m["inventory_terminal_status"] = "unresolved"
                m["inventory_terminal_evidence"] = "latest_snapshot_unpaired"
                continue
            prior = self._conn.execute(
                "SELECT ts, unpaired_shares FROM inventory_snapshots "
                "WHERE cid=? AND ts>=? AND ts<=? "
                "AND ABS(COALESCE(unpaired_shares,0))>? "
                "ORDER BY id DESC LIMIT 1",
                (cid, day_start, flat_ts, 1e-9),
            ).fetchone()
            if prior is None:
                m["inventory_terminal_status"] = "unresolved"
                m["inventory_terminal_evidence"] = "no_prior_unpaired_snapshot"
                continue
            opened_ts, unpaired = prior
            needed = abs(float(unpaired))
            merged = self._conn.execute(
                "SELECT COALESCE(SUM(pairs),0) FROM merges "
                "WHERE cid=? AND ts>=? AND ts<=?",
                (cid, opened_ts, flat_ts),
            ).fetchone()[0]
            if merged >= needed:
                m["inventory_terminal_status"] = "paired"
                m["inventory_terminal_evidence"] = "confirmed_merge"
                continue
            exited = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM fills "
                "WHERE cid=? AND exit=1 AND side=? AND ts>=? AND ts<=?",
                (cid, "YES" if unpaired > 0 else "NO", opened_ts, flat_ts),
            ).fetchone()[0]
            if exited >= needed:
                m["inventory_terminal_status"] = "exit"
                m["inventory_terminal_evidence"] = "reduce_only_exit"
                continue
            hedged = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM hedges "
                "WHERE cid=? AND ts>=? AND ts<=?",
                (cid, opened_ts, flat_ts),
            ).fetchone()[0]
            if hedged >= needed:
                m["inventory_terminal_status"] = "hedged"
                m["inventory_terminal_evidence"] = "forced_complement_buy"
            else:
                m["inventory_terminal_status"] = "unresolved"
                m["inventory_terminal_evidence"] = "insufficient_disposition_quantity"

        account_realized = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards WHERE date=?",
            (date,),
        ).fetchone()[0]
        prior_tokens: dict[str, dict[str, float]] = {}
        for cid, yes, no in self._conn.execute(
            "SELECT cid, "
            "COALESCE(SUM(CASE WHEN side='YES' AND exit=0 THEN size "
            "WHEN side='YES' AND exit=1 THEN -size ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN side='NO' AND exit=0 THEN size "
            "WHEN side='NO' AND exit=1 THEN -size ELSE 0 END),0) "
            "FROM fills WHERE ts<? GROUP BY cid",
            (day_start,),
        ):
            prior_tokens[cid] = {"YES": yes or 0.0, "NO": no or 0.0}
        for cid, pairs in self._conn.execute(
            "SELECT cid, COALESCE(SUM(pairs),0) FROM merges "
            "WHERE ts<? GROUP BY cid",
            (day_start,),
        ):
            state = prior_tokens.setdefault(cid, {"YES": 0.0, "NO": 0.0})
            state["YES"] -= pairs or 0.0
            state["NO"] -= pairs or 0.0
        for m in markets.values():
            m["trading_pnl_usd"] = (
                m["merge_proceeds_usd"]
                + m["exit_proceeds_usd"]
                - m["buy_cost_usd"]
                - m["fees_usd"]
            )
            # This is deliberately an estimate: realized rewards are only
            # available as a daily account-level total, not per market.
            m["net_pnl_est_usd"] = m["trading_pnl_usd"] + m["est_rewards_usd"]
            if m["unpaired_inventory_mtm_usd"] is not None:
                m["net_pnl_with_unpaired_mtm_est_usd"] = (
                    m["net_pnl_est_usd"] + m["unpaired_inventory_mtm_usd"])
            m["completed_pair_count"] = m["merge_proceeds_usd"]
            m["trade_event_count"] = (
                m["maker_fills"] + m["taker_fills"] + m["exits"])
            if m["realized_rewards_usd"] is not None:
                est = m["est_rewards_usd"]
                m["reward_calibration_ratio"] = (
                    m["realized_rewards_usd"] / est if est > 0 else None
                )
            else:
                m["reward_attribution_status"] = (
                    "account_total_only" if account_realized else "unavailable"
                )

            carry = prior_tokens.get(m["cid"], {"YES": 0.0, "NO": 0.0})
            carry_yes = max(0.0, carry["YES"])
            carry_no = max(0.0, carry["NO"])
            carry_pairs = min(carry_yes, carry_no)
            m["carry_in_yes_shares"] = carry_yes
            m["carry_in_no_shares"] = carry_no
            m["carry_in_paired_shares"] = carry_pairs
            m["cross_day_merge_pairs_upper_bound"] = min(
                m["merge_proceeds_usd"], carry_pairs)
            exits = day_exit_shares.get(m["cid"], {"YES": 0.0, "NO": 0.0})
            m["cross_day_exit_shares_upper_bound"] = (
                min(exits["YES"], carry_yes) + min(exits["NO"], carry_no)
            )
            has_closing_cashflow = bool(m["merge_proceeds_usd"] or sum(exits.values()))
            if (m["cross_day_merge_pairs_upper_bound"] > 1e-9
                    or m["cross_day_exit_shares_upper_bound"] > 1e-9):
                m["cashflow_attribution_status"] = "mixed_with_carry_in"
            elif has_closing_cashflow:
                m["cashflow_attribution_status"] = "same_day_cashflow"
                m["selection_cashflow_usd"] = m["trading_pnl_usd"]
                if m["completed_pair_count"] > 0:
                    m["cashflow_per_completed_pair_usd"] = (
                        m["selection_cashflow_usd"] / m["completed_pair_count"])
            else:
                m["cashflow_attribution_status"] = "no_closing_cashflow"

        rows = sorted(
            markets.values(),
            key=lambda r: (
                r["net_pnl_est_usd"],
                r["maker_fills"] + r["taker_fills"] + r["exits"],
            ),
            reverse=True,
        )
        summary = self.daily_report(date)
        return {"date": date, "summary": summary, "markets": rows,
                "shadow_selection": self.net_shadow_report(date)}

    def outcome_report(self, date: str | None = None, *,
                       lookback_days: int = 1,
                       snapshot_max_age_seconds: float | None = None) -> dict:
        """Closed-loop per-market outcome completeness summary.

        Delegates to ``build_market_outcomes()`` (from ``pmbot.outcomes``)
        and adds a high-level completeness breakdown: how many markets are
        ``realized`` / ``paired_unmerged`` / ``unpaired_marked`` /
        ``incomplete``, plus the reasons for incompleteness.

        ``date`` is a UTC date string like "2026-08-01". When ``lookback_days``
        > 1 the window spans that many full UTC days ending on ``date``.
        """
        from .outcomes import build_market_outcomes as _build

        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        # Window: [date 00:00 UTC, date+1 00:00 UTC).  The caller is
        # responsible for passing a completed UTC date — during an active
        # trading day main.py passes yesterday so the partial today is not
        # counted as a "complete UTC day".
        start_ts = day_start - (lookback_days - 1) * 86400.0
        end_ts = day_start + 86400.0

        # Build outcomes for the multi-day window (used for resolved/incomplete
        # counts and the complete_ratio).  Cycle counting is done separately
        # per UTC day below.
        outcomes = _build(self._conn, start_ts, end_ts,
                          snapshot_max_age_seconds=snapshot_max_age_seconds)

        # Three-way classification:
        #   realized   — fully closed, no open inventory (gate counts these)
        #   resolved   — has net_outcome_usd (realized + paired_unmerged + unpaired_marked)
        #   incomplete — insufficient data (missing MTM mid, etc.)
        realized: list[dict] = []
        resolved: list[dict] = []
        incomplete: list[dict] = []
        for o in outcomes:
            d = {
                "cid": o.cid, "state": o.state, "buy_cash_usd": o.buy_cash_usd,
                "sell_cash_usd": o.sell_cash_usd, "merge_cash_usd": o.merge_cash_usd,
                "fees_usd": o.fees_usd, "held_pairs": o.held_pairs,
                "unpaired_shares": o.unpaired_shares,
                "unpaired_mtm_usd": o.unpaired_mtm_usd,
                "trading_pnl_usd": o.trading_pnl_usd,
                "market_reward_usd": o.market_reward_usd,
                "net_outcome_usd": o.net_outcome_usd,
                "evidence_flags": list(o.evidence_flags),
            }
            if o.state == "realized":
                realized.append(d)
                resolved.append(d)
            elif o.net_outcome_usd is not None:
                resolved.append(d)
            else:
                incomplete.append(d)

        # Missing reasons: count each evidence flag across incomplete markets.
        missing_reasons: dict[str, int] = {}
        for d in incomplete:
            for flag in d["evidence_flags"]:
                missing_reasons[flag] = missing_reasons.get(flag, 0) + 1

        # Account-level reward — never enters market outcomes, shown separately.
        account_reward = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards "
            "WHERE date BETWEEN ? AND ?",
            (datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d"),
             date),
        ).fetchone()[0] or 0.0

        # Market-level reward total for cross-check.
        market_reward_total = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM market_rewards "
            "WHERE date BETWEEN ? AND ?",
            (datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d"),
             date),
        ).fetchone()[0] or 0.0

        total_trading_pnl = sum(
            o["trading_pnl_usd"] for o in realized
            if o["trading_pnl_usd"] is not None)

        # ── Per-day complete UTC days + closed-cycle counting ──
        # A single multi-day build_market_outcomes() window can produce at most
        # ONE realized outcome per CID — the CID either closed during the full
        # window or it didn't.  To get actual cycle counts we must walk each
        # UTC day separately: a CID that closed on 3 different days within the
        # lookback has N=3 closed cycles.
        #
        # Similarly, "complete UTC days" are days where at least one CID
        # produced a realized outcome.  This is derived from the per-day pass.
        first_day = datetime.fromtimestamp(start_ts, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        last_day = datetime.fromtimestamp(end_ts, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        # last_day is the most recent COMPLETE UTC day in the window
        # (end_ts = midnight of date, which is the exclusive upper bound)
        cycle_counts: dict[str, int] = {}        # CID → closed cycles
        complete_utc_dates: set[str] = set()
        day_cursor = last_day
        while day_cursor >= first_day:
            d_start = day_cursor.timestamp()
            d_end = d_start + 86400.0
            day_outcomes = _build(self._conn, d_start, d_end,
                                  snapshot_max_age_seconds=snapshot_max_age_seconds)
            day_realized = [o for o in day_outcomes if o.state == "realized"]
            if day_realized:
                complete_utc_dates.add(day_cursor.strftime("%Y-%m-%d"))
                for o in day_outcomes:
                    if o.state == "realized":
                        cycle_counts[o.cid] = cycle_counts.get(o.cid, 0) + 1
            day_cursor -= timedelta(days=1)

        complete_utc_days = len(complete_utc_dates)
        total_closed_cycles = sum(cycle_counts.values())
        cids_with_min_cycles = sum(
            1 for c in cycle_counts.values()
            if c >= 3)  # min_closed_samples from net_shadow config

        # ── P2.2 risk metrics (design doc §5.2) ──
        # Design doc requires: "shadow Top-N 的强制对冲率、300 秒负
        # markout 率、最大单市场暴露均不劣于 legacy" — same scan,
        # same eligibility set, per-market inventory.  This requires
        # per-scan legacy-vs-shadow candidate pairing data
        # (net_shadow_candidates / native ranking snapshots) that is not
        # available in a static outcome_report().
        #
        # Additionally, max_single_market_exposure needs per-market
        # inventory snapshots; the equity table stores only total
        # inventory_usd from broker.total_inventory_usd(), not the
        # largest single-market position as a fraction of equity.
        #
        # These three metrics are deferred to a post-hoc analysis
        # pipeline.  Returning None causes the gate to fail with
        # "insufficient_risk_data" — fail-closed until the comparison
        # pipeline is built.  Zero observations (no recovery events,
        # no markout rows) also map to None rather than a misleading
        # 0.0 "safe" rate.
        forced_hedge_rate: float | None = None
        negative_markout_300s_rate: float | None = None
        max_single_exposure_pct: float | None = None

        # ── P2.2 data-integrity checks (design doc §5.2) ──
        # "连续 3 个 UTC 日没有 fill_without_inventory_closure、
        # 重复 fill 或 reward double-count".  These require cross-day
        # closure tracking and fill/reward deduplication infrastructure.
        # Returning None causes the gate to fail with
        # "insufficient_integrity_data".
        consecutive_clean_days: int | None = None
        fill_closure_gaps: int | None = None
        duplicate_fill_count: int | None = None
        reward_double_count: int | None = None

        # ── P2.2 net-outcome advantage ──
        # Design intent: compare (shadow Top-N avg net/hour) minus
        # (legacy Top-N avg net/hour) on the SAME scan and SAME eligibility
        # set, with bootstrap 95% CI lower bound > 0.
        #
        # This is NOT the global realized PnL rate.  We cannot compute it
        # from a static outcome_report() because we lack the per-scan
        # legacy-vs-shadow candidate pairings.  That data lives in
        # net_shadow_candidates/native ranking snapshots, and the
        # comparison requires a separate post-hoc analysis pipeline.
        #
        # RETURNING NONE IS CORRECT.  The gate will fail at the advantage
        # check, which is the safe state: net_outcome mode cannot activate
        # until the ranking-comparison advantage is properly computed.
        advantage: float | None = None

        return {
            "date": date,
            "window_start_ts": start_ts,
            "window_end_ts": end_ts,
            "lookback_days": lookback_days,
            "complete_utc_days": complete_utc_days,
            "realized_count": len(realized),
            "resolved_count": len(resolved),
            "incomplete_count": len(incomplete),
            "total_closed_cycles": total_closed_cycles,
            "cids_with_min_cycles": cids_with_min_cycles,
            "complete_ratio": (len(realized) / len(outcomes)
                               if outcomes else None),
            "forced_hedge_rate": forced_hedge_rate,
            "negative_markout_300s_rate": negative_markout_300s_rate,
            "max_single_exposure_pct": max_single_exposure_pct,
            "consecutive_clean_days": consecutive_clean_days,
            "fill_closure_gaps": fill_closure_gaps,
            "duplicate_fill_count": duplicate_fill_count,
            "reward_double_count": reward_double_count,
            "total_trading_pnl_usd": total_trading_pnl,
            "market_reward_total_usd": market_reward_total,
            "account_reward_total_usd": account_reward,
            "net_outcome_advantage_usd_per_hour": advantage,
            "advantage_bootstrap_95ci_lower": None,  # requires per-scan pairing pipeline
            "missing_reasons": missing_reasons,
            "outcomes": sorted(resolved, key=lambda o: (
                o.get("net_outcome_usd") or 0.0), reverse=True),
            "incomplete": sorted(incomplete, key=lambda o: o["cid"]),
        }

    def close(self) -> None:
        self._flush_uptime(int(time.time()) // 60)
        self._conn.close()
