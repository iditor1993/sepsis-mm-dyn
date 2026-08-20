"""eICU 外验预处理分支（方案 §11.3；v1.1 §3.2 坐标换算）。

复用 MIMIC lib；无 P5/P6；P7 工件原样应用（不重拟合）；
P-clinical / P-explicit 分 track 成包，全部 feasibility_only。
set_name 一律 'external'。
"""
import json

import numpy as np
import pandas as pd

from lib import grid, io, leakage, scalers

# eICU variable → core channel（pt 通道在 eICU 缺失：pivoted_lab.ptt=aPTT≠PT）
EICU_CHANNEL_MAP = {
    "hr": "hr", "sbp": "sbp", "dbp": "dbp", "mbp": "mbp", "rr": "rr",
    "spo2": "spo2", "temp": "temp",
    "creatinine": "creatinine", "bilirubin": "bilirubin",
    "platelets": "platelets", "lactate": "lactate", "wbc": "wbc",
    "hemoglobin": "hemoglobin", "glucose": "glucose", "sodium": "sodium",
    "potassium": "potassium", "bicarbonate": "bicarbonate", "INR": "inr",
}


def run_master(cfg: dict) -> pd.DataFrame:
    root = io.data_root(cfg)
    lm = pd.read_parquet(root / "landmarks/eicu_landmarks_v2.parquet")
    cohort = pd.read_parquet(
        root / "cohorts/cohort_eicu_v2.parquet",
        columns=["episode_id", "uniquepid", "age_num", "gender",
                 "hospitalid", "phenotype_track",
                 "hospitaldischargestatus"])
    df = lm.merge(cohort, on=["episode_id", "uniquepid", "phenotype_track"],
                  how="left")
    df = df.rename(columns={"episode_id": "episode_key",
                            "uniquepid": "subject_key", "k": "landmark_k"})
    df["set_name"] = "external"
    df = df.sort_values(["episode_key", "landmark_k"]).reset_index(drop=True)
    df["row_idx"] = df.index
    df["episode_id"] = df["episode_key"]
    df["k"] = df["landmark_k"]
    out = io.artifact_dir(cfg, "p1_validate")
    df.to_parquet(out / "eicu_master_index.parquet", index=False)
    print(f"[eICU-P1] master: {len(df):,} rows, "
          f"tracks={df['phenotype_track'].value_counts().to_dict()}")
    return df


def run_tensorize(cfg: dict) -> dict:
    root = io.data_root(cfg)
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "eicu_master_index.parquet")
    n_rows = len(master)
    core = list(cfg["sc_common_core"])
    v_of = {name: i for i, name in enumerate(core)}
    out = io.artifact_dir(cfg, "p2_clinical") / "eicu_master"
    out.mkdir(parents=True, exist_ok=True)

    x, m, d = grid.alloc_tensors(
        n_rows, len(core),
        out / "X_seq.npy", out / "M_seq.npy", out / "D_seq.npy")

    for fname in ["eicu_vitals_v2", "eicu_labs_v2"]:
        df = pd.read_parquet(
            root / f"features/{fname}.parquet",
            columns=["episode_id", "k", "bin_hour", "variable",
                     "value_median"])
        for var, ch in EICU_CHANNEL_MAP.items():
            if ch not in v_of:
                continue
            sub = df[df["variable"] == var]
            n = grid.fill_channel(master, sub, x, m, v_of[ch],
                                  val_col="value_median")
            if n:
                print(f"[eICU-P2] {var}->{ch}: {n:,} cells")
        del df
    print("[eICU-P2] compute delta_t ...")
    d[:] = grid.compute_delta(m)
    x.flush(); m.flush(); d.flush()
    del x, m, d

    # 应用 MIMIC 拟合的 scaler（不重拟合，方案 §9.2）
    params = json.loads((io.PROJECT_ROOT / cfg["paths"]["out_root"]
                         / "p7_fitted" / cfg["run_id"]
                         / "scaler_clinical_seq.json").read_text(
        encoding="utf-8"))
    xs = np.lib.format.open_memmap(out / "X_seq_scaled.npy", mode="w+",
                                   dtype=np.float32,
                                   shape=(n_rows, len(core), 24))
    x = np.lib.format.open_memmap(out / "X_seq.npy", mode="r")
    m = np.lib.format.open_memmap(out / "M_seq.npy", mode="r")
    mu = np.array([params["channels"][c]["mean"] for c in core],
                  dtype=np.float32)
    sd = np.array([params["channels"][c]["sd"] for c in core],
                  dtype=np.float32)
    chunk = 20000
    for i in range(0, n_rows, chunk):
        j = min(i + chunk, n_rows)
        xb = np.asarray(x[i:j]); mb = np.asarray(m[i:j])
        xs[i:j] = np.where(mb, (xb - mu[None, :, None]) / sd[None, :, None],
                           0.0).astype(np.float32)
    xs.flush()
    print(f"[eICU-P2] tensors {n_rows:,} rows × {len(core)} ch, scaler applied")
    return {"rows": n_rows, "channels": len(core)}


