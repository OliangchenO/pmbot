"""Market scanner: finds reward-paying markets worth quoting via the Gamma API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("pmbot.gamma")

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"
SCAN_PAGES = 20
TIMEOUT = httpx.Timeout(15.0)
GAMMA_FETCH_ATTEMPTS = 3


@dataclass
class Market:
    question: str
    condition_id: str
    yes_token: str
    no_token: str
    min_size: float
    max_spread_cents: float
    daily_pool: float
    liquidity: float
    volume_24h: float
    tick: float
    end_date: datetime | None
    neg_risk: bool
    event_id: str | None = None
    slug: str = ""
    # Taker fee rate in bps (fd.r × 10000) and its exponent (fd.e), from the
    # CLOB clob-markets endpoint. Makers are never charged on Polymarket, so
    # this only prices crossing the spread on a merge/exit.
    fee_bps: int = 0
    fee_exponent: float = 1.0
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade: float | None = None
    score: float = field(default=0.0)
    density: float = field(default=0.0)   # raw pool/liquidity (always computed)
    capture: float = field(default=0.0)   # expected captured reward $/day (capture mode only)
    net_shadow_score: float = field(default=0.0)  # expected net $/hour; observation only
    net_shadow_inputs: dict[str, object] = field(default_factory=dict)
    selection_reason: str = field(default="legacy")  # how this market was selected / why not

    @property
    def mid_hint(self) -> float:
        if self.best_bid is not None and self.best_ask is not None and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return self.last_trade if self.last_trade is not None else 0.5


def _parse_market(m: dict, require_rewards: bool = True) -> Market | None:
    rewards = m.get("clobRewards") or []
    daily_pool = sum(float(r.get("rewardsDailyRate") or 0) for r in rewards)
    if require_rewards and daily_pool <= 0:
        return None
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if len(token_ids) != 2:
        return None
    end_raw = m.get("endDate")
    end_date = None
    if end_raw:
        try:
            end_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    def _f(key: str) -> float | None:
        v = m.get(key)
        return float(v) if v is not None else None

    event_id = None
    for key in ("eventId", "eventSlug", "groupItemTitle"):
        if m.get(key):
            event_id = str(m[key])
            break
    if event_id is None and m.get("events"):
        ev = m["events"]
        if isinstance(ev, list) and ev:
            event_id = str(ev[0].get("id") or ev[0].get("slug") or "")

    return Market(
        question=m.get("question") or "",
        condition_id=m.get("conditionId") or "",
        yes_token=str(token_ids[0]),
        no_token=str(token_ids[1]),
        min_size=float(m.get("rewardsMinSize") or 0),
        max_spread_cents=float(m.get("rewardsMaxSpread") or 0),
        daily_pool=daily_pool,
        liquidity=float(m.get("liquidityNum") or 0),
        volume_24h=float(m.get("volume24hr") or 0),
        tick=float(m.get("orderPriceMinTickSize") or 0.01),
        end_date=end_date,
        neg_risk=bool(m.get("negRisk")),
        event_id=event_id or None,
        slug=str(m.get("slug") or ""),
        best_bid=_f("bestBid"),
        best_ask=_f("bestAsk"),
        last_trade=_f("lastTradePrice"),
    )


def fetch_market(condition_id: str) -> Market | None:
    """Load one market by condition id, including non-reward held positions."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(GAMMA_URL, params={"condition_ids": condition_id})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("查询持仓市场失败（%s…）：%s", condition_id[:12], e)
        return None

    rows = payload if isinstance(payload, list) else [payload]
    for raw in rows:
        if isinstance(raw, dict):
            parsed = _parse_market(raw, require_rewards=False)
            if parsed and parsed.condition_id == condition_id:
                return parsed
    log.warning("持仓市场查询未返回可用市场（%s…）", condition_id[:12])
    return None


