# 4Funds

4Funds is a modular trading-bot project for the Roostoo APAC University Quant Trading Hackathon.

The repository is starting with a safe v0 scaffold. The modules and interfaces are in place, but the trading strategy, API integration, order execution, and backtest are not implemented yet. The initial CLI makes no network requests and places no orders.

## v0 strategy plan

The planned baseline is a low-frequency, long-or-cash trend strategy. It will consider tradable, liquid assets; favor assets with positive recent momentum that remain above a recent trend average; and hold cash when no assets qualify. Risk controls will cap exposure. The parameters below are starting defaults; backtest results will be documented after implementation.

## Project layout

- src/fourfunds/settings.py — validated run mode and environment configuration
- src/fourfunds/models.py — shared data structures between modules
- src/fourfunds/api.py — Roostoo HTTP client interface
- src/fourfunds/data.py — market-history storage interface
- src/fourfunds/strategy.py — baseline target-weight interface
- src/fourfunds/risk.py — portfolio valuation and order-planning interface
- src/fourfunds/execution.py — order submission and reconciliation interface
- src/fourfunds/runtime.py — bot-cycle orchestration interface
- src/fourfunds/backtest.py — historical replay interface

The corresponding work is tracked in the repository's v0 milestone and issues.

## Local setup

Requires Python 3.10 or newer.

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -e .
    Copy-Item .env.example .env
    python -m fourfunds

The CLI only reports the selected mode and states that trading is not implemented. Do not put real credentials in .env.example or commit .env. The resource-pack PDF is intentionally ignored by Git because it contains credentials.

## Configuration

Copy `.env.example` to `.env` to configure the bot. Strategy and risk values are non-secret settings that can be adjusted there. The defaults are:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `STRATEGY_MOMENTUM_LOOKBACK_HOURS` | `24` | Momentum window |
| `STRATEGY_TREND_LOOKBACK_HOURS` | `168` | Trend average window |
| `STRATEGY_TOP_K` | `3` | Maximum number of selected assets |
| `STRATEGY_MINIMUM_QUOTE_TURNOVER_24H` | `100000` | Minimum 24-hour turnover in the quote asset |
| `RISK_MAX_ASSET_WEIGHT` | `0.25` | Maximum weight per asset |
| `RISK_MAX_TOTAL_EXPOSURE` | `0.75` | Maximum portfolio exposure |
| `RISK_MINIMUM_CASH_RESERVE` | `0.10` | Minimum portfolio cash reserve |
| `RISK_MAX_DAILY_LOSS_FRACTION` | `0.03` | Daily loss circuit-breaker threshold |
| `RISK_MAX_DRAWDOWN_FRACTION` | `0.10` | Drawdown circuit-breaker threshold |
| `RISK_MAX_QUOTE_AGE_SECONDS` | `120` | Maximum quote age for order planning |
| `RISK_FEE_RATE` | `0.001` | Fee allowance per trade |

Weights, loss limits, and the fee rate are decimal fractions: `0.25` means 25%. `BOT_MODE` defaults to `dry_run`. `live` requires API credentials and an explicitly configured HTTPS `ROOSTOO_BASE_URL`; secrets are excluded from settings representations and startup output.

## Safety

- The default mode is dry_run.
- live mode is rejected until API integration and order execution are implemented.
- No manual or automated orders are sent by this scaffold.
- Use rotated credentials from the environment; never paste keys into source, issues, logs, or README files.
