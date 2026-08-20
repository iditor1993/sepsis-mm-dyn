"""ECG 滤波审计（技术文档 §20，SCE 训练前置）。

三个审计问题：
  Q1 设备是否已预滤波 → 60Hz 功率占比分布（训练集抽样）
  Q2 基线漂移三方案比较 {无高通, 0.05Hz HP, 0.5Hz HP}（训练集抽样，
     零相位 filtfilt 2 阶 Butterworth；质量 + 形态保真度双维度）
  Q3 起搏信号检出率（启发式阈值法，训练集抽样）
输出：qa/ecg_filter_audit_v1.md（测量分布 + 推荐建议，供 PI 签字）。
"""
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "preprocess" / "src"))
sys.path.insert(0, str(ROOT / "src" / "data"))

from lib import io  # noqa: E402

warnings.filterwarnings("ignore")

N_BASELINE_SAMPLE = 500
N_SPECTRA_SAMPLE = 3000
N_PACING_SAMPLE = 3000
WORKERS = 6


def _train_studies(cfg):
    cache = ROOT / cfg["paths"]["out_root"] / "p5_ecg_cache" / cfg["run_id"]
    idx = pd.read_parquet(cache / "ecg_cache_index.parquet")
    splits = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "splits/split_assignments_v2.parquet")
    ecg_rec = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "ecg_index/ecg_landmark_index_v2.parquet",
        columns=["study_id", "episode_id"])
    cohort = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "cohorts/cohort_mimic_v2.parquet",
        columns=["episode_id", "subject_id"])
    st2sub = ecg_rec.merge(cohort, on="episode_id")[
        ["study_id", "subject_id"]].drop_duplicates("study_id")
    df = idx.merge(st2sub, on="study_id", how="left")
    df = df.merge(splits[["subject_id", "set_name"]],
                  on="subject_id", how="left")
    return df[df["set_name"] == "train"].reset_index(drop=True)


def _read(path):
    import wfdb
    rec = wfdb.rdrecord(path)
    return rec.p_signal.astype(np.float64), rec.fs


def _spectral_60hz(path):
    try:
        sig, fs = _read(path)
        x = sig[:, 1] if sig.shape[1] > 1 else sig[:, 0]
        x = x - np.mean(x)
        f = np.fft.rfft(x)
        psd = np.abs(f) ** 2
        freqs = np.fft.rfftfreq(len(x), 1 / fs)
        band = (freqs >= 59) & (freqs <= 61)
        total = (freqs >= 0.5) & (freqs <= 100)
        return float(psd[band].sum() / max(psd[total].sum(), 1e-12))
    except Exception:
        return np.nan


def _pacing_detect(path):
    """启发式：二阶差分尖峰（高斜率窄脉冲）≥3 个 → pacing_flag。"""
    try:
        sig, fs = _read(path)
        hits = 0
        for c in range(min(sig.shape[1], 12)):
            x = sig[:, c]
            d2 = np.abs(np.diff(x, n=2))
            if len(d2) == 0:
                continue
            thr = max(np.median(d2) + 8 * np.median(np.abs(d2 - np.median(d2))),
                      0.5)  # mV 级阈值
            hits += int((d2 > thr).sum())
        return bool(hits >= 3)
    except Exception:
        return False


def _hp(x, fs, cutoff):
    from scipy.signal import butter, filtfilt
    b, a = butter(2, cutoff / (fs / 2), btype="high")
    return filtfilt(b, a, x)


def _baseline_metrics(path):
    try:
        sig, fs = _read(path)
        x = sig[:, 1] if sig.shape[1] > 1 else sig[:, 0]
        ref = x
        outs = {}
        for name, cut in [("none", None), ("hp_005", 0.05), ("hp_05", 0.5)]:
            y = ref if cut is None else _hp(ref, fs, cut)
            k = max(int(fs), 1) | 1
            from scipy import ndimage
            bl = ndimage.median_filter(y, size=k, mode="nearest")
            # 形态保真：与原信号相关、QRS 振幅保持、T 波区（后 60%）RMS 变化
            corr = float(np.corrcoef(ref, y)[0, 1]) if cut else 1.0
            qrs_ratio = float(np.max(np.abs(y)) / max(np.max(np.abs(ref)),
                                                      1e-9))
            t0 = int(len(ref) * 0.6)
            t_rms_change = float(np.sqrt(np.mean(y[t0:] ** 2))
                                 / max(np.sqrt(np.mean(ref[t0:] ** 2)), 1e-9))
            outs[name] = {
                "baseline_mv": float(bl.max() - bl.min()),
                "corr_with_orig": corr,
                "qrs_amp_ratio": qrs_ratio,
                "late_rms_ratio": t_rms_change,
            }
        return outs
    except Exception:
        return None


def _worker(task):
    kind, path = task
    if kind == "spectra":
        return _spectral_60hz(path)
    if kind == "pacing":
        return _pacing_detect(path)
    if kind == "baseline":
        return _baseline_metrics(path)