def _fetch_market_fees(
    condition_id: str, cache: dict[str, tuple[int, float] | None],
    attempts: int = 2, backoff: float = 0.5,
) -> tuple[int, float]:
    """Per-market TAKER fee from the CLOB clob-markets endpoint.

    Returns (taker_fee_bps, exponent). Polymarket charges only takers (fd.to),
    so this rate prices crossing the spread on a forced hedge or merge/exit —
    our resting maker quotes pay nothing regardless. We read fd.r directly
    because the legacy /fee-rate endpoint (and the mbf/tbf base-fee fields)
    report a flat 1000 that does not reflect the real fee rate.

    Robustness: the lookup is retried a few times, and on persistent failure it
    FAILS OPEN with (0 bps, 1.0) rather than dropping the market. Since we earn
    rewards purely as a maker (zero fee), a transient inability to read the
    taker fee must not cost us an otherwise-good market — that previously left
    a quoting slot empty for a full refresh cycle. The only thing we lose
    visibility into on failure is the taker cost of a rare forced hedge/exit;
    the ``max_fee_bps`` guard still applies whenever the rate is readable.
    """
    if condition_id in cache and cache[condition_id] is not None:
        return cache[condition_id]
    last_err: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{CLOB_URL}/clob-markets/{condition_id}")
                resp.raise_for_status()
                fd = resp.json().get("fd") or {}
            rate = float(fd.get("r") or 0.0)
            exponent = float(fd.get("e") or 1.0)
            result = (round(rate * 10000), exponent)
            cache[condition_id] = result
            return result
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i + 1 < attempts:
                time.sleep(backoff * (i + 1))
    log.warning("查询 CLOB 手续费失败（%s…），已重试 %d 次（%s）；"
                "assuming 0bps taker fee (makers pay no fee, so we still quote)",
                condition_id[:12], attempts, last_err)
    result = (0, 1.0)
    cache[condition_id] = result
    return result


# def fetch_reward_markets() -> list[Market]:
#     """Fetch active markets with a nonzero daily reward pool."""
#     seen: set[str] = set()
#     markets: list[Market] = []
#     # Gamma rejects ``rewards_min_size`` combined with ``order=rewardsDailyRate``
#     # (HTTP 422). We rank locally in ``scan``, so one un-ordered, reward-only
#     # pagination pass is both sufficient and compatible with the live API.
#     orderings = [None]
#     with httpx.Client(timeout=TIMEOUT) as client:
#         for order in orderings:
#             for page in range(SCAN_PAGES):
#                 batch = None
#                 for attempt in range(GAMMA_FETCH_ATTEMPTS):
#                     try:
#                         resp = client.get(
#                             GAMMA_URL,
#                             params={
#                                 "active": "true",
#                                 "closed": "false",
#                                 "limit": 100,
#                                 "offset": page * 100,
#                                 "order": order
#                             },
#                         )
#                         resp.raise_for_status()
#                         batch = resp.json()
#                         break
#                     except Exception as e:  # noqa: BLE001
#                         if attempt + 1 < GAMMA_FETCH_ATTEMPTS:
#                             log.warning(
#                                 "gamma fetch transient error (order=%s page=%d): %s; retry %d/%d",
#                                 "default", page, e, attempt + 1, GAMMA_FETCH_ATTEMPTS - 1,
#                             )
#                             time.sleep(0.3 * (attempt + 1))
#                         else:
#                             log.warning(
#                                 "gamma fetch failed (order=%s page=%d) after %d attempts: %s",
#                                 "default", page, GAMMA_FETCH_ATTEMPTS, e,
#                             )
#                 if batch is None:
#                     break
#                 if not batch:
#                     break
#                 for raw in batch:
#                     parsed = _parse_market(raw)
#                     if parsed and parsed.condition_id not in seen:
#                         seen.add(parsed.condition_id)
#                         markets.append(parsed)
#     return markets

def fetch_reward_markets() -> list[Market]:
    """Fetch active markets with a nonzero daily reward pool."""
    seen: set[str] = set()
    markets: list[Market] = []
    orderings = [
        ("rewardsDailyRate", "false"),
        ("volume24hr", "false"),
    ]
    with httpx.Client(timeout=TIMEOUT) as client:
        for order, ascending in orderings:
            for page in range(SCAN_PAGES):
                try:
                    resp = client.get(
                        GAMMA_URL,
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": 100,
                            "offset": page * 100,
                            "order": order,
                            "ascending": ascending,
                        },
                    )
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.debug("Gamma 市场列表请求失败（排序=%s，页=%d）：%s", order, page, e)
                    break
                if not batch:
                    break
                for raw in batch:
                    parsed = _parse_market(raw)
                    if parsed and parsed.condition_id not in seen:
                        seen.add(parsed.condition_id)
                        markets.append(parsed)
    return markets


