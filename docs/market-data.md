# Market data format

`SQLiteMarketDataStore` stores Roostoo snapshots and hourly candles in a local
SQLite database. Pass the database path when constructing the store; the parent
directory is created if needed. Snapshot timestamps are the Roostoo server time
in Unix milliseconds. Duplicate `(pair, server_time_ms)` snapshots and duplicate
`(pair, open_time_ms)` candles are rejected.

## Historical CSV

Historical input uses one row per pair per UTC hour, with these columns:

```text
pair,open_time_ms,close_time_ms,open,high,low,close,volume
```

Times are Unix milliseconds. `open_time_ms` is the hour boundary and
`close_time_ms` is the next hour boundary. Prices and volume are finite decimal
values; prices must be positive and volume cannot be negative. The CSV loader
returns the shared `Candle` model, and `record_candles` persists those candles.

## Hourly history

History contains completed UTC hours in chronological order. Exchange OHLC
candles take precedence over sampled bars for the same pair and hour. When only
snapshots are available, the store samples each hour's first, highest, lowest,
and last observed price and marks the result `sampled_snapshot`. Snapshot data
does not include hourly volume, so sampled bars use zero volume rather than
estimating it from 24-hour turnover.

`get_market_history` returns timestamped closes and close-to-close hourly returns.
It raises a market-data error when the requested window has gaps, too little
history, or a latest observation older than `max_age_ms`. It does not fill
missing hours or use snapshots from an incomplete hour.
