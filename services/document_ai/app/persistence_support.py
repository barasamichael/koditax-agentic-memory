"""Shared CockroachDB persistence helpers for document_ai runtime state."""

from __future__ import annotations

import os
import time
import random
from typing import Any
from typing import Literal
from typing import TypeVar
import logging
from pathlib import Path
from threading import RLock
from contextlib import suppress
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from collections.abc import Iterator

import psycopg
from psycopg import pq
from psycopg import conninfo
from psycopg_pool import ConnectionPool

from services.document_ai.app.config import get_document_ai_database_transaction_max_attempts
from services.document_ai.app.config import get_document_ai_database_transaction_backoff_max_ms
from services.document_ai.app.config import get_document_ai_database_transaction_backoff_base_ms

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"
DOCUMENT_AI_PERSISTENCE_MODE_ENV_VAR = "DOCUMENT_AI_PERSISTENCE_MODE"
DEFAULT_DOCUMENT_AI_PERSISTENCE_MODE: Literal["persistent"] = "persistent"
DOCUMENT_AI_DB_POOL_MIN_SIZE_ENV_VAR = "DOCUMENT_AI_DB_POOL_MIN_SIZE"
DOCUMENT_AI_DB_POOL_MAX_SIZE_ENV_VAR = "DOCUMENT_AI_DB_POOL_MAX_SIZE"
DOCUMENT_AI_DB_POOL_MAX_WAITING_ENV_VAR = "DOCUMENT_AI_DB_POOL_MAX_WAITING"
DOCUMENT_AI_DB_POOL_ACQUIRE_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_DB_POOL_ACQUIRE_TIMEOUT_SECONDS"
DOCUMENT_AI_DB_POOL_OPEN_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_DB_POOL_OPEN_TIMEOUT_SECONDS"
DOCUMENT_AI_DB_POOL_CLOSE_TIMEOUT_SECONDS_ENV_VAR = "DOCUMENT_AI_DB_POOL_CLOSE_TIMEOUT_SECONDS"
DOCUMENT_AI_DB_POOL_MAX_LIFETIME_SECONDS_ENV_VAR = "DOCUMENT_AI_DB_POOL_MAX_LIFETIME_SECONDS"
DOCUMENT_AI_DB_POOL_MAX_IDLE_SECONDS_ENV_VAR = "DOCUMENT_AI_DB_POOL_MAX_IDLE_SECONDS"
DOCUMENT_AI_DB_POOL_RECONNECT_TIMEOUT_SECONDS_ENV_VAR = (
    "DOCUMENT_AI_DB_POOL_RECONNECT_TIMEOUT_SECONDS"
)
_DOCUMENT_AI_APPLICATION_NAME = "document_ai"
_DOCUMENT_AI_POOL_NAME = "document_ai"
_DOCUMENT_AI_TRANSACTION_LOGGER = logging.getLogger("document_ai.persistence")
_DOCUMENT_AI_POOL_REGISTRY_LOCK = RLock()
_DOCUMENT_AI_POOL_REGISTRY: dict[tuple[object, ...], ConnectionPool[Any]] = {}
T = TypeVar("T")


DocumentAIPersistenceMode = Literal["persistent", "in_memory"]
DocumentAIPersistenceStatus = Literal["ready", "unavailable", "schema_mismatch"]
_DOCUMENT_AI_EXPECTED_DATABASE_NAME = "kodi_dev"
_DOCUMENT_AI_EXPECTED_DATABASE_ENGINE = "CockroachDB"
_DOCUMENT_AI_EXPECTED_SQL_USER = "hackathon_user"
_DOCUMENT_AI_SCHEMA_NAME = "public"


@dataclass(frozen=True)
class DocumentAISchemaColumnRequirement:
    """Represent one CockroachDB schema column requirement."""

    name: str
    is_nullable: bool | None = None
    data_type_contains: str | None = None


@dataclass(frozen=True)
class DocumentAIDatabasePoolConfig:
    """Represent bounded pool settings loaded from the environment."""

    min_size: int
    max_size: int
    max_waiting: int
    acquire_timeout_seconds: float
    open_timeout_seconds: float
    close_timeout_seconds: float
    max_lifetime_seconds: float
    max_idle_seconds: float
    reconnect_timeout_seconds: float


@dataclass(frozen=True)
class DocumentAIDatabaseTransactionConfig:
    """Represent bounded transaction retry settings loaded from the environment."""

    max_attempts: int
    backoff_base_ms: int
    backoff_max_ms: int


