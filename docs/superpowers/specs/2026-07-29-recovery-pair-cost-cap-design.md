# Inventory-recovery pair-cost cap

## Problem

When one outcome fills, the bot retains only a passive order for the complementary outcome. That recovery order currently uses the normal quote price. During a fast move it can fill at a price that makes the completed YES+NO pair cost more than $1.

## Scope

Apply the same break-even, fee-aware pair-cost ceiling already used by the forced-taker hedge to both ordinary inventory recovery and cooldown recovery. Do not change normal two-sided quotes, forced-hedge escalation, order sizing, or merge behavior.

## Behavior

- If there is meaningful unpaired inventory and its cost basis is known, retain only the complementary recovery bid whose price is at or below the pair-cost ceiling.
- If the desired recovery price exceeds the ceiling, do not place a recovery bid.
- If the cost basis is unavailable, do not place a recovery bid. This fails closed rather than guessing an economically safe price.
- The price ceiling remains fee-aware through the existing `_forced_hedge_max_price()` calculation.

For a held NO at 0.669, the YES ceiling is about 0.331 before any applicable taker fee. A recovery quote at 0.329 remains valid; quotes at 0.340 and 0.372 are rejected.

## Tests

1. A recovery quote below the ceiling is retained and sized to the unpaired position.
2. A recovery quote above the ceiling is removed.
3. A recovery quote is removed when cost basis is unavailable.
4. Cooldown recovery has the identical cost-cap behavior.

## Verification

Run the targeted recovery tests, then the full test suite. No live process is started or modified.
