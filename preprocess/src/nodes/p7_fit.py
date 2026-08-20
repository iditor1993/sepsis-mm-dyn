"""P7: 划分应用与训练集专属拟合（方案 §9）。

拟合：通道 scaler、静态 scaler、类别编码器、静态插补器——全部仅训练集；
registry.json 登记哈希 + fitted_on=train；生成 X_seq_scaled.npy。
"""
import numpy as np
import pandas as pd

from lib import io, manifest, scalers, static as lib_static
from nodes.p3_static import (DESCRIBE_ONLY, STATIC_CATEGORICAL,
                             STATIC_NUMERIC)


def run(cfg: dict) -> dict:
    out7 = io.artifact_dir(cfg, "p7_fitted")
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    tensor_meta = io.PROJECT_ROOT / cfg["paths"]["out_root"] \
        / "p2_clinical" / cfg["run_id"] / "master" / "tensor_meta.json"
    import json
    channels = json.loads(tensor_meta.read_text(encoding="utf-8"))["channels"]

    train_rows = master.loc[master["set_name"] == "train", "row_idx"] \
        .to_numpy()
    print(f"[P7] train rows: {len(train_rows):,}")

    # --- channel scaler（流式两趟：先 sum/sumsq/count，避免全量入内存） ---
    x = np.lib.format.open_memmap(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p2_clinical"
        / cfg["run_id"] / "master" / "X_seq.npy", mode="r")
    m = np.lib.format.open_memmap(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p2_clinical"
        / cfg["run_id"] / "master" / "M_seq.npy", mode="r")
    ch_params = _fit_channel_scaler_streaming(x, m, train_rows, channels)
    io.write_json(ch_params, out7 / "scaler_clinical_seq.json")
    manifest.register_artifact(cfg, "scaler_clinical_seq", "p7",
                               ch_params, fitted_on="train")

    # --- scaled tensor ---
    xs = np.lib.format.open_memmap(
        out7 / "X_seq_scaled.npy", mode="w+", dtype=np.float32,
        shape=x.shape)
    _apply_channel_scaler_streaming(x, m, xs, channels, ch_params)
    xs.flush()

    # --- static scaler / encoders / imputer ---
    sdf = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p3_static"
        / cfg["run_id"] / "static_raw.parquet")
    sdf = sdf.merge(master[["row_idx", "set_name"]], on="row_idx")
    train_sdf = sdf[sdf["set_name"] == "train"]

    st_scaler = scalers.fit_static_scaler(train_sdf, STATIC_NUMERIC)
    io.write_json(st_scaler, out7 / "scaler_static.json")
    manifest.register_artifact(cfg, "scaler_static", "p7", st_scaler,
                               fitted_on="train")

    encs = {}
    for col in STATIC_CATEGORICAL:
        encs[col] = lib_static.fit_categorical_encoder(train_sdf, col)
    io.write_json(encs, out7 / "categorical_encoders.json")
    manifest.register_artifact(cfg, "categorical_encoders", "p7", encs,
                               fitted_on="train")

    imp = lib_static.fit_static_imputer(train_sdf, STATIC_NUMERIC)
    io.write_json(imp, out7 / "imputers.json")
    manifest.register_artifact(cfg, "imputers", "p7", imp,
                               fitted_on="train")

    stats = {"train_rows": int(len(train_rows)),
             "channels": len(channels),
             "static_numeric": STATIC_NUMERIC,
             "static_categorical": STATIC_CATEGORICAL}
    io.write_json(stats, out7 / "p7_stats.json")
    print(f"[P7] done: {stats}")
    return stats


def _fit_channel_scaler_streaming(x, m, rows, channels, chunk=20000):
    n = len(channels)
    sums = np.zeros(n); sums2 = np.zeros(n); cnt = np.zeros(n)
    for i in range(0, len(rows), chunk):
        r = rows[i:i + chunk]
        xb = np.asarray(x[r]); mb = np.asarray(m[r])
        masked = np.where(mb, xb, 0.0)
        sums += masked.sum(axis=(0, 2))
        sums2 += (masked ** 2).sum(axis=(0, 2))
        cnt += mb.sum(axis=(0, 2))
    mean = np.where(cnt > 0, sums / np.maximum(cnt, 1), 0.0)
    var = np.where(cnt > 0,
                   sums2 / np.maximum(cnt, 1) - mean ** 2, 1.0)
    sd = np.sqrt(np.maximum(var, 0.0)) + 1e-8
    params = {"channels": {}, "fitted_on": "train"}
    for i, name in enumerate(channels):
        params["channels"][name] = {
            "mean": float(mean[i]), "sd": float(sd[i]),
            "n_observed": int(cnt[i])}
    return params


def _apply_channel_scaler_streaming(x, m, xs, channels, params, chunk=20000):
    n_rows = x.shape[0]
    mu = np.array([params["channels"][c]["mean"] for c in channels],
                  dtype=np.float32)
    sd = np.array([params["channels"][c]["sd"] for c in channels],
                  dtype=np.float32)
    for i in range(0, n_rows, chunk):
        j = min(i + chunk, n_rows)
        xb = np.asarray(x[i:j]); mb = np.asarray(m[i:j])
        xs[i:j] = np.where(mb, (xb - mu[None, :, None]) / sd[None, :, None],
                           0.0).astype(np.float32)