def _check_net_outcome_gate(cfg: dict, outcome_report: dict | None) -> tuple[bool, str]:
    """Check whether the net-outcome gate thresholds are all met.

    Returns (passed, reason_code).  ``outcome_report`` is the dict returned by
    ``MetricsStore.outcome_report()``; when ``None`` (e.g. no database yet) the
    gate returns ``(False, "no_outcome_data")`` so selection always falls back
    to legacy scoring.

    Reason codes (stable — usable in tests and logging):

      Data-availability (fail-closed when pipeline not yet built):
        * ``"no_outcome_data"`` — report unavailable or core fields missing
        * ``"insufficient_risk_data"`` — risk metrics (hedge/markout/exposure)
          not yet computed
        * ``"insufficient_integrity_data"`` — data-integrity fields
          (clean_days/gaps/dupes/double) not yet tracked
        * ``"non_finite_config_threshold"`` — a gate threshold in config is
          NaN/infinity, which would silently disable the threshold

      Sample-size thresholds:
        * ``"insufficient_utc_days"`` — complete_utc_days < min_utc_days
        * ``"insufficient_closed_cycles"`` — total_closed_cycles < min_closed_cycles
        * ``"insufficient_cids_with_cycles"`` — cids_with_min_cycles < min_cids
        * ``"insufficient_complete_ratio"`` — complete_ratio < min_complete_ratio

      Risk thresholds (conservative absolute floors):
        * ``"excessive_forced_hedge_rate"`` — forced_hedge_rate > max_forced_hedge_rate
        * ``"excessive_negative_markout_rate"`` — negative_markout_300s_rate
          > max_negative_markout_300s_rate
        * ``"excessive_single_market_exposure"`` — max_single_exposure_pct
          > max_single_market_exposure_pct

      Data-integrity thresholds:
        * ``"insufficient_clean_days"`` — consecutive_clean_days < min_consecutive_clean_days
        * ``"fill_closure_gaps"`` — fill_closure_gaps > 0
        * ``"duplicate_fills"`` — duplicate_fill_count > 0
        * ``"reward_double_count"`` — reward_double_count > 0

      Advantage and statistical significance:
        * ``"insufficient_advantage"`` — net_outcome_advantage_usd_per_hour
          <= min_advantage_usd_per_hour
        * ``"insufficient_significance"`` — advantage_bootstrap_95ci_lower
          absent or <= 0

      * ``"passed"`` — all thresholds met

    The gate checks are conservative: every threshold must be met, and missing
    data (e.g. no advantage pre-computed) always fails the gate rather than
    assuming the data would pass.  This matches the design doc's P2.2 effect
    thresholds — the gate is merely the code encoding of those thresholds.
    """
    gate_cfg = (cfg.get("scanner") or {}).get("net_outcome_gate") or {}

    if outcome_report is None:
        return False, "no_outcome_data"

    # Required fields — if any are missing the gate cannot be evaluated.
    # All fields are checked for presence; None on any field means the
    # corresponding data pipeline is not yet capable of producing the
    # metric → fail-closed rather than assuming safety.
    #
    # Non-finite values (NaN, +∞, -∞) are also rejected.  Python's IEEE 754
    # semantics mean ``nan > threshold`` is False, so a NaN value would
    # silently pass through every numeric comparison and reach "passed".
    # We guard against this by checking every metric value with isfinite().
    realized_count = outcome_report.get("realized_count")
    complete_utc_days = outcome_report.get("complete_utc_days")
    complete_ratio = outcome_report.get("complete_ratio")
    total_closed_cycles = outcome_report.get("total_closed_cycles")
    cids_with_min_cycles = outcome_report.get("cids_with_min_cycles")
    forced_hedge_rate = outcome_report.get("forced_hedge_rate")
    neg_markout_rate = outcome_report.get("negative_markout_300s_rate")
    max_exposure_pct = outcome_report.get("max_single_exposure_pct")
    consecutive_clean_days = outcome_report.get("consecutive_clean_days")
    fill_closure_gaps = outcome_report.get("fill_closure_gaps")
    duplicate_fill_count = outcome_report.get("duplicate_fill_count")
    reward_double_count = outcome_report.get("reward_double_count")
    advantage = outcome_report.get("net_outcome_advantage_usd_per_hour")
    advantage_ci_lower = outcome_report.get("advantage_bootstrap_95ci_lower")

    if (realized_count is None or complete_utc_days is None
            or complete_ratio is None
            or total_closed_cycles is None
            or cids_with_min_cycles is None):
        return False, "no_outcome_data"

    if (forced_hedge_rate is None or neg_markout_rate is None
            or max_exposure_pct is None):
        return False, "insufficient_risk_data"

    if (consecutive_clean_days is None or fill_closure_gaps is None
            or duplicate_fill_count is None or reward_double_count is None):
        return False, "insufficient_integrity_data"

    # ── non-finite guard (must precede all numeric comparisons) ──
    # Python: nan > x and nan <= 0 are both False.  A NaN metric value
    # would pass through every threshold check and reach "passed".
    # Every numeric field from the report is validated, including int
    # fields that could become NaN via float coercion in the pipeline.
    for val, label in (
        # float report fields
        (forced_hedge_rate, "forced_hedge_rate"),
        (neg_markout_rate, "negative_markout_300s_rate"),
        (max_exposure_pct, "max_single_exposure_pct"),
        (complete_ratio, "complete_ratio"),
        # int report fields (NaN coerces to float which is non-finite)
        (complete_utc_days, "complete_utc_days"),
        (total_closed_cycles, "total_closed_cycles"),
        (cids_with_min_cycles, "cids_with_min_cycles"),
        (consecutive_clean_days, "consecutive_clean_days"),
        (fill_closure_gaps, "fill_closure_gaps"),
        (duplicate_fill_count, "duplicate_fill_count"),
        (reward_double_count, "reward_double_count"),
        # advantage fields
        (advantage, "advantage"),
        (advantage_ci_lower, "advantage_ci"),
    ):
        if val is None:
            continue  # already caught by existence checks above
        reason = _refuse_non_finite(float(val), label)
        if reason:
            return False, reason

    # ── bootstrap CI no-longer-None check ──
    if advantage_ci_lower is None:
        return False, "insufficient_significance"

    try:
        min_utc_days = _gate_cfg_int(gate_cfg, "min_utc_days", 14)
        min_closed = _gate_cfg_int(gate_cfg, "min_closed_cycles", 30)
        min_ratio = _gate_cfg_float(gate_cfg, "min_complete_ratio", 0.90)
        min_adv = _gate_cfg_float(gate_cfg, "min_advantage_usd_per_hour", 0.0)
        min_cids = _gate_cfg_int(gate_cfg, "min_cids_with_cycles", 3)
    except ValueError:
        return False, "non_finite_config_threshold"

    # complete_utc_days counts UTC days with at least one realized market having
    # activity (fills, merges, or a non-zero inventory snapshot). This is NOT the
    # same as lookback_days — a 14-day lookback may only have 3 active UTC days.
    if complete_utc_days < min_utc_days:
        return False, "insufficient_utc_days"
    if total_closed_cycles < min_closed:
        return False, "insufficient_closed_cycles"
    if cids_with_min_cycles < min_cids:
        return False, "insufficient_cids_with_cycles"
    if complete_ratio < min_ratio:
        return False, "insufficient_complete_ratio"

    # ── risk checks (design doc §5.2) ──
    # "shadow Top-N 的强制对冲率、300 秒负 markout 率、最大单市场暴露
    # 均不劣于 legacy".  The same-eligibility comparison requires the
    # post-hoc pipeline; the gate imposes conservative absolute floors
    # on whatever values the future pipeline produces.  Passing None
    # (pipeline not built) is blocked by the existence check above.
    try:
        max_hedge = _gate_cfg_float(gate_cfg, "max_forced_hedge_rate", 0.50)
        max_neg_mo = _gate_cfg_float(gate_cfg, "max_negative_markout_300s_rate", 0.30)
        max_expo = _gate_cfg_float(gate_cfg, "max_single_market_exposure_pct", 0.50)
    except ValueError:
        return False, "non_finite_config_threshold"
    if forced_hedge_rate > max_hedge:
        return False, "excessive_forced_hedge_rate"
    if neg_markout_rate > max_neg_mo:
        return False, "excessive_negative_markout_rate"
    if max_exposure_pct > max_expo:
        return False, "excessive_single_market_exposure"

    # ── data-integrity checks (design doc §5.2) ──
    # "连续 3 个 UTC 日没有 fill_without_inventory_closure、重复 fill
    # 或 reward double-count".  Gate enforces minimum consecutive clean
    # days and zero tolerance for closure gaps / dupes.
    try:
        min_clean_days = _gate_cfg_int(gate_cfg, "min_consecutive_clean_days", 3)
    except ValueError:
        return False, "non_finite_config_threshold"
    if consecutive_clean_days < min_clean_days:
        return False, "insufficient_clean_days"
    if fill_closure_gaps > 0:
        return False, "fill_closure_gaps"
    if duplicate_fill_count > 0:
        return False, "duplicate_fills"
    if reward_double_count > 0:
        return False, "reward_double_count"

    # ── advantage and significance check ──
    # Design doc §5.2 requires both a positive point estimate AND
    # bootstrap 95% CI lower bound > 0.  A point estimate without
    # a confidence interval is not statistically meaningful.
    if advantage is None or advantage <= min_adv:
        return False, "insufficient_advantage"
    if advantage_ci_lower is None or advantage_ci_lower <= 0:
        return False, "insufficient_significance"

    return True, "passed"


