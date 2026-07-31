from pmbot.market_quality import classify_market_quality


def test_classify_market_quality_marks_stable_eligible_market_green():
    """Removing the confirmation/markout gates would wrongly promote an unsafe market."""
    quality = classify_market_quality(
        samples=120,
        confirmed_eligible_uptime_pct=97.0,
        markout_cents=0.2,
        markout_samples=5,
        forced_hedges=0,
        post_failures=0,
    )

    assert quality.level == "绿色"
    assert quality.reason == "确认合格率高，未发现显著逆向选择"


def test_classify_market_quality_marks_forced_hedge_market_red():
    """A single forced hedge is enough to prevent a reward-looking market from expanding."""
    quality = classify_market_quality(
        samples=120,
        confirmed_eligible_uptime_pct=98.0,
        markout_cents=0.3,
        markout_samples=5,
        forced_hedges=1,
        post_failures=0,
    )

    assert quality.level == "红色"
    assert quality.reason == "发生强制对冲，暂停扩仓并建议观察"


def test_classify_market_quality_keeps_short_history_yellow():
    """No-fill or newly entered markets are unknown, not proof that they are low toxicity."""
    quality = classify_market_quality(
        samples=5,
        confirmed_eligible_uptime_pct=100.0,
        markout_cents=None,
        markout_samples=0,
        forced_hedges=0,
        post_failures=0,
    )

    assert quality.level == "黄色"
    assert quality.reason == "样本不足，维持当前报价并继续观察"
