"""Tests for market scanner safety behavior."""

from datetime import datetime, timedelta, timezone

from pmbot import gamma


def _mk(cid, pool, liquidity, volume_24h, band=3.0, mid=0.5):
    end = datetime.now(timezone.utc) + timedelta(hours=48)
    return gamma.Market(
        question=f"market {cid}", condition_id=cid,
        yes_token=f"{cid}-y", no_token=f"{cid}-n",
        min_size=50.0, max_spread_cents=band, daily_pool=pool,
        liquidity=liquidity, volume_24h=volume_24h, tick=0.01,
        end_date=end, neg_risk=False, best_bid=mid - 0.01, best_ask=mid + 0.01,
    )


def _scan_cfg(**scanner_overrides):
    sc = {
        "mid_range": [0.15, 0.85], "min_hours_to_end": 14,
        "exclude_keywords": [], "min_pool_per_day": 25,
        "max_min_size_shares": 100, "min_pool_to_liquidity": 0.01,
        "max_fee_bps": 0, "fee_penalty_mult": 0.5, "top_n_markets": 5,
    }
    sc.update(scanner_overrides)
    return {"scanner": sc, "quoting": {"max_capital_per_market": 50,
                                         "offset_frac_of_max_spread": 0.35}}


def test_turnover_penalty_demotes_high_churn_market(monkeypatch):
    # Two markets, identical reward density (pool/liquidity), but one churns 20x
    # more volume — the toxicity penalty should rank the calm one first.
    calm = _mk("calm", pool=100, liquidity=5000, volume_24h=5000)
    churn = _mk("churn", pool=100, liquidity=5000, volume_24h=100000)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [churn, calm])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.05, band_room_bonus=0.0,
                                      ranking_mode="capture"))
    assert [m.condition_id for m in ranked] == ["calm", "churn"]


def test_min_liquidity_floor_drops_thin_books(monkeypatch):
    # The density ranking favors thin books; the absolute liquidity floor must
    # drop a shallow market even when its pool/liquidity density is high.
    thin = _mk("thin", pool=40, liquidity=431, volume_24h=0)  # density ~0.093
    deep = _mk("deep", pool=100, liquidity=6000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [thin, deep])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    ranked = gamma.scan(_scan_cfg(min_liquidity=3000, min_pool_per_day=25))
    assert [m.condition_id for m in ranked] == ["deep"]


def test_min_liquidity_floor_defaults_off(monkeypatch):
    # Absent/zero floor preserves prior behavior (thin book still eligible).
    thin = _mk("thin", pool=40, liquidity=431, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [thin])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    ranked = gamma.scan(_scan_cfg(min_pool_per_day=25))
    assert [m.condition_id for m in ranked] == ["thin"]


def test_exclude_cids_backfills_next_best(monkeypatch):
    # Rotation: excluding the top market promotes the next-best into its slot.
    top = _mk("top", pool=300, liquidity=5000, volume_24h=0)   # higher density
    mid = _mk("mid", pool=200, liquidity=5000, volume_24h=0)
    low = _mk("low", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [low, mid, top])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    cfg = _scan_cfg(min_pool_per_day=25, top_n_markets=2)
    assert [m.condition_id for m in gamma.scan(cfg)] == ["top", "mid"]
    rotated = gamma.scan(cfg, exclude_cids={"top"})
    assert [m.condition_id for m in rotated] == ["mid", "low"]


