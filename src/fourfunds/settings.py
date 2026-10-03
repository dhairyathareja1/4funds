# Application settings.

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://mock-api.roostoo.com"
DEFAULT_MOMENTUM_LOOKBACK_HOURS = 24
DEFAULT_TREND_LOOKBACK_HOURS = 168
DEFAULT_TOP_K = 3
DEFAULT_MINIMUM_QUOTE_TURNOVER_24H = Decimal("100000")
DEFAULT_MAX_ASSET_WEIGHT = Decimal("0.25")
DEFAULT_MAX_TOTAL_EXPOSURE = Decimal("0.75")
DEFAULT_MINIMUM_CASH_RESERVE = Decimal("0.10")
DEFAULT_MAX_DAILY_LOSS_FRACTION = Decimal("0.03")
DEFAULT_MAX_DRAWDOWN_FRACTION = Decimal("0.10")
DEFAULT_DRAWDOWN_COOLDOWN_HOURS = 24
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120
DEFAULT_FEE_RATE = Decimal("0.001")
DEFAULT_CYCLE_INTERVAL_SECONDS = 60 * 60
SECONDS_PER_HOUR = 60 * 60


class RunMode(str, Enum):
    READ_ONLY = "read_only"
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass(frozen=True)
class Settings:
    mode: RunMode
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    momentum_lookback_hours: int = DEFAULT_MOMENTUM_LOOKBACK_HOURS
    trend_lookback_hours: int = DEFAULT_TREND_LOOKBACK_HOURS
    top_k: int = DEFAULT_TOP_K
    minimum_quote_turnover_24h: Decimal = DEFAULT_MINIMUM_QUOTE_TURNOVER_24H
    max_asset_weight: Decimal = DEFAULT_MAX_ASSET_WEIGHT
    max_total_exposure: Decimal = DEFAULT_MAX_TOTAL_EXPOSURE
    minimum_cash_reserve: Decimal = DEFAULT_MINIMUM_CASH_RESERVE
    max_daily_loss_fraction: Decimal = DEFAULT_MAX_DAILY_LOSS_FRACTION
    max_drawdown_fraction: Decimal = DEFAULT_MAX_DRAWDOWN_FRACTION
    drawdown_cooldown_hours: int = DEFAULT_DRAWDOWN_COOLDOWN_HOURS
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS
    fee_rate: Decimal = DEFAULT_FEE_RATE
    cycle_interval_seconds: int = DEFAULT_CYCLE_INTERVAL_SECONDS
    market_history_csv: str | None = None