def _gate_cfg_float(cfg: dict, key: str, default: float) -> float:
    """Read a float gate config value, respecting explicit zero.

    ``or`` chains replace 0.0 with the RHS, so we must distinguish "key absent"
    from "key present with value 0".  This helper returns the explicit value
    (including 0) when the key exists, and the default otherwise.

    Non-finite config values (NaN, infinity) are rejected: they make every
    ``rate > threshold`` comparison False, silently removing the threshold.
    A rejected value raises ValueError so the caller can return a clear
    reason code rather than silently disabling protection.
    """
    import math

    val = cfg.get(key)
    if val is not None:
        try:
            f = float(val)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(f):
            raise ValueError(f"non_finite_config_threshold:{key}")
        return f
    return default


def _gate_cfg_int(cfg: dict, key: str, default: int) -> int:
    """Read an int gate config value, respecting explicit zero.

    Non-finite values are rejected the same way as _gate_cfg_float.

    The non-finite check uses ``float(val)`` *before* ``int(val)`` because
    ``int(float('nan'))`` raises ``ValueError`` — which the except clause
    would silently convert to ``default``, defeating the guard.
    """
    import math

    val = cfg.get(key)
    if val is not None:
        try:
            f = float(val)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(f):
            raise ValueError(f"non_finite_config_threshold:{key}")
        try:
            return int(val)
        except (TypeError, ValueError):
            return default
    return default


