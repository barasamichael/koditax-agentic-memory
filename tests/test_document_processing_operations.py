"""Milestone 9 operation authority and compatibility invariants."""

from __future__ import annotations

from uuid import uuid4
from pathlib import Path

import pytest

from services.document_ai.app.processing_operations import ProcessingOperation
from services.document_ai.app.processing_operations import request_cancellation
from services.document_ai.app.processing_operations import transition_operation
from services.document_ai.app.processing_operations import validate_operation_kind
from services.document_ai.app.processing_operations import ProcessingOperationError


def _operation() -> ProcessingOperation:
    return ProcessingOperation(
        processing_operation_id=uuid4(), tenant_id="tenant-a", document_id=uuid4(),
        document_version_id=uuid4(), operation_kind="general_document_understanding",
    )


def test_general_operation_kind_rejects_extraction_provider_and_document_type_semantics() -> None:
    for invalid in ("p9_extraction", "invoice", "textract", "openai", "extraction_profile"):
        with pytest.raises(ProcessingOperationError, match="invalid_processing_operation_kind"):
            validate_operation_kind(invalid)


def test_cancellation_is_durable_and_prevents_unguarded_completion() -> None:
    cancelled_request = request_cancellation(_operation())
    assert cancelled_request.cancellation_requested is True
    running = transition_operation(_operation(), "running")
    with pytest.raises(
        ProcessingOperationError, match="processing_operation_cancellation_requested"
    ):
        transition_operation(
            request_cancellation(running), "succeeded", result_reference="representation:1"
        )


def test_terminal_operation_cannot_be_overwritten_by_stale_update() -> None:
    complete = transition_operation(
        transition_operation(_operation(), "running"),
        "succeeded",
        result_reference="representation:1",
    )
    with pytest.raises(ProcessingOperationError, match="processing_operation_terminal"):
        transition_operation(complete, "failed", failure_category="internal")


def test_upload_confirmation_uses_inspection_operation_and_never_creates_extraction_job() -> None:
    source = Path("services/document_ai/app/document_registry.py").read_text(encoding="utf-8")
    transaction = source[source.index("def _register_persistent_confirmation_transaction"):]
    assert "source_inspection" in transaction
    assert "general_document_understanding_requested" not in transaction
    assert "processing_operation_id=operation_id" in transaction
    endpoint = Path("services/document_ai/app/main.py").read_text(encoding="utf-8")
    confirmation = endpoint[endpoint.index("def register_upload_completion_endpoint"):]
    assert "register_extraction_job(" not in confirmation
    assert "/extractions" not in endpoint