def _read_int(
    source: Mapping[str, str], name: str, default: int, *, minimum: int = 1
) -> int:
    raw_value = source.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        raise ValueError(f"{name} must be an integer.") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def _read_decimal(source: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    raw_value = source.get(name, str(default)).strip()
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a decimal number.") from None
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")
    return value


def _read_fraction(
    source: Mapping[str, str], name: str, default: Decimal, *, allow_zero: bool = True
) -> Decimal:
    value = _read_decimal(source, name, default)
    if value > 1 or value < 0 or (not allow_zero and value == 0):
        lower_bound = "greater than 0" if not allow_zero else "at least 0"
        raise ValueError(f"{name} must be {lower_bound} and at most 1.")
    return value


def _validate_base_url(raw_url: str, mode: RunMode) -> str:
    base_url = raw_url.strip().rstrip("/")
    try:
        parsed_url = urlsplit(base_url)
        hostname = parsed_url.hostname
        _ = parsed_url.port
    except ValueError:
        raise ValueError(
            "ROOSTOO_BASE_URL must be a valid absolute HTTP(S) URL."
        ) from None

    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or not hostname
        or any(character.isspace() for character in hostname)
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError(
            "ROOSTOO_BASE_URL must be an absolute HTTP(S) URL "
            "without credentials, query, or fragment."
        )
    if mode is RunMode.LIVE and parsed_url.scheme != "https":
        raise ValueError("Live mode requires an HTTPS ROOSTOO_BASE_URL.")
    return base_url


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    if environ is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = environ

    raw_mode = source.get("BOT_MODE", RunMode.DRY_RUN.value).strip().lower()
    try:
        mode = RunMode(raw_mode)
    except ValueError:
        allowed = ", ".join(item.value for item in RunMode)
        raise ValueError(f"BOT_MODE must be one of: {allowed}.") from None

    if mode is RunMode.LIVE and not source.get("ROOSTOO_BASE_URL", "").strip():
        raise ValueError("Live mode requires an explicit ROOSTOO_BASE_URL.")

    base_url = _validate_base_url(
        source.get("ROOSTOO_BASE_URL", DEFAULT_BASE_URL), mode
    )
    api_key = source.get("ROOSTOO_API_KEY", "").strip() or None
    api_secret = source.get("ROOSTOO_API_SECRET", "").strip() or None
    if mode is RunMode.LIVE and not (api_key and api_secret):
        raise ValueError("Live mode requires ROOSTOO_API_KEY and ROOSTOO_API_SECRET.")

    momentum_lookback_hours = _read_int(
        source, "STRATEGY_MOMENTUM_LOOKBACK_HOURS", DEFAULT_MOMENTUM_LOOKBACK_HOURS
    )
    trend_lookback_hours = _read_int(
        source,
        "STRATEGY_TREND_LOOKBACK_HOURS",
        DEFAULT_TREND_LOOKBACK_HOURS,
        minimum=2,
    )
    top_k = _read_int(source, "STRATEGY_TOP_K", DEFAULT_TOP_K)
    minimum_quote_turnover_24h = _read_decimal(
        source,
        "STRATEGY_MINIMUM_QUOTE_TURNOVER_24H",
        DEFAULT_MINIMUM_QUOTE_TURNOVER_24H,
    )
    if minimum_quote_turnover_24h < 0:
        raise ValueError("STRATEGY_MINIMUM_QUOTE_TURNOVER_24H cannot be negative.")

    max_asset_weight = _read_fraction(
        source, "RISK_MAX_ASSET_WEIGHT", DEFAULT_MAX_ASSET_WEIGHT, allow_zero=False
    )
    max_total_exposure = _read_fraction(
        source, "RISK_MAX_TOTAL_EXPOSURE", DEFAULT_MAX_TOTAL_EXPOSURE, allow_zero=False
    )
    minimum_cash_reserve = _read_fraction(
        source, "RISK_MINIMUM_CASH_RESERVE", DEFAULT_MINIMUM_CASH_RESERVE
    )
    max_daily_loss_fraction = _read_fraction(
        source,
        "RISK_MAX_DAILY_LOSS_FRACTION",
        DEFAULT_MAX_DAILY_LOSS_FRACTION,
        allow_zero=False,
    )
    max_drawdown_fraction = _read_fraction(
        source,
        "RISK_MAX_DRAWDOWN_FRACTION",
        DEFAULT_MAX_DRAWDOWN_FRACTION,
        allow_zero=False,
    )
    drawdown_cooldown_hours = _read_int(
        source,
        "RISK_DRAWDOWN_COOLDOWN_HOURS",
        DEFAULT_DRAWDOWN_COOLDOWN_HOURS,
    )
    max_quote_age_seconds = _read_int(
        source, "RISK_MAX_QUOTE_AGE_SECONDS", DEFAULT_MAX_QUOTE_AGE_SECONDS
    )
    fee_rate = _read_fraction(source, "RISK_FEE_RATE", DEFAULT_FEE_RATE)
    cycle_interval_seconds = _read_int(
        source,
        "BOT_CYCLE_INTERVAL_SECONDS",
        DEFAULT_CYCLE_INTERVAL_SECONDS,
        minimum=SECONDS_PER_HOUR,
    )
    if cycle_interval_seconds % SECONDS_PER_HOUR:
        raise ValueError("BOT_CYCLE_INTERVAL_SECONDS must be a multiple of one hour.")
    market_history_csv = source.get("MARKET_HISTORY_CSV", "").strip() or None

    if max_asset_weight > max_total_exposure:
        raise ValueError("RISK_MAX_ASSET_WEIGHT cannot exceed RISK_MAX_TOTAL_EXPOSURE.")
    if max_total_exposure + minimum_cash_reserve > 1:
        raise ValueError("Total exposure plus minimum cash reserve cannot exceed 1.")

    return Settings(
        mode=mode,
        base_url=base_url,
        api_key=api_key,
        api_secret=api_secret,
        momentum_lookback_hours=momentum_lookback_hours,
        trend_lookback_hours=trend_lookback_hours,
        top_k=top_k,
        minimum_quote_turnover_24h=minimum_quote_turnover_24h,
        max_asset_weight=max_asset_weight,
        max_total_exposure=max_total_exposure,
        minimum_cash_reserve=minimum_cash_reserve,
        max_daily_loss_fraction=max_daily_loss_fraction,
        max_drawdown_fraction=max_drawdown_fraction,
        drawdown_cooldown_hours=drawdown_cooldown_hours,
        max_quote_age_seconds=max_quote_age_seconds,
        fee_rate=fee_rate,
        cycle_interval_seconds=cycle_interval_seconds,
        market_history_csv=market_history_csv,
    )


def validate_runtime_settings(settings: Settings) -> None:
    if not isinstance(settings.mode, RunMode):
        raise ValueError("BOT_MODE must be a valid RunMode value.")
    if (
        not isinstance(settings.cycle_interval_seconds, int)
        or isinstance(settings.cycle_interval_seconds, bool)
        or settings.cycle_interval_seconds < SECONDS_PER_HOUR
        or settings.cycle_interval_seconds % SECONDS_PER_HOUR
    ):
        raise ValueError("BOT_CYCLE_INTERVAL_SECONDS must be a multiple of one hour.")
    if not (settings.api_key and settings.api_key.strip()):
        raise ValueError("Bot cycles require ROOSTOO_API_KEY for account reads.")
    if not (settings.api_secret and settings.api_secret.strip()):
        raise ValueError(
            "Bot cycles require ROOSTOO_API_SECRET for account reads."
        )
    if settings.mode is RunMode.LIVE:
        _validate_base_url(settings.base_url, settings.mode)
