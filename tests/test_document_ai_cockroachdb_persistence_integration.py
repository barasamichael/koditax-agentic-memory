from __future__ import annotations

from uuid import uuid4
from typing import Any
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Barrier
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg

from services.document_ai.app import persistence_support
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.upload_sessions import UploadSessionRecord
from services.document_ai.app.upload_sessions import PersistentUploadSessionStore
from services.document_ai.app.document_registry import UploadCompletionRequest
from services.document_ai.app.document_registry import PersistentDocumentRegistryStore
from services.document_ai.app.document_registry import register_durable_upload_confirmation
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.persistence_support import close_document_ai_connection_pool
from services.document_ai.app.persistence_support import resolve_document_ai_persistence_status
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction


def test_document_ai_pooled_connection_recovers_from_aborted_transaction() -> None:
    database_url = _resolve_document_ai_database_url()
    close_document_ai_connection_pool(database_url=database_url)

    with pytest.raises(psycopg.Error):
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


def test_document_ai_pooled_connections_support_parallel_queries() -> None:
    database_url = _resolve_document_ai_database_url()
    close_document_ai_connection_pool(database_url=database_url)

    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)

    def _round_trip(value: int) -> int:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s", (value,))
                row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(_round_trip, range(12)))

    assert results == list(range(12))


def test_document_ai_transaction_executor_retries_real_cockroachdb_serialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _resolve_document_ai_database_url()
    close_document_ai_connection_pool(database_url=database_url)
    monkeypatch.setenv("DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS", "1")
    monkeypatch.setenv("DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS", "10")

    login_id_normalized = f"txn-retry-{uuid4().hex}"
    source_ip = "198.51.100.10"
    attempt_counts = {"a": 0, "b": 0}
    barrier = Barrier(2)

    try:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_login_lockouts (
                        login_id_normalized, source_ip, failed_attempt_count,
                        last_failed_attempt_at, lockout_expires_at
                    ) VALUES (%s, %s, 0, NULL, NULL)
                    ON CONFLICT (login_id_normalized, source_ip) DO UPDATE SET
                        failed_attempt_count = EXCLUDED.failed_attempt_count,
                        last_failed_attempt_at = EXCLUDED.last_failed_attempt_at,
                        lockout_expires_at = EXCLUDED.lockout_expires_at
                    """,
                    (login_id_normalized, source_ip),
                )
            connection.commit()

        def _increment_revision(label: str) -> int:
            def _callback(cursor: psycopg.Cursor[Any]) -> int:
                attempt_counts[label] += 1
                cursor.execute(
                    """
                    SELECT failed_attempt_count
                    FROM auth_login_lockouts
                    WHERE login_id_normalized = %s AND source_ip = %s
                    """,
                    (login_id_normalized, source_ip),
                )
                row = cursor.fetchone()
                assert row is not None
                current_revision = int(row[0])
                if attempt_counts[label] == 1:
                    barrier.wait(timeout=15)
                cursor.execute(
                    """
                    UPDATE auth_login_lockouts
                    SET failed_attempt_count = %s, last_failed_attempt_at = now()
                    WHERE login_id_normalized = %s AND source_ip = %s
                    """,
                    (current_revision + 1, login_id_normalized, source_ip),
                )
                return current_revision + 1

            return execute_document_ai_database_transaction(
                database_url=database_url,
                transaction_name=f"document_ai.test.contention.{label}",
                transaction_callback=_callback,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_increment_revision, ("a", "b")))

        assert sorted(results) == [1, 2]
        assert max(attempt_counts.values()) >= 2

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT failed_attempt_count
                    FROM auth_login_lockouts
                    WHERE login_id_normalized = %s AND source_ip = %s
                    """,
                    (login_id_normalized, source_ip),
                )
                row = cursor.fetchone()
                assert row == (2,)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
    finally:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM auth_login_lockouts
                    WHERE login_id_normalized = %s AND source_ip = %s
                    """,
                    (login_id_normalized, source_ip),
                )
            connection.commit()


def test_document_ai_upload_completion_reconciles_ambiguous_commit_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _resolve_document_ai_database_url()
    if (
        resolve_document_ai_persistence_status(
            database_url=database_url,
            required_tables=(
                "document_ai_upload_sessions",
                "document_ai_documents",
                "document_ai_document_versions",
                "document_ai_source_artifacts",
                "document_ai_processing_operations",
                "document_ai_processing_work_items",
                "document_ai_processing_outbox",
                "document_ai_completion_idempotency",
            ),
        )
        != "ready"
    ):
        pytest.skip("Document AI persistence schema is not available.")
    close_document_ai_connection_pool(database_url=database_url)

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    session_id = uuid4()
    document_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    object_key = build_tenant_document_object_key(tenant_id, document_id)
    checksum_sha256 = "a" * 64
    session_record = UploadSessionRecord(
        session_id=session_id,
        document_id=document_id,
        session_state="active",
        created_at=_utc_now_iso(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        content_type="application/pdf",
        expected_size_bytes=1024,
        checksum_sha256=checksum_sha256,
        expires_at=_utc_future_iso(minutes=15),
        original_filename="invoice.pdf",
        storage_provider="in_memory",
        storage_key=object_key,
        completed_at=None,
    )
    upload_completion_request = UploadCompletionRequest(
        session_id=session_id,
        object_key=object_key,
        checksum_sha256=checksum_sha256,
        size_bytes=1024,
        content_type="application/pdf",
    )
    store = PersistentDocumentRegistryStore(database_url=database_url)

    class _AmbiguousCommitError(psycopg.Error):
        def __init__(self) -> None:
            super().__init__("simulated ambiguous commit")
            self.sqlstate = "40003"

    class _AmbiguousCommitTransaction:
        def __init__(self, inner: Any, *, commit_error: BaseException | None) -> None:
            self._inner = inner
            self._commit_error = commit_error

        def __enter__(self) -> Any:
            return self._inner.__enter__()

        def __exit__(
            self,
            exc_type: object | None,
            exc: object | None,
            tb: object | None,
        ) -> bool:
            outcome = self._inner.__exit__(exc_type, exc, tb)
            if exc_type is None and self._commit_error is not None:
                raise self._commit_error
            return outcome

    class _AmbiguousCommitConnection:
        def __init__(self, inner: Any, *, commit_error: BaseException | None) -> None:
            self._inner = inner
            self._commit_error = commit_error

        def transaction(self) -> _AmbiguousCommitTransaction:
            return _AmbiguousCommitTransaction(
                self._inner.transaction(),
                commit_error=self._commit_error,
            )

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    original_connect = persistence_support.connect_document_ai_database
    commit_state = {"should_raise": True}

    @contextmanager
    def _ambiguous_connection(database_url_value: str):
        with original_connect(database_url_value) as connection:
            commit_error = _AmbiguousCommitError() if commit_state["should_raise"] else None
            commit_state["should_raise"] = False
            yield _AmbiguousCommitConnection(connection, commit_error=commit_error)

    monkeypatch.setattr(persistence_support, "connect_document_ai_database", _ambiguous_connection)

    try:
        response = register_durable_upload_confirmation(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-document-ai-ambiguous",
            document_registry_store=store,
        )

        assert response.document.document_id == document_id
        assert response.document.state == "processing"
        assert response.processing_operation_id is not None
        assert store.get_completion(idempotency_key) is not None

        replay = register_durable_upload_confirmation(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-document-ai-ambiguous-replay",
            document_registry_store=store,
        )

        assert replay.model_dump(mode="json") == response.model_dump(mode="json")

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_completion_idempotency
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                assert cursor.fetchone() == (1,)
    finally:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_outbox
                    WHERE processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_work_items
                    WHERE processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_operations
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_source_artifacts
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_completion_idempotency
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_upload_sessions
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_documents
                    WHERE document_id = %s
                    """,
                    (document_id,),
                )
            connection.commit()


