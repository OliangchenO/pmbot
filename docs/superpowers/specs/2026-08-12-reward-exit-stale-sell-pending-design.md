# Reward Exit Startup Cleanup Design

## Goal

Preserve an active `SELL_PENDING` reward-exit batch across a process restart so it continues its batch-owned passive SELL path and never falls through to generic P1 inventory recovery.

## Decision

At the first reward-exit tick after startup, stale cleanup will consider only `TAKE_PENDING` batches. A `SELL_PENDING` batch is a valid long-lived exit state: its owned exit order has an expiry and is replaced by the batch lifecycle, so its age alone is not grounds for closing or unlocking the market.

## Boundaries

- Keep the existing `reward_exit_stale_after_secs` / `reward_exit_terminal_after_secs` configuration and its current meaning for `TAKE_PENDING`.
- Do not add configuration, change SELL target pricing, or alter generic P1 recovery.
- Retain the existing lock rule: any non-closed batch locks its CID.

## Verification

Add a regression test that runs the startup reward-exit tick with an old `SELL_PENDING` batch and proves it remains open and its CID remains locked. The test must fail against the previous cleanup condition and pass after the change.
