"""IO helpers: config loading, parquet readers (read-only discipline)."""
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "preprocess" / "configs" / "preprocess_v1.yaml"
LOCAL_PATHS_PATH = PROJECT_ROOT / "preprocess" / "configs" / "local_paths.yaml"


def load_config(path: Path = None) -> dict:
    p = path or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if LOCAL_PATHS_PATH.exists():
        local = yaml.safe_load(LOCAL_PATHS_PATH.read_text(encoding="utf-8"))
        if isinstance(local, dict) and isinstance(local.get("paths"), dict):
            cfg.setdefault("paths", {}).update(local["paths"])
    _expand_env(cfg)
    unresolved = [
        key for key in ("mimic_db", "eicu_db", "ecg_wfdb_root")
        if "${" in str(cfg["paths"].get(key, ""))
    ]
    if unresolved:
        raise FileNotFoundError(
            "Unresolved environment variables in "
            f"{p} for paths: {', '.join(unresolved)}. "
            "Export MIMIC_DB, EICU_DB, ECG_WFDB_ROOT or create "
            "preprocess/configs/local_paths.yaml "
            "(see preprocess/configs/local_paths.example.yaml).")
    cfg["_config_path"] = str(p)
    cfg["_config_hash"] = hashlib.sha256(
        Path(p).read_bytes()).hexdigest()
    return cfg


def _expand_env(cfg: dict) -> None:
    """Expand ${VAR} placeholders in the paths block."""
    if not isinstance(cfg.get("paths"), dict):
        return
    for key, value in cfg["paths"].items():
        if isinstance(value, str):
            cfg["paths"][key] = os.path.expandvars(value)


def data_root(cfg: dict) -> Path:
    return PROJECT_ROOT / cfg["paths"]["data_pipeline_root"]


def artifact_dir(cfg: dict, node: str) -> Path:
    d = PROJECT_ROOT / cfg["paths"]["out_root"] / node / cfg["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def qa_dir(cfg: dict) -> Path:
    d = PROJECT_ROOT / cfg["paths"]["qa_root"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def meta_dir(cfg: dict) -> Path:
    d = PROJECT_ROOT / cfg["paths"]["meta_root"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_pq(root: Path, rel: str, columns=None) -> pd.DataFrame:
    """Read an extraction parquet (read-only; never writes back)."""
    return pd.read_parquet(root / rel, columns=columns)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def seed_for(cfg: dict, node_id: str) -> int:
    """Deterministic per-node seed (方案 §15.2 派生规则)。"""
    import numpy as np
    g = np.random.Generator(np.random.PCG64(
        [cfg["seed_root"], int(hashlib.sha256(
            node_id.encode()).hexdigest()[:8], 16)]))
    return int(g.integers(0, 2**31 - 1))
