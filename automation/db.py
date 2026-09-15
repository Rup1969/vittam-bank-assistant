"""DuckDB connection and table DDL for the freshness/structured-data pipeline.

Deliberately a single small DuckDB file (data/automation.duckdb) holding both
the fetch/change-log (Phase A) and the structured product tables (Phase B) —
one source of truth instead of a separate per-source state file plus a
database, per the plan's design note.

This module is fully separate from the existing app.py/build_index.py/FAISS
pipeline. Nothing here reads or writes indexes/*.index or indexes/*.json.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "automation.duckdb"

_DDL = """
CREATE TABLE IF NOT EXISTS fetch_log (
    source_id  VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    file_path  VARCHAR NOT NULL,
    text_hash  VARCHAR NOT NULL,
    changed    BOOLEAN NOT NULL,
    error      VARCHAR
);

CREATE TABLE IF NOT EXISTS fd_rates (
    bank_id               VARCHAR NOT NULL,
    product_type          VARCHAR NOT NULL DEFAULT 'fixed_deposit',
    tenure_label          VARCHAR NOT NULL,
    tenure_days_min       INTEGER,
    tenure_days_max       INTEGER,
    interest_rate_general DOUBLE,
    interest_rate_senior  DOUBLE,
    min_deposit           DOUBLE,
    effective_date        VARCHAR,
    source_url            VARCHAR NOT NULL,
    fetched_at            VARCHAR NOT NULL,
    data_source           VARCHAR NOT NULL DEFAULT 'live_fetch',
    deposit_ceiling       DOUBLE,
    deposit_variant       VARCHAR NOT NULL DEFAULT 'retail'
);

CREATE TABLE IF NOT EXISTS loan_rates (
    bank_id         VARCHAR NOT NULL,
    loan_type       VARCHAR NOT NULL DEFAULT 'home_loan',
    rate_type       VARCHAR NOT NULL,
    benchmark_type  VARCHAR,
    benchmark_rate  DOUBLE,
    rate_min        DOUBLE NOT NULL,
    rate_max        DOUBLE NOT NULL,
    tier_type       VARCHAR,
    tier_label      VARCHAR,
    processing_fee  VARCHAR,
    max_ltv         DOUBLE,
    effective_date  VARCHAR,
    source_url      VARCHAR NOT NULL,
    fetched_at      VARCHAR NOT NULL,
    data_source     VARCHAR NOT NULL DEFAULT 'live_fetch'
);

CREATE TABLE IF NOT EXISTS extraction_log (
    source_id   VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    status      VARCHAR NOT NULL,  -- 'ok' or 'rejected'
    detail      VARCHAR,
    logged_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS news_archive (
    headline    VARCHAR NOT NULL,
    summary     VARCHAR,
    link        VARCHAR NOT NULL UNIQUE,
    outlet      VARCHAR NOT NULL,
    pub_date    TIMESTAMP,
    archived_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
"""


def get_connection() -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the automation DuckDB file with tables ready."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(_DDL)
    # `_DDL`'s CREATE TABLE IF NOT EXISTS only applies to a brand-new file —
    # an already-existing fd_rates table (every DB before 2026-08-16) never
    # gets the data_source column from it. This ALTER is idempotent and
    # backfills DEFAULT 'live_fetch' into every pre-existing row, which is
    # correct for every bank except manually-loaded ones — those get
    # corrected to 'manual_load_frozen' by a one-time UPDATE (see
    # migrate_data_source_backfill.py), not by this function.
    # DuckDB's ALTER TABLE ADD COLUMN doesn't support NOT NULL (only CREATE
    # TABLE does) — DEFAULT alone still backfills every existing row.
    con.execute(
        "ALTER TABLE fd_rates ADD COLUMN IF NOT EXISTS "
        "data_source VARCHAR DEFAULT 'live_fetch'"
    )
    con.execute(
        "ALTER TABLE fd_rates ADD COLUMN IF NOT EXISTS deposit_ceiling DOUBLE"
    )
    # Backfills 'retail' into every pre-existing row (correct for all of
    # them except SBI's, which gets corrected to the real per-row value by
    # a fresh extraction run — see extract_structured.py's SBI prompt).
    con.execute(
        "ALTER TABLE fd_rates ADD COLUMN IF NOT EXISTS "
        "deposit_variant VARCHAR DEFAULT 'retail'"
    )
    return con


def last_hash(con: duckdb.DuckDBPyConnection, source_id: str) -> str | None:
    """Most recent successfully-recorded text_hash for a source, or None
    if it has never been fetched before (or every prior attempt errored)."""
    row = con.execute(
        """
        SELECT text_hash FROM fetch_log
        WHERE source_id = ? AND error IS NULL
        ORDER BY fetched_at DESC
        LIMIT 1
        """,
        [source_id],
    ).fetchone()
    return row[0] if row else None


def already_extracted(con: duckdb.DuckDBPyConnection, source_id: str, fetched_at) -> bool:
    """Whether this exact fetch_log row has already been through extraction —
    prevents re-extracting (and re-billing the LLM call for) the same
    unchanged snapshot if extract_structured.py is run more than once."""
    row = con.execute(
        "SELECT 1 FROM extraction_log WHERE source_id = ? AND fetched_at = ?",
        [source_id, fetched_at],
    ).fetchone()
    return row is not None


def resolve_scraped_path(file_path: str) -> Path | None:
    """Turns a `fetch_log.file_path` value back into a real, current Path
    on THIS machine, or None if the referenced snapshot genuinely isn't
    here. Callers (extract_structured.py, extract_loan_rates.py) must
    skip gracefully on None rather than assume the file exists.

    Two real cases this has to handle, confirmed 2026-09-16 (a GitHub
    Actions run crashed on this): fetch_and_track.py now stores file_path
    RELATIVE to PROJECT_ROOT (POSIX separators) so it's portable — that
    case just joins cleanly. But automation.duckdb is also committed and
    shipped to other environments (PROJECT_STATUS.md §54), which means
    OLDER rows already in that shipped snapshot still hold an ABSOLUTE
    path from whatever machine originally fetched them (e.g. a Windows
    path like `C:\\Users\\...`) — on the SAME machine that's often still
    valid (`PROJECT_ROOT / <that absolute path>` resolves back to the
    absolute path itself, standard pathlib behavior), but on a different
    machine or OS it resolves to something that was never written there.
    Rather than trying to detect which case a given string is, this just
    tries the join and checks reality with `.exists()` — correct either
    way, and the one thing that actually matters: a fetch_log row whose
    snapshot isn't available HERE gets skipped with a clear reason
    instead of crashing the whole run over one stale/foreign reference.
    """
    candidate = PROJECT_ROOT / file_path
    return candidate if candidate.exists() else None
