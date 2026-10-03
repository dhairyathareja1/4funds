# 4Funds

4Funds is a modular trading bot for the Roostoo APAC University Quant Trading
Hackathon. It includes validated settings, the Roostoo API client, market-history
storage, the v0 baseline strategy, risk-checked order planning, order execution,
and hourly cycle orchestration with durable decision logs. Historical backtesting
is available through `run_backtest`; its assumptions and metrics are described
in [Backtesting](docs/backtesting.md). The replay does not submit orders.

## v0 strategy

The baseline is a deterministic, long-only hourly trend strategy. A pair must be
tradable, meet the configured 24-hour quote-turnover minimum, and have enough
contiguous hourly candles. Its current quote must have positive momentum over
the configured lookback and sit above the average of the latest hourly closes.
Qualifying pairs are ranked by momentum, with pair name breaking ties, and the
top `top_k` receive equal target weights. If no pair qualifies, the strategy
returns a full-cash target (`pair=None`) with a reason. Risk controls cap
exposure and apply daily-loss and drawdown limits.

## Project layout

- `src/fourfunds/settings.py` — validated run mode and environment configuration
- `src/fourfunds/models.py` — shared data structures between modules
- `src/fourfunds/api.py` — Roostoo HTTP client
- `src/fourfunds/data.py` — market-history storage
- `src/fourfunds/strategy.py` — baseline target-weight strategy
- `src/fourfunds/risk.py` — portfolio valuation and order planning
- `src/fourfunds/execution.py` — market-order submission and reconciliation
- `src/fourfunds/execution_store.py` — durable execution journal and trade report
- `src/fourfunds/runtime.py` — bot-cycle orchestration and decision capture
- `src/fourfunds/runtime_store.py` — cycle logs and persisted risk state
- `src/fourfunds/main.py` — dependency wiring and hourly scheduling
- `src/fourfunds/backtest.py` — historical replay interface

The corresponding work is tracked in the repository's v0 milestone and issues.

## Local setup

Requires Python 3.10 or newer.

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -e .
    Copy-Item .env.example .env
    python -m fourfunds --once

Set API credentials in `.env` before running a cycle. `--once` runs one cycle;
without it, the bot runs at the configured hourly interval. `--report` prints
the number of orders with recorded fills and the UTC dates those fills were
recorded. The report reads the execution journal and does not create sample
trades.

## Configuration

Copy `.env.example` to `.env`. The default mode is `dry_run`; account reads need
API credentials in every mode. Only `live` submits exchange orders. Live mode
also requires an explicitly configured HTTPS `ROOSTOO_BASE_URL`.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `BOT_MODE` | `dry_run` | `read_only`, `dry_run`, or `live` |
| `BOT_CYCLE_INTERVAL_SECONDS` | `3600` | Cycle interval in whole hours |
| `STRATEGY_MOMENTUM_LOOKBACK_HOURS` | `24` | Momentum window |
| `STRATEGY_TREND_LOOKBACK_HOURS` | `168` | Trend average window |
| `STRATEGY_TOP_K` | `3` | Maximum number of selected assets |
| `STRATEGY_MINIMUM_QUOTE_TURNOVER_24H` | `100000` | Minimum 24-hour quote turnover |
| `RISK_MAX_ASSET_WEIGHT` | `0.25` | Maximum weight per asset |
| `RISK_MAX_TOTAL_EXPOSURE` | `0.75` | Maximum portfolio exposure |
| `RISK_MINIMUM_CASH_RESERVE` | `0.10` | Minimum portfolio cash reserve |
| `RISK_MAX_DAILY_LOSS_FRACTION` | `0.03` | Daily loss circuit-breaker threshold |
| `RISK_MAX_DRAWDOWN_FRACTION` | `0.10` | Drawdown circuit-breaker threshold |
| `RISK_MAX_QUOTE_AGE_SECONDS` | `120` | Maximum quote age for order planning |
| `RISK_FEE_RATE` | `0.001` | Fee allowance per trade |

Weights, loss limits, and the fee rate are decimal fractions: `0.25` means 25%.
Configuration values and fingerprints are stored with each decision; API keys
and secrets are excluded from logs and settings representations.

The risk planner's inputs, output, portfolio valuation, and circuit-breaker
behavior are described in [Risk planning](docs/risk-planning.md). Order
submission, reconciliation, fee estimates, and timeout handling are described
in [Order execution](docs/execution.md).

The hourly cycle, restart behavior, decision logs, and trade report are described
in [Runtime orchestration](docs/runtime.md).

## Safety

- The default mode is `dry_run` and records intents without submitting orders.
- Live submission requires explicit `BOT_MODE=live` and validated credentials.
- Stale market snapshots prevent the cycle from reaching order execution.
- Unresolved live orders are reconciled before another cycle can submit orders.
- Keep credentials in environment variables; never commit `.env` or paste keys
  into source, issues, logs, or README files.
- The resource-pack PDF is intentionally ignored by Git because it contains
  credentials.