def test_scan_full_returns_all_ranked_not_just_top_n(monkeypatch):
    # full=True returns every eligible market (best first) so the bot can run
    # its own sticky selection; the default still slices to top_n.
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=200, liquidity=5000, volume_24h=0)
    c = _mk("c", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [c, b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    cfg = _scan_cfg(min_pool_per_day=25, top_n_markets=2)
    assert [m.condition_id for m in gamma.scan(cfg)] == ["a", "b"]
    assert [m.condition_id for m in gamma.scan(cfg, full=True)] == ["a", "b", "c"]


def test_shadow_score_does_not_change_legacy_scan_order(monkeypatch):
    """P2.1 is observational: a better shadow score cannot replace a legacy pick."""
    high = _mk("high", pool=300, liquidity=5000, volume_24h=0)
    low = _mk("low", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [low, high])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    cfg = _scan_cfg(min_pool_per_day=25, top_n_markets=2, net_shadow={
        "min_reward_samples": 1, "min_uptime_samples": 1,
        "min_markout_samples": 1, "min_recovery_samples": 1,
        "reward_realization_prior": 0.5, "uptime_prior": 0.5,
        "markout_cost_per_hour_prior": 0.2,
        "recovery_cost_per_hour_prior": 0.3,
    })
    inputs = {
        "high": {"reward_realization": 0.1, "reward_samples": 1,
                 "uptime_ratio": 0.1, "uptime_samples": 1,
                 "markout_cost_per_hour": 2.0, "markout_samples": 1,
                 "recovery_cost_per_hour": 1.0, "recovery_samples": 1,
                 "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
        "low": {"reward_realization": 1.0, "reward_samples": 1,
                "uptime_ratio": 1.0, "uptime_samples": 1,
                "markout_cost_per_hour": 0.0, "markout_samples": 1,
                "recovery_cost_per_hour": 0.0, "recovery_samples": 1,
                "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
    }

    ranked = gamma.scan(cfg, full=True, shadow_inputs=inputs)

    assert [m.condition_id for m in ranked] == ["high", "low"]
    assert ranked[1].net_shadow_score > ranked[0].net_shadow_score


def test_fetch_market_loads_held_market_without_rewards(monkeypatch):
    """A held position must be manageable even after its reward pool ends."""
    raw = {
        "question": "held market", "conditionId": "held-cid",
        "clobTokenIds": '["yes-held", "no-held"]',
        "rewardsMinSize": 0, "rewardsMaxSpread": 0,
        "liquidityNum": 12, "volume24hr": 3,
        "orderPriceMinTickSize": 0.01, "negRisk": False,
    }
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return [raw]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            calls.append(kwargs["params"])
            return FakeResp()

    monkeypatch.setattr(gamma.httpx, "Client", FakeClient)

    market = gamma.fetch_market("held-cid")

    assert market is not None
    assert market.condition_id == "held-cid"
    assert market.yes_token == "yes-held"
    assert market.no_token == "no-held"
    assert calls == [{"condition_ids": "held-cid"}]


def test_fetch_reward_markets_requests_reward_bearing_books(monkeypatch):
    """The scanner must not depend on Gamma's unstable default pagination."""
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            calls.append(kwargs["params"])
            return FakeResp()

    monkeypatch.setattr(gamma.httpx, "Client", FakeClient)

    assert gamma.fetch_reward_markets() == []
    # Verify the request was made with the expected ordering params.
    assert calls[0]["order"] in ("rewardsDailyRate", "volume24hr")
    assert calls[0]["ascending"] == "false"


def test_fetch_reward_markets_retries_transient_page_failure(monkeypatch):
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            calls.append(kwargs["params"])
            if len(calls) == 1:
                raise RuntimeError("temporary Gamma failure")
            return FakeResp()

    monkeypatch.setattr(gamma.httpx, "Client", FakeClient)
    monkeypatch.setattr(gamma.time, "sleep", lambda _: None)

    assert gamma.fetch_reward_markets() == []
    assert len(calls) == 2  # first page retries, then the successful empty page


def test_band_room_bonus_prefers_wider_band(monkeypatch):
    narrow = _mk("narrow", pool=100, liquidity=5000, volume_24h=5000, band=1.0)
    wide = _mk("wide", pool=100, liquidity=5000, volume_24h=5000, band=4.0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [narrow, wide])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.0, band_room_bonus=0.10,
                                      ranking_mode="capture"))
    assert ranked[0].condition_id == "wide"


def test_zero_weights_reproduce_density_ranking(monkeypatch):
    # Graceful fallback: with both weights 0, ranking is pure reward density.
    a = _mk("a", pool=200, liquidity=5000, volume_24h=999999)  # higher density
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.0, band_room_bonus=0.0))
    assert ranked[0].condition_id == "a"  # density wins, turnover ignored


def _fake_httpx_client(payload=None, raise_on_get=False, fail_first=0):
    # ``fail_first`` raises on the first N get() calls then succeeds — used to
    # exercise the retry path. ``raise_on_get`` raises on every call.
    state = {"calls": 0}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            state["calls"] += 1
            if raise_on_get or state["calls"] <= fail_first:
                raise RuntimeError("fee api down")
            return FakeResp()

    return FakeClient


def test_fee_fetch_fails_open_assumes_zero(monkeypatch):
    monkeypatch.setattr(gamma.httpx, "Client", _fake_httpx_client(raise_on_get=True))
    # Persistent failure FAILS OPEN: makers pay no fee, so we still quote the
    # market rather than dropping it. attempts=1 keeps the test fast (no backoff).
    assert gamma._fetch_market_fees("cid1", {}, attempts=1) == (0, 1.0)


