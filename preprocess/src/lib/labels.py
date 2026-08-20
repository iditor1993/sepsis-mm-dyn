"""P4 sample sets + weights + competing-risk bins (方案 §6)."""
import numpy as np
import pandas as pd

MAIN_GRID_MAX_K = 11
COMPETING_HORIZON_H = 168
COMPETING_BIN_H = 6
N_COMPETING_BINS = 28


def build_sample_indices(master: pd.DataFrame,
                         labels: pd.DataFrame,
                         ecg_index: pd.DataFrame = None,
                         fresh_h: int = 24) -> dict:
    """Four sample-set indices (方案 §6.3).

    master columns: episode_key, subject_key, landmark_k, t_landmark_ts,
                    hours_since_sepsis, row_idx
    labels columns: episode_id, k, y_24h, label_status, outcome_ascertainable
    """
    lab = labels[["episode_id", "k", "y_24h", "label_status",
                  "outcome_ascertainable"]].rename(
        columns={"episode_id": "episode_key", "k": "landmark_k"})
    df = master.merge(lab, on=["episode_key", "landmark_k"], how="left")

    asc = df["outcome_ascertainable"] == True  # noqa: E712
    idx = {
        "idx_deployment_all": df[asc].copy(),
    }
    if ecg_index is not None:
        ecg = ecg_index[ecg_index["ecg_selected_for_model"]][
            ["episode_id", "k"]].rename(
            columns={"episode_id": "episode_key", "k": "landmark_k"})
        paired = df[asc].merge(ecg, on=["episode_key", "landmark_k"],
                               how="inner")
        idx["idx_paired_ecg"] = paired
        for h in (48, 72):
            col = f"within_{h}h"
            if col in ecg_index.columns:
                e2 = ecg_index[ecg_index[col]][["episode_id", "k"]].rename(
                    columns={"episode_id": "episode_key",
                             "k": "landmark_k"})
                idx[f"idx_ecg_sensitivity_{h}h"] = df[asc].merge(
                    e2, on=["episode_key", "landmark_k"], how="inner")
    for name, d in idx.items():
        d["in_main_grid"] = d["landmark_k"] <= MAIN_GRID_MAX_K
    return idx


def add_patient_weights(df: pd.DataFrame,
                        subject_col="subject_key") -> pd.DataFrame:
    """患者等权：w = 1 / n_landmarks(patient)（方案 §6.4）。"""
    n = df.groupby(subject_col)["landmark_k"].transform("count")
    out = df.copy()
    out["weight"] = 1.0 / n
    return out


def competing_bins(competing: pd.DataFrame,
                   ep_col="episode_id", k_col="k") -> pd.DataFrame:
    """事件时间离散化：(t, t+168h] 按 6h 分 28 区间（方案 §6.2）。"""
    df = competing.copy()
    t0 = pd.to_datetime(df["t_landmark_ts"])
    et = pd.to_datetime(df["event_or_censor_time"])
    hours = (et - t0).dt.total_seconds() / 3600.0
    bin_idx = np.ceil(hours / COMPETING_BIN_H).astype("float")
    bin_idx = bin_idx.clip(lower=1)
    # 删失 → 28；事件 → 落入区间（1..28）
    df["event_time_bin"] = np.where(
        df["event_type"] == 0, N_COMPETING_BINS, bin_idx).astype(int)
    return df[[ep_col, k_col, "event_type", "event_time_bin",
               "event_or_censor_time", "censor_type"]].rename(
        columns={ep_col: "episode_key", k_col: "landmark_k"})
