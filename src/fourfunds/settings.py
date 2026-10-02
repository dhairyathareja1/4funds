# Application settings.

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from dotenv import load_dotenv


class RunMode(str, Enum):
    READ_ONLY = "read_only"
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass(frozen=True)
class Settings:
    mode: RunMode
    base_url: str
    api_key: str | None = None
    api_secret: str | None = None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    # Trading defaults to dry-run.
    if environ is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = environ
    raw_mode = source.get("BOT_MODE", RunMode.DRY_RUN.value).strip().lower()
    try:
        mode = RunMode(raw_mode)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in RunMode)
        raise ValueError(f"BOT_MODE must be one of: {allowed}") from exc

    api_key = source.get("ROOSTOO_API_KEY") or None
    api_secret = source.get("ROOSTOO_API_SECRET") or None
    if mode is RunMode.LIVE and not (api_key and api_secret):
        raise ValueError("Live mode requires ROOSTOO_API_KEY and ROOSTOO_API_SECRET.")

    return Settings(
        mode=mode,
        base_url=source.get(
            "ROOSTOO_BASE_URL", "https://mock-api.roostoo.com"
        ).rstrip("/"),
        api_key=api_key,
        api_secret=api_secret,
    )
