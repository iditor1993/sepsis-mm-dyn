"""E-4: ECG 数据驱动 QC——训练集拟合阈值并冻结（方案 §7.2、冻结清单 E-4）。

指标（原始信号计算，不用归一化缓存）：
  snr_db          信号功率 / 高频噪声功率（一阶差分）
  baseline_mv     基线漂移幅度（1s 滑动中值滤波残余的峰峰值，mV）
  sat_ratio       饱和比例（重复极端值占比）
  extreme_ratio   极端振幅比例（|x| > 5 mV）
  lead_corr       导联相关性（可用导联间 |corr| 中位数）
阈值：训练集 study 的 median ± 3×MAD（fitted_on=train），落盘冻结。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from lib import io, manifest


def _metrics_one(path: str):
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import wfdb
        rec = wfdb.rdrecord(path)
        from scipy import ndimage
        sig = rec.p_signal.astype(np.float64)
        vars_, noises = [], []
        for c in range(sig.shape[1]):
            x = sig[:, c]
            x = x[np.isfinite(x)]
            if len(x) < 100:
                continue
            vars_.append(np.var(x))
            noises.append(np.var(np.diff(x)) / 2.0)
        if not vars_:
            return None
        snr = 10 * np.log10(max(np.mean(vars_), 1e-12)
                            / max(np.mean(noises), 1e-12))
        # 基线漂移：中值滤波（C 实现，快）残余的峰峰值
        ref = sig[:, 1] if sig.shape[1] > 1 else sig[:, 0]
        k = max(int(rec.fs), 1) | 1
        bl_line = ndimage.median_filter(ref, size=k, mode="nearest")
        baseline_mv = float(bl_line.max() - bl_line.min())
        # 饱和：最大/最小值重复占比
        sat = float(np.mean((ref == ref.max()) | (ref == ref.min())))
        extreme = float(np.mean(np.abs(ref) > 5.0))
        # 导联相关（10 倍降采样加速，估计量等价）
        n = min(sig.shape[1], 12)
        if n >= 2:
            cc = np.corrcoef(sig[::10, :n].T)
            iu = np.abs(cc[np.triu_indices(n, 1)])
            lead_corr = float(np.median(iu))
        else:
            lead_corr = 0.0
        return {"snr_db": float(snr), "baseline_mv": baseline_mv,
                "sat_ratio": sat, "extreme_ratio": extreme,
                "lead_corr": lead_corr}
    except Exception:
        return None


def _worker(args):
    import os
    import sys as _sys
    _sys.stdout = open(os.devnull, "w")   # 抑制 wfdb 每文件日志
    _sys.stderr = open(os.devnull, "w")
    study_id, path = args
    m = _metrics_one(path)
    if m is None:
        return {"study_id": study_id, "qc_metrics_ok": False}
    return {"study_id": study_id, "qc_metrics_ok": True, **m}


def run(cfg: dict) -> dict:
    out = io.artifact_dir(cfg, "p7_fitted")
    cache_dir = io.PROJECT_ROOT / cfg["paths"]["out_root"] \
        / "p5_ecg_cache" / cfg["run_id"]
    idx = pd.read_parquet(cache_dir / "ecg_cache_index.parquet")
    met_path = cache_dir / "ecg_quality_metrics.parquet"
    qc_path = cache_dir / "ecg_data_qc.parquet"

    if met_path.exists():
        met = pd.read_parquet(met_path)
    else:
        partial = cache_dir / "ecg_quality_metrics_partial.parquet"
        done_ids = set()
        rows = []
        if partial.exists():
            prev = pd.read_parquet(partial)
            rows = prev.to_dict("records")
            done_ids = set(prev["study_id"].tolist())
        tasks = [(int(r.study_id),
                  str(io.PROJECT_ROOT / cfg["paths"]["ecg_wfdb_root"]
                      / str(r.ecg_path)))
                 for r in idx.itertuples(index=False)
                 if int(r.study_id) not in done_ids]
        print(f"[E-4] metrics todo: {len(tasks):,} (done: {len(done_ids):,})")
        from multiprocessing import Pool
        with Pool(int(cfg["ecg"].get("workers", 6))) as pool:
            for i, r in enumerate(pool.imap_unordered(_worker, tasks,
                                                      chunksize=64)):
                rows.append(r)
                if (i + 1) % 5000 == 0:
                    pd.DataFrame(rows).to_parquet(partial, index=False)
                    print(f"[E-4] metrics {i + 1:,}/{len(tasks):,}")
        met = pd.DataFrame(rows)
        met.to_parquet(met_path, index=False)
        partial.unlink(missing_ok=True)
    print(f"[E-4] metrics: {len(met):,} studies")

    # 训练集 study：subject ∈ train split
    splits = pd.read_parquet(
        io.data_root(cfg) / "splits/split_assignments_v2.parquet")
    ecg_rec = pd.read_parquet(
        io.data_root(cfg) / "ecg_index/ecg_landmark_index_v2.parquet",
        columns=["study_id", "episode_id"])
    cohort = pd.read_parquet(
        io.data_root(cfg) / "cohorts/cohort_mimic_v2.parquet",
        columns=["episode_id", "subject_id"])
    st2sub = ecg_rec.merge(cohort, on="episode_id")[
        ["study_id", "subject_id"]].drop_duplicates("study_id")
    met = met.merge(st2sub, on="study_id", how="left")
    met = met.merge(splits[["subject_id", "set_name"]],
                    on="subject_id", how="left")
    train_met = met[(met["set_name"] == "train") & met["qc_metrics_ok"]]
    print(f"[E-4] train studies for threshold fitting: {len(train_met):,}")

    metrics = ["snr_db", "baseline_mv", "sat_ratio", "extreme_ratio",
               "lead_corr"]
    # 零膨胀指标（sat/extreme）用 p99 上界；其余用 median ± 3×MAD；
    # 全部 nanmedian（基线/导联相关存在少量 NaN）
    thr = {"fitted_on": "train", "method": "median_pm_3MAD_or_p99_zero_inflated",
           "metrics": {}}
    for c in metrics:
        v = train_met[c].to_numpy(dtype=float)
        v = v[np.isfinite(v)]
        if c in ("sat_ratio", "extreme_ratio"):
            med = float(np.median(v))
            hi = float(np.quantile(v, 0.99))
            thr["metrics"][c] = {"median": med, "mad": None,
                                 "lo": float("-inf"), "hi": hi}
        else:
            med = float(np.median(v))
            mad = float(np.median(np.abs(v - med))) + 1e-12
            thr["metrics"][c] = {"median": med, "mad": mad,
                                 "lo": float(med - 3 * mad),
                                 "hi": float(med + 3 * mad)}
    io.write_json(thr, out / "ecg_quality_thresholds.json")
    manifest.register_artifact(cfg, "ecg_quality_thresholds", "e4", thr,
                               fitted_on="train")

    def _pass(row):
        if not row["qc_metrics_ok"]:
            return False
        for c in metrics:
            val = row[c]
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return False
            t = thr["metrics"][c]
            if not (t["lo"] <= val <= t["hi"]):
                return False
        return True

    met["data_qc_pass"] = met.apply(_pass, axis=1)
    met[["study_id", "qc_metrics_ok", "data_qc_pass"]].to_parquet(
        qc_path, index=False)
    stats = {"studies": len(met), "train_fit": len(train_met),
             "pass": int(met["data_qc_pass"].sum()),
             "thresholds": {c: [round(thr["metrics"][c]["lo"], 4),
                                round(thr["metrics"][c]["hi"], 4)]
                            for c in metrics}}
    io.write_json(stats, cache_dir / "ecg_data_qc_stats.json")
    print(f"[E-4] data_qc_pass: {stats['pass']:,}/{stats['studies']:,} "
          f"({stats['pass']/stats['studies']:.1%})")
    return stats
