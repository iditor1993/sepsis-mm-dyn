"""
Shared utilities for the SEPSIS-MM-DYN extraction pipeline (v2.4.1 rewrite).

DuckDB connection management, Parquet I/O, canonical serialization
(eICU source_event_id, 提取方案 §2.2 C6a / A.4), schema contract checks.
"""
import hashlib
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

_D = Path(__file__).resolve().parent
if str(_D) not in sys.path:
    sys.path.insert(0, str(_D))

import config  # noqa: E402


# --------------- DuckDB connection management ---------------

def connect_mimic(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Connect to the MIMIC-IV DuckDB database (read-only)."""
    con = duckdb.connect(config.MIMIC_DB, read_only=read_only)
    con.execute("SET threads TO 4")
    con.execute("SET memory_limit = '12GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = 'E:/clinical_research/_duckdb_tmp'")
    return con


def connect_eicu(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Connect to the eICU-CRD DuckDB database (read-only)."""
    con = duckdb.connect(config.EICU_DB, read_only=read_only)
    con.execute("SET threads TO 4")
    con.execute("SET memory_limit = '12GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = 'E:/clinical_research/_duckdb_tmp'")
    return con


# --------------- Parquet I/O ---------------

def write_parquet(df: pd.DataFrame, path: Path, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, **kwargs)


def read_parquet(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def write_duckdb_table(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> int:
    """Execute a query and write the result to Parquet. Returns row count."""
    df = con.execute(query).fetchdf()
    write_parquet(df, path)
    return len(df)


def write_duckdb_table_direct(con: duckdb.DuckDBPyConnection, query: str,
                              path: Path) -> int:
    """Stream query result directly to Parquet via COPY TO (no pandas round-trip).

    For large result sets (tens of millions of rows). Returns row count.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    p = str(path).replace("\\", "/")
    con.execute(
        f"COPY ({query}) TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    n = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{p}')").fetchone()[0]
    return n


# --------------- Schema contract check ---------------

def check_schema(path: Path, expected_cols: list, strict: bool = True) -> dict:
    """Compare parquet columns against the contract column list.

    Returns a report dict; raises AssertionError when strict and mismatch.
    """
    df = pd.read_parquet(path)
    actual = list(df.columns)
    missing = [c for c in expected_cols if c not in actual]
    extra = [c for c in actual if c not in expected_cols]
    report = {
        "file": str(path), "rows": len(df), "n_cols": len(actual),
        "missing_cols": missing, "extra_cols": extra,
        "pass": (not missing) and (not extra),
    }
    if strict and not report["pass"]:
        raise AssertionError(
            f"Schema contract violation in {path}: missing={missing} extra={extra}")
    return report


def content_hash_df(df: pd.DataFrame) -> str:
    """Stable content hash for provenance registration."""
    blob = pd.util.hash_pandas_object(df, index=False).values.tobytes()
    return hashlib.sha256(blob).hexdigest()


# --------------- Canonical serialization (§2.2 C6a / A.4) ---------------

def canonical_serialize(row: dict, field_order: list, table_name: str) -> tuple:
    """Produce (canonical_serialized_event, source_event_id) for one row.

    Frozen rules: UTF-8, fixed field order per table schema, explicit types,
    JSON null for missing, canonical decimal floats, standard JSON escaping,
    Unicode NFC, SHA-256.
    """
    obj = {
        "source_table": table_name,
        **{k: _normalize_value(row.get(k)) for k in field_order},
    }
    serialized = json.dumps(obj, ensure_ascii=False, sort_keys=False,
                            allow_nan=False, separators=(",", ":"))
    serialized = unicodedata.normalize("NFC", serialized)
    event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return serialized, event_id


def _normalize_value(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def canonicalize_str_sql(col: str) -> str:
    """SQL fragment: canonical form of a string column (trim + lower)."""
    return f"LOWER(TRIM({col}))"


def struct_json_sql(fields: dict) -> str:
    """Build a DuckDB struct_pack -> to_json expression with frozen field order.

    fields: {json_field_name: sql_expression}
    注：DuckDB STRUCT_PACK 命名参数语法为 `name := expr`（不支持 `expr AS name`，
    1.5.3 实测 ParserException）；字段名均为冻结 schema 标识符，可安全内联。
    """
    parts = ", ".join(f"{name} := {expr}" for name, expr in fields.items())
    return f"TO_JSON(STRUCT_PACK({parts}))"


# --------------- Convenience ---------------

def log_step(step_name: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {step_name}")


def episode_stays_cte(merge_version: str = None) -> str:
    """SQL fragment: episode -> stays mapping from the final episode map."""
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    ep_path = str(config.OUTPUT_DIRS["episodes"] / "mimic_icu_episode_map_final.parquet")
    return f"""
    SELECT episode_id, stay_id
    FROM read_parquet('{ep_path}')
    WHERE episode_mapping_version = '{mv}'
    """
