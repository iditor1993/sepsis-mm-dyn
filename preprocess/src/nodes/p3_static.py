"""P3: 静态与 landmark 上下文编码（方案 §5）。

输出 static_raw.parquet（编码在 P7 拟合后于 P9 应用）。
"""
import pandas as pd

from lib import io

STATIC_NUMERIC = ["age", "delta_icu_sepsis_h", "weight_kg", "height_cm",
                  "charlson_prior"]
STATIC_CATEGORICAL = ["gender", "admission_type", "admission_location",
                      "icu_type", "admission_route"]
STATIC_FLAGS = ["charlson_prior_available", "weight_missing",
                "extreme_weight_flag", "invasive_vent_current",
                "vaso_current"]
DESCRIBE_ONLY = ["flag_transfer_from_outside",
                 "flag_ecmo_before_first_landmark",
                 "flag_solid_organ_transplant_90d",
                 "flag_dnr_cco_before_first_landmark"]


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    bs = pd.read_parquet(root / "features/baseline_static_v2.parquet")
    ctx = pd.read_parquet(root / "features/landmark_context_v2.parquet")
    ctx = ctx.rename(columns={"episode_id": "episode_key",
                              "k": "landmark_k"})
    bs = bs.rename(columns={"episode_id": "episode_key"})

    df = master[["episode_key", "subject_key", "landmark_k", "row_idx"]] \
        .merge(ctx, on=["episode_key", "landmark_k"], how="left")
    df = df.merge(
        bs[["episode_key", "age", "gender", "admission_type",
            "admission_location", "icu_type", "admission_route",
            "charlson_prior", "charlson_prior_available",
            "prior_hospital_count"] + DESCRIBE_ONLY],
        on="episode_key", how="left")

    keep = (["episode_key", "subject_key", "landmark_k", "row_idx"]
            + STATIC_NUMERIC + STATIC_CATEGORICAL + STATIC_FLAGS
            + DESCRIBE_ONLY + ["nee_current"])
    out = df[keep]
    out_path = io.artifact_dir(cfg, "p3_static") / "static_raw.parquet"
    out.to_parquet(out_path, index=False)
    print(f"[P3] static_raw: {out.shape}")
    n_missing_weight = int(out["weight_kg"].isna().sum())
    print(f"[P3] weight missing rows: {n_missing_weight:,}")
    return {"rows": len(out), "cols": list(out.columns)}
