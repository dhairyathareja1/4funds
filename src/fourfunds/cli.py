# Command-line entry point.

from fourfunds.settings import RunMode, load_settings


def main() -> None:
    settings = load_settings()
    if settings.mode is RunMode.LIVE:
        raise SystemExit("Live trading is not available yet.")
    print(f"4Funds is configured for {settings.mode.value} mode.")
    print("Trading is not implemented yet.")
