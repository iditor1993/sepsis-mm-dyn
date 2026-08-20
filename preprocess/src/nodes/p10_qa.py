"""P10: 预处理 QA 与防泄漏复测（方案 §12）。"""
import json

import numpy as np
import pandas as pd

from lib import io, leakage


def run(cfg: dict) -> dict:
    qa = io.qa_dir(cfg)
    results = []

    def check(name, ok, detail=""):
        results.append({"assertion": name, "pass": bool(ok),
                        "detail": str(detail)})
        print(f"[P10] [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    out_root = io.PROJECT_ROOT / cfg["paths"]["out_root"]
    rid = cfg["run_id"]
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")

    # 1. 拟合工件 fitted_on=train
    reg = json.loads((out_root / "p7_fitted" / rid / "registry.json")
                     .read_text(encoding="utf-8"))
    fitted = [a for a in reg["artifacts"]
              if a["name"] in ("scaler_clinical_seq", "scaler_static",
                               "categorical_encoders", "imputers")]
    check("工件 fitted_on=train",
          all(a["fitted_on"] == "train" for a in fitted),
          f"n={len(fitted)}")

    # 2. 划分纯度（患者不跨集合）
    try:
        leakage.assert_split_purity(
            master[["subject_key", "set_name"]].drop_duplicates())
        check("患者级划分纯度", True)
    except AssertionError as e:
        check("患者级划分纯度", False, str(e))

    # 3. 张量形状与 NaN 策略（抽样）
    xs = np.lib.format.open_memmap(
        out_root / "p7_fitted" / rid / "X_seq_scaled.npy", mode="r")
    mm = np.lib.format.open_memmap(
        out_root / "p2_clinical" / rid / "master" / "M_seq.npy", mode="r")
    rng = np.random.default_rng(io.seed_for(cfg, "p10"))
    rows = rng.choice(xs.shape[0], size=min(5000, xs.shape[0]),
                      replace=False)
    xb = np.asarray(xs[sorted(rows)])
    mb = np.asarray(mm[sorted(rows)])
    try:
        leakage.assert_mask_nan_policy(xb, mb)
        check("X_seq NaN/mask 策略（抽样）", True)
    except AssertionError as e:
        check("X_seq NaN/mask 策略（抽样）", False, str(e))
    check("张量形状 [N,21,24]",
          xs.shape[1] == 21 and xs.shape[2] == 24, str(xs.shape))

    # 4. paired 索引跨包哈希一致
    h = {}
    for model in ["sc_common_paired", "sce_common_paired"]:
        idx = pd.read_parquet(out_root / "p9_packages" / rid / model
                              / "train" / "index.parquet",
                              columns=["episode_key", "landmark_k"])
        h[model] = pd.util.hash_pandas_object(
            idx.sort_values(["episode_key", "landmark_k"])
               .reset_index(drop=True)).sum()
    check("paired 索引跨包一致", h["sc_common_paired"] == h["sce_common_paired"])

    # 5. ECG 防泄漏（modality index）
    mod = pd.read_parquet(out_root / "p6_modality" / rid
                          / "modality_index.parquet")
    lm = master[["row_idx", "t_landmark_ts"]]
    mod = mod.merge(lm, on="row_idx", how="left")
    try:
        leakage.assert_ecg_leakage_free(mod)
        check("ECG available<=landmark", True)
    except AssertionError as e:
        check("ECG available<=landmark", False, str(e))

    # 6. 包计数对账
    for model in ["sc_common_paired", "sc_common_all"]:
        tr = pd.read_parquet(out_root / "p9_packages" / rid / model
                             / "train" / "index.parquet")
        va = pd.read_parquet(out_root / "p9_packages" / rid / model
                             / "validation" / "index.parquet")
        te = pd.read_parquet(out_root / "p9_packages" / rid / model
                             / "test" / "index.parquet")
        check(f"{model} 计数>0",
              len(tr) > 0 and len(va) > 0 and len(te) > 0,
              f"train={len(tr):,} val={len(va):,} test={len(te):,}")

    # 7. training_ready=false
    man = json.loads((out_root / "p9_packages" / rid / "sc_common_paired"
                      / "train" / "manifest.json").read_text(
        encoding="utf-8"))
    check("training_ready=true", man["training_ready"] is True)

    rep = pd.DataFrame(results)
    io.write_json(results, qa / "p10_leakage_report.json")
    md = "# p10_leakage_report\n\n" + _md_table(rep) + "\n"
    (qa / "p10_leakage_report.md").write_text(md, encoding="utf-8")
    n_fail = int((rep["pass"] == False).sum())  # noqa: E712
    print(f"[P10] {n_fail} failed")
    if n_fail:
        raise SystemExit(f"[P10] {n_fail} assertions failed")
    return {"fail": n_fail}


def _md_table(df) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)
