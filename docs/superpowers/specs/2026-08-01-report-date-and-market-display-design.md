# Report date filter and market display

## Goal

Make historical daily reporting available from `report`, and ensure market names
remain identifiable in PowerShell output.

## CLI contract

- `python -m pmbot.main report` continues to display the current UTC date.
- `python -m pmbot.main report --date YYYY-MM-DD` displays that UTC date.
- Invalid dates surface the same validation error used by the metrics layer.
- `performance --date` remains unchanged.

## Output contract

- Per-market report tables render the complete market question rather than a
  hard-coded prefix. Rich may wrap the column to fit the terminal; no market
  label is silently truncated by application code.
- The report date is used consistently for the daily PnL, recovery, reward,
  and trading-ledger figures. All-time figures remain all-time and are labeled
  as such.

## Implementation and tests

- Extend `cmd_report` and its argparse subcommand with an optional `--date`.
- Add a date-aware metrics-store report path, keeping the no-argument behavior
  backward-compatible.
- Remove hard-coded market-name slicing from terminal report renderers.
- Add focused CLI/rendering and metrics tests for a historical report date and
  an untruncated market label, then run the affected test modules.
