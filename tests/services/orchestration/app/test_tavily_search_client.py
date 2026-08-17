"""Tavily client request-shape tests for orchestration web fallback."""

from __future__ import annotations

from datetime import date

import pytest

from services.orchestration.app.config import TavilyWebSearchConfig
from services.orchestration.app import tavily_search_client as tavily_module
from services.orchestration.app.tavily_search_client import TavilyWebSearchClient


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:
        return cls(2026, 7, 30)


def test_search_tax_topic_without_year_limits_tavily_to_three_calendar_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": []}

    class _DummyClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> _DummyClient:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> _DummyResponse:
            captured["url"] = url
            captured["json"] = json
            return _DummyResponse()

    monkeypatch.setattr(tavily_module, "date", _FixedDate)
    monkeypatch.setattr(tavily_module.httpx, "Client", _DummyClient)

    client = TavilyWebSearchClient(
        config=TavilyWebSearchConfig(
            api_key="test-key",
            timeout_seconds=3.0,
            max_results=5,
            enabled=True,
        )
    )

    client.search_tax_topic(
        query="What is VAT?",
        tax_year=None,
        jurisdiction="Kenya",
        tax_domain_hint="vat",
    )

    assert captured["url"] == "https://api.tavily.com/search"
    request_body = captured["json"]
    assert isinstance(request_body, dict)
    assert request_body["days"] == 942