def test_fee_fetch_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(
        gamma.httpx, "Client",
        _fake_httpx_client({"fd": {"r": 0.04, "e": 1}}, fail_first=1),
    )
    # First call raises, retry succeeds — a transient blip must not drop the fee.
    assert gamma._fetch_market_fees("cid1", {}, attempts=2, backoff=0.0) == (400, 1.0)


def test_fee_fetch_parses_fd_rate_and_exponent(monkeypatch):
    monkeypatch.setattr(
        gamma.httpx, "Client",
        _fake_httpx_client({"fd": {"r": 0.04, "e": 1, "to": True}}),
    )
    # fd.r 0.04 -> 400 bps taker fee; exponent carried through.
    assert gamma._fetch_market_fees("cid1", {}) == (400, 1.0)


def test_fee_fetch_defaults_to_zero_when_fd_missing(monkeypatch):
    monkeypatch.setattr(gamma.httpx, "Client", _fake_httpx_client({}))
    assert gamma._fetch_market_fees("cid1", {}) == (0, 1.0)


# ── P2 Task 4: gated net_outcome selection mode ──


def _net_cfg(**overrides):
    """Config with selection_mode=net_outcome and realistic gate thresholds."""
    sc = {
        "mid_range": [0.15, 0.85], "min_hours_to_end": 14,
        "exclude_keywords": [], "min_pool_per_day": 25,
        "max_min_size_shares": 100, "min_pool_to_liquidity": 0.01,
        "max_fee_bps": 0, "fee_penalty_mult": 0.5, "top_n_markets": 2,
        "selection_mode": "net_outcome",
        "net_outcome_gate": {
            "min_utc_days": 14,
            "min_closed_cycles": 30,
            "min_complete_ratio": 0.90,
            "min_advantage_usd_per_hour": 0.0,
        },
        "net_shadow": {
            "min_reward_samples": 1, "min_uptime_samples": 1,
            "min_markout_samples": 1, "min_recovery_samples": 1,
            "reward_realization_prior": 0.5, "uptime_prior": 0.5,
            "markout_cost_per_hour_prior": 0.2,
            "recovery_cost_per_hour_prior": 0.3,
            "min_closed_samples": 3,
        },
    }
    sc.update(overrides)
    return {"scanner": sc, "quoting": {"max_capital_per_market": 50,
                                         "offset_frac_of_max_spread": 0.35}}


