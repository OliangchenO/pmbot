"""Risk manager: daily loss limit, hard kill, inventory caps, toxicity guards."""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal

log = logging.getLogger("pmbot.risk")


ActionLiteral = Literal["allow", "widen", "pull"]


@dataclass(frozen=True)
class QuoteRiskDecision:
    """Pure-function decision for per-market quote admission and width adjustment.

    Determines whether each side's bid should stay (allow), be widened (widen),
    or be pulled entirely (pull). Only affects normal maker quotes — inventory
    recovery orders bypass this module entirely.
    """

    yes_action: ActionLiteral
    no_action: ActionLiteral
    yes_widen: float   # price units to widen the YES bid (0 when action != widen)
    no_widen: float    # price units to widen the NO bid
    reason: str
    score: float       # composite risk score in [0, 1]


class RiskAction(Enum):
    OK = "ok"
    PAUSE_QUOTES = "pause_quotes"  # cancel quotes until risk state is healthy
    PAUSE_DAY = "pause_day"  # cancel quotes, resume next UTC day
    KILL = "kill"  # cancel everything and exit


class RiskManager:
    def __init__(self, cfg: dict, start_equity: float):
        self.cfg = cfg["risk"]
        self.baseline_capital = float(cfg["capital_usd"])
        self.day_start_equity = start_equity
        self.day = self._today()
        self.paused = False
        self._equity_history: deque[float] = deque(maxlen=20)
        self.last_observation: dict[str, float | str] = {}

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def scale(self, equity: float) -> float:
        if not self.cfg.get("scale_with_equity") or equity != equity:
            return 1.0
        raw = equity / max(self.baseline_capital, 1e-9)
        return max(float(self.cfg["scale_min"]), min(float(self.cfg["scale_max"]), raw))

    def _smoothed_equity(self, equity: float) -> float:
        """Median of recent equity readings for loss-limit checks."""
        self._equity_history.append(equity)
        if len(self._equity_history) < 3:
            return equity
        return statistics.median(self._equity_history)

    def check(self, equity: float, total_inventory_usd: float,
              scale: float = 1.0) -> RiskAction:
        if equity != equity:
            log.warning("权益未知，暂停报价直到账户余额和持仓刷新完成")
            return RiskAction.PAUSE_QUOTES
        if self.day_start_equity != self.day_start_equity:
            self.day_start_equity = equity
            log.info("权益基准已设置：$%.2f", equity)

        today = self._today()
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            self._equity_history.clear()
            if self.paused:
                log.info("新的 UTC 日开始，恢复此前暂停的报价")
                self.paused = False

        smoothed = self._smoothed_equity(equity)
        day_loss = self.day_start_equity - smoothed
        self.last_observation = {
            "equity": equity,
            "smoothed_equity": smoothed,
            "day_loss": day_loss,
            "inventory_usd": total_inventory_usd,
        }
        if day_loss >= self.cfg["hard_kill_loss_usd"]:
            log.error("HARD KILL：当日亏损 $%.2f >= $%.2f，停止运行",
                      day_loss, self.cfg["hard_kill_loss_usd"])
            return RiskAction.KILL
        if day_loss >= self.cfg["daily_loss_limit_usd"]:
            if not self.paused:
                log.warning("达到当日亏损限额（$%.2f），暂停至下一个 UTC 日", day_loss)
                self.paused = True
            return RiskAction.PAUSE_DAY
        if self.paused:
            return RiskAction.PAUSE_DAY

        if total_inventory_usd > self.cfg["max_total_inventory_usd"] * scale:
            log.warning("总库存 $%.0f 超过上限，暂停新增报价",
                        total_inventory_usd)
            return RiskAction.PAUSE_QUOTES
        return RiskAction.OK

    def market_inventory_ok(self, net_exposure_usd: float, cap: float | None = None) -> bool:
        if cap is None:
            cap = self.cfg["max_inventory_usd_per_market"]
        return abs(net_exposure_usd) < cap

    def _theme_groups(self) -> dict[str, list[str]]:
        raw = self.cfg.get("theme_groups") or {}
        return {name: [k.lower() for k in keywords] for name, keywords in raw.items()}

    def market_themes(self, market) -> list[str]:
        """Theme names for keyword groups and neg-risk event groups."""
        themes = []
        q = market.question.lower()
        for name, keywords in self._theme_groups().items():
            if any(k in q for k in keywords):
                themes.append(name)
        event_id = getattr(market, "event_id", None)
        if event_id:
            themes.append(f"event:{event_id}")
        return themes

    def theme_exposure_usd(self, theme: str, markets, net_exposure_fn) -> float:
        return sum(
            abs(net_exposure_fn(m))
            for m in markets
            if theme in self.market_themes(m)
        )

    def _theme_cap(self, scale: float) -> float:
        cap = float(self.cfg.get("theme_max_inventory_usd") or 0)
        return cap * scale if cap > 0 else 0.0

    def theme_quoting_ok(self, market, markets, net_exposure_fn, scale: float = 1.0) -> bool:
        cap = self._theme_cap(scale)
        if cap <= 0:
            return True
        if abs(net_exposure_fn(market)) >= 0.01:
            return True
        return all(
            self.theme_exposure_usd(t, markets, net_exposure_fn) < cap
            for t in self.market_themes(market)
        )

    def theme_at_cap(self, market, markets, net_exposure_fn, scale: float = 1.0) -> bool:
        cap = self._theme_cap(scale)
        if cap <= 0:
            return False
        return any(
            self.theme_exposure_usd(t, markets, net_exposure_fn) >= cap
            for t in self.market_themes(market)
        )


