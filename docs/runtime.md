# Runtime orchestration

The CLI wires the API client, market-data store, baseline strategy, risk manager,
executor, and SQLite journals together. By default, it runs a cycle immediately
and then at UTC interval boundaries. `BOT_CYCLE_INTERVAL_SECONDS` defaults to
3600 and must be a multiple of one hour. `python -m fourfunds --once` runs the
current interval once; a cycle already recorded for that interval is skipped.

## Cycle flow

Each cycle follows this order:

1. In live mode, reconcile unresolved orders from the execution journal.
2. Read exchange rules, tickers, and the account wallet.
3. Persist the ticker snapshot and load available hourly history.
4. Calculate baseline targets and update the persisted portfolio risk state.
5. Plan orders and execute them only in `dry_run` or `live` mode.
6. Store the inputs, targets, risk decisions, orders, results, and errors.

Ticker snapshots older than `RISK_MAX_QUOTE_AGE_SECONDS` are recorded but refuse
the cycle before planning or execution. Stale or gappy hourly history is handled
per pair: when `MARKET_HISTORY_CSV` is configured, the runtime attempts to
repair the pair's requested window; repaired history is used for strategy
evaluation, while pairs that remain stale or incomplete are excluded. Other
healthy pairs continue through the cycle. The decision log keeps each pair's
original history error and whether repair was attempted and succeeded, including
the repair error when it fails. API reads retry at most twice after the initial
request, using a bounded exponential delay. Order submissions are never
retried automatically.

Set `MARKET_HISTORY_CSV` to a historical candle CSV path to backfill missing
hourly candles before strategy evaluation. The runtime reads this file only
when a pair's stored history is stale or insufficient for the configured
strategy window. Existing pair/hour candles are left unchanged, so repeating
startup is safe. If the variable is unset, affected pairs are excluded and
healthy pairs continue.

Live mode reconciles orders with known IDs before planning new submissions. An
order that remains pending or has an unknown outcome blocks the cycle from
submitting any new orders. Orders without an exchange ID remain blocked until
they are independently reconciled. `SIGINT` and `SIGTERM` stop future cycles;
the current cycle is allowed to finish.

## Persisted records

The bot stores market snapshots in `data/market.sqlite3`, order outcomes in
`data/execution.sqlite3`, and cycle decisions plus risk state in
`data/runtime.sqlite3`. These local files are ignored by Git. Each decision
record includes its UTC cycle time, mode, strategy version, non-secret settings
and their fingerprint, exchange rules, ticker and wallet snapshots, available
history, targets, risk plan, execution results, and errors. This provides the
inputs needed to inspect how a decision was made. API keys and secrets are never
included.

Run `python -m fourfunds --report` to count execution records with a positive
filled quantity and list their UTC result dates. Active trading days are derived
only from records with fills; empty execution history reports zero trades and
zero active days.
