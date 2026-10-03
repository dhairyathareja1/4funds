# 4Funds

4Funds is a modular trading bot for the Roostoo APAC University Quant Trading
Hackathon. The implemented v0 includes a long-only hourly trend strategy,
market data storage, risk-checked order planning, order execution and
reconciliation, hourly cycle orchestration, and historical backtesting.

The repository's strategy rules and operational behavior are summarized below.
Details that are unavailable in the repository are marked **Not documented** or
**Not yet implemented** rather than inferred.

## What the bot does

The CLI wires together the Roostoo API client, SQLite market-data store,
baseline strategy, risk manager, order executor, and local SQLite journals. A
cycle reads exchange rules, tickers, and the wallet; records market snapshots;
loads available hourly history; calculates target weights; updates persisted
risk state; plans orders; and records decisions and results.

In live mode, the bot reconciles unresolved orders before planning new
submissions. A cycle already recorded for the current interval is skipped.
Cycles run immediately and then at UTC interval boundaries; the default
interval is one hour. `SIGINT` and `SIGTERM` stop future cycles while allowing
the current cycle to finish.

## v0 strategy

`BaselineStrategy` is a deterministic, long-only hourly trend strategy. A pair
must be marked tradable by the exchange, meet the configured 24-hour quote
turnover minimum, and have enough valid, contiguous hourly candles. The current
quote must have positive momentum over the configured lookback and be above the
average of the latest hourly closes over the trend lookback. Qualifying pairs
are ranked by momentum, with pair name as the tie-breaker. Up to `top_k` pairs
receive equal target weights. If no pair qualifies, the strategy targets 100%
cash.

The rationale for choosing these signals, thresholds, and defaults is **Not
documented** in the repository.

Default strategy settings:

| Setting | Default |
| --- | ---: |
| `STRATEGY_MOMENTUM_LOOKBACK_HOURS` | `24` |
| `STRATEGY_TREND_LOOKBACK_HOURS` | `168` |
| `STRATEGY_TOP_K` | `3` |
| `STRATEGY_MINIMUM_QUOTE_TURNOVER_24H` | `100000` |

## Data source and storage

The runtime reads server time, exchange rules, tickers, and wallet balances
from the Roostoo API. Account reads require `ROOSTOO_API_KEY` and
`ROOSTOO_API_SECRET` in every run mode. The default API URL is
`https://mock-api.roostoo.com`; live mode requires an explicitly configured
HTTPS `ROOSTOO_BASE_URL`.

The runtime records ticker snapshots in `data/market.sqlite3`. The store can
also persist hourly OHLC candles; `load_historical_candles` parses the CSV and
`record_candles` persists the returned candles. Historical data is not
automatically imported by the runtime. The documented CSV columns are:

```text
pair,open_time_ms,close_time_ms,open,high,low,close,volume
```

Times are Unix milliseconds on UTC hourly boundaries. A CLI command for
importing historical CSV data or downloading a historical dataset was not
found in the repository. When exchange OHLC candles are unavailable, the store
can sample snapshots into hourly bars with zero volume. Gaps, insufficient
history, and stale observations are not filled or forward-priced.

See [Market data format](docs/market-data.md) for validation and storage
details.

## Local setup

Requires Python 3.10 or newer.

### macOS or Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Set `ROOSTOO_API_KEY` and `ROOSTOO_API_SECRET` in `.env` for account reads.
Keep real credentials private; `.env` is ignored by Git. Do not paste secrets
into source files, issues, logs, or this README. `.env.example` contains empty
credential values and is safe to copy as a starting point.

## Run modes

Set `BOT_MODE` in `.env` to one of:

| Mode | Behavior |
| --- | --- |
| `read_only` | Reads and records data and decisions; skips order execution. |
| `dry_run` | Default. Records order intents without submitting exchange orders; a fee estimate is available when the intent has a reference price. |
| `live` | Can submit market orders after startup validation and order reconciliation. Requires credentials and an explicit HTTPS base URL. |

Bot cycles in every mode require API credentials for account reads. Live order
submission is never the default.

Run one cycle:

```sh
python -m fourfunds --once
```

Run continuously at the configured interval:

```sh
python -m fourfunds
```

The interval is configured with `BOT_CYCLE_INTERVAL_SECONDS` and must be a
whole number of hours; the default is `3600`. To print the execution report:

```sh
python -m fourfunds --report
```

The report counts execution records with positive filled quantities and lists
their UTC dates. It does not create sample trades.

## Risk controls

The order planner:

- caps each asset at `RISK_MAX_ASSET_WEIGHT` and total exposure at
  `RISK_MAX_TOTAL_EXPOSURE`;
- preserves at least `RISK_MINIMUM_CASH_RESERVE`;
- rejects stale quotes using `RISK_MAX_QUOTE_AGE_SECONDS`;
- requires fresh direct quotes to value non-cash wallet assets;
- disables buys and targets cash when the daily-loss or drawdown threshold is
  reached; and
