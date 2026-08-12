# Durable reward-exit take submission Implementation Plan

**Goal:** Persist submitted takes and prohibit retries until real fills reconcile.

1. Add a submission table and tests for pending/restart/fragmented fills.
2. Persist before submission, update response quantity, reconcile durable real fills.
3. Rehydrate pending submissions at startup and run focused regressions.
