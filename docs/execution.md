# Order execution

`OrderExecutor` accepts approved `OrderIntent` values and submits them as
market orders in `LIVE` mode without changing their quantities. It refuses
non-market intents and sells that exceed the wallet's free balance.

`DRY_RUN` records every intent without making exchange calls. Its taker-fee
estimate uses the intent's reference price; it is unavailable when no reference
price is supplied. Live results use the exchange commission when present and
estimate it from fill quantity, average price, and the configured taker-fee rate
when the response omits it. Pass `settings.fee_rate` as `taker_fee_rate` when
constructing the executor, and pass `settings.max_quote_age_seconds` as
`max_quote_age_seconds`. Estimated fees are marked in `ExecutionResult`.

Each result is returned, logged, and stored in `data/execution.sqlite3`. The
journal records the intent, normalized outcome, fill details, fee, and the
latest wallet snapshot and pending-order count. The database is ignored by Git.

Before each live order, the executor checks for pending orders on that pair and
reads the wallet. After submission, it queries orders with an ID and refreshes
the wallet and pending-order count. An ambiguous timeout is never retried
automatically. Its pair remains blocked across restarts while the outcome is
unresolved. Orders with an ID are queried before a later intent for that pair;
an order without an ID stays blocked until it has been independently
reconciled. Then `confirm_reconciled` records its confirmed terminal result,
provided the exchange reports no pending orders for the pair.

Results use `FILLED`, `REJECTED`, `PENDING`, `CANCELED`, `UNKNOWN`, or `DRY_RUN`
statuses. `UNKNOWN` and `PENDING` records prevent another order for that pair.
The CLI invokes the executor as part of each cycle. `read_only` skips order
execution, `dry_run` records intents, and `live` can submit orders after startup
validation and reconciliation.
