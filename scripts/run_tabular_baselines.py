# -*- coding: utf-8 -*-
"""LR / XGBoost tabular baselines on the frozen baseline_tabular package.

The training plan (v1.0/v1.1) prespecified logistic regression and
gradient-boosted trees on tabular summaries as baselines, but their results
were not previously computed.  This script trains both models on the frozen
train package, evaluates on the deployment test set and on the paired ECG
test subset, and reports iAUROC on the primary grid (k = 0-11), Brier, and
per-landmark AUROC.

Outputs:
    src/models/runs/baselines/REPORT.md
    src/models/runs/baselines/results.json
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models.train.metrics import landmark_metrics  # noqa: E402

PKG = ROOT / "preprocess" / "artifacts" / "p9_packages" / "pp_v1_20260730" \
    / "baseline_tabular"
OUT = ROOT / "src" / "models" / "runs" / "baselines"
OUT.mkdir(parents=True, exist_ok=True)

KEY_COLS = [
    "episode_key", "subject_key", "landmark_k", "t_landmark_ts",
    "hours_since_sepsis", "in_risk_set", "in_main_grid", "k0",
    "episode_mapping_version", "hadm_id", "anchor_year_group", "set_name",
    "row_idx", "episode_id", "k", "y_24h", "label_status",
    "outcome_ascertainable", "weight",
]

CAT_COLS = ["gender", "admission_type", "icu_type"]


def load(split):
    return pd.read_parquet(PKG / split / "features.parquet")


def feature_matrix(df, fit=None):
    """Return (X, y, k, w) with numeric imputation/standardization (fit) or
    using the training-fit transform."""
    feat = df.drop(columns=[c for c in KEY_COLS if c in df.columns])
    if fit is None:
        # training fit: build column lists
        num_cols = [c for c in feat.columns if c not in CAT_COLS]
        medians = feat[num_cols].median(numeric_only=True)
        means = feat[num_cols].mean(numeric_only=True)
        stds = feat[num_cols].std(numeric_only=True).replace(0, 1.0)
        cats = {}
        for c in CAT_COLS:
            cats[c] = sorted(feat[c].dropna().astype(str).unique().tolist())
        fit = {"num_cols": num_cols, "medians": medians, "means": means,
               "stds": stds, "cats": cats}
    num_cols, medians, means, stds, cats = (
        fit["num_cols"], fit["medians"], fit["means"], fit["stds"],
        fit["cats"])
    Xn = feat[num_cols].fillna(medians)
    Xn = (Xn - means) / stds
    parts = [Xn.to_numpy(dtype=np.float32)]
    for c in CAT_COLS:
        codes = feat[c].astype(str)
        for j, cat in enumerate(cats[c]):
            parts.append((codes == cat).to_numpy(dtype=np.float32)
                         .reshape(-1, 1))
        # unknown category bucket
        known = set(cats[c])
        parts.append((~codes.isin(known)).to_numpy(dtype=np.float32)
                     .reshape(-1, 1))
    X = np.hstack(parts)
    y = df["y_24h"].to_numpy(dtype=np.float32)
    k = df["landmark_k"].to_numpy(dtype=np.int64)
    w = df["weight"].to_numpy(dtype=np.float32)
    return X, y, k, w, fit


def paired_mask(test_df):
    """Rows of the deployment test set that belong to the paired ECG test
    subset (9,344 landmarks), keyed on subject_key + landmark_k."""
    z = np.load(ROOT / "src" / "models" / "runs" / "main_calibration"
                / "test_scores_paired.npz", allow_pickle=True)
    keys = set(zip(z["subject_key"].tolist(), z["landmark_k"].tolist()))
    mask = np.array([(s, int(k)) in keys for s, k in zip(
        test_df["subject_key"].to_numpy(), test_df["landmark_k"].to_numpy())])
    return mask


def run():
    tr = load("train")
    va = load("validation")
    te = load("test")
    Xtr, ytr, ktr, wtr, fit = feature_matrix(tr)
    Xva, yva, kva, wva, _ = feature_matrix(va, fit)
    Xte, yte, kte, wte, _ = feature_matrix(te, fit)
    pmask = paired_mask(te)
    print(f"[base] train {Xtr.shape} / validation {Xva.shape} / "
          f"test {Xte.shape}; paired test n={int(pmask.sum())}", flush=True)

    results = {}
    prev = float(ytr.mean())
    spw = (1 - prev) / max(prev, 1e-9)

    # ---- Logistic regression ----
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(max_iter=2000, C=0.1,
                            class_weight="balanced")
    lr.fit(Xtr, ytr, sample_weight=wtr)
    p_lr_te = lr.predict_proba(Xte)[:, 1]
    p_lr_paired = p_lr_te[pmask]
    p_lr_va = lr.predict_proba(Xva)[:, 1]
    results["lr"] = {
        "validation": landmark_metrics(yva, p_lr_va, kva),
        "deployment": landmark_metrics(yte, p_lr_te, kte),
        "paired": landmark_metrics(yte[pmask], p_lr_paired, kte[pmask]),
    }
    print(f"[base] LR deployment iAUROC="
          f"{results['lr']['deployment']['iauroc']:.4f} "
          f"paired="
          f"{results['lr']['paired']['iauroc']:.4f}", flush=True)

    # ---- XGBoost ----
    import xgboost as xgb
    xm = xgb.XGBClassifier(
        n_estimators=500, max_depth=6, eta=0.05, subsample=0.8,
        colsample_bytree=0.8, min_child_weight=1, scale_pos_weight=spw,
        eval_metric="auc", tree_method="hist", n_jobs=8,
        random_state=20260730, enable_categorical=False,
        early_stopping_rounds=20)
    xm.fit(Xtr, ytr, sample_weight=wtr,
           eval_set=[(Xva, yva)], verbose=False)
    p_xgb_te = xm.predict_proba(Xte)[:, 1]
    p_xgb_paired = p_xgb_te[pmask]
    p_xgb_va = xm.predict_proba(Xva)[:, 1]
    results["xgb"] = {
        "validation": landmark_metrics(yva, p_xgb_va, kva),
        "deployment": landmark_metrics(yte, p_xgb_te, kte),
        "paired": landmark_metrics(yte[pmask], p_xgb_paired, kte[pmask]),
        "best_iteration": int(xm.best_iteration),
    }
    print(f"[base] XGB deployment iAUROC="
          f"{results['xgb']['deployment']['iauroc']:.4f} "
          f"paired="
          f"{results['xgb']['paired']['iauroc']:.4f}", flush=True)

    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Tabular baselines (LR / XGBoost)",
        "",
        "Trained on the frozen `baseline_tabular` train package "
        "(218,509 landmarks; 166 columns), evaluated on the deployment test "
        "set (72,067 landmarks) and the paired ECG test subset (9,344 "
        "landmarks). iAUROC is computed on the primary grid k = 0-11.",
        "",
        "| Model | Deployment iAUROC | Paired iAUROC | Deployment Brier | "
        "Paired Brier |",
        "|---|---|---|---|---|",
    ]
    for name, label in (("lr", "Logistic regression"),
                        ("xgb", "XGBoost (gradient-boosted trees)")):
        d = results[name]["deployment"]
        p = results[name]["paired"]
        lines.append(
            f"| {label} | {d['iauroc']:.4f} | {p['iauroc']:.4f} | "
            f"{d['brier']:.4f} | {p['brier']:.4f} |")
    lines += [
        "",
        f"- Logistic regression: validation iAUROC = "
        f"{results['lr']['validation']['iauroc']:.4f}; test iAUROC = "
        f"{results['lr']['deployment']['iauroc']:.4f}.",
        f"- XGBoost: validation iAUROC = "
        f"{results['xgb']['validation']['iauroc']:.4f}; test iAUROC = "
        f"{results['xgb']['deployment']['iauroc']:.4f} "
        f"(best iteration {results['xgb']['best_iteration']}).",
        "",
        "For reference (frozen GRU-D models):",
        "- SC-common-all deployment iAUROC = 0.8316; "
        "SC-common-paired iAUROC = 0.8194.",
        "- SCE-deployment iAUROC = 0.8423; SCE-common-paired iAUROC = 0.8279.",
        "",
        "Per-landmark AUROC (deployment test):",
        "",
        "| k | LR | XGBoost |",
        "|---|---|---|",
    ]
    for kk in range(12):
        a_lr = results["lr"]["deployment"]["auroc_per_landmark"].get(kk)
        a_xgb = results["xgb"]["deployment"]["auroc_per_landmark"].get(kk)
        lines.append(f"| {kk} | {a_lr:.3f} | {a_xgb:.3f} |")
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[base] report -> {OUT / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    run()
