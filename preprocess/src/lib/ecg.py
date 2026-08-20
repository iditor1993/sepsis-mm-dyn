"""P5 ECG waveform preprocessing (方案 §7；技术文档 §20).

本 run 范围（冻结前分期，config.ecg 登记）：
  ✔ 12 导联重排 + lead_mask；原生 500Hz；截长补短 10s；mV 物理单位
  ✔ 结构性 QC（可读 / 时长≥9s / 导联数≥8 / 非全平线）
  ✔ per-record z-score（主方案）
  ✘ 陷波 / 基线滤波（pending_p7_audit）；起搏检测（pending）；
    数据驱动 QC 阈值（pending_e4）
"""
import numpy as np

LEADS = ["I", "II", "III", "aVR", "aVL", "aVF",
         "V1", "V2", "V3", "V4", "V5", "V6"]
TARGET_FS = 500
DUR_S = 10
MIN_DURATION_S = 9.0
MIN_LEADS = 8


def load_and_standardize(record_path: str, target_fs: int = TARGET_FS,
                         dur_s: int = DUR_S):
    """Read one WFDB record → (tensor[12,5000], lead_mask, qc, meta)."""
    import wfdb
    n = target_fs * dur_s
    out = np.zeros((12, n), dtype=np.float32)
    lead_mask = np.zeros(12, dtype=bool)
    qc = {"readable": False, "duration_s": 0.0, "n_sig": 0,
          "flatline": True, "structurally_valid": False}
    meta = {"fs": None, "sig_name": None}
    try:
        rec = wfdb.rdrecord(record_path)
    except Exception:
        return out, lead_mask, qc, meta
    qc["readable"] = True
    fs = rec.fs
    sig = rec.p_signal
    units = rec.units
    meta["fs"] = fs
    meta["sig_name"] = list(rec.sig_name)
    qc["n_sig"] = rec.n_sig
    duration = sig.shape[0] / fs if fs else 0.0
    qc["duration_s"] = float(duration)
    name_to_idx = {str(nm).strip(): i for i, nm in enumerate(rec.sig_name)}
    for li, lead in enumerate(LEADS):
        if lead not in name_to_idx:
            continue
        s = sig[:, name_to_idx[lead]]
        if fs and fs != target_fs:
            # MIMIC-IV-ECG 原生 500Hz；非 500Hz 按简单整数比处理（登记）
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(int(fs), int(target_fs))
            s = resample_poly(s, target_fs // g, int(fs) // g)
        s = s[:n]
        if len(s) < n:
            continue
        out[li] = np.nan_to_num(s.astype(np.float32), nan=0.0)
        lead_mask[li] = True
    qc["flatline"] = bool((np.nan_to_num(out).std(axis=1) < 1e-4).all())
    qc["structurally_valid"] = bool(
        qc["readable"] and duration >= MIN_DURATION_S
        and qc["n_sig"] >= MIN_LEADS and not qc["flatline"]
        and units is not None)
    return out, lead_mask, qc, meta


def normalize_per_record(x: np.ndarray, lead_mask: np.ndarray) -> np.ndarray:
    """per-record z-score（主方案；缺失导联保持 0；NaN 样本安全处理）。

    原始 WFDB 中部分记录含 NaN 采样点（信号缺口）：
    - 部分 NaN 的导联：用 nanmean/nanstd 计算，NaN 位置归 0；
    - 全 NaN 的导联：保持零张量（等效缺失导联）。
    """
    out = x.copy()
    for li in range(x.shape[0]):
        if not lead_mask[li]:
            continue
        s = x[li]
        if np.isnan(s).all():
            out[li] = 0.0
            lead_mask[li] = False
            continue
        mu = np.nanmean(s)
        sd = np.nanstd(s)
        out[li] = np.nan_to_num((s - mu) / (sd + 1e-8), nan=0.0)
    return out.astype(np.float32)


def process_one(record_path: str):
    """Worker: full pipeline for one study. Returns dict for cache write."""
    x, lead_mask, qc, meta = load_and_standardize(record_path)
    if qc["structurally_valid"]:
        x = normalize_per_record(x, lead_mask)
    return {"tensor": x, "lead_mask": lead_mask, "qc": qc, "meta": meta}