def _refuse_non_finite(value: float, label: str) -> str | None:
    """Return a reason code if *value* is non-finite, otherwise None.

    NaN and infinities are numeric but not meaningful metric values.
    ``nan > threshold`` and ``nan <= 0`` are both False, so a NaN value
    can silently pass through every comparison gate.  This check must be
    the first thing done with any pipeline-produced float.
    """
    import math
    if not math.isfinite(value):
        return f"non_finite_{label}"
    return None


def scan(cfg: dict, exclude_cids: set[str] | None = None,
         full: bool = False,
         shadow_inputs: dict[str, dict[str, float | int]] | None = None,
         outcome_report: dict | None = None) -> list[Market]:
    """Filter and rank reward markets per scanner config. Returns best first.

    ``exclude_cids`` skips specific markets before ranking, so the next-best
    eligible markets backfill the top_n slots — used to rotate out of a
    guard-tripped market into a fresh one instead of wasting the slot.

    ``full=True`` returns every eligible market (best first) instead of just the
    top_n slice, so the caller can run its own sticky selection (keep markets
    we are already quoting unless a candidate is materially better). Fee lookups
    already run for every market that clears the cheap filters, so returning the
    full list costs nothing extra.

    ``outcome_report`` is the dict from ``MetricsStore.outcome_report()``. When
    ``selection_mode`` is ``"net_outcome"`` the gate thresholds inside
    ``scanner.net_outcome_gate`` are checked against this report; if all pass,
    candidates are sorted by ``net_shadow_score`` instead of legacy ``score``.
    When ``None`` or when the gate fails, legacy scoring is used and each
    market's ``selection_reason`` records why.
    """
    sc = cfg["scanner"]
    skip_cids = exclude_cids or set()
    lo, hi = sc["mid_range"]
    min_end = datetime.now(timezone.utc) + timedelta(hours=sc["min_hours_to_end"])
    exclude = [k.lower() for k in sc.get("exclude_keywords") or []]
    max_capital = cfg["quoting"]["max_capital_per_market"]
    # Absolute book-liquidity floor (USD). The reward-density ranking
    # (pool ÷ liquidity) structurally prefers thin books, which then trip the
    # volatility / book-not-quotable guards and tank in-band uptime. This floor
    # drops books too shallow to two-side without getting picked off. 0 disables.
    min_liquidity = float(sc.get("min_liquidity", 0.0))
    fee_penalty = float(sc.get("fee_penalty_mult", 0.5))
    max_fee_bps = int(sc.get("max_fee_bps", 0))
    # Ranking mode: "density" = pool/liquidity (current behavior, ignores
    # toxicity/band weights); "capture" = expected captured reward with
    # toxicity/band multipliers active.
    ranking_mode = str(sc.get("ranking_mode", "density")).lower()
    gamma = float(sc.get("competition_gamma", 0.0))
    # Knock-down factors for high-turnover / thin-band markets. In density mode
    # these are pinned to 1.0 (no effect), guaranteeing identical ranking to
    # the current behavior regardless of their config values.
    turnover_w = float(sc.get("toxicity_turnover_penalty", 0.0))
    band_w = float(sc.get("band_room_bonus", 0.0))

    fee_cache: dict[str, tuple[int, float] | None] = {}
    candidates = []
    for m in fetch_reward_markets():
        if m.condition_id in skip_cids:
            continue
        if m.daily_pool < sc["min_pool_per_day"]:
            continue
        if m.liquidity < min_liquidity:
            continue
        if m.min_size <= 0 or m.min_size > sc["max_min_size_shares"]:
            continue
        if m.max_spread_cents <= 0:
            continue
        if not (lo <= m.mid_hint <= hi):
            continue
        if m.end_date and m.end_date < min_end:
            continue
        if any(k in m.question.lower() for k in exclude):
            continue
        if m.min_size * 1.0 > max_capital:
            continue
        # Fails open to (0, 1.0) on a persistent lookup error — a missing taker
        # fee never drops a market we can make on as a (zero-fee) maker.
        m.fee_bps, m.fee_exponent = _fetch_market_fees(m.condition_id, fee_cache)
        if m.fee_bps > max_fee_bps:
            # Guard against pathological fee rates. Makers pay no fee on
            # Polymarket, so our reward quotes are unaffected; this only caps
            # markets where the taker merge/exit cost would be extreme.
            continue
        # --- reward density (always computed for eligibility gate and display)
        density = m.daily_pool / max(m.liquidity, 100.0)
        if m.fee_bps > 0:
            density *= max(0.1, 1.0 - m.fee_bps / 10000.0 * fee_penalty)
        m.density = density

        # Eligibility gate on raw reward density — unchanged behavior.
        if density < sc["min_pool_to_liquidity"]:
            continue

        # --- expected captured reward (capture mode only)
        m.capture = 0.0
        if ranking_mode == "capture":
            alpha = (1.0 - float(cfg["quoting"]["offset_frac_of_max_spread"])) ** 2
            our_q = alpha * float(max_capital)
            comp_q = gamma * max(m.liquidity, 100.0)
            denom = our_q + comp_q
            m.capture = m.daily_pool * our_q / denom if denom > 0 else 0.0

        # --- toxicity & band multipliers (capture mode only)
        if ranking_mode == "capture":
            turnover = m.volume_24h / max(m.liquidity, 100.0)
            tox_mult = 1.0 / (1.0 + turnover_w * turnover)
            band_mult = 1.0 + band_w * max(0.0, m.max_spread_cents - 1.0)
            m.score = m.capture * tox_mult * band_mult
        else:
            m.score = density

        candidates.append(m)

    # P2.1 shadows are always computed for audit / monitoring.
    from .strategy import compute_net_shadow_score
    inputs_by_cid = shadow_inputs or {}
    for market in candidates:
        market.net_shadow_score, market.net_shadow_inputs = compute_net_shadow_score(
            market, inputs_by_cid.get(market.condition_id, {}), cfg)

    # ── selection mode ──
    selection_mode = str(sc.get("selection_mode", "legacy")).lower()

    if selection_mode == "net_outcome":
        gate_passed, reason = _check_net_outcome_gate(cfg, outcome_report)
        if gate_passed:
            # Sort by net_shadow_score — the empirically verified per-hour
            # net result. Higher is better.
            for m in candidates:
                m.selection_reason = "net_outcome"
            candidates.sort(key=lambda m: m.net_shadow_score, reverse=True)
        else:
            # Gate not passed — fall back to legacy score and record why.
            for m in candidates:
                m.selection_reason = reason
            candidates.sort(key=lambda m: m.score, reverse=True)
    else:
        # Legacy (density or capture) — unchanged behavior.
        for m in candidates:
            m.selection_reason = "legacy"
        candidates.sort(key=lambda m: m.score, reverse=True)

    if full:
        return candidates
    return candidates[: sc["top_n_markets"]]