def run(cfg: dict):
    qa_dir = ROOT / cfg["paths"]["data_pipeline_root"] / "qa"
    out_md = qa_dir / "ecg_filter_audit_v1.md"
    train = _train_studies(cfg)
    print(f"[audit] train studies: {len(train):,}")
    rng = np.random.default_rng(io.seed_for(cfg, "ecg_filter_audit"))

    root = ROOT / cfg["paths"]["ecg_wfdb_root"]
    spectra_idx = rng.choice(len(train),
                             size=min(N_SPECTRA_SAMPLE, len(train)),
                             replace=False)
    baseline_idx = rng.choice(len(train),
                              size=min(N_BASELINE_SAMPLE, len(train)),
                              replace=False)
    pacing_idx = rng.choice(len(train),
                            size=min(N_PACING_SAMPLE, len(train)),
                            replace=False)

    from multiprocessing import Pool
    tasks = ([("spectra", str(root / str(train.iloc[i].ecg_path)))
              for i in spectra_idx]
             + [("pacing", str(root / str(train.iloc[i].ecg_path)))
                for i in pacing_idx]
             + [("baseline", str(root / str(train.iloc[i].ecg_path)))
                for i in baseline_idx])
    results = []
    with Pool(WORKERS) as pool:
        for i, r in enumerate(pool.imap_unordered(_worker, tasks,
                                                  chunksize=32)):
            results.append(r)
            if (i + 1) % 1000 == 0:
                print(f"[audit] {i + 1:,}/{len(tasks):,}")

    spec = np.array(results[:len(spectra_idx)], dtype=float)
    pace = np.array(results[len(spectra_idx):
                            len(spectra_idx) + len(pacing_idx)], dtype=bool)
    bl = results[len(spectra_idx) + len(pacing_idx):]

    spec = spec[np.isfinite(spec)]
    pace_rate = float(pace.mean())

    schemes = {}
    for name in ["none", "hp_005", "hp_05"]:
        arr = [r[name] for r in bl if r]
        schemes[name] = {
            "baseline_mv_median": float(np.median(
                [a["baseline_mv"] for a in arr])),
            "corr_median": float(np.median(
                [a["corr_with_orig"] for a in arr])),
            "qrs_ratio_median": float(np.median(
                [a["qrs_amp_ratio"] for a in arr])),
            "late_rms_ratio_median": float(np.median(
                [a["late_rms_ratio"] for a in arr])),
        }

    md = f"""# ecg_filter_audit_v1（技术文档 §20 波形层滤波审计，SCE 前置）

- 生成：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；样本：训练集 study（总 {len(train):,}）中
  频谱 {len(spec):,} 份、基线比较 {len(bl):,} 份、起搏 {len(pacing_idx):,} 份
- 滤波实现：零相位 `scipy.signal.filtfilt`，2 阶 Butterworth 高通（相位模式与阶数如被采纳将同参数锁定）

## Q1 60Hz 工频（设备预滤波评估）

| 指标 | 值 |
|---|---|
| 60Hz 功率占比（59–61Hz / 0.5–100Hz）中位 | {np.median(spec):.5f} |
| p90 | {np.quantile(spec, 0.9):.5f} |
| p99 | {np.quantile(spec, 0.99):.5f} |
| 占比 >0.01 的比例 | {(spec > 0.01).mean():.1%} |

**读法**：中位与 p90 接近 0 → 设备层面工频已被抑制，主方案**不加 60Hz 陷波**；
若个别记录显著（p99 高），记录级 QC 已在 E-4 覆盖。**无陷波版本同时保留为敏感性输入**（§20 硬性要求）。

## Q2 基线漂移三方案比较（{len(bl):,} 份训练集 study）

| 方案 | 漂移幅度中位 (mV) | 与原信号相关中位 | QRS 振幅保持中位 | 后段（T 波区）RMS 比中位 |
|---|---|---|---|---|
| 无高通 none | {schemes['none']['baseline_mv_median']:.4f} | 1.000 | 1.000 | 1.000 |
| 0.05Hz 高通 | {schemes['hp_005']['baseline_mv_median']:.4f} | {schemes['hp_005']['corr_median']:.4f} | {schemes['hp_005']['qrs_ratio_median']:.4f} | {schemes['hp_005']['late_rms_ratio_median']:.4f} |
| 0.5Hz 高通 | {schemes['hp_05']['baseline_mv_median']:.4f} | {schemes['hp_05']['corr_median']:.4f} | {schemes['hp_05']['qrs_ratio_median']:.4f} | {schemes['hp_05']['late_rms_ratio_median']:.4f} |

**读法**：0.5Hz 对 T 波区 RMS 的改变量若明显（比值偏离 1），按 §20 应避免；
0.05Hz 若几乎不损形态且能去漂移，是稳妥折中。最终决定权在 PI（见 §5 签署区）。

## Q3 起搏信号检出（启发式候选规则）

| 指标 | 值 |
|---|---|
| pacing_flag 检出率（训练集 {len(pacing_idx):,} 份） | {pace_rate:.2%} |

规则：任一导联 |d²x/dt²| 超过鲁棒阈值（median+8×MAD 与 0.5mV 取大）的尖峰 ≥3 个。
仅标记不剔除；正式阈值可在 P5 质量分析后微调。

## 待 PI 签署（签后写入 `p7_fitted/filter_decision.json`）

- [ ] 60Hz 陷波：不加（推荐，按 Q1） / 加
- [ ] 基线高通：none / 0.05Hz（按 Q2 数据二选一） / 0.5Hz（若选需补全参数附件）
- [ ] 起搏标记规则：按上述启发式 / 调整
- [ ] 若任何滤波被采纳：同意重跑 P5 重生成 ECG 缓存；否则确认现有缓存直接用于 SCE 训练
"""
    out_md.write_text(md, encoding="utf-8")
    print(f"[audit] report: {out_md}")
    return {"spectra": {"median": float(np.median(spec)),
                        "p90": float(np.quantile(spec, 0.9))},
            "schemes": schemes, "pacing_rate": pace_rate}


if __name__ == "__main__":
    cfg = io.load_config()
    run(cfg)
