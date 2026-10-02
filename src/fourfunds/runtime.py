# Bot runtime orchestration.


class BotRunner:
    def run_once(self) -> None:
        raise NotImplementedError

    def run_forever(self) -> None:
        raise NotImplementedError