class MarketGuards:
    """Per-market circuit breakers against adverse selection.

    Optional callbacks fire when a guard newly trips, so the bot can pull
    quotes immediately instead of waiting for the next loop tick:
      on_trip(condition_id)    — whole-market pause
      on_side_block(token_id)  — single-side pull
    """

    def __init__(self, cfg: dict):
        self.on_trip: Callable[[str], None] | None = None
        self.on_side_block: Callable[[str], None] | None = None
        self._load(cfg)
        self._mids: dict[str, list[tuple[float, float]]] = {}
        self._paused_until: dict[str, float] = {}
        self._trade_times: dict[str, deque] = {}
        self._taker_sides: dict[str, deque] = {}
        self._side_blocked_until: dict[str, float] = {}
        self._flow: dict[str, deque] = {}
        # Correlated-bracket defense: brackets of the same neg-risk event get
        # picked off together (e.g. every "Toy Story 5 box office between X-Y"
        # range). When one bracket trips, the whole event is cooled down so the
        # scanner can't immediately re-enter a sibling and bleed the same way.
        self._event_of: dict[str, str] = {}
        self._paused_events: dict[str, float] = {}
        # Hysteresis state for quote_risk_decision: per-market, per-side last
        # action. "allow" is omitted from the dict to save space — absence = allow.
        self._risk_state: dict[str, dict[Literal["yes", "no"], ActionLiteral]] = {}
        # Pull cooldown: after a side is pulled, a minimum time must pass before
        # it can re-allow via hysteresis alone — prevents re-entry churn when
        # the signal briefly dips below resume_score.
        self._pull_cooldown_until: dict[str, dict[Literal["yes", "no"], float]] = {}

    def _load(self, cfg: dict) -> None:
        g = cfg["guards"]
        self.vol_window = float(g["vol_window_secs"])
        self.vol_move = float(g["vol_max_move_cents"]) / 100.0
        self.max_fills = int(g["max_same_side_fills"])
        self.fill_window = float(g["same_side_window_minutes"]) * 60
        self.cooldown = float(g["market_cooldown_minutes"]) * 60
        self.vel_window = float(g["velocity_window_secs"])
        self.vel_max = int(g["velocity_max_trades"])
        self.dir_consec = int(g["directional_consecutive"])
        self.side_cooldown = float(g["side_cooldown_minutes"]) * 60
        self.flow_window = float(g["flow_window_secs"])
        self.flow_min_vol = float(g["flow_min_volume_shares"])
        self.flow_widen_thr = float(g["flow_widen_threshold"])
        self.flow_pull_thr = float(g["flow_pull_threshold"])
        self.flow_widen_max = float(g["flow_widen_max_cents"]) / 100.0
        # P0: quote-risk decision engine parameters
        self.quote_risk_mode = str(g.get("quote_risk_mode", "shadow"))
        self.quote_risk_widen_score = float(g.get("quote_risk_widen_score", 0.60))
        self.quote_risk_pull_score = float(g.get("quote_risk_pull_score", 0.85))
        self.quote_risk_resume_score = float(g.get("quote_risk_resume_score", 0.45))
        # markout thresholds for the composite score
        self.markout_min_samples = int(g.get("markout_min_samples", 3))
        self.markout_trip_cents = float(g.get("markout_trip_cents", -1.5))

    def reload(self, cfg: dict) -> None:
        """Re-read guard thresholds after the controller mutates config.

        Only refreshes scalar thresholds; in-flight per-market state
        (cooldowns, flow deques, taker-side history) is preserved.
        """
        self._load(cfg)

    def register_markets(self, markets) -> None:
        """Learn condition_id -> event_id so a guard trip on one bracket can
        cool down its sibling brackets (same neg-risk event). Markets without
        an event_id are unaffected — event logic simply no-ops for them."""
        for m in markets:
            ev = getattr(m, "event_id", None)
            if ev:
                self._event_of[m.condition_id] = str(ev)

    def allow(self, cid: str, now: float) -> bool:
        if now < self._paused_until.get(cid, 0.0):
            return False
        ev = self._event_of.get(cid)
        if ev is not None and now < self._paused_events.get(ev, 0.0):
            return False
        return True

    def paused_cids(self, now: float) -> set[str]:
        """Markets currently inside a trip cooldown (not quotable now) — either
        their own or an event-level cooldown from a sibling bracket."""
        out = {cid for cid, until in self._paused_until.items() if now < until}
        paused_events = {ev for ev, until in self._paused_events.items() if now < until}
        if paused_events:
            out |= {cid for cid, ev in self._event_of.items()
                    if ev in paused_events}
        return out

    def paused_event_ids(self, now: float) -> set[str]:
        """Events with an active cooldown — the scanner uses this to avoid
        entering a fresh sibling bracket of a market that just got picked off."""
        return {ev for ev, until in self._paused_events.items() if now < until}

    def _trip(self, cid: str, now: float, reason: str, question: str) -> None:
        # Base the "newly tripped" decision on this market's own timer so the
        # immediate quote-pull callback still fires even when an event-level
        # cooldown (from a sibling) is already in effect.
        newly_tripped = now >= self._paused_until.get(cid, 0.0)
        if newly_tripped:
            log.warning("市场风控触发（%s），暂停“%s” %.0f 分钟",
                        reason, question[:50], self.cooldown / 60)
        self._paused_until[cid] = now + self.cooldown
        ev = self._event_of.get(cid)
        if ev is not None:
            until = now + self.cooldown
            if until > self._paused_events.get(ev, 0.0):
                self._paused_events[ev] = until
        if newly_tripped and self.on_trip is not None:
            self.on_trip(cid)

    def record_mid(self, cid: str, mid: float, now: float, question: str) -> None:
        hist = self._mids.setdefault(cid, [])
        hist.append((now, mid))
        cutoff = now - self.vol_window
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        if hist and abs(mid - hist[0][1]) >= self.vol_move:
            self._trip(cid, now, f"mid moved {abs(mid - hist[0][1]) * 100:.1f}c "
                                 f"in {now - hist[0][0]:.0f}s", question)

    def allow_side(self, token_id: str, now: float) -> bool:
        return now >= self._side_blocked_until.get(token_id, 0.0)

    def side_block_remaining(self, token_id: str, now: float) -> float:
        """返回单边报价保护剩余秒数；未保护时为零。"""
        return max(0.0, self._side_blocked_until.get(token_id, 0.0) - now)

    def _sync_p0_cooldown(self, cid: str, token_id: str,
                          yes_token: str, no_token: str,
                          until: float) -> None:
        """同步写入 P0 pull_cooldown，防止传统 guard 和 P0 串行叠加。

        原因：传统 guard（check_flow/record_trade）设了
        _side_blocked_until 后，P0 quote_risk_decision 不知道这个
        冷却已经激活。当传统 guard 的 cooldown 到期、allow_side 返回
        True 时，P0 可能已经通过自己的 hysteresis 又设置了新的
        pull_cooldown，导致实际冷却 ≈ 传统 + P0 之和。

        写入 _pull_cooldown_until 让两套系统共享同一个冷却时钟。
        """
        side: Literal["yes", "no"] = "yes" if token_id == yes_token else "no"
        cd = self._pull_cooldown_until.setdefault(cid, {"yes": 0.0, "no": 0.0})
        if until > cd[side]:
            cd[side] = until

    def trip_market(self, cid: str, now: float, reason: str, question: str) -> None:
        self._trip(cid, now, reason, question)

    def record_trade(self, market, token_id: str, side: str, size: float,
                     now: float) -> None:
        cid = market.condition_id
        if size > 0 and side.upper() in ("BUY", "SELL"):
            sign = 1 if (side.upper() == "BUY") == (token_id == market.yes_token) else -1
            flow = self._flow.setdefault(cid, deque())
            flow.append((now, sign * size))
        times = self._trade_times.setdefault(cid, deque())
        times.append(now)
        cutoff = now - self.vel_window
        while times and times[0] < cutoff:
            times.popleft()
        if len(times) >= self.vel_max:
            self._trip(cid, now, f"{len(times)} trades in {self.vel_window:.0f}s",
                       market.question)
            times.clear()
            return

        s = side.upper()
        if s not in ("BUY", "SELL"):
            return
        sides = self._taker_sides.setdefault(token_id, deque(maxlen=self.dir_consec))
        sides.append(s)
        if len(sides) == self.dir_consec and len(set(sides)) == 1:
            if s == "SELL":
                blocked = token_id
            else:
                blocked = market.no_token if token_id == market.yes_token else market.yes_token
            newly_blocked = self.allow_side(blocked, now)
            if newly_blocked:
                log.warning("方向性成交流（连续 %d 笔 %s），撤下“%s”的一侧买单 %.0f 分钟",
                            self.dir_consec, s, market.question[:45], self.side_cooldown / 60)
            self._side_blocked_until[blocked] = now + self.side_cooldown
            self._sync_p0_cooldown(cid, blocked, market.yes_token, market.no_token,
                                   now + self.side_cooldown)
            if newly_blocked and self.on_side_block is not None:
                self.on_side_block(blocked)
            sides.clear()

    def _flow_stats(self, market, now: float) -> tuple[float, float, float]:
        """Return (volume, net_signed, imbalance_ratio) in YES-equivalent terms."""
        flow = self._flow.get(market.condition_id)
        if not flow:
            return 0.0, 0.0, 0.0
        cutoff = now - self.flow_window
        while flow and flow[0][0] < cutoff:
            flow.popleft()
        volume = sum(abs(s) for _, s in flow)
        if volume < self.flow_min_vol:
            return volume, 0.0, 0.0
        net = sum(s for _, s in flow)
        return volume, net, abs(net) / volume

    def flow_imbalance(self, market, now: float) -> float:
        """Signed flow imbalance in [-1, 1] for strategy drift."""
        _, net, imb = self._flow_stats(market, now)
        if imb < self.flow_widen_thr:
            return 0.0
        sign = 1.0 if net > 0 else -1.0
        return sign * imb

    def check_flow(self, market, now: float) -> tuple[float, float]:
        """Returns (widen_yes, widen_no) in price units for the endangered side."""
        volume, net, imbalance = self._flow_stats(market, now)
        if volume < self.flow_min_vol or imbalance < self.flow_widen_thr:
            return 0.0, 0.0
        endangered_no = net > 0
        if imbalance >= self.flow_pull_thr:
            blocked = market.no_token if endangered_no else market.yes_token
            newly_blocked = self.allow_side(blocked, now)
            if newly_blocked:
                log.warning("单边流量失衡 %.0f%%（%.0f 股），暂停“%s”的 %s 买单 "
                            "%.0f 分钟", imbalance * 100, volume,
                            market.question[:45], "NO" if endangered_no else "YES",
                            self.side_cooldown / 60)
            self._side_blocked_until[blocked] = now + self.side_cooldown
            self._sync_p0_cooldown(market.condition_id, blocked,
                                   market.yes_token, market.no_token,
                                   now + self.side_cooldown)
            if newly_blocked and self.on_side_block is not None:
                self.on_side_block(blocked)
            return 0.0, 0.0
        frac = (imbalance - self.flow_widen_thr) / max(
            self.flow_pull_thr - self.flow_widen_thr, 1e-9)
        widen = self.flow_widen_max * frac
        return (0.0, widen) if endangered_no else (widen, 0.0)

    def check_fills(self, fills: list[dict], now: float) -> None:
        recent: dict[tuple[str, str], int] = {}
        for f in fills:
            if now - f["ts"] > self.fill_window or "cid" not in f:
                continue
            if f.get("taker") or f.get("exit"):
                continue
            key = (f["cid"], f["side"])
            recent[key] = recent.get(key, 0) + 1
            if recent[key] >= self.max_fills:
                self._trip(f["cid"], now,
                           f"{recent[key]} {f['side']} fills in "
                           f"{self.fill_window / 60:.0f} min", f["market"])

    # ── P0: Quote risk decision engine ──

    def quote_risk_decision(
        self,
        market,
        now: float,
        markout_avg: float | None,
        markout_samples: int,
        markout_yes_avg: float | None = None,
        markout_no_avg: float | None = None,
        markout_yes_samples: int = 0,
        markout_no_samples: int = 0,
    ) -> QuoteRiskDecision:
        """Pure-function decision for per-side quote admission.

        Composes three observed signals into a composite risk score in [0, 1]:
          1. Flow imbalance (from ``_flow_stats``) — signed taker volume.
          2. Markout (post-fill price drift) — direct adverse selection evidence.
          3. Mid velocity — short-term price speed.

        Uses hysteresis: ``pull`` is held until the score drops below
        ``quote_risk_resume_score``, and ``widen`` is held until the score
        drops below ``quote_risk_widen_score`` — preventing per-loop churn.
        Pull actions also have a minimum time cooldown before re-allow.

        When flow is absent, per-side markout directs the ``pull``/``widen``
        to the specific side that was actually picked off — no longer flags
        both sides out of caution.

        Returns ``QuoteRiskDecision(allow, allow, 0.0, 0.0, "no_signal", 0.0)``
        when ``quote_risk_mode`` is ``"off"`` or no signal is present.
        """
        if self.quote_risk_mode == "off":
            return QuoteRiskDecision("allow", "allow", 0.0, 0.0,
                                     "off", 0.0)

        flow_score = self._compute_flow_score(market, now)
        markout_score = self._compute_markout_score(markout_avg, markout_samples)
        mid_score = self._compute_mid_score(market, now)

        # Composite: max of the three component scores.
        # The strongest signal dominates — flow, markout, or velocity can each
        # independently trigger protect/widen/pull.
        score = max(flow_score, markout_score, mid_score)

        if score <= 0.0:
            # Enforce pull cooldown before clearing state — zero score
            # must not bypass an active cooldown (e.g. flow evaporates
            # but the side was pulled less than side_cooldown ago).
            cd = self._pull_cooldown_until.get(market.condition_id, {})
            yes_hold = now < cd.get("yes", 0.0)
            no_hold = now < cd.get("no", 0.0)
            if yes_hold or no_hold:
                return QuoteRiskDecision(
                    "pull" if yes_hold else "allow",
                    "pull" if no_hold else "allow",
                    0.0, 0.0,
                    "pull_cooldown", 0.0,
                )
            # Clear hysteresis state when signal vanishes entirely
            self._risk_state.pop(market.condition_id, None)
            return QuoteRiskDecision("allow", "allow", 0.0, 0.0,
                                     "no_signal", 0.0)

        # Determine which side is dangerous from flow direction.
        flow = self._flow.get(market.condition_id)
        net = 0.0
        if flow:
            cutoff = now - self.flow_window
            relevant = [s for _, s in flow if _ >= cutoff]
            net = sum(relevant)

        # With no flow signal, use per-side markout to target the
        # specific side that got picked off — instead of pessimistically
        # flagging both sides (old behavior that caused both YES and NO
        # to be pulled under negative markout alone).
        # With a clear flow signal: net > 0 (YES buying) → NO side is
        # dangerous (matches existing check_flow convention).
        no_danger = net > 0.0 if abs(net) > 1e-9 else (
            markout_no_avg is not None
            and markout_no_samples >= self.markout_min_samples
            and markout_no_avg < 0.0
        )
        yes_danger = net < 0.0 if abs(net) > 1e-9 else (
            markout_yes_avg is not None
            and markout_yes_samples >= self.markout_min_samples
            and markout_yes_avg < 0.0
        )

        # Apply hysteresis: resolve the final action from the composite score
        # and the previous state for each side independently.
        prev = self._risk_state.setdefault(market.condition_id, {})
        yes_action = self._resolve_action(score, prev.get("yes", "allow"), "yes")
        no_action = self._resolve_action(score, prev.get("no", "allow"), "no")

        # Pre-load cooldown state: if either side was previously pulled and
        # cooldown hasn't expired, reinstate pull regardless of score.
        cd = self._pull_cooldown_until.setdefault(
            market.condition_id, {"yes": 0.0, "no": 0.0})
        if yes_action != "pull" and now < cd["yes"]:
            yes_action = "pull"
        if no_action != "pull" and now < cd["no"]:
            no_action = "pull"

        # Only the dangerous side is affected — the safe side stays allow,
        # unless mid velocity (market-wide) pushes both sides.
        # BUT: pull cooldown overrides the "safe side → allow" rule.  A side
        # that was pulled must stay pulled for at least side_cooldown_minutes
        # even if flow reverses direction and marks the other side dangerous.
        if mid_score > 0.0 and mid_score >= score:
            # Mid velocity is market-wide; both sides may be dangerous.
            yes_action = self._resolve_action(score, prev.get("yes", "allow"), "yes")
            no_action = self._resolve_action(score, prev.get("no", "allow"), "no")
            # Re-apply cooldown after mid-velocity re-resolution —
            # _resolve_action may have downgraded a pull to widen/allow
            # when score is below pull threshold but cooldown hasn't expired.
            if yes_action != "pull" and now < cd.get("yes", 0.0):
                yes_action = "pull"
            if no_action != "pull" and now < cd.get("no", 0.0):
                no_action = "pull"
        else:
            # Before setting safe side to allow, check cooldown.
            if not yes_danger and now < cd.get("yes", 0.0):
                pass  # cooldown still active — keep pull
            elif not yes_danger:
                yes_action = "allow"
            if not no_danger and now < cd.get("no", 0.0):
                pass  # cooldown still active — keep pull
            elif not no_danger:
                no_action = "allow"

        # Write cooldown deadlines: only after the final action is settled
        # (dangerous-side + cooldown checks), not from the raw hysteresis
        # output.  This prevents writing cd["yes"] when YES wasn't actually
        # kept as pull after the dangerous-side filter.
        if yes_action == "pull":
            cd["yes"] = now + self.side_cooldown
        if no_action == "pull":
            cd["no"] = now + self.side_cooldown

        # Reset safe side hysteresis state.
        if yes_action == "allow":
            prev.pop("yes", None)
        else:
            prev["yes"] = yes_action
        if no_action == "allow":
            prev.pop("no", None)
        else:
            prev["no"] = no_action
        if not prev:
            self._risk_state.pop(market.condition_id, None)

        # Compute widen amounts — flow-derived, scaled by score.
        yes_widen = 0.0
        no_widen = 0.0
        if yes_action == "widen":
            yes_widen = self._compute_widen_amount(score, market)
        if no_action == "widen":
            no_widen = self._compute_widen_amount(score, market)

        parts = []
        if flow_score > 0:
            parts.append(f"flow={flow_score:.2f}")
        if markout_score > 0:
            parts.append(f"markout={markout_score:.2f}")
        if mid_score > 0:
            parts.append(f"mid_vel={mid_score:.2f}")
        reason = ",".join(parts) if parts else "no_signal"

        return QuoteRiskDecision(
            yes_action=yes_action, no_action=no_action,
            yes_widen=yes_widen, no_widen=no_widen,
            reason=reason, score=score,
        )

    def _compute_flow_score(self, market, now: float) -> float:
        """Normalized flow-imbalance score in [0, 1]."""
        volume, _net, imbalance = self._flow_stats(market, now)
        if volume < self.flow_min_vol:
            return 0.0
        if imbalance <= 0.0:
            return 0.0
        # Scale the imbalance into [0, 1] using widen/pull thresholds as anchors.
        score = max(0.0, min(1.0, (imbalance - self.flow_widen_thr * 0.5)
                             / max(self.flow_pull_thr - self.flow_widen_thr * 0.5, 1e-9)))
        return score

    def _compute_markout_score(
        self, markout_avg: float | None, markout_samples: int,
    ) -> float:
        """Normalized markout score in [0, 1]. Ignores markout with too few samples."""
        if (markout_avg is None or markout_samples < self.markout_min_samples
                or markout_avg >= 0.0):
            return 0.0
        # markout_avg is in price units (e.g. -0.03 = -3c).
        # trip_cents is the worst acceptable markout (e.g. -1.5c).
        # Score scales from 0 at 0c to 1.0 at 2× trip_cents.
        trip = abs(self.markout_trip_cents) / 100.0  # convert cents → price units
        if trip <= 0:
            return 0.0
        return max(0.0, min(1.0, abs(markout_avg) / (trip * 2.0)))

    def _compute_mid_score(self, market, now: float) -> float:
        """Normalized mid-velocity score in [0, 1]."""
        hist = self._mids.get(market.condition_id)
        if not hist:
            return 0.0
        cutoff = now - self.vol_window
        window = [(t, m) for t, m in hist if t >= cutoff]
        if len(window) < 2:
            return 0.0
        move = abs(window[-1][1] - window[0][1])
        if move <= 0:
            return 0.0
        return max(0.0, min(1.0, move / self.vol_move))

    def _resolve_action(
        self, score: float, prev: ActionLiteral, _side: str,
    ) -> ActionLiteral:
        """Apply hysteresis to determine the next action."""
        if score >= self.quote_risk_pull_score:
            return "pull"
        if score >= self.quote_risk_widen_score:
            # From pull, hold until score drops below resume.
            if prev == "pull" and score > self.quote_risk_resume_score:
                return "pull"
            return "widen"
        if score > self.quote_risk_resume_score:
            # Below widen threshold but above resume — hold prior action.
            if prev in ("pull", "widen"):
                return prev
            return "allow"
        return "allow"

    def _compute_widen_amount(self, score: float, market) -> float:
        """Compute widen amount in price units for a dangerous side.

        ``widen`` action can fire when score >= quote_risk_widen_score, but
        floating-point rounding can make score epsilon-below that threshold
        (e.g. 0.5999999 where widen_score=0.60).  In that case, frac is
        effectively zero → widen_amount = 0.00.

        Floor at ``market.tick`` (not a fixed 25 % of max) because a widen of
        0.005 rounds back to the original price at tick = 0.01, leaving the
        quote unchanged despite the decision recording ``widen``.  The tick
        floor guarantees at least one visible tick of movement.
        """
        frac = (score - self.quote_risk_widen_score) / max(
            self.quote_risk_pull_score - self.quote_risk_widen_score, 1e-9)
        # At least one tick, but never exceed 1.0 × flow_widen_max.
        min_frac = market.tick / max(self.flow_widen_max, 1e-9)
        frac = max(min_frac, min(1.0, frac))
        return self.flow_widen_max * frac


