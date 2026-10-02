# Command-line entry point.

from fourfunds.settings import RunMode, load_settings


def main() -> None:
    settings = load_settings()
    print(f"Run mode: {settings.mode.value}.")
    if settings.mode is RunMode.LIVE:
        raise SystemExit("Live trading is not available yet.")
    print("Trading is not implemented yet.")