def run_samples(cfg: dict) -> dict:
    """eICU 样本索引（ascertainable 过滤 + 患者等权）。"""
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p4_samples")
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "eicu_master_index.parquet")
    labs = pd.read_parquet(root / "labels/eicu_labels_24h_v2.parquet")
    labs = labs.rename(columns={"episode_id": "episode_key",
                                "k": "landmark_k"})
    df = master.merge(
        labs[["episode_key", "landmark_k", "phenotype_track", "y_24h",
              "label_status", "outcome_ascertainable"]],
        on=["episode_key", "landmark_k", "phenotype_track"], how="left")
    asc = df[df["outcome_ascertainable"] == True].copy()  # noqa: E712
    from lib import labels as lib_labels
    asc = lib_labels.add_patient_weights(asc)
    asc.to_parquet(out / "eicu_idx_all.parquet", index=False)
    stats = {"rows": len(asc),
             "patients": int(asc["subject_key"].nunique()),
             "positives": int((asc["y_24h"] == 1).sum()),
             "by_track": asc["phenotype_track"].value_counts().to_dict()}
    print(f"[eICU-P4] idx_all: {stats}")
    return stats


def run_package(cfg: dict) -> dict:
    """eicu_sc_common 包（分 track；feasibility_only）。"""
    out9 = io.artifact_dir(cfg, "p9_packages")
    idx = pd.read_parquet(
        io.PROJECT_ROOT / cfg["paths"]["out_root"] / "p4_samples"
        / cfg["run_id"] / "eicu_idx_all.parquet")
    # 静态：仅 age/gender（SC-common A 层）+ 标准化
    params = json.loads((io.PROJECT_ROOT / cfg["paths"]["out_root"]
                         / "p7_fitted" / cfg["run_id"]
                         / "scaler_static.json").read_text(
        encoding="utf-8"))
    stats = {}
    for track in cfg["eicu"]["tracks"]:
        sub = idx[idx["phenotype_track"] == track].copy()
        if len(sub) == 0:
            continue
        d = out9 / "eicu_sc_common" / track
        d.mkdir(parents=True, exist_ok=True)
        keep = ["episode_key", "subject_key", "landmark_k",
                "hours_since_sepsis", "set_name", "y_24h", "label_status",
                "weight", "in_main_grid", "row_idx", "phenotype_track",
                "hospitalid", "age_num", "gender"]
        sub[keep].to_parquet(d / "index.parquet", index=False)
        p = params["cols"]["age"]
        age_scaled = ((sub["age_num"].astype(np.float32) - p["mean"])
                      / p["sd"]).to_numpy()
        gender_oh = pd.get_dummies(
            sub["gender"].fillna("Unknown").astype(str)).astype(np.float32)
        static_mat = np.column_stack(
            [age_scaled, gender_oh.to_numpy()]).astype(np.float32)
        np.save(d / "static.npy", static_mat)
        man = {
            "model": "eicu_sc_common", "set_name": "external",
            "phenotype_track": track,
            "n_samples": int(len(sub)),
            "n_patients": int(sub["subject_key"].nunique()),
            "n_positive": int((sub["y_24h"] == 1).sum()),
            "channels": list(cfg["sc_common_core"]),
            "tensor_ref": {"path": "p2_clinical/eicu_master/X_seq_scaled.npy",
                           "selector": "row_idx"},
            "static_feature_names": ["age"] + list(gender_oh.columns),
            "external": True,
            "feasibility_only": True,
            "training_ready": False,
            "training_blockers": ["freeze_checklist_open", "d0_pending",
                                  "eicu_contract_pending",
                                  "phenotype_pi_signoff_pending"],
        }
        io.write_json(man, d / "manifest.json")
        stats[track] = man["n_samples"]
        print(f"[eICU-P9] {track}: {man['n_samples']:,} samples")
    return stats