class DocumentAITransactionAmbiguousResultError(RuntimeError):
    """Represent a transaction whose commit outcome could not be reconciled safely."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        sqlstate: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.sqlstate = sqlstate
        self.details = details or {}


_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES: tuple[str, ...] = (
    "document_ai_upload_sessions",
    "document_ai_documents",
    "document_ai_completion_idempotency",
    "document_ai_extraction_jobs",
    "document_ai_extractions",
    "document_ai_extraction_verifications",
    "document_ai_evidence_linkages",
    "document_ai_signed_access_usage",
    "document_ai_compliance_overrides",
    "document_ai_dead_letters",
    "document_ai_lifecycle_audit_evidence",
    "document_ai_compliance_override_audit_evidence",
    "document_ai_purge_operations",
    "document_ai_purge_targets",
    "document_ai_purge_attempts",
    "document_ai_extraction_calculations",
    "document_ai_document_versions",
    "document_ai_source_artifacts",
    "document_ai_document_bindings",
    "document_ai_processing_operations",
    "document_ai_processing_work_items",
    "document_ai_processing_attempts",
    "document_ai_processing_checkpoints",
    "document_ai_processing_outbox",
    "document_ai_processing_outbox_attempts",
    "document_ai_processing_dead_letters",
    "document_ai_provider_results",
    "document_ai_provider_result_reservations",
    "document_ai_canonical_representations",
    "document_ai_canonical_elements",
    "document_ai_canonical_relationships",
    "document_ai_source_regions",
    "document_ai_retrieval_chunks",
    "document_ai_chunk_embeddings",
    "document_ai_embedding_records",
    "document_ai_effective_values",
    "document_ai_corrections",
    "document_ai_correction_invalidations",
    "document_ai_reprocessing_candidates",
    "document_ai_correction_remappings",
    "document_ai_legacy_migrations",
    "document_ai_legacy_migration_observations",
    "document_ai_legacy_compatibility_callers",
    "document_ai_legacy_compatibility_traffic",
    "document_ai_source_inspections",
    "document_ai_structural_scopes",
    "document_ai_provider_partitions",
    "document_ai_evidence_items",
    "document_ai_evidence_sources",
    "document_ai_evidence_conflicts",
    "document_ai_evidence_requirements",
    "document_ai_embedding_records",
    "document_ai_workflow_projections",
    "document_ai_migration_mappings",
)

_DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS: dict[
    str, tuple[DocumentAISchemaColumnRequirement, ...]
] = {
    "document_ai_documents": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("owner_user_id"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("storage_key"),
        DocumentAISchemaColumnRequirement("uploaded_at"),
        DocumentAISchemaColumnRequirement("checksum_sha256"),
        DocumentAISchemaColumnRequirement("size_bytes"),
        DocumentAISchemaColumnRequirement("content_type"),
        DocumentAISchemaColumnRequirement("display_name"),
        DocumentAISchemaColumnRequirement("category"),
        DocumentAISchemaColumnRequirement("tags"),
        DocumentAISchemaColumnRequirement("description"),
        DocumentAISchemaColumnRequirement("revision"),
        DocumentAISchemaColumnRequirement("registry_revision"),
        DocumentAISchemaColumnRequirement("active_document_version_id"),
        DocumentAISchemaColumnRequirement("purge_eligible_at"),
        DocumentAISchemaColumnRequirement("purged_at"),
        DocumentAISchemaColumnRequirement("compliance_lock_until"),
    ),
    "document_ai_document_versions": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("version_number"),
        DocumentAISchemaColumnRequirement("version_state"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("supersedes_document_version_id"),
        DocumentAISchemaColumnRequirement("idempotency_key"),
    ),
    "document_ai_source_artifacts": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("storage_key"),
        DocumentAISchemaColumnRequirement("checksum_sha256"),
        DocumentAISchemaColumnRequirement("content_type"),
        DocumentAISchemaColumnRequirement("size_bytes"),
        DocumentAISchemaColumnRequirement("retention_state"),
        DocumentAISchemaColumnRequirement("integrity_state"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_processing_operations": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("operation_kind"),
        DocumentAISchemaColumnRequirement("processing_policy_version"),
        DocumentAISchemaColumnRequirement("processor_version"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("requested_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("idempotency_key"),
        DocumentAISchemaColumnRequirement("request_payload"),
        DocumentAISchemaColumnRequirement("cancellation_requested_at"),
        DocumentAISchemaColumnRequirement("cancellation_requested_by_user_id"),
        DocumentAISchemaColumnRequirement("result_reference"),
        DocumentAISchemaColumnRequirement("failure_category"),
    ),
    "document_ai_processing_work_items": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("work_kind"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("priority"),
        DocumentAISchemaColumnRequirement("available_at"),
        DocumentAISchemaColumnRequirement("leased_until"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("current_processing_attempt_id"),
        DocumentAISchemaColumnRequirement("fencing_token"),
        DocumentAISchemaColumnRequirement("lease_issued_at"),
        DocumentAISchemaColumnRequirement("last_heartbeat_at"),
        DocumentAISchemaColumnRequirement("workload_class"),
        DocumentAISchemaColumnRequirement("retry_count"),
        DocumentAISchemaColumnRequirement("max_attempts"),
        DocumentAISchemaColumnRequirement("first_attempted_at"),
        DocumentAISchemaColumnRequirement("max_retry_elapsed_seconds"),
        DocumentAISchemaColumnRequirement("next_retry_at"),
        DocumentAISchemaColumnRequirement("failure_category"),
        DocumentAISchemaColumnRequirement("dead_lettered_at"),
        DocumentAISchemaColumnRequirement("dead_letter_reason"),
        DocumentAISchemaColumnRequirement("manual_recovery_count"),
    ),
    "document_ai_processing_attempts": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_work_item_id"),
        DocumentAISchemaColumnRequirement("attempt_number"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("started_at"),
        DocumentAISchemaColumnRequirement("finished_at"),
        DocumentAISchemaColumnRequirement("error_code"),
        DocumentAISchemaColumnRequirement("error_detail"),
        DocumentAISchemaColumnRequirement("worker_id"),
        DocumentAISchemaColumnRequirement("fencing_token"),
        DocumentAISchemaColumnRequirement("lease_expires_at"),
        DocumentAISchemaColumnRequirement("last_heartbeat_at"),
        DocumentAISchemaColumnRequirement("checkpoint_sequence"),
    ),
    "document_ai_processing_checkpoints": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_attempt_id"),
        DocumentAISchemaColumnRequirement("checkpoint_key"),
        DocumentAISchemaColumnRequirement("checkpoint_payload"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("sequence"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_processing_outbox": (
        DocumentAISchemaColumnRequirement("processing_outbox_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("event_type"),
        DocumentAISchemaColumnRequirement("payload"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("publish_attempts"),
        DocumentAISchemaColumnRequirement("last_error_code"),
        DocumentAISchemaColumnRequirement("published_at"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("processing_work_item_id"),
        DocumentAISchemaColumnRequirement("routing_key"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("next_attempt_at"),
        DocumentAISchemaColumnRequirement("claimed_at"),
        DocumentAISchemaColumnRequirement("claim_token"),
        DocumentAISchemaColumnRequirement("last_error_class"),
    ),
    "document_ai_processing_outbox_attempts": (
        DocumentAISchemaColumnRequirement("processing_outbox_attempt_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_outbox_id"),
        DocumentAISchemaColumnRequirement("attempt_number"),
        DocumentAISchemaColumnRequirement("claim_token"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("error_code"),
        DocumentAISchemaColumnRequirement("error_class"),
        DocumentAISchemaColumnRequirement("broker_message_id"),
        DocumentAISchemaColumnRequirement("attempted_at"),
        DocumentAISchemaColumnRequirement("acknowledged_at"),
    ),
    "document_ai_processing_dead_letters": (
        DocumentAISchemaColumnRequirement("processing_dead_letter_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("processing_work_item_id"),
        DocumentAISchemaColumnRequirement("processing_attempt_id"),
        DocumentAISchemaColumnRequirement("attempt_number"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("work_kind"),
        DocumentAISchemaColumnRequirement("operation_kind"),
        DocumentAISchemaColumnRequirement("worker_id"),
        DocumentAISchemaColumnRequirement("fencing_token"),
        DocumentAISchemaColumnRequirement("failure_class"),
        DocumentAISchemaColumnRequirement("failure_category"),
        DocumentAISchemaColumnRequirement("reason_code"),
        DocumentAISchemaColumnRequirement("retry_count"),
        DocumentAISchemaColumnRequirement("max_attempts"),
        DocumentAISchemaColumnRequirement("max_retry_elapsed_seconds"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("error_code"),
        DocumentAISchemaColumnRequirement("error_detail"),
        DocumentAISchemaColumnRequirement("dead_lettered_at"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("diagnostic_payload"),
    ),
    "document_ai_provider_results": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("processing_attempt_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("provider_name"),
        DocumentAISchemaColumnRequirement("provider_response_id"),
        DocumentAISchemaColumnRequirement("provider_request_id"),
        DocumentAISchemaColumnRequirement("request_fingerprint"),
        DocumentAISchemaColumnRequirement("model_policy"),
        DocumentAISchemaColumnRequirement("processing_policy_version"),
        DocumentAISchemaColumnRequirement("prompt_version"),
        DocumentAISchemaColumnRequirement("canonical_schema_version"),
        DocumentAISchemaColumnRequirement("source_scope_id"),
        DocumentAISchemaColumnRequirement("provider_result_state"),
        DocumentAISchemaColumnRequirement("validated_result"),
        DocumentAISchemaColumnRequirement("usage"),
        DocumentAISchemaColumnRequirement("latency_ms"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("processing_work_item_id"),
        DocumentAISchemaColumnRequirement("provider_result_reservation_id"),
    ),
    "document_ai_provider_result_reservations": (
        DocumentAISchemaColumnRequirement("reservation_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("processing_attempt_id"),
        DocumentAISchemaColumnRequirement("processing_work_item_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("provider_name"),
        DocumentAISchemaColumnRequirement("model_policy"),
        DocumentAISchemaColumnRequirement("processing_policy_version"),
        DocumentAISchemaColumnRequirement("prompt_version"),
        DocumentAISchemaColumnRequirement("canonical_schema_version"),
        DocumentAISchemaColumnRequirement("source_scope_id"),
        DocumentAISchemaColumnRequirement("request_fingerprint"),
        DocumentAISchemaColumnRequirement("structural_scope_ids"),
        DocumentAISchemaColumnRequirement("source_checksum_sha256"),
        DocumentAISchemaColumnRequirement("source_size_bytes"),
        DocumentAISchemaColumnRequirement("reservation_state"),
        DocumentAISchemaColumnRequirement("reservation_generation"),
        DocumentAISchemaColumnRequirement("reservation_expires_at"),
        DocumentAISchemaColumnRequirement("reserved_at"),
        DocumentAISchemaColumnRequirement("in_progress_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
        DocumentAISchemaColumnRequirement("provider_request_id"),
        DocumentAISchemaColumnRequirement("provider_response_id"),
        DocumentAISchemaColumnRequirement("provider_result_id"),
        DocumentAISchemaColumnRequirement("validated_result"),
        DocumentAISchemaColumnRequirement("usage"),
        DocumentAISchemaColumnRequirement("latency_ms"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_canonical_representations": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("canonical_schema_version"),
        DocumentAISchemaColumnRequirement("processing_policy_family"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("is_active"),
        DocumentAISchemaColumnRequirement("representation_payload"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("activated_at"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("provider_result_id"),
        DocumentAISchemaColumnRequirement("assembly_policy_version"),
        DocumentAISchemaColumnRequirement("content_hash_sha256"),
        DocumentAISchemaColumnRequirement("canonical_validation_version"),
        DocumentAISchemaColumnRequirement("validation_report"),
        DocumentAISchemaColumnRequirement("readiness_state"),
        DocumentAISchemaColumnRequirement("validated_at"),
        DocumentAISchemaColumnRequirement("rejected_at"),
    ),
    "document_ai_canonical_elements": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("canonical_representation_id"),
        DocumentAISchemaColumnRequirement("parent_element_id"),
        DocumentAISchemaColumnRequirement("element_type"),
        DocumentAISchemaColumnRequirement("ordinal"),
        DocumentAISchemaColumnRequirement("observed_value"),
        DocumentAISchemaColumnRequirement("normalized_value"),
        DocumentAISchemaColumnRequirement("uncertainty"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("stable_key"),
        DocumentAISchemaColumnRequirement("page_number"),
        DocumentAISchemaColumnRequirement("reading_order"),
    ),
    "document_ai_canonical_relationships": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("canonical_representation_id"),
        DocumentAISchemaColumnRequirement("source_element_id"),
        DocumentAISchemaColumnRequirement("target_element_id"),
        DocumentAISchemaColumnRequirement("relationship_type"),
        DocumentAISchemaColumnRequirement("ordinal"),
        DocumentAISchemaColumnRequirement("relationship_payload"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_source_regions": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("canonical_element_id"),
        DocumentAISchemaColumnRequirement("structural_unit_kind"),
        DocumentAISchemaColumnRequirement("structural_unit_index"),
        DocumentAISchemaColumnRequirement("region_payload"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_retrieval_chunks": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("canonical_representation_id"),
        DocumentAISchemaColumnRequirement("chunk_key"),
        DocumentAISchemaColumnRequirement("content_hash_sha256"),
        DocumentAISchemaColumnRequirement("chunking_policy_version"),
        DocumentAISchemaColumnRequirement("embedding_text"),
        DocumentAISchemaColumnRequirement("canonical_element_keys"),
        DocumentAISchemaColumnRequirement("source_location"),
        DocumentAISchemaColumnRequirement("structural_context"),
        DocumentAISchemaColumnRequirement("lifecycle_state"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_chunk_embeddings": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("retrieval_chunk_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("canonical_representation_id"),
        DocumentAISchemaColumnRequirement("content_hash_sha256"),
        DocumentAISchemaColumnRequirement("chunking_policy_version"),
        DocumentAISchemaColumnRequirement("embedding_model"),
        DocumentAISchemaColumnRequirement("embedding_version"),
        DocumentAISchemaColumnRequirement("embedding_dimensions"),
        DocumentAISchemaColumnRequirement("embedding", data_type_contains="vector"),
        DocumentAISchemaColumnRequirement("index_state"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_embedding_records": (
        DocumentAISchemaColumnRequirement("embedding_record_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("retrieval_chunk_id"),
        DocumentAISchemaColumnRequirement("embedding_model"),
        DocumentAISchemaColumnRequirement("embedding_dimensions"),
        DocumentAISchemaColumnRequirement("embedding_vector_json"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_effective_values": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("canonical_element_id"),
        DocumentAISchemaColumnRequirement("source_observed_value"),
        DocumentAISchemaColumnRequirement("original_interpreted_value"),
        DocumentAISchemaColumnRequirement("corrected_value"),
        DocumentAISchemaColumnRequirement("effective_value"),
        DocumentAISchemaColumnRequirement("active_correction_id"),
        DocumentAISchemaColumnRequirement("correction_state"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_corrections": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("canonical_element_id"),
        DocumentAISchemaColumnRequirement("evidence_item_id"),
        DocumentAISchemaColumnRequirement("correction_state"),
        DocumentAISchemaColumnRequirement("reversal_of_correction_id"),
        DocumentAISchemaColumnRequirement("idempotency_key"),
        DocumentAISchemaColumnRequirement("source_observed_value"),
        DocumentAISchemaColumnRequirement("original_interpreted_value"),
        DocumentAISchemaColumnRequirement("effective_value"),
        DocumentAISchemaColumnRequirement("policy_version"),
    ),
    "document_ai_compliance_overrides": (
        DocumentAISchemaColumnRequirement("override_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("requested_action"),
        DocumentAISchemaColumnRequirement("requested_by_user_id"),
        DocumentAISchemaColumnRequirement("requested_by_role"),
        DocumentAISchemaColumnRequirement("justification"),
        DocumentAISchemaColumnRequirement("status"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("expires_at"),
        DocumentAISchemaColumnRequirement("approved_by_user_id"),
        DocumentAISchemaColumnRequirement("approved_by_role"),
        DocumentAISchemaColumnRequirement("approved_at"),
        DocumentAISchemaColumnRequirement("rejected_by_user_id"),
        DocumentAISchemaColumnRequirement("rejected_by_role"),
        DocumentAISchemaColumnRequirement("rejected_at"),
        DocumentAISchemaColumnRequirement("consumed_by_user_id"),
        DocumentAISchemaColumnRequirement("consumed_at"),
        DocumentAISchemaColumnRequirement("response_payload"),
        DocumentAISchemaColumnRequirement("updated_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
    ),
    "document_ai_lifecycle_audit_evidence": (
        DocumentAISchemaColumnRequirement("audit_evidence_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("action"),
        DocumentAISchemaColumnRequirement("action_status"),
        DocumentAISchemaColumnRequirement("previous_state"),
        DocumentAISchemaColumnRequirement("new_state"),
        DocumentAISchemaColumnRequirement("user_id"),
        DocumentAISchemaColumnRequirement("reason_code"),
        DocumentAISchemaColumnRequirement("trace_id"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("event_time"),
        DocumentAISchemaColumnRequirement("payload"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
    ),
    "document_ai_compliance_override_audit_evidence": (
        DocumentAISchemaColumnRequirement("audit_evidence_id"),
        DocumentAISchemaColumnRequirement("override_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("event_type"),
        DocumentAISchemaColumnRequirement("event_status"),
        DocumentAISchemaColumnRequirement("requested_action"),
        DocumentAISchemaColumnRequirement("actor_user_id"),
        DocumentAISchemaColumnRequirement("actor_role"),
        DocumentAISchemaColumnRequirement("reason_code"),
        DocumentAISchemaColumnRequirement("state_before"),
        DocumentAISchemaColumnRequirement("state_after"),
        DocumentAISchemaColumnRequirement("trace_id"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("event_time"),
        DocumentAISchemaColumnRequirement("payload"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
    ),
    "document_ai_correction_invalidations": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("correction_id"),
        DocumentAISchemaColumnRequirement("dependency_kind"),
        DocumentAISchemaColumnRequirement("dependency_id"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
    ),
    "document_ai_purge_operations": (
        DocumentAISchemaColumnRequirement("purge_operation_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("requested_by_user_id"),
        DocumentAISchemaColumnRequirement("requested_by_role"),
        DocumentAISchemaColumnRequirement("requested_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("idempotency_key"),
        DocumentAISchemaColumnRequirement("request_fingerprint"),
        DocumentAISchemaColumnRequirement("payload_fingerprint"),
        DocumentAISchemaColumnRequirement("manifest_version"),
        DocumentAISchemaColumnRequirement("replay_count"),
        DocumentAISchemaColumnRequirement("last_reconciled_at"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_purge_targets": (
        DocumentAISchemaColumnRequirement("purge_target_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("purge_operation_id"),
        DocumentAISchemaColumnRequirement("target_kind"),
        DocumentAISchemaColumnRequirement("target_reference"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("completed_at"),
        DocumentAISchemaColumnRequirement("failure_detail"),
        DocumentAISchemaColumnRequirement("attempt_count"),
        DocumentAISchemaColumnRequirement("verified_at"),
        DocumentAISchemaColumnRequirement("required"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_purge_attempts": (
        DocumentAISchemaColumnRequirement("purge_attempt_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("purge_operation_id"),
        DocumentAISchemaColumnRequirement("attempt_number"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("requested_by_user_id"),
        DocumentAISchemaColumnRequirement("requested_by_role"),
        DocumentAISchemaColumnRequirement("correlation_id"),
        DocumentAISchemaColumnRequirement("request_fingerprint"),
        DocumentAISchemaColumnRequirement("started_at"),
        DocumentAISchemaColumnRequirement("completed_at"),
        DocumentAISchemaColumnRequirement("failure_detail"),
        DocumentAISchemaColumnRequirement("created_at"),
        DocumentAISchemaColumnRequirement("updated_at"),
    ),
    "document_ai_reprocessing_candidates": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("prior_active_representation_id"),
        DocumentAISchemaColumnRequirement("candidate_representation_id"),
        DocumentAISchemaColumnRequirement("model_policy_version"),
        DocumentAISchemaColumnRequirement("prompt_version"),
        DocumentAISchemaColumnRequirement("canonical_schema_version"),
        DocumentAISchemaColumnRequirement("embedding_version"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("validation_report"),
        DocumentAISchemaColumnRequirement("activated_at"),
        DocumentAISchemaColumnRequirement("rolled_back_at"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_correction_remappings": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("reprocessing_candidate_id"),
        DocumentAISchemaColumnRequirement("correction_id"),
        DocumentAISchemaColumnRequirement("prior_stable_key"),
        DocumentAISchemaColumnRequirement("candidate_stable_key"),
        DocumentAISchemaColumnRequirement("state"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_source_inspections": (
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("policy_version"),
        DocumentAISchemaColumnRequirement("disposition"),
        DocumentAISchemaColumnRequirement("reason_code"),
        DocumentAISchemaColumnRequirement("observed_media_type"),
        DocumentAISchemaColumnRequirement("observed_source_family"),
        DocumentAISchemaColumnRequirement("observed_source_format"),
        DocumentAISchemaColumnRequirement("source_size_bytes"),
        DocumentAISchemaColumnRequirement("page_count"),
        DocumentAISchemaColumnRequirement("structural_scopes"),
        DocumentAISchemaColumnRequirement("diagnostic_payload"),
        DocumentAISchemaColumnRequirement("inspected_at"),
    ),
    "document_ai_structural_scopes": (
        DocumentAISchemaColumnRequirement("structural_scope_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("source_inspection_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("policy_version"),
        DocumentAISchemaColumnRequirement("scope_kind"),
        DocumentAISchemaColumnRequirement("scope_ordinal"),
        DocumentAISchemaColumnRequirement("parent_structural_scope_id"),
        DocumentAISchemaColumnRequirement("structural_coordinates"),
        DocumentAISchemaColumnRequirement("scope_payload"),
        DocumentAISchemaColumnRequirement("scope_identity"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
    "document_ai_provider_partitions": (
        DocumentAISchemaColumnRequirement("provider_partition_id"),
        DocumentAISchemaColumnRequirement("tenant_id"),
        DocumentAISchemaColumnRequirement("document_id"),
        DocumentAISchemaColumnRequirement("document_version_id"),
        DocumentAISchemaColumnRequirement("source_artifact_id"),
        DocumentAISchemaColumnRequirement("source_inspection_id"),
        DocumentAISchemaColumnRequirement("processing_operation_id"),
        DocumentAISchemaColumnRequirement("policy_version"),
        DocumentAISchemaColumnRequirement("partition_kind"),
        DocumentAISchemaColumnRequirement("partition_ordinal"),
        DocumentAISchemaColumnRequirement("parent_structural_scope_id"),
        DocumentAISchemaColumnRequirement("structural_scope_ids"),
        DocumentAISchemaColumnRequirement("structural_coordinates"),
        DocumentAISchemaColumnRequirement("partition_payload"),
        DocumentAISchemaColumnRequirement("partition_identity"),
        DocumentAISchemaColumnRequirement("estimated_input_bytes"),
        DocumentAISchemaColumnRequirement("partition_state"),
        DocumentAISchemaColumnRequirement("created_at"),
    ),
}

_DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "document_ai_documents": (
        "chk_document_ai_documents_registry_revision",
        "fk_document_ai_documents_active_version_scope",
    ),
    "document_ai_document_versions": (
        "chk_document_ai_document_versions_number",
        "chk_document_ai_document_versions_state",
        "fk_document_ai_document_versions_document_scope",
        "fk_document_ai_document_versions_supersedes",
        "uq_document_ai_document_versions_number",
        "uq_document_ai_document_versions_idempotency",
    ),
    "document_ai_source_artifacts": (
        "chk_document_ai_source_artifacts_checksum",
        "chk_document_ai_source_artifacts_size",
        "chk_document_ai_source_artifacts_retention_state",
        "chk_document_ai_source_artifacts_integrity_state",
        "fk_document_ai_source_artifacts_version_scope",
        "uq_document_ai_source_artifacts_storage_key",
    ),
    "document_ai_processing_operations": (
        "chk_document_ai_processing_operations_state",
        "chk_document_ai_processing_operations_completion",
        "chk_document_ai_processing_operations_failure_category",
        "fk_document_ai_processing_operations_version_scope",
        "uq_document_ai_processing_operations_scope",
        "uq_document_ai_processing_operations_ingestion",
        "uq_document_ai_processing_operations_idempotency",
    ),
    "document_ai_processing_work_items": (
        "chk_document_ai_processing_work_items_state",
        "chk_document_ai_processing_work_items_workload_class",
        "chk_document_ai_processing_work_items_priority",
        "chk_document_ai_processing_work_items_retry_budget",
        "chk_document_ai_processing_work_items_dead_letter",
        "fk_document_ai_processing_work_items_operation_scope",
        "fk_document_ai_processing_work_items_current_attempt_scope",
        "uq_document_ai_processing_work_items_scope",
        "uq_document_ai_processing_work_items_operation_kind",
    ),
    "document_ai_processing_attempts": (
        "chk_document_ai_processing_attempts_number",
        "chk_document_ai_processing_attempts_state",
        "chk_document_ai_processing_attempts_checkpoint_sequence",
        "fk_document_ai_processing_attempts_work_item_scope",
        "uq_document_ai_processing_attempts_scope",
        "uq_document_ai_processing_attempts_number",
    ),
    "document_ai_processing_checkpoints": (
        "fk_document_ai_processing_checkpoints_attempt_scope",
        "uq_document_ai_processing_checkpoints_scope",
        "uq_document_ai_processing_checkpoints_key",
        "chk_document_ai_processing_checkpoints_sequence",
    ),
    "document_ai_processing_outbox": (
        "fk_document_ai_processing_outbox_operation_scope",
        "fk_document_ai_processing_outbox_work_item_scope",
        "chk_document_ai_processing_outbox_state",
        "chk_document_ai_processing_outbox_publish_attempts",
        "chk_document_ai_processing_outbox_routing_key",
        "chk_document_ai_processing_outbox_error_class",
        "uq_document_ai_processing_outbox_scope",
        "uq_document_ai_processing_outbox_operation_event",
    ),
    "document_ai_processing_outbox_attempts": (
        "fk_document_ai_processing_outbox_attempt_outbox_scope",
        "chk_document_ai_processing_outbox_attempt_number",
        "chk_document_ai_processing_outbox_attempt_state",
        "chk_document_ai_processing_outbox_attempt_error_class",
        "uq_document_ai_processing_outbox_attempt_scope",
        "uq_document_ai_processing_outbox_attempt_number",
    ),
    "document_ai_provider_results": (
        "chk_document_ai_provider_results_provider",
        "chk_document_ai_provider_results_state",
        "chk_document_ai_provider_results_latency",
        "fk_document_ai_provider_results_operation_scope",
        "fk_document_ai_provider_results_attempt_scope",
        "fk_document_ai_provider_results_version_scope",
        "fk_document_ai_provider_results_artifact_scope",
        "uq_document_ai_provider_results_scope",
        "uq_document_ai_provider_results_operation_attempt",
        "uq_document_ai_provider_results_operation",
        "fk_document_ai_provider_results_reservation_scope",
    ),
    "document_ai_provider_result_reservations": (
        "uq_document_ai_provider_result_reservations_scope",
        "uq_document_ai_provider_result_reservations_operation",
        "fk_document_ai_provider_result_reservations_operation_scope",
        "fk_document_ai_provider_result_reservations_attempt_scope",
        "fk_document_ai_provider_result_reservations_work_scope",
        "fk_document_ai_provider_result_reservations_version_scope",
        "fk_document_ai_provider_result_reservations_artifact_scope",
        "chk_document_ai_provider_result_reservations_state",
        "chk_document_ai_provider_result_reservations_generation",
        "chk_document_ai_provider_result_reservations_latency",
        "chk_document_ai_provider_result_reservations_size",
    ),
    "document_ai_canonical_representations": (
        "chk_document_ai_canonical_representations_state",
        "chk_document_ai_canonical_representations_active",
        "chk_document_ai_canonical_representations_readiness_state",
        "fk_document_ai_canonical_representations_version_scope",
        "fk_document_ai_canonical_representations_operation_scope",
        "fk_document_ai_canonical_representations_artifact_scope",
        "fk_document_ai_canonical_representations_provider_result_scope",
        "uq_document_ai_canonical_representations_scope",
        "uq_document_ai_active_canonical_representation",
        "uq_document_ai_canonical_representation_provider_result",
    ),
    "document_ai_canonical_elements": (
        "chk_document_ai_canonical_elements_ordinal",
        "fk_document_ai_canonical_elements_representation_scope",
        "fk_document_ai_canonical_elements_parent",
        "uq_document_ai_canonical_elements_scope",
        "uq_document_ai_canonical_elements_ordinal",
        "uq_document_ai_canonical_elements_stable_key",
        "uq_document_ai_canonical_elements_reading_order",
    ),
    "document_ai_canonical_relationships": (
        "chk_document_ai_canonical_relationships_ordinal",
        "chk_document_ai_canonical_relationships_nonreflexive",
        "fk_document_ai_canonical_relationships_representation_scope",
        "fk_document_ai_canonical_relationships_source_scope",
        "fk_document_ai_canonical_relationships_target_scope",
    ),
    "document_ai_source_regions": (
        "chk_document_ai_source_regions_index",
        "fk_document_ai_source_regions_artifact_scope",
        "fk_document_ai_source_regions_element_scope",
        "uq_document_ai_source_regions_scope",
    ),
    "document_ai_retrieval_chunks": (
        "chk_document_ai_retrieval_chunks_hash",
        "chk_document_ai_retrieval_chunks_lifecycle",
        "chk_document_ai_retrieval_chunks_ordinal",
        "fk_document_ai_retrieval_chunks_document_scope",
        "fk_document_ai_retrieval_chunks_version_scope",
        "fk_document_ai_retrieval_chunks_representation_scope",
        "uq_document_ai_retrieval_chunks_scope",
        "uq_document_ai_retrieval_chunks_identity",
    ),
    "document_ai_chunk_embeddings": (
        "chk_document_ai_chunk_embeddings_dimensions",
        "chk_document_ai_chunk_embeddings_state",
        "fk_document_ai_chunk_embeddings_chunk_scope",
        "fk_document_ai_chunk_embeddings_version_scope",
        "fk_document_ai_chunk_embeddings_representation_scope",
        "uq_document_ai_chunk_embeddings_model",
    ),
    "document_ai_embedding_records": (
        "chk_document_ai_embedding_records_dimensions",
        "chk_document_ai_embedding_records_vector",
        "fk_document_ai_embedding_records_chunk_scope",
        "uq_document_ai_embedding_records_model",
        "uq_document_ai_embedding_records_scope",
    ),
    "document_ai_effective_values": (
        "chk_document_ai_effective_values_state",
        "fk_document_ai_effective_values_element",
        "fk_document_ai_effective_values_correction",
    ),
    "document_ai_corrections": (
        "chk_document_ai_corrections_state",
        "fk_document_ai_corrections_version_scope",
        "fk_document_ai_corrections_element_scope",
        "fk_document_ai_corrections_evidence_scope",
        "fk_document_ai_corrections_reversal",
        "uq_document_ai_corrections_scope",
        "uq_document_ai_corrections_idempotency",
    ),
    "document_ai_compliance_overrides": ("chk_document_ai_compliance_overrides_status",),
    "document_ai_lifecycle_audit_evidence": (),
    "document_ai_compliance_override_audit_evidence": (),
    "document_ai_correction_invalidations": (
        "fk_document_ai_correction_invalidations_correction",
        "uq_document_ai_correction_invalidations_scope",
    ),
    "document_ai_purge_operations": (
        "chk_document_ai_purge_operations_state",
        "fk_document_ai_purge_operations_document_scope",
        "uq_document_ai_purge_operations_scope",
        "uq_document_ai_purge_operations_idempotency",
    ),
    "document_ai_purge_targets": (
        "chk_document_ai_purge_targets_state",
        "fk_document_ai_purge_targets_operation_scope",
        "uq_document_ai_purge_targets_scope",
        "uq_document_ai_purge_targets_reference",
    ),
    "document_ai_purge_attempts": (
        "fk_document_ai_purge_attempts_operation_scope",
        "uq_document_ai_purge_attempts_scope",
        "uq_document_ai_purge_attempts_number",
        "chk_document_ai_purge_attempts_number",
        "chk_document_ai_purge_attempts_state",
    ),
    "document_ai_reprocessing_candidates": (
        "chk_document_ai_reprocessing_candidate_state",
        "fk_document_ai_reprocessing_candidate_version",
        "fk_document_ai_reprocessing_candidate_prior",
        "fk_document_ai_reprocessing_candidate_representation",
        "uq_document_ai_reprocessing_candidates_scope",
    ),
    "document_ai_correction_remappings": (
        "chk_document_ai_correction_remappings_state",
        "fk_document_ai_correction_remappings_candidate",
        "uq_document_ai_correction_remappings_candidate_correction",
    ),
    "document_ai_legacy_migrations": (
        "chk_document_ai_legacy_migrations_state",
        "fk_document_ai_legacy_migrations_document",
    ),
    "document_ai_legacy_migration_observations": (
        "chk_document_ai_legacy_migration_observation_state",
        "fk_document_ai_legacy_migration_observation_version",
        "fk_document_ai_legacy_migration_observation_element",
    ),
    "document_ai_legacy_compatibility_traffic": ("fk_document_ai_legacy_compatibility_caller",),
    "document_ai_source_inspections": (
        "chk_document_ai_source_inspections_disposition",
        "chk_document_ai_source_inspections_reason_code",
        "fk_document_ai_source_inspections_version_scope",
        "fk_document_ai_source_inspections_artifact_scope",
        "fk_document_ai_source_inspections_operation_scope",
        "uq_document_ai_source_inspections_scope",
        "uq_document_ai_source_inspections_version_policy",
    ),
    "document_ai_structural_scopes": (
        "chk_document_ai_structural_scopes_ordinal",
        "chk_document_ai_structural_scopes_kind",
        "fk_document_ai_structural_scopes_document_scope",
        "fk_document_ai_structural_scopes_version_scope",
        "fk_document_ai_structural_scopes_artifact_scope",
        "fk_document_ai_structural_scopes_inspection_scope",
        "fk_document_ai_structural_scopes_operation_scope",
        "fk_document_ai_structural_scopes_parent_scope",
        "uq_document_ai_structural_scopes_scope",
        "uq_document_ai_structural_scopes_identity",
        "uq_document_ai_structural_scopes_ordinal",
    ),
    "document_ai_provider_partitions": (
        "chk_document_ai_provider_partitions_ordinal",
        "chk_document_ai_provider_partitions_kind",
        "chk_document_ai_provider_partitions_estimated_input_bytes",
        "chk_document_ai_provider_partitions_state",
        "fk_document_ai_provider_partitions_document_scope",
        "fk_document_ai_provider_partitions_version_scope",
        "fk_document_ai_provider_partitions_artifact_scope",
        "fk_document_ai_provider_partitions_inspection_scope",
        "fk_document_ai_provider_partitions_operation_scope",
        "fk_document_ai_provider_partitions_parent_scope",
        "uq_document_ai_provider_partitions_scope",
        "uq_document_ai_provider_partitions_identity",
        "uq_document_ai_provider_partitions_ordinal",
    ),
}

_DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES: dict[str, tuple[str, ...]] = {
    "document_ai_upload_sessions": (
        "idx_document_ai_upload_sessions_document_id",
        "idx_document_ai_upload_sessions_scope",
    ),
    "document_ai_documents": (
        "idx_document_ai_documents_scope",
        "idx_document_ai_documents_scope_checksum",
        "idx_document_ai_documents_visible_scope",
        "idx_document_ai_documents_exact_metadata",
    ),
    "document_ai_extraction_jobs": ("idx_document_ai_extraction_jobs_document_id",),
    "document_ai_extractions": ("idx_document_ai_extractions_document_id",),
    "document_ai_evidence_linkages": ("idx_document_ai_evidence_linkages_document_id",),
    "document_ai_compliance_overrides": ("idx_document_ai_compliance_overrides_document_id",),
    "document_ai_dead_letters": ("idx_document_ai_dead_letters_document_id",),
    "document_ai_lifecycle_audit_evidence": (
        "idx_document_ai_lifecycle_audit_document_id",
        "idx_document_ai_lifecycle_audit_correlation_id",
    ),
    "document_ai_compliance_override_audit_evidence": (
        "idx_document_ai_compliance_override_audit_document_id",
        "idx_document_ai_compliance_override_audit_override_id",
        "idx_document_ai_compliance_override_audit_correlation_id",
    ),
    "document_ai_extraction_calculations": ("idx_document_ai_extraction_calculations_extraction",),
    "document_ai_document_versions": ("idx_document_ai_document_versions_scope",),
    "document_ai_source_artifacts": ("idx_document_ai_source_artifacts_version",),
    "document_ai_document_bindings": ("idx_document_ai_document_bindings_scope",),
    "document_ai_processing_operations": (
        "idx_document_ai_processing_operations_scope",
        "idx_document_ai_processing_operations_document_state",
    ),
    "document_ai_processing_work_items": (
        "idx_document_ai_processing_work_items_claim",
        "idx_document_ai_processing_work_items_lease_recovery",
        "idx_document_ai_processing_work_items_due_priority",
        "idx_document_ai_processing_work_items_tenant_due",
        "idx_document_ai_processing_work_items_discovery",
    ),
    "document_ai_processing_attempts": ("idx_document_ai_processing_attempts_current_lease",),
    "document_ai_processing_outbox": (
        "idx_document_ai_processing_outbox_pending",
        "idx_document_ai_processing_outbox_reconciliation",
        "idx_document_ai_processing_outbox_stale_claim",
    ),
    "document_ai_processing_dead_letters": (
        "idx_document_ai_processing_dead_letters_document_id",
        "idx_document_ai_processing_dead_letters_operation_id",
        "idx_document_ai_processing_dead_letters_work_item_id",
    ),
    "document_ai_provider_results": ("idx_document_ai_provider_results_operation",),
    "document_ai_provider_result_reservations": (
        "idx_document_ai_provider_result_reservations_operation",
        "idx_document_ai_provider_result_reservations_reconciliation",
    ),
    "document_ai_canonical_representations": (
        "uq_document_ai_active_canonical_representation",
        "idx_document_ai_canonical_validation_readiness",
    ),
    "document_ai_canonical_elements": (
        "uq_document_ai_canonical_elements_stable_key",
        "uq_document_ai_canonical_elements_reading_order",
        "idx_document_ai_canonical_elements_representation",
    ),
    "document_ai_source_regions": ("idx_document_ai_source_regions_structural_lookup",),
    "document_ai_retrieval_chunks": (
        "idx_document_ai_retrieval_chunks_scope",
        "idx_document_ai_retrieval_chunks_active_canonical_scope",
        "idx_document_ai_retrieval_chunks_exact_lexical",
        "idx_document_ai_retrieval_chunks_exact_source_location",
        "idx_document_ai_retrieval_chunks_exact_structural_context",
    ),
    "document_ai_chunk_embeddings": (
        "idx_document_ai_chunk_embeddings_scope",
        "idx_document_ai_chunk_embeddings_vector_search",
    ),
    "document_ai_corrections": ("idx_document_ai_corrections_element",),
    "document_ai_purge_operations": ("idx_document_ai_purge_operations_scope",),
    "document_ai_purge_attempts": ("idx_document_ai_purge_attempts_operation_scope",),
    "document_ai_legacy_migrations": ("idx_document_ai_legacy_migrations_reconcile",),
    "document_ai_legacy_compatibility_traffic": (
        "idx_document_ai_legacy_compatibility_traffic_recent",
    ),
    "document_ai_source_inspections": ("idx_document_ai_source_inspections_gate",),
    "document_ai_structural_scopes": (
        "idx_document_ai_structural_scopes_lookup",
        "idx_document_ai_structural_scopes_inspection",
    ),
    "document_ai_provider_partitions": (
        "idx_document_ai_provider_partitions_lookup",
        "idx_document_ai_provider_partitions_inspection",
    ),
    "document_ai_reprocessing_candidates": ("idx_document_ai_reprocessing_candidates_scope",),
    "document_ai_purge_targets": ("idx_document_ai_purge_unresolved",),
}


def get_document_ai_persistence_mode() -> DocumentAIPersistenceMode:
    """Return the configured mode, failing closed for production durability."""

    runtime_mode = os.getenv("DOCUMENT_AI_RUNTIME_MODE", "development").strip().lower()
    configured = os.getenv(DOCUMENT_AI_PERSISTENCE_MODE_ENV_VAR)
    if configured is None:
        if runtime_mode == "test":
            return "in_memory"
        return DEFAULT_DOCUMENT_AI_PERSISTENCE_MODE
    normalized = configured.strip().lower()
    if normalized == "in_memory":
        if runtime_mode != "test":
            raise RuntimeError(
                "DOCUMENT_AI_PERSISTENCE_MODE=in_memory is permitted only when "
                "DOCUMENT_AI_RUNTIME_MODE=test."
            )
        return "in_memory"
    return "persistent"


def load_document_ai_database_url() -> str | None:
    """Load the PostgreSQL connection URL from env or local `.env`."""

    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value.strip()

    env_values = _read_env_values()
    direct_value = env_values.get(DATABASE_URL_ENV_VAR)
    if direct_value:
        return direct_value

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def get_document_ai_connection_pool(database_url: str) -> ConnectionPool[Any]:
    """Return the shared bounded CockroachDB pool for Document AI."""

    normalized_conninfo = _build_document_ai_conninfo(database_url)
    config = _load_document_ai_pool_config()
    pool_key = (
        normalized_conninfo,
        config.min_size,
        config.max_size,
        config.max_waiting,
        config.acquire_timeout_seconds,
        config.open_timeout_seconds,
        config.close_timeout_seconds,
        config.max_lifetime_seconds,
        config.max_idle_seconds,
        config.reconnect_timeout_seconds,
    )
    with _DOCUMENT_AI_POOL_REGISTRY_LOCK:
        existing_pool = _DOCUMENT_AI_POOL_REGISTRY.get(pool_key)
        if existing_pool is not None:
            return existing_pool

        pool = ConnectionPool(
            conninfo=normalized_conninfo,
            name=_DOCUMENT_AI_POOL_NAME,
            min_size=config.min_size,
            max_size=config.max_size,
            open=False,
            timeout=config.acquire_timeout_seconds,
            max_waiting=config.max_waiting,
            max_lifetime=config.max_lifetime_seconds,
            max_idle=config.max_idle_seconds,
            reconnect_timeout=config.reconnect_timeout_seconds,
            close_returns=False,
            check=_document_ai_pool_connection_check,
            reset=_document_ai_pool_connection_reset,
        )
        try:
            pool.open(wait=True, timeout=config.open_timeout_seconds)
        except Exception:
            with suppress(Exception):
                pool.close(timeout=config.close_timeout_seconds)
            raise
        _DOCUMENT_AI_POOL_REGISTRY[pool_key] = pool
        return pool


def close_document_ai_connection_pool(
    *,
    database_url: str | None = None,
    connection_pool: ConnectionPool[Any] | None = None,
) -> None:
    """Close and forget one shared CockroachDB pool."""

    with _DOCUMENT_AI_POOL_REGISTRY_LOCK:
        if connection_pool is not None:
            matching_keys = [
                key for key, pool in _DOCUMENT_AI_POOL_REGISTRY.items() if pool is connection_pool
            ]
        elif database_url is not None:
            normalized_conninfo = _build_document_ai_conninfo(database_url)
            matching_keys = [
                key for key in _DOCUMENT_AI_POOL_REGISTRY if key and key[0] == normalized_conninfo
            ]
        else:
            matching_keys = list(_DOCUMENT_AI_POOL_REGISTRY)
        for key in matching_keys:
            pool = _DOCUMENT_AI_POOL_REGISTRY.pop(key)
            with suppress(Exception):
                pool.close(timeout=_load_document_ai_pool_config().close_timeout_seconds)


@contextmanager
def connect_document_ai_database(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    """Borrow one PostgreSQL connection from the shared pool."""

    pool = get_document_ai_connection_pool(database_url)
    with pool.connection() as connection:
        yield connection


def execute_document_ai_database_transaction(
    *,
    database_url: str,
    transaction_name: str,
    transaction_callback: Callable[[psycopg.Cursor[Any]], T],
    reconcile_ambiguous_result: Callable[[psycopg.Connection[Any]], T | None] | None = None,
    max_attempts: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[], float] = random.random,
) -> T:
    """Execute one replay-safe Document AI transaction with bounded retry handling.

    The callback must contain replay-safe SQL and deterministic in-memory work only.
    It must not perform provider calls, filesystem effects, or other irreversible
    side effects because CockroachDB may replay the callback.
    """

    resolved_transaction_name = transaction_name.strip()
    if not resolved_transaction_name:
        raise ValueError("document_ai_transaction_name_required")
    resolved_max_attempts = (
        get_document_ai_database_transaction_max_attempts()
        if max_attempts is None
        else max_attempts
    )
    if resolved_max_attempts < 1:
        raise ValueError("document_ai_transaction_max_attempts_must_be_positive")
    transaction_config = DocumentAIDatabaseTransactionConfig(
        max_attempts=resolved_max_attempts,
        backoff_base_ms=get_document_ai_database_transaction_backoff_base_ms(),
        backoff_max_ms=get_document_ai_database_transaction_backoff_max_ms(),
    )
    last_error: psycopg.Error | None = None
    for attempt_number in range(1, transaction_config.max_attempts + 1):
        should_sleep = False
        delay_seconds = 0.0
        with connect_document_ai_database(database_url) as connection:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        result = transaction_callback(cursor)
                _log_document_ai_transaction_success(
                    transaction_name=resolved_transaction_name,
                    attempt_number=attempt_number,
                    maximum_attempts=transaction_config.max_attempts,
                )
                return result
            except psycopg.Error as error:
                last_error = error
                sqlstate = _extract_sqlstate(error)
                sqlstate_class = _classify_document_ai_transaction_sqlstate(sqlstate)
                if sqlstate_class == "ambiguous_transaction_result":
                    with suppress(Exception):
                        connection.rollback()
                    _log_document_ai_transaction_failure(
                        transaction_name=resolved_transaction_name,
                        attempt_number=attempt_number,
                        maximum_attempts=transaction_config.max_attempts,
                        sqlstate=sqlstate,
                        retrying=False,
                    )
                    if reconcile_ambiguous_result is None:
                        raise DocumentAITransactionAmbiguousResultError(
                            reason_code="document_ai_persistence_ambiguous_result",
                            message=(
                                "Document AI transaction commit outcome is ambiguous and could "
                                "not be reconciled."
                            ),
                            sqlstate=sqlstate,
                            details={
                                "transaction_name": resolved_transaction_name,
                                "attempt_number": attempt_number,
                                "maximum_attempts": transaction_config.max_attempts,
                            },
                        ) from error
                    _log_document_ai_transaction_ambiguous_result(
                        transaction_name=resolved_transaction_name,
                        sqlstate=sqlstate,
                    )
                    reconciled_result = _reconcile_document_ai_transaction_ambiguous_result(
                        database_url=database_url,
                        transaction_name=resolved_transaction_name,
                        reconcile_ambiguous_result=reconcile_ambiguous_result,
                        sqlstate=sqlstate,
                    )
                    if reconciled_result is not None:
                        return reconciled_result
                    raise DocumentAITransactionAmbiguousResultError(
                        reason_code="document_ai_persistence_ambiguous_result",
                        message=(
                            "Document AI transaction commit outcome is ambiguous and could "
                            "not be reconciled."
                        ),
                        sqlstate=sqlstate,
                        details={
                            "transaction_name": resolved_transaction_name,
                            "attempt_number": attempt_number,
                            "maximum_attempts": transaction_config.max_attempts,
                        },
                    ) from error
                if sqlstate_class != "retryable_serialization_failure":
                    _log_document_ai_transaction_failure(
                        transaction_name=resolved_transaction_name,
                        attempt_number=attempt_number,
                        maximum_attempts=transaction_config.max_attempts,
                        sqlstate=sqlstate,
                        retrying=False,
                    )
                    raise
                retrying = attempt_number < transaction_config.max_attempts
                _log_document_ai_transaction_failure(
                    transaction_name=resolved_transaction_name,
                    attempt_number=attempt_number,
                    maximum_attempts=transaction_config.max_attempts,
                    sqlstate=sqlstate,
                    retrying=retrying,
                )
                if not retrying:
                    raise
                delay_seconds = _calculate_document_ai_retry_delay_seconds(
                    attempt_number=attempt_number,
                    base_delay_ms=transaction_config.backoff_base_ms,
                    max_delay_ms=transaction_config.backoff_max_ms,
                    jitter_fn=jitter_fn,
                )
                should_sleep = True
        if should_sleep:
            sleep_fn(delay_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError("document_ai_transaction_retry_failed_without_error")


def _classify_document_ai_transaction_sqlstate(sqlstate: str | None) -> str:
    if sqlstate == "40001":
        return "retryable_serialization_failure"
    if sqlstate == "40003":
        return "ambiguous_transaction_result"
    return "non_retryable"


def _reconcile_document_ai_transaction_ambiguous_result(
    *,
    database_url: str,
    transaction_name: str,
    reconcile_ambiguous_result: Callable[[psycopg.Connection[Any]], T | None],
    sqlstate: str | None,
) -> T | None:
    _log_document_ai_transaction_reconciliation_start(
        transaction_name=transaction_name,
        sqlstate=sqlstate,
    )
    try:
        with connect_document_ai_database(database_url) as connection:
            result = reconcile_ambiguous_result(connection)
    except Exception as error:  # noqa: BLE001
        _log_document_ai_transaction_reconciliation_failure(
            transaction_name=transaction_name,
            sqlstate=sqlstate,
        )
        raise DocumentAITransactionAmbiguousResultError(
            reason_code="document_ai_persistence_ambiguous_result",
            message=(
                "Document AI transaction commit outcome is ambiguous and could not be reconciled."
            ),
            sqlstate=sqlstate,
            details={"transaction_name": transaction_name},
        ) from error
    if result is not None:
        _log_document_ai_transaction_reconciliation_success(
            transaction_name=transaction_name,
            sqlstate=sqlstate,
        )
    return result


def resolve_document_ai_persistence_status(
    *,
    database_url: str,
    required_tables: tuple[str, ...],
) -> DocumentAIPersistenceStatus:
    """Return DB readiness for the requested document_ai persistence schema."""

    try:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                status = _validate_document_ai_persistence_runtime(
                    cursor=cursor,
                    required_tables=required_tables,
                )
                if status != "ready":
                    return status
    except psycopg.Error:
        return "unavailable"
    except Exception:
        return "unavailable"
    return "ready"


def _validate_document_ai_persistence_runtime(
    *,
    cursor: psycopg.Cursor[Any],
    required_tables: tuple[str, ...],
) -> DocumentAIPersistenceStatus:
    version_value, current_database, current_user = _fetch_document_ai_runtime_identity(cursor)
    if not _is_cockroachdb_engine(version_value):
        return "schema_mismatch"
    if current_database != _DOCUMENT_AI_EXPECTED_DATABASE_NAME:
        return "schema_mismatch"
    if current_user != _DOCUMENT_AI_EXPECTED_SQL_USER:
        return "schema_mismatch"

    table_names = tuple(
        dict.fromkeys((*required_tables, *_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES))
    )
    if not _document_ai_required_tables_exist(cursor, table_names):
        return "schema_mismatch"
    if not _document_ai_required_columns_exist(cursor):
        return "schema_mismatch"
    if not _document_ai_required_constraints_exist(cursor):
        return "schema_mismatch"
    if not _document_ai_required_indexes_exist(cursor):
        return "schema_mismatch"
    return "ready"


def _fetch_document_ai_runtime_identity(
    cursor: psycopg.Cursor[Any],
) -> tuple[str, str, str]:
    cursor.execute("SELECT version(), current_database(), current_user")
    row = cursor.fetchone()
    if row is None or len(row) < 3:
        raise psycopg.OperationalError("document_ai_persistence_identity_unavailable")
    return (_safe_text(row[0]), _safe_text(row[1]), _safe_text(row[2]))


def _is_cockroachdb_engine(version_value: str) -> bool:
    return "cockroachdb" in version_value.lower()


def _document_ai_required_tables_exist(
    cursor: psycopg.Cursor[Any],
    table_names: tuple[str, ...],
) -> bool:
    cursor.execute(
        """
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = %s
           AND table_name = ANY(%s)
        """,
        (_DOCUMENT_AI_SCHEMA_NAME, list(table_names)),
    )
    present_tables = {str(row[0]) for row in cursor.fetchall() if row and row[0] is not None}
    return all(table_name in present_tables for table_name in table_names)


def _document_ai_required_columns_exist(cursor: psycopg.Cursor[Any]) -> bool:
    if not _DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS:
        return True
    table_names = list(_DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS)
    cursor.execute(
        """
        SELECT table_name, column_name, data_type, udt_name, is_nullable
          FROM information_schema.columns
         WHERE table_schema = %s
           AND table_name = ANY(%s)
        """,
        (_DOCUMENT_AI_SCHEMA_NAME, table_names),
    )
    observed_columns: dict[str, dict[str, tuple[str, str, bool]]] = {}
    for row in cursor.fetchall():
        if len(row) < 5:
            continue
        table_name = _safe_text(row[0])
        column_name = _safe_text(row[1])
        data_type = _safe_text(row[2])
        udt_name = _safe_text(row[3])
        is_nullable = _safe_text(row[4]).upper() == "YES"
        observed_columns.setdefault(table_name, {})[column_name] = (
            data_type,
            udt_name,
            is_nullable,
        )
    for table_name, requirements in _DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS.items():
        table_columns = observed_columns.get(table_name)
        if table_columns is None:
            return False
        for requirement in requirements:
            observed = table_columns.get(requirement.name)
            if observed is None:
                return False
            data_type, udt_name, is_nullable = observed
            if requirement.is_nullable is not None and is_nullable != requirement.is_nullable:
                return False
            if requirement.data_type_contains is not None:
                haystack = f"{data_type} {udt_name}".lower()
                if requirement.data_type_contains.lower() not in haystack:
                    return False
    return True


def _document_ai_required_constraints_exist(cursor: psycopg.Cursor[Any]) -> bool:
    if not _DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS:
        return True
    table_names = list(_DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS)
    cursor.execute(
        """
        SELECT table_name, constraint_name
          FROM information_schema.table_constraints
         WHERE table_schema = %s
           AND table_name = ANY(%s)
        """,
        (_DOCUMENT_AI_SCHEMA_NAME, table_names),
    )
    observed_constraints: dict[str, set[str]] = {}
    for row in cursor.fetchall():
        if len(row) < 2:
            continue
        table_name = _safe_text(row[0])
        constraint_name = _safe_text(row[1])
        observed_constraints.setdefault(table_name, set()).add(constraint_name)
    for table_name, requirements in _DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS.items():
        table_constraints = observed_constraints.get(table_name)
        if table_constraints is None:
            return False
        if not set(requirements).issubset(table_constraints):
            return False
    return True


def _document_ai_required_indexes_exist(cursor: psycopg.Cursor[Any]) -> bool:
    if not _DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES:
        return True
    table_names = list(_DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES)
    cursor.execute(
        """
        SELECT table_name, index_name
          FROM information_schema.statistics
         WHERE table_schema = %s
           AND table_name = ANY(%s)
        """,
        (_DOCUMENT_AI_SCHEMA_NAME, table_names),
    )
    observed_indexes: dict[str, set[str]] = {}
    for row in cursor.fetchall():
        if len(row) < 2:
            continue
        table_name = _safe_text(row[0])
        index_name = _safe_text(row[1])
        observed_indexes.setdefault(table_name, set()).add(index_name)
    for table_name, requirements in _DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES.items():
        table_indexes = observed_indexes.get(table_name)
        if table_indexes is None:
            return False
        if not set(requirements).issubset(table_indexes):
            return False
    return True


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_document_ai_pool_config() -> DocumentAIDatabasePoolConfig:
    return DocumentAIDatabasePoolConfig(
        min_size=_get_positive_int_env(DOCUMENT_AI_DB_POOL_MIN_SIZE_ENV_VAR, 1),
        max_size=_get_positive_int_env(DOCUMENT_AI_DB_POOL_MAX_SIZE_ENV_VAR, 8),
        max_waiting=_get_non_negative_int_env(DOCUMENT_AI_DB_POOL_MAX_WAITING_ENV_VAR, 0),
        acquire_timeout_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_ACQUIRE_TIMEOUT_SECONDS_ENV_VAR, 5.0
        ),
        open_timeout_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_OPEN_TIMEOUT_SECONDS_ENV_VAR, 5.0
        ),
        close_timeout_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_CLOSE_TIMEOUT_SECONDS_ENV_VAR, 5.0
        ),
        max_lifetime_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_MAX_LIFETIME_SECONDS_ENV_VAR, 3600.0
        ),
        max_idle_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_MAX_IDLE_SECONDS_ENV_VAR, 600.0
        ),
        reconnect_timeout_seconds=_get_positive_float_env(
            DOCUMENT_AI_DB_POOL_RECONNECT_TIMEOUT_SECONDS_ENV_VAR, 300.0
        ),
    )


def _build_document_ai_conninfo(database_url: str) -> str:
    normalized = database_url.strip()
    if not normalized:
        raise ValueError("document_ai_database_url_required")
    parsed = conninfo.conninfo_to_dict(normalized)
    if parsed.get("application_name"):
        return normalized
    return conninfo.make_conninfo(normalized, application_name=_DOCUMENT_AI_APPLICATION_NAME)


def _document_ai_pool_connection_check(connection: psycopg.Connection[Any]) -> None:
    """Validate that a pooled connection is still reusable."""

    if getattr(connection, "closed", False):
        raise psycopg.OperationalError("document_ai_pooled_connection_closed")
    _reset_unusable_connection(connection)


def _document_ai_pool_connection_reset(connection: psycopg.Connection[Any]) -> None:
    """Return only healthy connections to the shared pool."""

    _reset_unusable_connection(connection)


def _reset_unusable_connection(connection: psycopg.Connection[Any]) -> None:
    transaction_status = connection.info.transaction_status
    if transaction_status == pq.TransactionStatus.IDLE:
        return
    with suppress(Exception):
        connection.rollback()
    if connection.info.transaction_status != pq.TransactionStatus.IDLE:
        raise psycopg.OperationalError("document_ai_pooled_connection_unusable")


def _get_positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer.") from error
    if parsed <= 0 or parsed > 86_400:
        raise RuntimeError(f"{name} must be between 1 and 86400.")
    return parsed


def _get_non_negative_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a non-negative integer.") from error
    if parsed < 0 or parsed > 86_400:
        raise RuntimeError(f"{name} must be between 0 and 86400.")
    return parsed


def _get_positive_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive number.") from error
    if parsed <= 0 or parsed > 86_400:
        raise RuntimeError(f"{name} must be between 0 and 86400.")
    return parsed


def _read_env_values() -> dict[str, str]:
    env_file = Path(".env")
    if not env_file.exists():
        return {}
    try:
        raw_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _extract_sqlstate(error: BaseException) -> str | None:
    sqlstate = getattr(error, "sqlstate", None)
    return str(sqlstate) if sqlstate is not None else None


def _calculate_document_ai_retry_delay_seconds(
    *,
    attempt_number: int,
    base_delay_ms: int,
    max_delay_ms: int,
    jitter_fn: Callable[[], float],
) -> float:
    bounded_delay_ms = min(max_delay_ms, base_delay_ms * (2 ** (attempt_number - 1)))
    jitter = jitter_fn()
    if jitter < 0:
        jitter = 0.0
    elif jitter > 1:
        jitter = 1.0
    return (bounded_delay_ms / 1000.0) * jitter


def _log_document_ai_transaction_success(
    *,
    transaction_name: str,
    attempt_number: int,
    maximum_attempts: int,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.succeeded",
        extra={
            "transaction_name": transaction_name,
            "attempt_number": attempt_number,
            "maximum_attempts": maximum_attempts,
        },
    )


def _log_document_ai_transaction_failure(
    *,
    transaction_name: str,
    attempt_number: int,
    maximum_attempts: int,
    sqlstate: str | None,
    retrying: bool,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.retrying" if retrying else "document_ai.transaction.failed",
        extra={
            "transaction_name": transaction_name,
            "attempt_number": attempt_number,
            "maximum_attempts": maximum_attempts,
            "sqlstate": sqlstate,
            "retrying": retrying,
        },
    )


def _log_document_ai_transaction_ambiguous_result(
    *,
    transaction_name: str,
    sqlstate: str | None,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.ambiguous",
        extra={
            "transaction_name": transaction_name,
            "sqlstate": sqlstate,
        },
    )


def _log_document_ai_transaction_reconciliation_start(
    *,
    transaction_name: str,
    sqlstate: str | None,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.reconciliation_started",
        extra={
            "transaction_name": transaction_name,
            "sqlstate": sqlstate,
        },
    )


def _log_document_ai_transaction_reconciliation_success(
    *,
    transaction_name: str,
    sqlstate: str | None,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.reconciliation_succeeded",
        extra={
            "transaction_name": transaction_name,
            "sqlstate": sqlstate,
        },
    )


def _log_document_ai_transaction_reconciliation_failure(
    *,
    transaction_name: str,
    sqlstate: str | None,
) -> None:
    _DOCUMENT_AI_TRANSACTION_LOGGER.info(
        "document_ai.transaction.reconciliation_failed",
        extra={
            "transaction_name": transaction_name,
            "sqlstate": sqlstate,
        },
    )
