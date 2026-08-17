"""Bounded persistent conversation-state store for orchestration follow-up resolution."""

from __future__ import annotations

import os
import json
from datetime import datetime
from typing import NotRequired
from typing import cast
from typing import Protocol
from typing import TypedDict

import psycopg

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.request_timer import timed_print
from services.orchestration.app.action_execution_store import load_database_url


class ConversationStateStoreError(RuntimeError):
    """Represent deterministic persistence failure in conversation-state store."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class ConversationStateRecord(TypedDict):
    """Represent one stored conversation-state record."""

    execution_id: str
    tenant_id: str
    conversation_id: str
    user_id: str
    context_payload: dict[str, object]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]


class ConversationStateStore(Protocol):
    """Describe deterministic storage for orchestration conversation state."""

    def list_recent(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        """Return recent stored conversation-state records in deterministic order."""
        ...

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        """Return recent stored conversation-state records for one user."""
        ...

    def put(self, record: ConversationStateRecord) -> None:
        """Persist one conversation-state record deterministically."""
        ...

    def delete(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
    ) -> int:
        """Delete all persisted state for one scoped conversation and return removed rows."""
        ...

    def rename(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        conversation_title: str,
    ) -> int:
        """Rename all persisted state for one scoped conversation and return updated rows."""
        ...

    def clear(self) -> None:
        """Reset stored conversation-state records for deterministic test isolation."""
        ...

    def purge_expired(self) -> int:
        """Delete expired state records and return the number removed."""
        ...


class InMemoryConversationStateStore:
    """Provide deterministic in-memory conversation-state storage."""

    def __init__(self) -> None:
        self._sequence = 0
        self._records: list[tuple[int, ConversationStateRecord]] = []

    def list_recent(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        timed_print("[STATE_STORE] About to list recent in-memory conversation state")
        matched: list[tuple[int, ConversationStateRecord]] = []
        for sequence, record in self._records:
            if record["tenant_id"] != tenant_id:
                continue
            if record["conversation_id"] != conversation_id:
                continue
            if record["user_id"] != user_id:
                continue
            matched.append((sequence, record))
        matched.sort(key=lambda item: item[0], reverse=True)
        records = tuple(record for _, record in matched[:limit])
        timed_print(
            "[STATE_STORE] Listed recent in-memory conversation state "
            f"record_count={len(records)}"
        )
        return records

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        timed_print("[STATE_STORE] About to list user in-memory conversation state")
        matched: list[tuple[int, ConversationStateRecord]] = []
        for sequence, record in self._records:
            if record["tenant_id"] != tenant_id:
                continue
            if record["user_id"] != user_id:
                continue
            matched.append((sequence, record))
        matched.sort(key=lambda item: item[0], reverse=True)
        records = tuple(record for _, record in matched[:limit])
        timed_print(
            "[STATE_STORE] Listed user in-memory conversation state "
            f"record_count={len(records)}"
        )
        return records

    def put(self, record: ConversationStateRecord) -> None:
        timed_print("[STATE_STORE] About to persist in-memory conversation state")
        for index, (_, existing) in enumerate(self._records):
            if existing["execution_id"] != record["execution_id"]:
                continue
            if canonical_json_dumps(existing["context_payload"]) != canonical_json_dumps(
                record["context_payload"]
            ):
                raise ConversationStateStoreError(
                    reason_code="conversation_state_conflict",
                    message=(
                        "Conversation-state record conflicts with an existing execution context."
                    ),
                )
            self._records[index] = (self._records[index][0], record)
            timed_print(
                "[STATE_STORE] Persisted in-memory conversation state "
                f"execution_id={record['execution_id']}"
            )
            return
        self._sequence += 1
        self._records.append((self._sequence, record))
        timed_print(
            "[STATE_STORE] Persisted in-memory conversation state "
            f"execution_id={record['execution_id']}"
        )

    def delete(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
    ) -> int:
        timed_print("[STATE_STORE] About to delete in-memory conversation state")
        before = len(self._records)
        self._records = [
            (sequence, record)
            for sequence, record in self._records
            if not (
                record["tenant_id"] == tenant_id
                and record["conversation_id"] == conversation_id
                and record["user_id"] == user_id
            )
        ]
        deleted = before - len(self._records)
        timed_print(
            "[STATE_STORE] Deleted in-memory conversation state "
            f"conversation_id={conversation_id} deleted_count={deleted}"
        )
        return deleted

    def rename(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        conversation_title: str,
    ) -> int:
        timed_print("[STATE_STORE] About to rename in-memory conversation state")
        updated = 0
        for index, (_, record) in enumerate(self._records):
            if record["tenant_id"] != tenant_id:
                continue
            if record["conversation_id"] != conversation_id:
                continue
            if record["user_id"] != user_id:
                continue
            current_title = str(record["context_payload"].get("conversation_title") or "")
            if current_title == conversation_title:
                continue
            updated_record: ConversationStateRecord = {
                **record,
                "context_payload": {
                    **record["context_payload"],
                    "conversation_title": conversation_title,
                },
            }
            self._records[index] = (self._records[index][0], updated_record)
            updated += 1
        timed_print(
            "[STATE_STORE] Renamed in-memory conversation state "
            f"conversation_id={conversation_id} updated_count={updated}"
        )
        return updated

    def clear(self) -> None:
        self._records.clear()
        self._sequence = 0

    def purge_expired(self) -> int:
        """Return zero because in-memory state is process-local and non-durable."""
        return 0


class PersistentConversationStateStore:
    """Persist bounded conversation-state records in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url
        self._has_retention_expires_at = self._detect_retention_expires_at_column()

    def list_recent(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        timed_print("[STATE_STORE] About to list recent persistent conversation state")
        query = """
            SELECT execution_id, tenant_id, conversation_id, user_id, context_payload
            FROM orchestration_conversation_state_records
            WHERE tenant_id = %s
              AND conversation_id = %s
        """
        params: list[object] = [tenant_id, conversation_id]
        query += " AND user_id = %s"
        params.append(user_id)
        if self._has_retention_expires_at:
            query += " AND retention_expires_at > now()"
        query += """
            ORDER BY created_at DESC, execution_id DESC
            LIMIT %s
        """
        params.append(limit)
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            timed_print("[STATE_STORE] Persistent conversation-state listing failed")
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error

        normalized: list[ConversationStateRecord] = []
        for row in rows:
            normalized.append(
                {
                    "execution_id": cast(str, row[0]),
                    "tenant_id": cast(str, row[1]),
                    "conversation_id": cast(str, row[2]),
                    "user_id": cast(str, row[3]),
                    "context_payload": _coerce_json_object(row[4]),
                }
            )
        records = tuple(normalized)
        timed_print(
            "[STATE_STORE] Listed recent persistent conversation state "
            f"record_count={len(records)}"
        )
        return records

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationStateRecord, ...]:
        timed_print("[STATE_STORE] About to list user persistent conversation state")
        query = """
            SELECT execution_id, tenant_id, conversation_id, user_id, context_payload,
                   created_at, updated_at
            FROM orchestration_conversation_state_records
            WHERE tenant_id = %s
              AND user_id = %s
        """
        params: list[object] = [tenant_id, user_id]
        if self._has_retention_expires_at:
            query += " AND retention_expires_at > now()"
        query += """
            ORDER BY created_at DESC, execution_id DESC
            LIMIT %s
        """
        params.append(limit)
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            timed_print("[STATE_STORE] Persistent user conversation-state listing failed")
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error

        normalized: list[ConversationStateRecord] = []
        for row in rows:
            normalized.append(
                {
                    "execution_id": cast(str, row[0]),
                    "tenant_id": cast(str, row[1]),
                    "conversation_id": cast(str, row[2]),
                    "user_id": cast(str, row[3]),
                    "context_payload": _coerce_json_object(row[4]),
                    "created_at": serialize_database_timestamp(row[5]),
                    "updated_at": serialize_database_timestamp(row[6]),
                }
            )
        records = tuple(normalized)
        timed_print(
            "[STATE_STORE] Listed user persistent conversation state "
            f"record_count={len(records)}"
        )
        return records

    def put(self, record: ConversationStateRecord) -> None:
        timed_print("[STATE_STORE] About to persist persistent conversation state")
        payload_json = canonical_json_dumps(record["context_payload"])
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._purge_expired_rows(cursor)
                    cursor.execute(
                        """
                        INSERT INTO orchestration_conversation_state_records (
                            execution_id,
                            tenant_id,
                            conversation_id,
                            user_id,
                            context_payload
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            record["execution_id"],
                            record["tenant_id"],
                            record["conversation_id"],
                            record["user_id"],
                            payload_json,
                        ),
                    )
                connection.commit()
        except psycopg.errors.UniqueViolation as error:
            timed_print("[STATE_STORE] Persistent conversation-state write conflicted")
            existing = self.list_recent(
                tenant_id=record["tenant_id"],
                conversation_id=record["conversation_id"],
                user_id=record["user_id"],
                limit=50,
            )
            matching = next(
                (item for item in existing if item["execution_id"] == record["execution_id"]),
                None,
            )
            if matching is not None and canonical_json_dumps(
                matching["context_payload"]
            ) == canonical_json_dumps(record["context_payload"]):
                return
            raise ConversationStateStoreError(
                reason_code="conversation_state_conflict",
                message="Conversation-state record conflicts with existing execution context.",
            ) from error
        except psycopg.Error as error:
            timed_print("[STATE_STORE] Persistent conversation-state write failed")
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error
        timed_print(
            "[STATE_STORE] Persisted persistent conversation state "
            f"execution_id={record['execution_id']}"
        )

    def delete(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
    ) -> int:
        timed_print("[STATE_STORE] About to delete persistent conversation state")
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM orchestration_conversation_state_records
                        WHERE tenant_id = %s
                          AND conversation_id = %s
                          AND user_id = %s
                        """,
                        (tenant_id, conversation_id, user_id),
                    )
                    deleted = cursor.rowcount
                connection.commit()
        except psycopg.Error as error:
            timed_print("[STATE_STORE] Persistent conversation-state delete failed")
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error
        timed_print(
            "[STATE_STORE] Deleted persistent conversation state "
            f"conversation_id={conversation_id} deleted_count={deleted}"
        )
        return deleted

    def rename(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        conversation_title: str,
    ) -> int:
        timed_print("[STATE_STORE] About to rename persistent conversation state")
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE orchestration_conversation_state_records
                        SET context_payload = jsonb_set(
                                context_payload,
                                '{conversation_title}',
                                to_jsonb(%s::text),
                                true
                            ),
                            updated_at = now()
                        WHERE tenant_id = %s
                          AND conversation_id = %s
                          AND user_id = %s
                          AND COALESCE(
                                context_payload->>'conversation_title',
                                ''
                            ) IS DISTINCT FROM %s
                        """,
                        (
                            conversation_title,
                            tenant_id,
                            conversation_id,
                            user_id,
                            conversation_title,
                        ),
                    )
                    updated = cursor.rowcount
                connection.commit()
        except psycopg.Error as error:
            timed_print("[STATE_STORE] Persistent conversation-state rename failed")
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error
        timed_print(
            "[STATE_STORE] Renamed persistent conversation state "
            f"conversation_id={conversation_id} updated_count={updated}"
        )
        return updated

    def clear(self) -> None:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM orchestration_conversation_state_records")
                connection.commit()
        except psycopg.Error as error:
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error

    def purge_expired(self) -> int:
        """Purge expired records once across replicas using a transaction advisory lock."""
        if not self._has_retention_expires_at:
            return 0
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(4820192026)")
                    lock_row = cursor.fetchone()
                    if lock_row is None or lock_row[0] is not True:
                        return 0
                    cursor.execute(
                        "DELETE FROM orchestration_conversation_state_records "
                        "WHERE retention_expires_at <= now()"
                    )
                    deleted = cursor.rowcount
                connection.commit()
        except psycopg.Error as error:
            raise ConversationStateStoreError(
                reason_code="conversation_state_unavailable",
                message="Orchestration conversation-state persistence is unavailable.",
            ) from error
        return deleted

    def _purge_expired_rows(self, cursor: psycopg.Cursor) -> int:
        if not self._has_retention_expires_at:
            return 0
        cursor.execute(
            "DELETE FROM orchestration_conversation_state_records "
            "WHERE retention_expires_at <= now()"
        )
        return cursor.rowcount

    def _detect_retention_expires_at_column(self) -> bool:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'orchestration_conversation_state_records'
                              AND column_name = 'retention_expires_at'
                        )
                        """
                    )
                    row = cursor.fetchone()
        except psycopg.Error:
            return False
        return bool(row and row[0] is True)