- sizes orders using available balances, exchange precision, minimum order
  value, and a fee allowance.

Risk state is persisted across restarts. The default limits are:

| Setting | Default |
| --- | ---: |
| `RISK_MAX_ASSET_WEIGHT` | `0.25` |
| `RISK_MAX_TOTAL_EXPOSURE` | `0.75` |
| `RISK_MINIMUM_CASH_RESERVE` | `0.10` |
| `RISK_MAX_DAILY_LOSS_FRACTION` | `0.03` |
| `RISK_MAX_DRAWDOWN_FRACTION` | `0.10` |
| `RISK_MAX_QUOTE_AGE_SECONDS` | `120` |
| `RISK_FEE_RATE` | `0.001` |

Fractions are decimal values (`0.25` means 25%). See [Risk-checked order
planning](docs/risk-planning.md) for valuation, order sizing, and circuit
breaker behavior.

## Order lifecycle and fees

The planner creates market-order intents; it does not make network calls.
`dry_run` records intents without exchange submission. In `live` mode, the
executor checks pending orders and wallet balances before submitting. Orders
with IDs are queried during reconciliation. Pending or unknown outcomes block
later orders for that pair; ambiguous submission timeouts are not retried
automatically.

The executor uses the exchange commission when returned. If it is absent, it
estimates commission from fill quantity, average price, and `RISK_FEE_RATE`.
Dry-run fee estimates use the intent reference price and are unavailable when
no reference price is supplied.

The backtest charges the configured trading fee rate on buys and sells.

See [Order execution](docs/execution.md) for statuses, reconciliation, and
timeout behavior.

## Backtesting

The backtest is available as the Python function `run_backtest` in
`fourfunds.backtest`. It replays `BaselineStrategy` over aligned, continuous
hourly candles for pairs sharing one quote currency. It uses a default initial
portfolio value of 10,000 units of that quote currency. Signals use completed
candle closes and are rebalanced at the next candle's open; the final signal
is not executed without a following open.

`BacktestResult` includes initial and final values, total return, maximum
hourly drawdown, UTC daily returns, fees paid, and active trading days. The
replay uses raw strategy weights and does not apply the live risk planner's
caps or circuit breakers. It uses fractional quantities and does not model
exchange precision, minimum order sizes, spread, or slippage. Turnover is
estimated as `volume * close` over the latest 24 candles, assuming volume is in
the base asset; sampled snapshot bars have zero volume and do not pass the
default turnover filter.

A bundled historical dataset, backtest CLI command, and recorded v0 scorecard
were not found in the repository. Reproducing a score therefore requires
supplying aligned candle data to `run_backtest`; a specific v0 score is **Not
documented**.

See [Historical backtesting](docs/backtesting.md) for further assumptions and
input validation.

## Logs and local state

The CLI emits INFO-level logs to the console. Local SQLite files store market
snapshots, execution records, cycle decisions, and risk state:

| File | Contents |
| --- | --- |
| `data/market.sqlite3` | Market snapshots and hourly candles |
| `data/execution.sqlite3` | Order intents, results, fill and fee details, wallet snapshots, and pending-order counts |
| `data/runtime.sqlite3` | Cycle decisions and persisted portfolio risk state |

These data files are ignored by Git. Decision records include non-secret
settings and their fingerprint, exchange rules, market and wallet snapshots,
available history, targets, risk plan, execution results, and errors. API keys
and secrets are excluded.

## AWS deployment

An AWS run or deployment procedure is **Not documented** in this repository.

## Limitations and v0 scope

Implemented v0 scope is the baseline strategy, Roostoo API reads, local SQLite
storage, risk-checked order planning, dry-run and live execution paths,
reconciliation, cycle logging, and a Python backtest function.

Known backtest limitations include no exchange precision or minimum-order
simulation and no spread or slippage model. A bundled dataset and scorecard
were not found in the repository. Sampled snapshot bars have no volume. The
runtime does not fill data gaps. A strategy
version comparison and promotion process is **Not documented**. AWS deployment
is **Not documented** in the repository.

## Project layout

- `src/fourfunds/settings.py` — validated run modes and environment settings
- `src/fourfunds/models.py` — shared data structures
- `src/fourfunds/api.py` — Roostoo HTTP client
- `src/fourfunds/data.py` — market-history storage and CSV loader
- `src/fourfunds/strategy.py` — v0 baseline strategy
- `src/fourfunds/risk.py` — portfolio valuation and order planning
- `src/fourfunds/execution.py` — order submission and reconciliation
- `src/fourfunds/execution_store.py` — execution journal and trade report
- `src/fourfunds/runtime.py` — bot-cycle orchestration
- `src/fourfunds/runtime_store.py` — cycle journal and persisted risk state
- `src/fourfunds/main.py` — dependency wiring and hourly scheduling
- `src/fourfunds/backtest.py` — historical replay function
