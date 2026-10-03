# Historical backtesting

`run_backtest` replays the `BaselineStrategy` over aligned hourly candles. It
accepts a mapping from `BASE/QUOTE` pair names to candle sequences, an optional
starting portfolio value, and optional strategy settings. The default starting
value is 10,000 units of the common quote currency. If settings are omitted,
the strategy uses the project defaults, including its 24-hour turnover filter
and fee rate.

The strategy evaluates completed candles at their close. Each target is
rebalanced at the next candle's open, so a signal never trades at the close
that produced it. The final candle's signal is not executed because the input
contains no following open. Trades use fractional quantities and close-derived
prices; exchange precision, minimum order sizes, spread, and slippage are not
modeled. The configured fee rate is charged on both buys and sells.

The strategy's 24-hour quote-turnover input is estimated by summing
`volume * close` over the most recent 24 candles. This estimate assumes candle
volume is in the base asset. Sampled snapshot candles have zero volume, so they
do not pass the default turnover filter. Every pair must have the same
continuous hourly timestamps and quote currency; missing or misaligned history
raises `ValueError` instead of being filled or forward-priced.

The replay uses the strategy's raw target weights and does not apply the risk
planner's portfolio caps or circuit breakers. `BacktestResult` reports the
initial and final values, total return, maximum hourly drawdown, UTC daily
returns, fees paid, and the number of UTC days with at least one executed
rebalance.