def build_default_conversation_state_store() -> ConversationStateStore:
    """Build the default conversation-state store with DB-backed persistence when available."""

    mode = os.getenv("ORCHESTRATION_CONVERSATION_STATE_PERSISTENCE_MODE", "auto").strip().lower()
    if mode in {"in_memory", "in-memory"}:
        return InMemoryConversationStateStore()

    database_url = load_database_url()
    if not database_url:
        return InMemoryConversationStateStore()
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.orchestration_conversation_state_records')"
                )
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    return InMemoryConversationStateStore()
    except psycopg.Error:
        if mode == "persistent":
            raise
        return InMemoryConversationStateStore()
    return PersistentConversationStateStore(database_url=database_url)


_default_conversation_state_store: ConversationStateStore = build_default_conversation_state_store()


def get_default_conversation_state_store() -> ConversationStateStore:
    """Return the process-level default conversation-state store."""

    return _default_conversation_state_store


def set_default_conversation_state_store(store: ConversationStateStore) -> None:
    """Override the process-level default conversation-state store for tests."""

    global _default_conversation_state_store
    _default_conversation_state_store = store


def reset_default_conversation_state_store() -> None:
    """Reset the process-level default conversation-state store."""

    global _default_conversation_state_store
    _default_conversation_state_store = build_default_conversation_state_store()


def _coerce_json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        loaded = json.loads(value)
        assert isinstance(loaded, dict)
        return cast(dict[str, object], loaded)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def serialize_database_timestamp(value: object) -> str:
    """Return the ISO-8601 representation required by the browser history API."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value
    raise ConversationStateStoreError(
        reason_code="conversation_state_invalid_timestamp",
        message="Conversation-state persistence returned an invalid timestamp.",
    )
