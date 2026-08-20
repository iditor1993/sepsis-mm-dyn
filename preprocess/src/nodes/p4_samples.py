"""P4: 标签与样本集装配（方案 §6）。"""
import pandas as pd

from lib import io, labels as lib_labels


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p4_samples")
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    labs = pd.read_parquet(root / "labels/labels_24h_v2.parquet")

    # E-5：优先使用冻结选片（结构+数据驱动两层 QC）；否则回退提取层结构层选片
    mod_path = (io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p6_modality"
                / cfg["run_id"] / "modality_index.parquet")
    if mod_path.exists():
        ecg = pd.read_parquet(
            mod_path,
            columns=["episode_key", "landmark_k",
                     "ecg_selected_for_model_frozen", "within_48h",
                     "within_72h"])
        ecg = ecg.rename(
            columns={"episode_key": "episode_id", "landmark_k": "k",
                     "ecg_selected_for_model_frozen":
                     "ecg_selected_for_model"})
        print("[P4] 使用冻结选片（ecg_selected_for_model_frozen）")
    else:
        ecg = pd.read_parquet(root / "ecg_index/ecg_landmark_index_v2.parquet")
        print("[P4] 使用提取层结构层选片（modality_index 未生成）")

    idx_sets = lib_labels.build_sample_indices(master, labs, ecg)
    stats = {}
    for name, df in idx_sets.items():
        w = lib_labels.add_patient_weights(df)
        w.to_parquet(out / f"{name}.parquet", index=False)
        stats[name] = {
            "rows": len(w),
            "patients": int(w["subject_key"].nunique()),
            "episodes": int(w["episode_key"].nunique()),
            "positives": int((w["y_24h"] == 1).sum()),
            "weight_sum_per_patient_ok": bool(
                (w.groupby("subject_key")["weight"].sum()
                 .sub(1.0).abs() < 1e-6).all()),
        }
        print(f"[P4] {name}: {stats[name]}")

    # 竞争风险标签（DeepHit 用）
    comp = pd.read_parquet(root / "labels/labels_competing_7d_v2.parquet")
    comp_bins = lib_labels.competing_bins(comp)
    comp_bins.to_parquet(out / "competing_risk_labels.parquet",
                         index=False)
    stats["competing"] = {"rows": len(comp_bins)}
    print(f"[P4] competing_risk_labels: {len(comp_bins):,} rows")

    io.write_json(stats, out / "sample_stats.json")
    return stats
