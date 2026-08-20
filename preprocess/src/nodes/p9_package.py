"""P9: 模型输入打包（方案 §11）。

包结构：index.parquet + static.npy + manifest.json；
时序张量引用 p7 的 X_seq_scaled 主存（row_idx，不复制）。
"""
import json

import numpy as np
import pandas as pd

from lib import io, static as lib_static
from nodes.p3_static import (STATIC_CATEGORICAL, STATIC_FLAGS,
                             STATIC_NUMERIC)

MODELS = {
    "sc_common_paired": {"samples": "idx_paired_ecg", "ecg": False},
    "sce_common_paired": {"samples": "idx_paired_ecg", "ecg": True},
    "sc_common_all": {"samples": "idx_deployment_all", "ecg": False},
    "sce_deployment": {"samples": "idx_deployment_all", "ecg": True},
}
BLOCKERS = []  # 冻结生效（2026-07-30，31/31 关闭，D0 已锁定）


def run(cfg: dict) -> dict:
    out9 = io.artifact_dir(cfg, "p9_packages")
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    reg = json.loads((io.PROJECT_ROOT / cfg["paths"]["out_root"]
                      / "p7_fitted" / cfg["run_id"] / "registry.json")
                     .read_text(encoding="utf-8"))
    artifact_hashes = {a["name"]: a["content_hash"]
                       for a in reg["artifacts"]}

    static_mat, static_names = _build_static_matrix(cfg, master)
    core = list(cfg["sc_common_core"])
    comp = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p4_samples"
        / cfg["run_id"] / "competing_risk_labels.parquet") \
        if (io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p4_samples"
            / cfg["run_id"] / "competing_risk_labels.parquet").exists() \
        else None

    stats = {}
    for model, spec in MODELS.items():
        samples = pd.read_parquet(
            io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p4_samples"
            / cfg["run_id"] / f"{spec['samples']}.parquet")
        for set_name in ["train", "validation", "test"]:
            pkg = samples[samples["set_name"] == set_name].copy()
            if len(pkg) == 0:
                continue
            d = out9 / model / set_name
            d.mkdir(parents=True, exist_ok=True)
            idx = pkg[["episode_key", "subject_key", "landmark_k",
                       "hours_since_sepsis", "set_name", "y_24h",
                       "label_status", "weight", "in_main_grid",
                       "row_idx"]].copy()
            if comp is not None:
                idx = idx.merge(
                    comp[["episode_key", "landmark_k", "event_type",
                          "event_time_bin"]],
                    on=["episode_key", "landmark_k"], how="left")
            if spec["ecg"]:
                mod = pd.read_parquet(
                    io.PROJECT_ROOT / cfg["paths"]["out_root"]
                    / "p6_modality" / cfg["run_id"]
                    / "modality_index.parquet")
                idx = idx.merge(
                    mod[["episode_key", "landmark_k", "study_id",
                         "ecg_selected_for_model",
                         "modality_dropout_group"]],
                    on=["episode_key", "landmark_k"], how="left")
            idx.to_parquet(d / "index.parquet", index=False)

            srows = pkg["row_idx"].to_numpy()
            np.save(d / "static.npy",
                    static_mat[srows].astype(np.float32))

            man = {
                "model": model, "set_name": set_name,
                "run_id": cfg["run_id"],
                "samples_source": spec["samples"],
                "n_samples": int(len(idx)),
                "n_patients": int(idx["subject_key"].nunique()),
                "n_positive": int((idx["y_24h"] == 1).sum()),
                "channels": core,
                "tensor_ref": {
                    "path": "p7_fitted/X_seq_scaled.npy",
                    "selector": "row_idx"},
                "static_feature_names": static_names,
                "artifact_hashes": artifact_hashes,
                "ecg": spec["ecg"],
                "training_ready": False,
                "training_blockers": BLOCKERS,
            }
            io.write_json(man, d / "manifest.json")
            stats[f"{model}/{set_name}"] = man["n_samples"]
            print(f"[P9] {model}/{set_name}: {man['n_samples']:,} samples")

    _build_baseline_tabular(cfg, master, static_mat, static_names,
                            artifact_hashes, out9, stats)
    return stats


