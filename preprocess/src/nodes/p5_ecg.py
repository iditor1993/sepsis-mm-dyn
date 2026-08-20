"""P5: ECG 波形预处理（方案 §7）。

36,648 个唯一 study → memmap [N,12,5000] float32 + 索引；多进程 + 断点续跑。
"""
import numpy as np
import pandas as pd

from lib import ecg as lib_ecg
from lib import io


def _worker(args):
    import os
    import sys as _sys
    _sys.stdout = open(os.devnull, "w")   # 抑制 wfdb 每文件日志
    _sys.stderr = open(os.devnull, "w")
    study_id, path = args
    try:
        r = lib_ecg.process_one(path)
        return (study_id, r["tensor"], r["lead_mask"], r["qc"])
    except Exception:
        return (study_id, None, None,
                {"readable": False, "structurally_valid": False})


def run(cfg: dict, suffix: str = "") -> dict:
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p5_ecg_cache")
    out.mkdir(parents=True, exist_ok=True)
    idx_path = out / f"ecg_cache_index{suffix}.parquet"
    tensor_path = out / f"ecg_cache{suffix}.npy"

    ecg_idx = pd.read_parquet(root / "ecg_index/ecg_landmark_index_v2.parquet")
    sel = ecg_idx[ecg_idx["ecg_selected_for_model"]]
    studies = sel[["study_id", "ecg_path"]].drop_duplicates("study_id")
    print(f"[P5] unique studies: {len(studies):,}")

    done = pd.DataFrame(columns=["study_id"])
    if idx_path.exists():
        done = pd.read_parquet(idx_path)[["study_id"]]
    done_ids = set(done["study_id"].tolist())
    todo = studies[~studies["study_id"].isin(done_ids)]
    print(f"[P5] todo: {len(todo):,} (cached: {len(done_ids):,})")

    n_target = len(studies)
    if idx_path.exists():
        cache_idx = pd.read_parquet(idx_path)
        arr = np.lib.format.open_memmap(tensor_path, mode="r+")
    else:
        cache_idx = studies[["study_id", "ecg_path"]].copy()
        cache_idx["cache_row"] = range(len(cache_idx))
        arr = np.lib.format.open_memmap(
            tensor_path, mode="w+", dtype=np.float32,
            shape=(len(cache_idx), 12, 5000))
        cache_idx.to_parquet(idx_path, index=False)

    qc_rows = []
    row_of = dict(zip(cache_idx["study_id"], cache_idx["cache_row"]))
    tasks = [(int(r.study_id),
              str(io.PROJECT_ROOT / cfg["paths"]["ecg_wfdb_root"]
                  / str(r.ecg_path)))
             for r in todo.itertuples(index=False)]

    if tasks:
        from multiprocessing import Pool
        workers = int(cfg["ecg"].get("workers", 6))
        with Pool(workers) as pool:
            for i, (study_id, tensor, lead_mask, qc) in enumerate(
                    pool.imap_unordered(_worker, tasks, chunksize=64)):
                if tensor is not None:
                    arr[row_of[study_id]] = tensor
                qc_rows.append({"study_id": study_id, **qc})
                if (i + 1) % 5000 == 0:
                    arr.flush()
                    print(f"[P5] processed {i + 1:,}/{len(tasks):,}")
        arr.flush()
    qc_df = pd.DataFrame(qc_rows)
    qc_path = out / "ecg_qc_flags.parquet"
    if qc_path.exists() and len(qc_df):
        old = pd.read_parquet(qc_path)
        qc_df = pd.concat([old[~old["study_id"].isin(
            qc_df["study_id"])], qc_df], ignore_index=True)
    elif len(qc_df) == 0 and qc_path.exists():
        qc_df = pd.read_parquet(qc_path)
    qc_df.to_parquet(qc_path, index=False)

    stats = {
        "studies": int(len(studies)),
        "cached_rows": int(len(cache_idx)),
        "structurally_valid": int(qc_df["structurally_valid"].sum()),
        "readable": int(qc_df["readable"].sum()),
        "normalization": cfg["ecg"]["normalization"],
        "pending": ["notch_decision", "baseline_filter_decision",
                    "pacing_detection", "data_driven_qc"],
    }
    io.write_json(stats, out / "ecg_cache_stats.json")
    print(f"[P5] done: {stats}")
    return stats
