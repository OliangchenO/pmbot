"""Tests for userfeed trade event parsing."""

from unittest.mock import MagicMock

from pmbot.userfeed import UserFeed


def _broker():
    b = MagicMock()
    b.address = "0xOurAddress"
    b.client.creds = MagicMock(api_key="k", api_secret="s", api_passphrase="p")
    return b


def test_maker_fill_opposite_side_on_same_token():
    feed = UserFeed(_broker())
    ev = {
        "event_type": "trade",
        "status": "MATCHED",
        "side": "BUY",
        "outcome": "Yes",
        "maker_orders": [{
            "maker_address": "0xOurAddress",
            "asset_id": "yes_tok",
            "price": "0.47",
            "matched_amount": "10",
            "outcome": "Yes",
        }],
    }
    feed._handle_trade(ev)
    feed.broker.record_user_fill.assert_called_once()
    args = feed.broker.record_user_fill.call_args
    assert args[0][1] == "SELL"


def test_taker_fill_when_not_maker():
    feed = UserFeed(_broker())
    ev = {
        "event_type": "trade",
        "status": "MATCHED",
        "side": "BUY",
        "asset_id": "yes_tok",
        "price": "0.50",
        "size": "10",
        "maker_orders": [{"maker_address": "0xOther"}],
    }
    feed._handle_trade(ev)
    feed.broker.record_user_fill.assert_called_once()
    assert feed.broker.record_user_fill.call_args[1]["taker"] is True


def test_skips_non_matched_status():
    feed = UserFeed(_broker())
    ev = {"event_type": "trade", "status": "MINED"}
    feed._handle_trade(ev)
    feed.broker.record_user_fill.assert_not_called()


def test_order_update_is_forwarded_as_cumulative_matched_shares():
    feed = UserFeed(_broker())
    feed._handle('{"event_type":"order","id":"order-1","asset_id":"yes_tok",'
                 '"price":"0.47","side":"BUY","size_matched":"33",'
                 '"timestamp":"1782753357257"}')

    feed.broker.observe_order_match.assert_called_once_with(
        "order-1", "yes_tok", "BUY", 0.47, 33.0, 1782753357.257)
