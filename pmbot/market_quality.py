"""P1 shadow classification for reward markets.

This module is deliberately read-only: it labels observed quality for logs and
reports but never changes market selection, prices, or quote size.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketQuality:
    level: str
    reason: str


def classify_market_quality(*, samples: int,
                            confirmed_eligible_uptime_pct: float,
                            markout_cents: float | None,
                            markout_samples: int,
                            forced_hedges: int,
                            post_failures: int,
                            min_samples: int = 30,
                            green_uptime_pct: float = 95.0,
                            red_uptime_pct: float = 85.0,
                            red_markout_cents: float = -0.8) -> MarketQuality:
    """Classify a market without treating absent fills as evidence of safety."""
    if samples < min_samples:
        return MarketQuality("黄色", "样本不足，维持当前报价并继续观察")
    if forced_hedges > 0:
        return MarketQuality("红色", "发生强制对冲，暂停扩仓并建议观察")
    if post_failures > 0:
        return MarketQuality("红色", "交易所确认失败，奖励资格不可靠")
    if markout_samples > 0 and markout_cents is not None and markout_cents <= red_markout_cents:
        return MarketQuality("红色", "成交后价格明显不利，存在逆向选择风险")
    if confirmed_eligible_uptime_pct < red_uptime_pct:
        return MarketQuality("红色", "确认合格率偏低，奖励在场时间不足")
    if (confirmed_eligible_uptime_pct >= green_uptime_pct
            and (markout_samples == 0 or markout_cents is None or markout_cents >= 0)):
        return MarketQuality("绿色", "确认合格率高，未发现显著逆向选择")
    return MarketQuality("黄色", "资格或成交质量一般，维持当前报价并继续观察")
