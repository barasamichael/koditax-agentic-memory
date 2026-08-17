"""DB-backed persistence baseline tests for the knowledge runtime."""

from __future__ import annotations

import os
import json
from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import date
from datetime import datetime
from collections.abc import Iterator
from collections.abc import Sequence

import pytest
import psycopg
from psycopg.abc import Query
from fastapi.testclient import TestClient

from services.knowledge.app.main import create_app
from services.knowledge.app.embeddings import KnowledgeEmbeddingProvider
from services.knowledge.app.repository import KnowledgeRepository

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"
KNOWLEDGE_MIGRATION_FILES = (
    Path("database/migrations/0017_knowledge_persistent_catalog_baseline.sql"),
    Path("database/migrations/0018_knowledge_hybrid_retrieval_embeddings.sql"),
)


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge persistence tests."""

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge persistence DB tests.")

    _ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge persistence DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def seeded_knowledge_catalog(
    db_connection: psycopg.Connection,
) -> Iterator[dict[str, object]]:
    """Insert one deterministic governed knowledge fixture and clean it up."""

    fixture = _seed_governed_knowledge_fixture(db_connection)
    yield fixture


class _StubEmbeddingProvider(KnowledgeEmbeddingProvider):
    @property
    def model_name(self) -> str:
        return "test-embedding-model"

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            normalized = " ".join(text.strip().lower().split())
            if normalized == "semantic deductible business expense":
                vectors.append((1.0, 0.0, 0.0))
            else:
                vectors.append((0.0, 1.0, 0.0))
        return tuple(vectors)


def test_persistent_search_returns_deterministic_governed_results(
    seeded_knowledge_catalog: dict[str, object],
) -> None:
    database_url = cast(str, seeded_knowledge_catalog["database_url"])
    source_ids = cast(dict[str, str], seeded_knowledge_catalog["source_ids_by_name"])
    search_token = cast(str, seeded_knowledge_catalog["search_token"])
    repository = KnowledgeRepository(database_url=database_url)
    app = create_app(repository=repository)
    payload = {"query": search_token, "tax_domain": "income_tax"}

    with TestClient(app) as client:
        first = client.post("/knowledge/search", json=payload)
        second = client.post("/knowledge/search", json=payload)

    first_payload = _json(first)
    second_payload = _json(second)
    first_items = _result_items(first_payload)
    assert first.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert [item["source_id"] for item in first_items] == [
        source_ids["ita_15_2"],
        source_ids["ita_5_1_b"],
    ]
    assert all(item["source_type"] == "tax_law" for item in first_items)


def test_hybrid_search_uses_stored_chunk_embeddings_inside_governed_filters(
    seeded_knowledge_catalog: dict[str, object],
    db_connection: psycopg.Connection,
) -> None:
    database_url = cast(str, seeded_knowledge_catalog["database_url"])
    source_ids = cast(dict[str, str], seeded_knowledge_catalog["source_ids_by_name"])
    chunk_ids = cast(dict[str, UUID], seeded_knowledge_catalog["chunk_ids"])
    _insert_chunk_embedding(
        db_connection,
        chunk_id=chunk_ids["ita_15_2"],
        embedding_model="test-embedding-model",
        vector=(1.0, 0.0, 0.0),
        content_checksum_sha256="test-checksum-semantic-deduction",
    )
    repository = KnowledgeRepository(
        database_url=database_url,
        embedding_provider=_StubEmbeddingProvider(),
    )

    results = repository.search_records(
        query="semantic deductible business expense",
        source_type="tax_law",
        tax_domain="income_tax",
        effective_date=None,
    )

    assert [record.source_id for record in results] == [source_ids["ita_15_2"]]


def test_persistent_retrieve_returns_deterministic_governed_results(
    seeded_knowledge_catalog: dict[str, object],
) -> None:
    database_url = cast(str, seeded_knowledge_catalog["database_url"])
    source_ids = cast(dict[str, str], seeded_knowledge_catalog["source_ids_by_name"])
    anchor_ids = cast(dict[str, str], seeded_knowledge_catalog["anchor_ids_by_name"])
    repository = KnowledgeRepository(database_url=database_url)
    app = create_app(repository=repository)
    payload = {
        "source_ids": [source_ids["ita_15_2"]],
        "anchor_ids": [anchor_ids["kra_paye_2023"]],
    }

    with TestClient(app) as client:
        first = client.post("/knowledge/retrieve", json=payload)
        second = client.post("/knowledge/retrieve", json=payload)

    first_payload = _json(first)
    second_payload = _json(second)
    first_items = _result_items(first_payload)
    assert first.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert [item["source_id"] for item in first_items] == [
        source_ids["ita_15_2"],
        source_ids["kra_paye_2023"],
    ]
    assert _required_item_fields().issubset(first_items[0].keys())


def test_publication_state_and_temporal_filters_fail_closed(
    seeded_knowledge_catalog: dict[str, object],
) -> None:
    database_url = cast(str, seeded_knowledge_catalog["database_url"])
    source_ids = cast(dict[str, str], seeded_knowledge_catalog["source_ids_by_name"])
    history_token = cast(str, seeded_knowledge_catalog["history_token"])
    repository = KnowledgeRepository(database_url=database_url)

    historical = repository.search_records(
        query=history_token,
        source_type="tax_law",
        tax_domain="income_tax",
        effective_date=date(2011, 6, 1),
    )
    out_of_window = repository.search_records(
        query=history_token,
        source_type="tax_law",
        tax_domain="income_tax",
        effective_date=date(2014, 1, 1),
    )
    draft_lookup = repository.retrieve_records(
        source_ids=(source_ids["draft_2024"],),
        anchor_ids=(),
    )

    assert [record.source_id for record in historical] == [source_ids["ita_old_2010"]]
    assert out_of_window == ()
    assert draft_lookup == ()


def test_lineage_and_forbidden_origin_constraints_are_enforced(
    db_connection: psycopg.Connection,
) -> None:
    fixture = _insert_source_family_only(
        db_connection,
        source_id="KNW-LINEAGE-GUARD",
        title="Governed lineage guard source",
        source_class="guidance",
        authority_level="guidance",
    )
    try:
        with pytest.raises(psycopg.Error):
            with db_connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO knowledge_source_versions (
                        id,
                        source_id,
                        point_in_time_url,
                        source_checksum_sha256,
                        source_version_form,
                        source_input_origin,
                        source_input_ref,
                        publication_state,
                        effective_from
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        "KNW-LINEAGE-GUARD",
                        "https://example.com/lineage-guard",
                        f"checksum-{uuid4().hex}",
                        "as_issued",
                        "official_source_url",
                        "official-source-url://lineage-guard",
                        "published",
                        date(2024, 1, 1),
                    ),
                )
                db_connection.commit()

        db_connection.rollback()
        with pytest.raises(psycopg.Error):
            with db_connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO knowledge_source_versions (
                        id,
                        source_id,
                        point_in_time_url,
                        source_checksum_sha256,
                        source_version_form,
                        source_input_origin,
                        source_input_ref,
                        publication_state,
                        effective_from
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        "KNW-LINEAGE-GUARD",
                        "https://example.com/customer-lineage",
                        f"checksum-{uuid4().hex}",
                        "as_issued",
                        "customer_uploaded_document",
                        "customer://private-source",
                        "draft",
                        date(2024, 1, 1),
                    ),
                )
                db_connection.commit()
    finally:
        db_connection.rollback()
        _cleanup_governed_knowledge_fixture(db_connection, fixture)


def test_same_family_overlap_and_published_immutability_are_enforced(
    seeded_knowledge_catalog: dict[str, object],
    db_connection: psycopg.Connection,
) -> None:
    fixture = seeded_knowledge_catalog
    source_ids = cast(dict[str, str], fixture["source_ids_by_name"])
    version_ids = cast(dict[str, UUID], fixture["version_ids"])
    chunk_ids = cast(dict[str, UUID], fixture["chunk_ids"])

    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_source_versions (
                    id,
                    source_id,
                    point_in_time_url,
                    source_checksum_sha256,
                    source_version_form,
                    source_input_origin,
                    source_input_ref,
                    publication_state,
                    effective_from,
                    effective_to,
                    publication_event_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    source_ids["ita_15_2"],
                    "https://example.com/overlap",
                    f"checksum-{uuid4().hex}",
                    "as_issued",
                    "official_source_url",
                    "official-source-url://overlap",
                    "published",
                    date(1980, 1, 1),
                    None,
                    cast(dict[str, UUID], fixture["publication_event_ids"])["ita_15_2"],
                ),
            )
            db_connection.commit()

    db_connection.rollback()
    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE knowledge_source_versions
                SET effective_to = %s
                WHERE id = %s
                """,
                (date(1990, 1, 1), version_ids["ita_15_2"]),
            )
            db_connection.commit()

    db_connection.rollback()
    with pytest.raises(psycopg.Error):
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE knowledge_chunks
                SET chunk_text = %s
                WHERE id = %s
                """,
                ("mutated chunk text", chunk_ids["ita_15_2"]),
            )
            db_connection.commit()

    db_connection.rollback()


def _seed_governed_knowledge_fixture(connection: psycopg.Connection) -> dict[str, object]:
    database_url = _load_database_url()
    assert database_url is not None

    user_id = uuid4()
    suffix = uuid4().hex
    search_token = f"fixture-search-{suffix}"
    history_token = f"fixture-history-{suffix}"
    source_ids_by_name = {
        "ita_15_2": f"KNW-ITA-15-2-{suffix}",
        "ita_5_1_b": f"KNW-ITA-5-1-B-{suffix}",
        "kra_paye_2023": f"KNW-KRA-PAYE-2023-{suffix}",
        "ita_old_2010": f"KNW-ITA-OLD-2010-{suffix}",
        "draft_2024": f"KNW-DRAFT-2024-{suffix}",
    }
    anchor_ids_by_name = {
        "ita_15_2": f"income-tax-act-15-2-{suffix}",
        "ita_5_1_b": f"income-tax-act-5-1-b-{suffix}",
        "kra_paye_2023": f"kra-paye-2023-07-{suffix}",
        "ita_old_2010": f"income-tax-old-2010-{suffix}",
    }
    publication_event_ids = {
        "ita_15_2": uuid4(),
        "ita_5_1_b": uuid4(),
        "kra_paye_2023": uuid4(),
        "ita_old_2010": uuid4(),
    }
    version_ids = {
        "ita_15_2": uuid4(),
        "ita_5_1_b": uuid4(),
        "kra_paye_2023": uuid4(),
        "ita_old_2010": uuid4(),
        "draft_2024": uuid4(),
    }
    chunk_ids = {"ita_15_2": uuid4()}

    with connection.cursor() as cursor:
        _insert_user(cursor=cursor, user_id=user_id, suffix=suffix)
        for event_id, event_name in publication_event_ids.items():
            _insert_publication_event(
                cursor=cursor,
                event_id=event_name,
                user_id=user_id,
                suffix=suffix,
                event_type=event_id,
            )

        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_ids_by_name["ita_15_2"],
            title="Income Tax Act (Cap. 470), Section 15(2)",
            source_class="tax_law",
            authority_level="statute",
            suffix=suffix,
        )
        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_ids_by_name["ita_5_1_b"],
            title="Income Tax Act (Cap. 470), Section 5(1)(b)",
            source_class="tax_law",
            authority_level="statute",
            suffix=suffix,
        )
        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_ids_by_name["kra_paye_2023"],
            title="KRA PAYE Guidance Effective 2023-07-01",
            source_class="guidance",
            authority_level="guidance",
            suffix=suffix,
        )
        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_ids_by_name["ita_old_2010"],
            title="Income Tax Act Historical Section 15 Window",
            source_class="tax_law",
            authority_level="statute",
            suffix=suffix,
        )
        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_ids_by_name["draft_2024"],
            title="Draft Knowledge Source 2024",
            source_class="commentary",
            authority_level="commentary",
            suffix=suffix,
        )

        _insert_source_version(
            cursor=cursor,
            version_id=version_ids["ita_15_2"],
            source_id=source_ids_by_name["ita_15_2"],
            publication_state="published",
            effective_from=date(1974, 1, 1),
            effective_to=None,
            publication_event_id=publication_event_ids["ita_15_2"],
            point_in_time_url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            source_input_ref="official-source-url://ita-15-2",
            tax_year=None,
        )
        _insert_source_version(
            cursor=cursor,
            version_id=version_ids["ita_5_1_b"],
            source_id=source_ids_by_name["ita_5_1_b"],
            publication_state="published",
            effective_from=date(1974, 1, 1),
            effective_to=None,
            publication_event_id=publication_event_ids["ita_5_1_b"],
            point_in_time_url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            source_input_ref="official-source-url://ita-5-1-b",
            tax_year=None,
        )
        _insert_source_version(
            cursor=cursor,
            version_id=version_ids["kra_paye_2023"],
            source_id=source_ids_by_name["kra_paye_2023"],
            publication_state="published",
            effective_from=date(2023, 7, 1),
            effective_to=None,
            publication_event_id=publication_event_ids["kra_paye_2023"],
            point_in_time_url="https://www.kra.go.ke/individual/paye",
            source_input_ref="official-source-url://kra-paye-2023-07-01",
            tax_year=2023,
        )
        _insert_source_version(
            cursor=cursor,
            version_id=version_ids["ita_old_2010"],
            source_id=source_ids_by_name["ita_old_2010"],
            publication_state="superseded",
            effective_from=date(2010, 1, 1),
            effective_to=date(2012, 12, 31),
            publication_event_id=publication_event_ids["ita_old_2010"],
            point_in_time_url="https://example.com/income-tax-old-2010",
            source_input_ref="official-source-url://ita-old-2010",
            tax_year=2011,
        )
        _insert_source_version(
            cursor=cursor,
            version_id=version_ids["draft_2024"],
            source_id=source_ids_by_name["draft_2024"],
            publication_state="draft",
            effective_from=date(2024, 1, 1),
            effective_to=None,
            publication_event_id=None,
            point_in_time_url="https://example.com/draft-knowledge-2024",
            source_input_ref="official-source-url://draft-knowledge-2024",
            tax_year=2024,
        )

        _insert_anchor(
            cursor=cursor,
            anchor_id=anchor_ids_by_name["ita_15_2"],
            source_version_id=version_ids["ita_15_2"],
            anchor_title="Income Tax Act section 15(2)",
            anchor_path="section-15-2",
            anchor_text=(
                f"Allowable deductions in production of income under section 15(2). {search_token}"
            ),
            temporal_scope_from=date(1974, 1, 1),
            temporal_scope_to=None,
        )
        _insert_anchor(
            cursor=cursor,
            anchor_id=anchor_ids_by_name["ita_5_1_b"],
            source_version_id=version_ids["ita_5_1_b"],
            anchor_title="Income Tax Act section 5(1)(b)",
            anchor_path="section-5-1-b",
            anchor_text=(
                "Chargeability for non-resident employment income under section 5(1)(b). "
                f"{search_token}"
            ),
            temporal_scope_from=date(1974, 1, 1),
            temporal_scope_to=None,
        )
        _insert_anchor(
            cursor=cursor,
            anchor_id=anchor_ids_by_name["kra_paye_2023"],
            source_version_id=version_ids["kra_paye_2023"],
            anchor_title="KRA PAYE guidance July 2023",
            anchor_path="paye-guidance-2023-07",
            anchor_text="PAYE guidance reference for resident employment bands from July 2023.",
            temporal_scope_from=date(2023, 7, 1),
            temporal_scope_to=None,
        )
        _insert_anchor(
            cursor=cursor,
            anchor_id=anchor_ids_by_name["ita_old_2010"],
            source_version_id=version_ids["ita_old_2010"],
            anchor_title="Historical Income Tax Act section 15 window",
            anchor_path="historical-section-15-window",
            anchor_text=(
                "Old deductible section text for the 2010 to 2012 income tax window. "
                f"{history_token}"
            ),
            temporal_scope_from=date(2010, 1, 1),
            temporal_scope_to=date(2012, 12, 31),
        )
        _insert_chunk(
            cursor=cursor,
            chunk_id=chunk_ids["ita_15_2"],
            anchor_id=anchor_ids_by_name["ita_15_2"],
            chunk_index=0,
            chunk_text="Allowable deductions in production of income under section 15(2).",
        )

    connection.commit()
    return {
        "database_url": database_url,
        "search_token": search_token,
        "history_token": history_token,
        "user_ids": (user_id,),
        "publication_event_ids": publication_event_ids,
        "source_ids_by_name": source_ids_by_name,
        "version_ids": version_ids,
        "anchor_ids_by_name": anchor_ids_by_name,
        "chunk_ids": chunk_ids,
    }


def _cleanup_governed_knowledge_fixture(
    connection: psycopg.Connection,
    fixture: dict[str, object],
) -> None:
    version_ids = cast(dict[str, UUID], fixture.get("version_ids", {}))
    chunk_ids = cast(dict[str, UUID], fixture.get("chunk_ids", {}))
    publication_event_ids = cast(dict[str, UUID], fixture.get("publication_event_ids", {}))
    source_ids = cast(tuple[str, ...], fixture.get("source_ids", ()))
    anchor_ids = cast(tuple[str, ...], fixture.get("anchor_ids", ()))
    user_ids = cast(tuple[UUID, ...], fixture.get("user_ids", ()))

    with connection.cursor() as cursor:
        if chunk_ids:
            cursor.execute(
                "DELETE FROM knowledge_chunk_embeddings WHERE chunk_id = ANY(%s::uuid[])",
                (list(chunk_ids.values()),),
            )
            cursor.execute(
                "DELETE FROM knowledge_chunks WHERE id = ANY(%s::uuid[])",
                (list(chunk_ids.values()),),
            )
        if anchor_ids:
            cursor.execute(
                "DELETE FROM knowledge_anchors WHERE anchor_id = ANY(%s::text[])",
                (list(anchor_ids),),
            )
        if version_ids:
            cursor.execute(
                "DELETE FROM knowledge_source_versions WHERE id = ANY(%s::uuid[])",
                (list(version_ids.values()),),
            )
        if source_ids:
            cursor.execute(
                "DELETE FROM knowledge_sources WHERE source_id = ANY(%s::text[])",
                (list(source_ids),),
            )
        if publication_event_ids:
            cursor.execute(
                "DELETE FROM audit_events WHERE id = ANY(%s::uuid[])",
                (list(publication_event_ids.values()),),
            )
        if user_ids:
            cursor.execute(
                "DELETE FROM users WHERE id = ANY(%s::uuid[])",
                (list(user_ids),),
            )
    connection.commit()


def _insert_source_family_only(
    connection: psycopg.Connection,
    *,
    source_id: str,
    title: str,
    source_class: str,
    authority_level: str,
) -> dict[str, object]:
    user_id = uuid4()
    suffix = uuid4().hex

    with connection.cursor() as cursor:
        _insert_user(cursor=cursor, user_id=user_id, suffix=suffix)
        _insert_source(
            cursor=cursor,
            user_id=user_id,
            source_id=source_id,
            title=title,
            source_class=source_class,
            authority_level=authority_level,
            suffix=suffix,
        )
    connection.commit()
    return {
        "user_ids": (user_id,),
        "publication_event_ids": {},
        "source_ids": (source_id,),
        "version_ids": {},
        "anchor_ids": (),
        "chunk_ids": {},
    }


def _insert_user(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    user_id: UUID,
    suffix: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO users (
            id,
            phone_number_encrypted,
            email_encrypted,
            role
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            user_id,
            f"knowledge-phone-{suffix}",
            f"knowledge-{suffix}@example.com",
            "Administrator",
        ),
    )


def _insert_publication_event(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    event_id: UUID,
    user_id: UUID,
    suffix: str,
    event_type: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO audit_events (
            id,
            user_id,
            role_at_time,
            event_type,
            resource_type,
            resource_id,
            correlation_id,
            retention_expires_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event_id,
            user_id,
            "Administrator",
            event_type,
            "knowledge_publication",
            event_id,
            f"knowledge-corr-{suffix}-{event_type}",
            datetime.now(UTC).replace(year=2030),
        ),
    )


def _insert_source(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    user_id: UUID,
    source_id: str,
    title: str,
    source_class: str,
    authority_level: str,
    suffix: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO knowledge_sources (
            source_id,
            source_family_id,
            title,
            canonical_url,
            source_class,
            authority_level,
            tax_domain,
            issuing_authority,
            created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            source_id,
            f"{source_id}-family-{suffix}",
            title,
            f"https://example.com/{source_id.lower()}",
            source_class,
            authority_level,
            "income_tax",
            "Kenya Revenue Authority",
            user_id,
        ),
    )


def _insert_source_version(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    version_id: UUID,
    source_id: str,
    publication_state: str,
    effective_from: date,
    effective_to: date | None,
    publication_event_id: UUID | None,
    point_in_time_url: str,
    source_input_ref: str,
    tax_year: int | None,
) -> None:
    cursor.execute(
        """
        INSERT INTO knowledge_source_versions (
            id,
            source_id,
            point_in_time_url,
            source_checksum_sha256,
            source_version_form,
            source_input_origin,
            source_input_ref,
            publication_state,
            effective_from,
            effective_to,
            tax_year,
            publication_event_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version_id,
            source_id,
            point_in_time_url,
            f"checksum-{uuid4().hex}",
            "as_issued",
            "official_source_url",
            source_input_ref,
            publication_state,
            effective_from,
            effective_to,
            tax_year,
            publication_event_id,
        ),
    )


