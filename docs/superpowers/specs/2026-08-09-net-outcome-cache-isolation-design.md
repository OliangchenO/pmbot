# Net-outcome cache isolation

## Goal

Keep P2 outcome/shadow observation work off the trading control path: a slow or locked metrics database must not delay market rescans, quote refreshes, quote cancellation, inventory recovery, or risk handling.

## Scope

Only the cached net-outcome refresh path in `Bot` and read-only metrics access change. Order placement, cancellation, inventory calculation, risk thresholds, and the legacy selection mode remain unchanged.

## Design

`_rescan()` reads the last successfully published cache and immediately proceeds with scanning and applying the result. It never waits for an in-flight outcome refresh.

`_refresh_outcome_cache()` opens an independent read-only SQLite connection for its work instead of reusing `self.metrics._conn`. The refresh computes both shadow inputs and the outcome report through that connection, then replaces both cached values together only after successful completion. Failures and timeouts retain the prior cache.

The background worker is not cancelled merely because a timeout expires. Its result is ignored if it misses the deadline, and no main-thread metrics write shares its connection. This removes the prior race where cancelling the asyncio wrapper did not stop the underlying worker thread.

## Safety and failure handling

- Startup cache is empty, so selection remains legacy until a successful refresh produces data.
- A failed, slow, or locked read leaves the last good cache unchanged.
- `selection_mode=legacy` keeps the existing candidate order regardless of cache state.
- `selection_mode=net_outcome` continues to fail closed when gate evidence is absent.

## Verification

- Add a regression test that an in-flight refresh does not make `_rescan()` await it before applying scan results.
- Add a test that the refresh uses a separate read-only store/connection and leaves the old cache untouched on timeout or failure.
- Run focused `test_main`, `test_metrics`, `test_gamma`, and `test_outcomes` suites plus `git diff --check`.