def _build_static_matrix(cfg: dict, master: pd.DataFrame):
    """全部 master 行的静态矩阵（impute + one-hot + scale，P7 工件应用）。"""
    sdf = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p3_static"
        / cfg["run_id"] / "static_raw.parquet")
    sdf = sdf.sort_values("row_idx").reset_index(drop=True)
    reg = json.loads((io.PROJECT_ROOT / cfg["paths"]["out_root"]
                      / "p7_fitted" / cfg["run_id"] / "registry.json")
                     .read_text(encoding="utf-8"))
    encs = next(a for a in reg["artifacts"]
                if a["name"] == "categorical_encoders")
    imp = next(a for a in reg["artifacts"] if a["name"] == "imputers")
    scaler = next(a for a in reg["artifacts"]
                  if a["name"] == "scaler_static")
    # registry 只存哈希；参数本体从 p7_fitted 目录读
    imp_params = _load_params(cfg, "imputers")
    enc_params = _load_params(cfg, "categorical_encoders")
    scaler_params = _load_params(cfg, "scaler_static")

    df = lib_static.apply_static_imputer(sdf, imp_params)
    parts, names = [], []
    for col in STATIC_NUMERIC:
        v = df[col].to_numpy(dtype=np.float32)
        p = scaler_params["cols"][col]
        parts.append(((v - p["mean"]) / p["sd"]).reshape(-1, 1))
        names.append(col)
        miss = f"{col}_missing"
        parts.append(df[miss].to_numpy(dtype=np.float32).reshape(-1, 1))
        names.append(miss)
    for col in STATIC_CATEGORICAL:
        oh, _ = lib_static.apply_categorical_encoder(df, col,
                                                     enc_params[col])
        parts.append(oh.to_numpy(dtype=np.float32))
        names.extend(list(oh.columns))
    for col in STATIC_FLAGS:
        parts.append(df[col].fillna(0).to_numpy(
            dtype=np.float32).reshape(-1, 1))
        names.append(col)
    mat = np.concatenate(parts, axis=1).astype(np.float32)
    io.write_json({"feature_names": names},
                  io.artifact_dir(cfg, "p9_packages")
                  / "static_feature_names.json")
    return mat, names


def _load_params(cfg: dict, name: str):
    p = io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p7_fitted" \
        / cfg["run_id"] / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _build_baseline_tabular(cfg, master, static_mat, static_names,
                            hashes, out9, stats):
    """LR/XGBoost 基线表格包（方案 §11.2）。"""
    summ = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p2_clinical"
        / cfg["run_id"] / "summary_features.parquet")
    summ = summ.rename(columns={"episode_id": "episode_key",
                                "k": "landmark_k"})
    wide = summ.pivot_table(
        index=["episode_key", "landmark_k"], columns="variable",
        values=["min", "median", "max", "sd", "n_obs", "last_value",
                "last_obs_age_h"])
    wide.columns = [f"{v}_{s}" for s, v in wide.columns]
    wide = wide.reset_index()
    samples = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p4_samples"
        / cfg["run_id"] / "idx_deployment_all.parquet")
    sdf = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p3_static"
        / cfg["run_id"] / "static_raw.parquet")
    keep_static = ["episode_key", "age", "gender", "admission_type",
                   "icu_type", "charlson_prior",
                   "charlson_prior_available", "delta_icu_sepsis_h"]
    sdf = sdf[[c for c in keep_static if c in sdf.columns
               or c == "episode_key"]].drop_duplicates("episode_key")
    for set_name in ["train", "validation", "test"]:
        pkg = samples[samples["set_name"] == set_name]
        df = pkg.merge(wide, on=["episode_key", "landmark_k"], how="left")
        df = df.merge(sdf, on="episode_key", how="left")
        d = out9 / "baseline_tabular" / set_name
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / "features.parquet", index=False)
        man = {"model": "baseline_tabular", "set_name": set_name,
               "n_samples": int(len(df)),
               "n_positive": int((df["y_24h"] == 1).sum()),
               "n_features": int(df.shape[1]),
               "artifact_hashes": hashes,
               "training_ready": False,
               "training_blockers": BLOCKERS}
        io.write_json(man, d / "manifest.json")
        stats[f"baseline_tabular/{set_name}"] = man["n_samples"]
        print(f"[P9] baseline_tabular/{set_name}: {man['n_samples']:,}")
