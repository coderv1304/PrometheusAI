from __future__ import annotations

from prometheus_ai.storage.postgres import SCHEMA_SQL


def test_postgres_schema_enables_pgvector_before_vector_columns() -> None:
    extension = SCHEMA_SQL.index("CREATE EXTENSION IF NOT EXISTS vector")
    first_vector_column = SCHEMA_SQL.index("embedding vector(768)")

    assert extension < first_vector_column


def test_postgres_schema_adds_postmortem_embedding_to_existing_databases() -> None:
    assert "ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS embedding vector(768)" in SCHEMA_SQL
