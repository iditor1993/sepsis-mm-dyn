"""P1: 输入校验与装载（方案 §3）。

校验 schema/主键/版本一致性 + 行数对账基线 + 构建 master 样本索引。
"""
import pandas as pd

from lib import io

ROWCOUNT_BASELINE = {
    "cohorts/cohort_mimic_v2.parquet": 31910,
    "landmarks/landmarks_v2.parquet": 443225,
    "labels/labels_24h_v2.parquet": 443225,
    "cohorts/cohort_eicu_v2.parquet": 62251,
    "ecg_index/ecg_landmark_index_v2.parquet": 211360,
    "splits/split_assignments_v2.parquet": 364627,
}


def build_master_index(root) -> pd.DataFrame:
    """master 样本索引 = landmarks ∩ cohort ∩ split（键重命名，§3.2）。"""
    lm = pd.read_parquet(root / "landmarks/landmarks_v2.parquet")
    cohort = pd.read_parquet(
        root / "cohorts/cohort_mimic_v2.parquet",
        columns=["episode_id", "subject_id", "hadm_id",
                 "anchor_year_group"])
    splits = pd.read_parquet(root / "splits/split_assignments_v2.parquet")
    df = lm.merge(cohort, on=["episode_id", "subject_id"], how="left")
    df = df.merge(splits[["subject_id", "set_name"]],
                  on="subject_id", how="left")
    df = df.rename(columns={"episode_id": "episode_key",
                            "subject_id": "subject_key", "k": "landmark_k"})
    df = df.sort_values(["episode_key", "landmark_k"]).reset_index(drop=True)
    df["row_idx"] = df.index
    # 原始列名保留映射依据（P2 长表 join 用原始名）
    df["episode_id"] = df["episode_key"]
    df["k"] = df["landmark_k"]
    return df


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    report = {"checks": [], "fail": 0}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "pass": bool(ok),
                                 "detail": str(detail)})
        if not ok:
            report["fail"] += 1
        print(f"[P1] [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 行数对账
    for rel, expect in ROWCOUNT_BASELINE.items():
        p = root / rel
        if not p.exists():
            check(f"exists {rel}", False, "missing")
            continue
        n = len(pd.read_parquet(p))
        check(f"rowcount {rel}", n == expect, f"{n:,} vs {expect:,}")

    # 主键与版本
    ep_map = pd.read_parquet(
        root / "episodes/mimic_icu_episode_map_final.parquet")
    mv = cfg["episode_mapping_version"]
    sub = ep_map[ep_map["episode_mapping_version"] == mv]
    check("final map stay->episode 一对一",
          sub.groupby("stay_id")["episode_id"].nunique().max() == 1)
    lm = pd.read_parquet(root / "landmarks/landmarks_v2.parquet")
    check("landmarks (episode,k) 唯一",
          not lm.duplicated(["episode_id", "k"]).any())
    # D0 / 冻结清单状态
    import json
    d0 = json.loads((root / "_meta/d0_decision.json").read_text(
        encoding="utf-8"))
    check("d0_decision 存在", d0.get("status") == "pending",
          f"status={d0.get('status')}（pending 期 training_ready=false）")

    # master 索引
    master = build_master_index(root)
    out = io.artifact_dir(cfg, "p1_validate")
    master.to_parquet(out / "master_index.parquet", index=False)
    check("master_index 构建", len(master) == ROWCOUNT_BASELINE[
        "landmarks/landmarks_v2.parquet"], f"rows={len(master):,}")
    unknown_set = master["set_name"].isna().sum()
    check("set_name 覆盖", unknown_set == 0, f"missing={unknown_set}")

    io.write_json(report, out / "p1_validation_report.json")
    if report["fail"]:
        raise SystemExit(f"[P1] {report['fail']} checks failed")
    return report
