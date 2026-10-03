# Risk-checked order planning

`plan_orders` returns an `OrderPlan`, which behaves as a sequence of approved
`OrderIntent` values. Its `rejections` field lists skipped or reduced orders
with reasons, and `portfolio_value` reports the marked wallet value. The
planner makes no network calls and submits no orders.

All non-cash wallet assets must have a fresh direct ticker in the portfolio's
quote currency. Mixed target quote currencies and unpriced holdings fail
closed. Ticker timestamps must be within `RISK_MAX_QUOTE_AGE_SECONDS` of the
wallet snapshot. Portfolio value includes free and locked balances, marking
non-cash assets at the ticker's last price.

The planner requires a `PortfolioRiskState` with the portfolio currency, the
current UTC day's midnight timestamp, the value at that time, and the account
high-water mark. Daily loss is `(day start - current value) / day start`;
drawdown is `(high-water mark - current value) / high-water mark`. If either
configured limit is reached, buys are disabled and the planner targets cash,
proposing sells for available balances. Missing, invalid, stale-day, or
mismatched risk state produces no orders.

Per-asset targets are capped by `RISK_MAX_ASSET_WEIGHT`. Total target exposure
is capped by the smaller of `RISK_MAX_TOTAL_EXPOSURE` and one minus
`RISK_MINIMUM_CASH_RESERVE`. The planner sizes buys against current exposure,
available quote balance, the cash reserve, and the fee allowance. It does not
use pending sale proceeds to fund buys in the same plan.

Order intents use market orders. Their reference prices conservatively round
the current ask up for buys and the current bid down for sells; these prices
support sizing and fee estimates but do not guarantee execution prices.
Quantities round down to exchange precision. Orders below the exchange minimum
notional, stale or invalid market data, locked sell balances, insufficient
free cash, and cap-limited quantities are reported in `rejections`.