def test_net_outcome_sort_when_gate_passed(monkeypatch):
    """When gate passes, markets sort by net_shadow_score descending."""
    # Two markets: "low_net" has worse shadow score but better legacy density.
    # A correct net_outcome sort puts "high_net" first.
    low_net = _mk("low_net", pool=300, liquidity=5000, volume_24h=0)
    high_net = _mk("high_net", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [high_net, low_net])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    shadow = {
        "low_net": {"reward_realization": 0.1, "reward_samples": 1,
                    "uptime_ratio": 0.1, "uptime_samples": 1,
                    "markout_cost_per_hour": 2.0, "markout_samples": 1,
                    "recovery_cost_per_hour": 1.0, "recovery_samples": 1,
                    "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
        "high_net": {"reward_realization": 1.0, "reward_samples": 1,
                     "uptime_ratio": 1.0, "uptime_samples": 1,
                     "markout_cost_per_hour": 0.0, "markout_samples": 1,
                     "recovery_cost_per_hour": 0.0, "recovery_samples": 1,
                     "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
    }

    # Gate: realized_count=35 (>30), complete_utc_days=14 (=14),
    # complete_ratio=1.0 (>0.90), advantage > min_advantage
    outcome = {
        "realized_count": 35,
        "resolved_count": 35,
        "complete_ratio": 1.0,
        "lookback_days": 14,
        "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,  # > min_advantage (0.0)
        "advantage_bootstrap_95ci_lower": 0.10,  # > 0, statistically significant
        "total_closed_cycles": 35,
        "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,   # non-None → passes existence check
        "negative_markout_300s_rate": 0.0,  # non-None → passes existence check
        "max_single_exposure_pct": 0.0,  # non-None → passes existence check
        "consecutive_clean_days": 3,  # ≥3 clean days
        "fill_closure_gaps": 0,
        "duplicate_fill_count": 0,
        "reward_double_count": 0,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs=shadow,
                        outcome_report=outcome)

    assert [m.condition_id for m in ranked] == ["high_net", "low_net"]
    assert all(m.selection_reason == "net_outcome" for m in ranked)
    # Verify it's shadow score order: high_net shadow is positive, low_net is negative
    assert ranked[0].net_shadow_score > ranked[1].net_shadow_score


def test_legacy_sort_when_gate_insufficient_closed_cycles(monkeypatch):
    """Gate fails on closed_cycles -> fall back to legacy score (density)."""
    high_density = _mk("high_den", pool=300, liquidity=5000, volume_24h=0)
    low_density = _mk("low_den", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [low_density, high_density])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    shadow = {
        "high_den": {"reward_realization": 0.1, "reward_samples": 1,
                     "uptime_ratio": 0.1, "uptime_samples": 1,
                     "markout_cost_per_hour": 2.0, "markout_samples": 1,
                     "recovery_cost_per_hour": 1.0, "recovery_samples": 1,
                     "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
        "low_den": {"reward_realization": 1.0, "reward_samples": 1,
                    "uptime_ratio": 1.0, "uptime_samples": 1,
                    "markout_cost_per_hour": 0.0, "markout_samples": 1,
                    "recovery_cost_per_hour": 0.0, "recovery_samples": 1,
                    "taker_fee_per_hour": 0.0, "taker_fee_samples": 0},
    }

    # Gate: total_closed_cycles=20 (<30 required) — fails on closed cycles
    outcome = {
        "realized_count": 20,
        "resolved_count": 20,
        "incomplete_count": 5,
        "complete_ratio": 0.80,
        "lookback_days": 14,
        "complete_utc_days": 14,
        "total_closed_cycles": 20,
        "cids_with_min_cycles": 1,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0,
        "duplicate_fill_count": 0,
        "reward_double_count": 0,
        "advantage_bootstrap_95ci_lower": 0.10,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs=shadow,
                        outcome_report=outcome)

    # Falls back to legacy density: high_density (pool=300) > low_density (pool=100)
    assert [m.condition_id for m in ranked] == ["high_den", "low_den"]
    assert all(m.selection_reason == "insufficient_closed_cycles" for m in ranked)


def test_legacy_sort_when_gate_insufficient_cids_with_cycles(monkeypatch):
    """Gate: enough cycles total but too few CIDs each with ≥3 cycles."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 40, "resolved_count": 40,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "total_closed_cycles": 40,  # sufficient
        "cids_with_min_cycles": 1,  # < 3 required → only one market has ≥3 cycles
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
        "advantage_bootstrap_95ci_lower": 0.10,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert [m.condition_id for m in ranked] == ["a", "b"]
    assert all(m.selection_reason == "insufficient_cids_with_cycles" for m in ranked)


def test_legacy_sort_when_gate_insufficient_utc_days(monkeypatch):
    """Gate fails on utc_days -> fall back, reason code is specific."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 40,
        "resolved_count": 40,
        "incomplete_count": 2,
        "complete_ratio": 0.95,
        "lookback_days": 14,
        "complete_utc_days": 7,  # < 14 required
        "total_closed_cycles": 40,
        "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0,
        "duplicate_fill_count": 0,
        "reward_double_count": 0,
        "advantage_bootstrap_95ci_lower": 0.10,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert [m.condition_id for m in ranked] == ["a", "b"]
    assert all(m.selection_reason == "insufficient_utc_days" for m in ranked)


def test_legacy_sort_when_gate_insufficient_complete_ratio(monkeypatch):
    """Gate fails on complete_ratio -> fall back, reason code is specific."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35,
        "resolved_count": 35,
        "incomplete_count": 10,
        "complete_ratio": 0.78,  # < 0.90 required
        "lookback_days": 14,
        "complete_utc_days": 14,
        "total_closed_cycles": 35,
        "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0,
        "duplicate_fill_count": 0,
        "reward_double_count": 0,
        "advantage_bootstrap_95ci_lower": 0.10,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert [m.condition_id for m in ranked] == ["a", "b"]
    assert all(m.selection_reason == "insufficient_complete_ratio" for m in ranked)


def test_legacy_sort_when_no_outcome_data(monkeypatch):
    """No outcome report at all -> fall back, reason = no_outcome_data."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=None)

    assert [m.condition_id for m in ranked] == ["a", "b"]
    assert all(m.selection_reason == "no_outcome_data" for m in ranked)


def test_legacy_mode_ignores_gate_and_outcome_report(monkeypatch):
    """When selection_mode=legacy, gate/outcome are not consulted at all."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    # Config with selection_mode=legacy (no net_outcome_gate)
    ranked = gamma.scan(_scan_cfg(), full=True, outcome_report={
        "realized_count": 0, "resolved_count": 0, "complete_ratio": 0.0,
        "lookback_days": 1, "complete_utc_days": 0,
    })

    assert [m.condition_id for m in ranked] == ["a", "b"]
    assert all(m.selection_reason == "legacy" for m in ranked)


def test_insufficient_advantage_reason_code(monkeypatch):
    """Gate fails on advantage threshold with specific reason code."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35,
        "resolved_count": 35,
        "incomplete_count": 2,
        "complete_ratio": 0.95,
        "lookback_days": 14,
        "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": -0.25,  # negative = worse than legacy
        "advantage_bootstrap_95ci_lower": 0.01,  # CI exists, but point estimate fails
        "total_closed_cycles": 35,
        "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0,
        "duplicate_fill_count": 0,
        "reward_double_count": 0,
    }

    ranked = gamma.scan(
        _net_cfg(net_outcome_gate={**_net_cfg()["scanner"]["net_outcome_gate"],
                                   "min_advantage_usd_per_hour": 0.0}),
        full=True, shadow_inputs={}, outcome_report=outcome)

    assert all(m.selection_reason == "insufficient_advantage" for m in ranked)


def test_shadow_score_computed_even_in_legacy_mode(monkeypatch):
    """net_shadow_score is always computed (P2.1 is observational) regardless
    of selection_mode — even in legacy mode with no outcome_report."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    cfg = _scan_cfg(net_shadow={
        "min_reward_samples": 1, "min_uptime_samples": 1,
        "min_markout_samples": 1, "min_recovery_samples": 1,
        "reward_realization_prior": 0.5, "uptime_prior": 0.5,
        "markout_cost_per_hour_prior": 0.2,
        "recovery_cost_per_hour_prior": 0.3,
    })
    ranked = gamma.scan(cfg, shadow_inputs={
        "a": {"reward_realization": 0.8, "reward_samples": 1,
              "uptime_ratio": 0.9, "uptime_samples": 1,
              "markout_cost_per_hour": 0.3, "markout_samples": 1,
              "recovery_cost_per_hour": 0.1, "recovery_samples": 1,
              "taker_fee_per_hour": 0.0, "taker_fee_samples": 0}
    })

    assert len(ranked) == 1
    assert ranked[0].net_shadow_score != 0.0
    assert ranked[0].selection_reason == "legacy"


# ── P2.2 gate regression tests ──


def test_risk_metrics_none_fails_gate_insufficient_risk_data(monkeypatch):
    """When risk metrics are None (pipeline not built), gate fails closed."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": None,          # pipeline not built yet
        "negative_markout_300s_rate": None,  # pipeline not built yet
        "max_single_exposure_pct": None,    # pipeline not built yet
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert all(m.selection_reason == "insufficient_risk_data" for m in ranked)


def test_non_finite_hedge_rate_rejected(monkeypatch):
    """NaN forced_hedge_rate must not silently pass comparisons."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": float("nan"),
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "non_finite_forced_hedge_rate" for m in ranked)


def test_non_finite_advantage_rejected(monkeypatch):
    """NaN advantage must not silently pass comparisons."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": float("nan"),
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "non_finite_advantage" for m in ranked)


def test_non_finite_ci_lower_rejected(monkeypatch):
    """NaN CI lower bound must not silently pass comparisons."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": float("nan"),
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "non_finite_advantage_ci" for m in ranked)


def test_max_hedge_rate_zero_respected_in_config(monkeypatch):
    """Setting max_forced_hedge_rate=0 must be respected, not replaced by default 0.50."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.01,  # > 0 should fail
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }

    cfg = _net_cfg(net_outcome_gate={**_net_cfg()["scanner"]["net_outcome_gate"],
                                     "max_forced_hedge_rate": 0})
    ranked = gamma.scan(cfg, full=True, shadow_inputs={}, outcome_report=outcome)
    assert all(m.selection_reason == "excessive_forced_hedge_rate" for m in ranked)


