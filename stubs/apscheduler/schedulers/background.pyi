from collections.abc import Callable

class BackgroundScheduler:
    def __init__(self) -> None: ...
    def add_job(
        self,
        func: Callable[[], object],
        *,
        trigger: object,
        id: str,
        replace_existing: bool,
        coalesce: bool,
        max_instances: int,
    ) -> object: ...
    def start(self) -> None: ...
    def shutdown(self, *, wait: bool = ...) -> None: ...
