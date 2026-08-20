"""P0: 环境与配置锁定（方案 §2）。"""
import platform
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from lib import io


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    upstream = {}
    for rel in ["cohorts/cohort_mimic_v2.parquet",
                "landmarks/landmarks_v2.parquet",
                "labels/labels_24h_v2.parquet",
                "features/vitals_realtime_strict_v2.parquet",
                "features/labs_hourly_v2.parquet",
                "features/sofa_hourly_v2.parquet",
                "ecg_index/ecg_landmark_index_v2.parquet",
                "splits/split_assignments_v2.parquet",
                "cohorts/cohort_eicu_v2.parquet"]:
        p = root / rel
        upstream[rel] = io.file_hash(p) if p.exists() else None
    d0 = {}
    d0_path = root / "_meta" / "d0_decision.json"
    if d0_path.exists():
        import json
        d0 = json.loads(d0_path.read_text(encoding="utf-8"))
    freeze_path = root / "_meta" / "freeze_checklist.json"
    freeze_status = "present" if freeze_path.exists() else "missing"

    meta = {
        "run_id": cfg["run_id"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config_hash": cfg["_config_hash"],
        "config_path": cfg["_config_path"],
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "deps": {"duckdb": duckdb.__version__, "pandas": pd.__version__,
                 "numpy": np.__version__},
        "upstream_hashes": upstream,
        "d0_decision": d0,
        "freeze_checklist": freeze_status,
        "seeds": {"seed_root": cfg["seed_root"]},
        "substitutions": [
            "pytest 未安装 → tests 使用 stdlib unittest（方案 §15.1 替代登记）",
            "lmdb 未安装 → ECG 缓存使用 npy memmap（方案 §7.3 替代登记）",
        ],
    }
    io.write_json(meta, io.meta_dir(cfg) / "preprocess_code_version.json")
    print(f"[P0] preprocess_code_version.json written; "
          f"upstream files hashed: {sum(v is not None for v in upstream.values())}")
    return meta
