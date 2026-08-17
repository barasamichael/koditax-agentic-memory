"""Test service contract guard communication map enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.validation.service_contract_guard import GuardResult
from shared.validation.service_contract_guard import UnknownServiceInMapError
from shared.validation.service_contract_guard import run_service_contract_guard
from shared.validation.service_contract_guard import UndeclaredServiceCallError
from shared.validation.service_contract_guard import ServiceContractGuardFailure
from shared.validation.service_contract_guard import MissingContractReferenceError
from shared.validation.service_contract_guard import enforce_service_contract_guard
from shared.validation.service_contract_guard import PhantomServiceCallDeclarationError
from shared.validation.service_contract_guard import DuplicateServiceCallDeclarationError


def test_service_contract_guard_passes_when_call_is_declared(tmp_path: Path) -> None:
    """Verify detected gateway-to-event_store call passes with valid declaration.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=True)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {
                    "outbound_calls": [
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:ping_tool"],
                        }
                    ]
                },
                "event_store": {"outbound_calls": []},
            }
        },
    )

    result = run_service_contract_guard(tmp_path)

    assert result == GuardResult(success=True, issues=())
    enforce_service_contract_guard(tmp_path)


def test_service_contract_guard_fails_when_detected_call_is_undeclared(tmp_path: Path) -> None:
    """Verify undeclared detected call fails with UndeclaredServiceCallError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=True)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {"outbound_calls": []},
                "event_store": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(isinstance(error, UndeclaredServiceCallError) for error in raised.value.errors)


def test_service_contract_guard_detects_symbolic_internal_base_url(tmp_path: Path) -> None:
    """Verify symbolic base_url resolving to service host requires declaration.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=False)
    _write_text(
        tmp_path / "services" / "gateway" / "app" / "main.py",
        (
            "import httpx\n\n"
            "DEFAULT_EVENT_STORE_BASE_URL = 'http://event-store'\n\n"
            "class HttpEventStoreAuditClient:\n"
            "    def __init__(self, base_url: str = DEFAULT_EVENT_STORE_BASE_URL) -> None:\n"
            "        self._base_url = base_url\n\n"
            "    async def append_audit_event(self) -> None:\n"
            "        async with httpx.AsyncClient(base_url=self._base_url) as client:\n"
            "            _ = client\n"
        ),
    )
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {"outbound_calls": []},
                "event_store": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(isinstance(error, UndeclaredServiceCallError) for error in raised.value.errors)


def test_service_contract_guard_ignores_localhost_literal_base_url(tmp_path: Path) -> None:
    """Verify localhost literal base_url does not count as internal service call.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=False)
    _write_text(
        tmp_path / "services" / "gateway" / "app" / "main.py",
        (
            "import httpx\n\n"
            "async def call_local() -> None:\n"
            "    async with httpx.AsyncClient(base_url='http://localhost:8001') as client:\n"
            "        _ = client\n"
        ),
    )
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {"outbound_calls": []},
                "event_store": {"outbound_calls": []},
            }
        },
    )

    result = run_service_contract_guard(tmp_path)

    assert result == GuardResult(success=True, issues=())
    enforce_service_contract_guard(tmp_path)


def test_service_contract_guard_fails_for_unknown_service_in_map(tmp_path: Path) -> None:
    """Verify unknown service key in map fails with UnknownServiceInMapError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=True)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {"outbound_calls": []},
                "event_store": {"outbound_calls": []},
                "unknown_service": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(isinstance(error, UnknownServiceInMapError) for error in raised.value.errors)


def test_service_contract_guard_fails_when_referenced_contract_is_missing(tmp_path: Path) -> None:
    """Verify missing referenced contract file fails with MissingContractReferenceError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=True)
    (tmp_path / "contracts" / "openapi" / "event_store.yaml").unlink()
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {
                    "outbound_calls": [
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:ping_tool"],
                        }
                    ]
                },
                "event_store": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(isinstance(error, MissingContractReferenceError) for error in raised.value.errors)


def test_service_contract_guard_fails_for_duplicate_declarations(tmp_path: Path) -> None:
    """Verify duplicate caller-target declarations fail with duplicate violation.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=True)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {
                    "outbound_calls": [
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:first_call"],
                        },
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:second_call"],
                        },
                    ]
                },
                "event_store": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(
        isinstance(error, DuplicateServiceCallDeclarationError) for error in raised.value.errors
    )


def test_service_contract_guard_allows_planned_declaration_without_detected_call(
    tmp_path: Path,
) -> None:
    """Verify planned declarations are allowed when reason is present.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=False)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {
                    "outbound_calls": [
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:planned_call"],
                            "planned": True,
                            "reason": "Pending runtime endpoint wiring.",
                        }
                    ]
                },
                "event_store": {"outbound_calls": []},
            }
        },
    )

    result = run_service_contract_guard(tmp_path)

    assert result == GuardResult(success=True, issues=())
    enforce_service_contract_guard(tmp_path)


def test_service_contract_guard_fails_for_phantom_non_planned_declaration(tmp_path: Path) -> None:
    """Verify non-planned declarations fail when no call is detected in code.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_fake_repo(tmp_path, gateway_calls_event_store=False)
    _write_map(
        tmp_path,
        {
            "services": {
                "gateway": {
                    "outbound_calls": [
                        {
                            "target_service": "event_store",
                            "contract": "contracts/openapi/event_store.yaml",
                            "call_sites": ["services/gateway/app/main.py:declared_call"],
                        }
                    ]
                },
                "event_store": {"outbound_calls": []},
            }
        },
    )

    with pytest.raises(ServiceContractGuardFailure) as raised:
        enforce_service_contract_guard(tmp_path)

    assert any(
        isinstance(error, PhantomServiceCallDeclarationError) for error in raised.value.errors
    )


def _create_fake_repo(tmp_path: Path, gateway_calls_event_store: bool) -> None:
    _write_text(
        tmp_path / "services" / "gateway" / "app" / "main.py",
        _gateway_source(gateway_calls_event_store),
    )
    _write_text(
        tmp_path / "services" / "event_store" / "app" / "main.py",
        "def append_event() -> None:\n    return None\n",
    )
    _write_text(tmp_path / "contracts" / "openapi" / "gateway.yaml", _openapi_document("gateway"))
    _write_text(
        tmp_path / "contracts" / "openapi" / "event_store.yaml",
        _openapi_document("event_store"),
    )


def _gateway_source(gateway_calls_event_store: bool) -> str:
    if gateway_calls_event_store:
        return (
            "import httpx\n\n"
            "def call_event_store() -> None:\n"
            "    client = httpx.Client(base_url='http://event-store')\n"
            "    _ = client\n"
        )

    return "def no_calls() -> None:\n    return None\n"


def _openapi_document(service_name: str) -> str:
    return f"openapi: 3.1.0\ninfo:\n  title: {service_name}\n  version: '1.0.0'\npaths: {{}}\n"


def _write_map(tmp_path: Path, document: dict[str, object]) -> None:
    _write_text(
        tmp_path / "contracts" / "service_communication_map.json",
        json.dumps(document, indent=2),
    )


def _write_text(file_path: Path, contents: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(contents, encoding="utf-8")