def test_risk_threshold_rejection_forced_hedge_rate(monkeypatch):
    """forced_hedge_rate > max → excessive_forced_hedge_rate."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.80,  # > 0.50 default max
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "excessive_forced_hedge_rate" for m in ranked)


def test_risk_threshold_rejection_negative_markout(monkeypatch):
    """negative_markout_300s_rate > max → excessive_negative_markout_rate."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.50,  # > 0.30 default max
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "excessive_negative_markout_rate" for m in ranked)


def test_risk_threshold_rejection_max_exposure(monkeypatch):
    """max_single_exposure_pct > max → excessive_single_market_exposure."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.75,  # > 0.50 default max
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "excessive_single_market_exposure" for m in ranked)


def test_integrity_clean_days_below_minimum(monkeypatch):
    """consecutive_clean_days < 3 → insufficient_clean_days."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 1,  # < 3 required
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "insufficient_clean_days" for m in ranked)


def test_integrity_closure_gaps_reject(monkeypatch):
    """fill_closure_gaps > 0 → fill_closure_gaps."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 2,  # > 0 → reject
        "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "fill_closure_gaps" for m in ranked)


def test_integrity_duplicate_fills_reject(monkeypatch):
    """duplicate_fill_count > 0 → duplicate_fills."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 1, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "duplicate_fills" for m in ranked)


