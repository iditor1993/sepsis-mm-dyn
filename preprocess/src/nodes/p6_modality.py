"""P6: ECG 配对与模态装配（方案 §8）。"""
import numpy as np
import pandas as pd

from lib import io


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p6_modality")
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    ecg = pd.read_parquet(root / "ecg_index/ecg_landmark_index_v2.parquet")
    splits = pd.read_parquet(root / "splits/split_assignments_v2.parquet")

    df = ecg.rename(columns={"episode_id": "episode_key", "k": "landmark_k"})
    df = df.merge(
        master[["episode_key", "subject_key", "landmark_k", "row_idx"]],
        on=["episode_key", "landmark_k"], how="inner")
    df = df.merge(splits[["subject_id", "set_name"]],
                  left_on="subject_key", right_on="subject_id", how="left") \
        .drop(columns=["subject_id"])

    # E-4：数据驱动 QC 结果并入（pass_frozen_qc = 结构合格 AND data_qc_pass）
    qc_path = (io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p5_ecg_cache"
               / cfg["run_id"] / "ecg_data_qc.parquet")
    if qc_path.exists():
        qc = pd.read_parquet(qc_path)
        df = df.merge(qc, on="study_id", how="left")
        df["data_qc_pass"] = df["data_qc_pass"].fillna(False)
        df["ecg_pass_frozen_qc"] = (df["ecg_structurally_valid"]
                                    & df["data_qc_pass"])
    else:
        df["data_qc_pass"] = None

    # modality_dropout_group（仅训练集；种子派生，方案 §8.2）
    seed = io.seed_for(cfg, "p6_modality")
    rng = np.random.default_rng(seed)
    df["modality_dropout_group"] = None
    train_mask = df["set_name"] == "train"
    p = float(cfg["sce_deployment"]["modality_dropout_p"])
    df.loc[train_mask, "modality_dropout_group"] = np.where(
        rng.random(int(train_mask.sum())) < p, "drop", "keep")

    # E-5：冻结 24h 主配对队列——选片要求通过两层 QC
    # （structural AND data-driven），在 ecg_selected_for_model（结构层）
    # 基础上叠加 pass_frozen_qc 重定最终选片
    df["ecg_selected_for_model_frozen"] = (
        df["ecg_selected_for_model"] & df["ecg_pass_frozen_qc"])

    keep = ["episode_key", "subject_key", "landmark_k", "row_idx",
            "study_id", "ecg_acquisition_time", "recording_duration_s",
            "ecg_available_time_assumed", "ecg_encounter_status",
            "pre_admission_ecg", "ecg_found_raw", "ecg_same_encounter",
            "ecg_structurally_valid", "ecg_pass_frozen_qc",
            "data_qc_pass",
            "ecg_selected_for_model", "ecg_selected_for_model_frozen",
            "ecg_available",
            "within_48h", "within_72h", "set_name",
            "modality_dropout_group", "ecg_path"]
    df[keep].to_parquet(out / "modality_index.parquet", index=False)
    stats = {"rows": len(df),
             "selected_structural": int(df["ecg_selected_for_model"].sum()),
             "selected_frozen": int(df["ecg_selected_for_model_frozen"].sum()),
             "train_dropout": int((df["modality_dropout_group"] == "drop").sum())}
    io.write_json(stats, out / "modality_stats.json")
    print(f"[P6] modality_index: {stats}")
    return stats
