# Held-market minimum net shares

## Goal

Treat a position as inventory exposure only when its unpaired directional size
is at least five shares.

## Rule

For each condition ID in the live position snapshot, calculate
`abs(yes_shares - no_shares)`. `_hydrate_held_markets` fetches and adopts the
market only when that value is at least `5`.

Examples:

- YES 4, NO 0: skip it.
- YES 5, NO 0: adopt it.
- YES 4, NO 4: skip it, because the net directional exposure is zero.

## Scope and boundaries

The threshold applies only to positions being adopted as held markets. It does
not change pending-hedge handling. Skipped CIDs are not fetched from Gamma and
therefore do not enter held-market book subscriptions or the usual inventory
management path.

## Verification

Add broker tests that show sub-threshold and offsetting positions do not fetch
or adopt markets, while a five-share net position does. Run the focused broker
tests and the relevant inventory regressions.