def test_integrity_reward_double_count_reject(monkeypatch):
    """reward_double_count > 0 → reward_double_count."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 1,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "reward_double_count" for m in ranked)


def test_significance_fails_without_ci_lower(monkeypatch):
    """Point estimate present but CI lower bound absent → insufficient_significance."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        # "advantage_bootstrap_95ci_lower" deliberately absent → None
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "insufficient_significance" for m in ranked)


def test_significance_fails_ci_lower_below_zero(monkeypatch):
    """Bootstrap 95% CI lower bound ≤ 0 → insufficient_significance."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,  # point estimate positive
        "advantage_bootstrap_95ci_lower": -0.05,    # but CI crosses zero
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "insufficient_significance" for m in ranked)
def test_integrity_fields_none_fails_gate_insufficient_integrity_data(monkeypatch):
    """When data-integrity fields are None, gate fails closed."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": None,     # pipeline not built yet
        "fill_closure_gaps": None,
        "duplicate_fill_count": None,
        "reward_double_count": None,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert all(m.selection_reason == "insufficient_integrity_data" for m in ranked)


def test_outcome_report_missing_risk_fields_fails_gate(monkeypatch):
    """gate survivable outcome dicts must not miss fields — missing = None = fail."""
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    # Missing forced_hedge_rate entirely (= None via .get default)
    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        # "forced_hedge_rate" deliberately absent
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }

    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)

    assert all(m.selection_reason == "insufficient_risk_data" for m in ranked)


def test_non_finite_int_field_rejected(monkeypatch):
    """NaN in an int report field (e.g. consecutive_clean_days) must be rejected.

    Pipeline bugs or Data API edge-cases can produce float('nan') in a field
    that should be an integer.  The gate must treat this as non-finite, not
    silently allow it through integer comparisons.
    """
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.0,
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": float("nan"),  # non-finite int field
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }
    ranked = gamma.scan(_net_cfg(), full=True, shadow_inputs={},
                        outcome_report=outcome)
    assert all(m.selection_reason == "non_finite_consecutive_clean_days"
               for m in ranked)


def test_non_finite_config_threshold_rejected(monkeypatch):
    """NaN in gate config must be rejected — must not silently disable threshold.

    ``max_forced_hedge_rate: .nan`` makes ``rate > nan`` always False, so every
    report passes.  The gate must detect and reject this at config-read time.
    """
    a = _mk("a", pool=300, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [a])
    monkeypatch.setattr(gamma, "_fetch_market_fees", lambda *a: (0, 1.0))

    outcome = {
        "realized_count": 35, "resolved_count": 35,
        "complete_ratio": 0.95, "lookback_days": 14, "complete_utc_days": 14,
        "net_outcome_advantage_usd_per_hour": 0.50,
        "advantage_bootstrap_95ci_lower": 0.10,
        "total_closed_cycles": 35, "cids_with_min_cycles": 5,
        "forced_hedge_rate": 0.99,  # would normally fail with max=0.50
        "negative_markout_300s_rate": 0.0,
        "max_single_exposure_pct": 0.0,
        "consecutive_clean_days": 3,
        "fill_closure_gaps": 0, "duplicate_fill_count": 0, "reward_double_count": 0,
    }

    cfg = _net_cfg(net_outcome_gate={**_net_cfg()["scanner"]["net_outcome_gate"],
                                     "max_forced_hedge_rate": float("nan")})
    ranked = gamma.scan(cfg, full=True, shadow_inputs={}, outcome_report=outcome)
    assert all(m.selection_reason == "non_finite_config_threshold" for m in ranked)