def _insert_anchor(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    anchor_id: str,
    source_version_id: UUID,
    anchor_title: str,
    anchor_path: str,
    anchor_text: str,
    temporal_scope_from: date,
    temporal_scope_to: date | None,
) -> None:
    cursor.execute(
        """
        INSERT INTO knowledge_anchors (
            anchor_id,
            source_version_id,
            anchor_title,
            anchor_path,
            anchor_text,
            normalized_anchor_text,
            temporal_scope_from,
            temporal_scope_to
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            anchor_id,
            source_version_id,
            anchor_title,
            anchor_path,
            anchor_text,
            anchor_text.lower(),
            temporal_scope_from,
            temporal_scope_to,
        ),
    )


def _insert_chunk(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    chunk_id: UUID,
    anchor_id: str,
    chunk_index: int,
    chunk_text: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO knowledge_chunks (
            id,
            anchor_id,
            chunk_index,
            chunk_text,
            normalized_chunk_text
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            chunk_id,
            anchor_id,
            chunk_index,
            chunk_text,
            chunk_text.lower(),
        ),
    )


def _insert_chunk_embedding(
    connection: psycopg.Connection,
    *,
    chunk_id: UUID,
    embedding_model: str,
    vector: tuple[float, ...],
    content_checksum_sha256: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO knowledge_chunk_embeddings (
                chunk_id,
                embedding_model,
                embedding_dimensions,
                embedding_vector_json,
                content_checksum_sha256
            )
            VALUES (%s, %s, %s, %s::jsonb, %s)
            """,
            (
                chunk_id,
                embedding_model,
                len(vector),
                json.dumps(list(vector)),
                content_checksum_sha256,
            ),
        )
    connection.commit()


def _ensure_knowledge_migration_applied(*, database_url: str) -> None:
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.knowledge_chunk_embeddings')")
                row = cursor.fetchone()
                if row is not None and row[0] is not None:
                    return
                for migration_file in KNOWLEDGE_MIGRATION_FILES:
                    sql_text = migration_file.read_text(encoding="utf-8")
                    cursor.execute(cast(Query, sql_text))
    except OSError as error:
        pytest.skip(f"Knowledge migration file is unreadable: {error}")
    except psycopg.Error:
        pytest.skip("Knowledge migration could not be applied for DB-backed tests.")


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

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


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _result_items(payload: dict[str, object]) -> list[dict[str, object]]:
    result = cast(dict[str, object], payload["result"])
    items = cast(list[object], result["items"])
    return [cast(dict[str, object], item) for item in items]


def _required_item_fields() -> set[str]:
    return {
        "source_id",
        "title",
        "url",
        "source_type",
        "tax_domain",
        "authority_level",
        "effective_from",
        "effective_to",
        "tax_year",
        "anchor_id",
    }