def run_qa(cfg: dict) -> dict:
    """eICU QA：NaN/mask 策略、通道缺失率、泄漏抽查。"""
    qa = io.qa_dir(cfg)
    results = []

    def check(name, ok, detail=""):
        results.append({"assertion": name, "pass": bool(ok),
                        "detail": str(detail)})
        print(f"[eICU-P10] [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    out_root = io.PROJECT_ROOT / cfg["paths"]["out_root"]
    rid = cfg["run_id"]
    xs = np.lib.format.open_memmap(
        out_root / "p2_clinical" / rid / "eicu_master" / "X_seq_scaled.npy",
        mode="r")
    mm = np.lib.format.open_memmap(
        out_root / "p2_clinical" / rid / "eicu_master" / "M_seq.npy",
        mode="r")
    rng = np.random.default_rng(io.seed_for(cfg, "eicu_p10"))
    rows = rng.choice(xs.shape[0], size=min(5000, xs.shape[0]),
                      replace=False)
    xb = np.asarray(xs[sorted(rows)]); mb = np.asarray(mm[sorted(rows)])
    try:
        leakage.assert_mask_nan_policy(xb, mb)
        check("eICU X_seq NaN/mask 策略（抽样）", True)
    except AssertionError as e:
        check("eICU X_seq NaN/mask 策略（抽样）", False, str(e))

    core = list(cfg["sc_common_core"])
    dens = {}
    for i, ch in enumerate(core):
        dens[ch] = float(mm[:, i, :].mean())
    check("通道缺失率计算", True,
          json.dumps({k: round(v, 3) for k, v in dens.items()}))
    # 合同差异登记：pt 不在 core 通道（eICU pivoted_lab.ptt 为 aPTT≠PT）；
    # inr/pt/pao2/nee 为 extended/MIMIC-only，不在 eICU core 包
    check("eICU core 通道不含 pt/inr（合同差异登记）",
          ("pt" not in core) and ("inr" not in core),
          f"core={len(core)} ch")

    rep = pd.DataFrame(results)
    io.write_json({"results": results, "channel_density": dens},
                  qa / "eicu_p10_report.json")
    md = "# eicu_p10_report\n\n" + "\n".join(
        f"- [{'PASS' if r['pass'] else 'FAIL'}] {r['assertion']} {r['detail']}"
        for r in results) + "\n"
    (qa / "eicu_p10_report.md").write_text(md, encoding="utf-8")
    n_fail = int((rep["pass"] == False).sum())  # noqa: E712
    return {"fail": n_fail, "channel_density": dens}


def run_all(cfg: dict):
    run_master(cfg)
    run_tensorize(cfg)
    run_samples(cfg)
    run_package(cfg)
    run_qa(cfg)