def test_document_ai_upload_completion_completes_upload_session_inside_transaction() -> None:
    database_url = _resolve_document_ai_database_url()
    if (
        resolve_document_ai_persistence_status(
            database_url=database_url,
            required_tables=(
                "document_ai_upload_sessions",
                "document_ai_documents",
                "document_ai_document_versions",
                "document_ai_source_artifacts",
                "document_ai_processing_operations",
                "document_ai_processing_work_items",
                "document_ai_processing_outbox",
                "document_ai_completion_idempotency",
            ),
        )
        != "ready"
    ):
        pytest.skip("Document AI persistence schema is not available.")
    upload_session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    session_id = uuid4()
    document_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    object_key = build_tenant_document_object_key(tenant_id, document_id)
    checksum_sha256 = "c" * 64
    session_record = UploadSessionRecord(
        session_id=session_id,
        document_id=document_id,
        session_state="active",
        created_at=_utc_now_iso(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        content_type="application/pdf",
        expected_size_bytes=1536,
        checksum_sha256=checksum_sha256,
        expires_at=_utc_future_iso(minutes=15),
        original_filename="invoice.pdf",
        storage_provider="in_memory",
        storage_key=object_key,
        completed_at=None,
    )
    upload_session_store.set_session(session_record)

    try:
        response = register_durable_upload_confirmation(
            upload_completion_request=UploadCompletionRequest(
                session_id=session_id,
                object_key=object_key,
                checksum_sha256=checksum_sha256,
                size_bytes=1536,
                content_type="application/pdf",
            ),
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-persistent-completion",
            document_registry_store=registry_store,
        )
        replay = register_durable_upload_confirmation(
            upload_completion_request=UploadCompletionRequest(
                session_id=session_id,
                object_key=object_key,
                checksum_sha256=checksum_sha256,
                size_bytes=1536,
                content_type="application/pdf",
            ),
            session_record=upload_session_store.get_session(session_id) or session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-persistent-completion-replay",
            document_registry_store=registry_store,
        )

        assert replay.model_dump(mode="json") == response.model_dump(mode="json")
        assert response.processing_operation_id is not None

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT session_state, completed_at, session_record
                    FROM document_ai_upload_sessions
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
                session_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_outbox
                    WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                )
                outbox_row = cursor.fetchone()

        assert session_row is not None
        assert str(session_row[0]) == "completed"
        assert session_row[1] is not None
        assert session_row[2]["session_state"] == "completed"
        assert outbox_row == (1,)
    finally:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_outbox
                    WHERE tenant_id = %s AND processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_work_items
                    WHERE tenant_id = %s AND processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_operations
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_source_artifacts
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_completion_idempotency
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_upload_sessions
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_documents
                    WHERE document_id = %s
                    """,
                    (document_id,),
                )
            connection.commit()


def _resolve_document_ai_database_url() -> str:
    database_url = load_document_ai_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("Document AI CockroachDB URL is not configured.")
    return database_url


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _utc_future_iso(*, minutes: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=minutes)).isoformat()