class MarkoutTracker:
    """Measures adverse selection directly via post-fill price drift."""

    def __init__(self, cfg: dict):
        g = cfg["guards"]
        self.horizons = [float(h) for h in g["markout_horizons_secs"]]
        self.window = float(g["markout_window_minutes"]) * 60
        self.min_samples = int(g["markout_min_samples"])
        self.trip_cents = float(g["markout_trip_cents"])
        self._pending: list[dict] = []
        self._seen_ts = 0.0
        self._samples: dict[str, list[tuple[float, float, float, str]]] = {}  # (ts, horizon, markout, token_id)
        self._session: dict[float, list[float]] = {h: [] for h in self.horizons}

    def reload(self, cfg: dict) -> None:
        """Re-read markout thresholds after the controller mutates config.

        Horizons are intentionally left fixed — pending samples are keyed by
        horizon, so changing them mid-session would orphan in-flight markouts.
        """
        g = cfg["guards"]
        self.window = float(g["markout_window_minutes"]) * 60
        self.min_samples = int(g["markout_min_samples"])
        self.trip_cents = float(g["markout_trip_cents"])

    def ingest(self, fills: list[dict]) -> None:
        newest = self._seen_ts
        for f in fills:
            if f["ts"] <= self._seen_ts:
                continue
            newest = max(newest, f["ts"])
            if f.get("taker") or f.get("exit") or "token" not in f or "price" not in f:
                continue
            self._pending.append({
                "ts": f["ts"], "cid": f["cid"], "token": f["token"],
                "price": f["price"], "market": f.get("market", ""),
                "done": set(),
            })
        self._seen_ts = newest

    def resolve(self, mid_lookup, now: float) -> list[dict]:
        """Resolve pending markouts; return newly computed samples for logging."""
        still_pending = []
        resolved: list[dict] = []
        for p in self._pending:
            for h in self.horizons:
                if h in p["done"] or now < p["ts"] + h:
                    continue
                p["done"].add(h)
                mid = mid_lookup(p["token"])
                if mid is None:
                    continue
                markout = mid - p["price"]
                self._samples.setdefault(p["cid"], []).append(
                    (now, h, markout, p["token"]))
                self._session[h].append(markout)
                resolved.append({
                    "ts": now, "fill_ts": p["ts"], "cid": p["cid"],
                    "market": p.get("market", ""), "horizon": h,
                    "markout": markout,
                })
            if len(p["done"]) < len(self.horizons):
                still_pending.append(p)
        self._pending = still_pending
        cutoff = now - self.window
        for cid in list(self._samples):
            self._samples[cid] = [s for s in self._samples[cid] if s[0] >= cutoff]
            if not self._samples[cid]:
                del self._samples[cid]
        return resolved

    def market_avg(self, cid: str, horizon: float | None = None) -> float | None:
        """Average markout (price units) for a market at the given horizon."""
        samples = self._samples.get(cid)
        if not samples:
            return None
        h = horizon if horizon is not None else max(self.horizons)
        vals = [m for _, hh, m, _token in samples if hh == h]
        if not vals:
            return None
        return sum(vals) / len(vals)

    def market_avg_by_side(
        self, cid: str, yes_token: str, no_token: str,
        horizon: float | None = None,
    ) -> tuple[float | None, float | None, int, int]:
        """Per-side markout average for directional danger targeting.

        Returns (yes_avg, no_avg, yes_samples, no_samples) so the risk
        decision engine can determine *which* side was picked off when
        there is no flow signal — rather than pessimistically flagging
        both sides as dangerous.
        """
        samples = self._samples.get(cid)
        if not samples:
            return None, None, 0, 0
        h = horizon if horizon is not None else max(self.horizons)
        yes_vals = [m for _, hh, m, tok in samples if hh == h and tok == yes_token]
        no_vals = [m for _, hh, m, tok in samples if hh == h and tok == no_token]
        yes_avg = sum(yes_vals) / len(yes_vals) if yes_vals else None
        no_avg = sum(no_vals) / len(no_vals) if no_vals else None
        return yes_avg, no_avg, len(yes_vals), len(no_vals)

    def recent_markout(self, horizon: float | None = None) -> tuple[float, int]:
        """Rolling cross-market markout (cents) and sample count.

        Aggregates every market's windowed samples at the long horizon — the
        adaptive controller's primary read on how toxic current flow is.
        Returns (0.0, 0) when no samples are available.
        """
        h = horizon if horizon is not None else (max(self.horizons) if self.horizons else 0.0)
        vals = [m for samples in self._samples.values()
                for _, hh, m, _token in samples if hh == h]
        if not vals:
            return 0.0, 0
        return sum(vals) / len(vals) * 100, len(vals)

    def toxic_markets(self) -> list[tuple[str, float, int]]:
        h_long = max(self.horizons)
        out = []
        for cid, samples in self._samples.items():
            vals = [m for _, h, m, _token in samples if h == h_long]
            if len(vals) < self.min_samples:
                continue
            avg_cents = sum(vals) / len(vals) * 100
            if avg_cents <= self.trip_cents:
                out.append((cid, avg_cents, len(vals)))
        return out

    def reset_market(self, cid: str) -> None:
        self._samples.pop(cid, None)
        self._pending = [p for p in self._pending if p["cid"] != cid]

    def session_stats(self) -> dict[float, tuple[float, int]]:
        return {
            h: ((sum(v) / len(v) * 100) if v else 0.0, len(v))
            for h, v in self._session.items()
        }
