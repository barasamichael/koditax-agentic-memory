from typing import Any

def initialize(username: str, api_key: str) -> None: ...

class _SMS:
    def send(
        self,
        message: str,
        recipients: list[str],
        sender_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

SMS: _SMS
