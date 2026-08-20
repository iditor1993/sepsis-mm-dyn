# -*- coding: utf-8 -*-
"""External evaluation of the frozen tabular models (LR / XGBoost) in eICU.

Builds eICU tabular features with the same 166-column schema and summary
statistics as the MIMIC baseline_tabular package (min/median/max/sd/n_obs/
last_value/last_obs_age_h per variable over the preceding 24 h), applies the
MIMIC-fitted imputation/standardization/categorical encoders without
refitting, and scores the eICU P-clinical and P-explicit cohorts with the
frozen models.  iAUROC is computed on the primary grid (k = 0-11), with
patient-level bootstrap 95% CIs, for comparison with the frozen GRU-D
clinical model (P-clinical iAUROC 0.704; P-explicit 0.707).
"""

import json
import pickle
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models.train.metrics import landmark_metrics  # noqa: E402

ART = ROOT / "preprocess" / "artifacts"
RID = "pp_v1_20260730"
BT = ART / "p9_packages" / RID / "baseline_tabular"
EICU_PKG = ART / "p9_packages" / RID / "eicu_sc_common"
FEAT_ROOT = ROOT / "src" / "data" / "_output" / "features"
OUT = ROOT / "src" / "models" / "runs" / "eicu_tabular_external"
OUT.mkdir(parents=True, exist_ok=True)

KEY_COLS = [
    "episode_key", "subject_key", "landmark_k", "t_landmark_ts",
    "hours_since_sepsis", "in_risk_set", "in_main_grid", "k0",
    "episode_mapping_version", "hadm_id", "anchor_year_group", "set_name",
    "row_idx", "episode_id", "k", "y_24h", "label_status",
    "outcome_ascertainable", "weight",
]
CAT_COLS = ["gender", "admission_type", "icu_type"]
N_BOOT = 1000
SEED = 20260730

VITALS = ["hr", "sbp", "dbp", "mbp", "rr", "spo2", "temp"]
LABS = ["creatinine", "bilirubin", "platelets", "lactate", "wbc",
        "hemoglobin", "glucose", "sodium", "potassium", "bicarbonate",
        "INR", "pao2"]


def fit_and_save_mimic_models():
    """Refit LR (C=1.0) and XGBoost on the MIMIC train package and save
    the models plus the preprocessing fit for reuse on eICU."""
    tr = pd.read_parquet(BT / "train" / "features.parquet")
    te = pd.read_parquet(BT / "test" / "features.parquet")
    feat = tr.drop(columns=[c for c in KEY_COLS if c in tr.columns])
    num_cols = [c for c in feat.columns if c not in CAT_COLS]
    medians = feat[num_cols].median(numeric_only=True)
    means = feat[num_cols].mean(numeric_only=True)
    stds = feat[num_cols].std(numeric_only=True).replace(0, 1.0)
    cats = {c: sorted(feat[c].dropna().astype(str).unique().tolist())
            for c in CAT_COLS}
    fit = {"num_cols": num_cols, "medians": medians, "means": means,
           "stds": stds, "cats": cats, "feature_cols": list(feat.columns)}

    def matrix(df):
        f = df.drop(columns=[c for c in KEY_COLS if c in df.columns])
        Xn = f[num_cols].fillna(medians)
        Xn = (Xn - means) / stds
        parts = [Xn.to_numpy(dtype=np.float32)]
        for c in CAT_COLS:
            codes = f[c].astype(str)
            for cat in cats[c]:
                parts.append((codes == cat).to_numpy(dtype=np.float32)
                             .reshape(-1, 1))
            known = set(cats[c])
            parts.append((~codes.isin(known)).to_numpy(dtype=np.float32)
                         .reshape(-1, 1))
        return np.hstack(parts)

    Xtr = matrix(tr)
    ytr = tr["y_24h"].to_numpy(dtype=np.float32)
    wtr = tr["weight"].to_numpy(dtype=np.float32)
    Xte = matrix(te)
    yte = te["y_24h"].to_numpy(dtype=np.float32)
    kte = te["landmark_k"].to_numpy(dtype=np.int64)

    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")
    lr.fit(Xtr, ytr, sample_weight=wtr)
    p_lr = lr.predict_proba(Xte)[:, 1]

    import xgboost as xgb
    va = pd.read_parquet(BT / "validation" / "features.parquet")
    Xva = matrix(va)
    yva = va["y_24h"].to_numpy(dtype=np.float32)
    prev = float(ytr.mean())
    spw = (1 - prev) / max(prev, 1e-9)
    xm = xgb.XGBClassifier(
        n_estimators=500, max_depth=6, eta=0.05, subsample=0.8,
        colsample_bytree=0.8, min_child_weight=1, scale_pos_weight=spw,
        eval_metric="auc", tree_method="hist", n_jobs=8,
        random_state=SEED, enable_categorical=False,
        early_stopping_rounds=20)
    xm.fit(Xtr, ytr, sample_weight=wtr, eval_set=[(Xva, yva)],
           verbose=False)
    p_xgb = xm.predict_proba(Xte)[:, 1]

    print(f"[eicu-tab] MIMIC reproduction: LR="
          f"{landmark_metrics(yte, p_lr, kte)['iauroc']:.4f} XGB="
          f"{landmark_metrics(yte, p_xgb, kte)['iauroc']:.4f}", flush=True)
    assert abs(landmark_metrics(yte, p_lr, kte)["iauroc"] - 0.8657) < 1e-3
    assert abs(landmark_metrics(yte, p_xgb, kte)["iauroc"] - 0.8760) < 1e-3

    import joblib
    joblib.dump(lr, OUT / "lr_mimic.joblib")
    xm.get_booster().save_model(str(OUT / "xgb_mimic.json"))
    with open(OUT / "preprocess_fit.pkl", "wb") as f:
        pickle.dump(fit, f)
    print("[eicu-tab] models and preprocessing fit saved", flush=True)


def build_eicu_summary():
    """Same summary statistics as the MIMIC baseline package, computed with
    duckdb on the eICU hourly-bin tables."""
    vit = str(FEAT_ROOT / "eicu_vitals_v2.parquet").replace("\\", "/")
    lab = str(FEAT_ROOT / "eicu_labs_v2.parquet").replace("\\", "/")
    vit_list = ",".join(f"'{v}'" for v in VITALS)
    lab_list = ",".join(f"'{v}'" for v in LABS)
    con = duckdb.connect()
    q = f"""
      WITH src AS (
        SELECT episode_id AS episode_key, phenotype_track, k, bin_hour,
               variable, value_median, max_event_episode_min,
               t_landmark_offset_min
        FROM read_parquet('{vit}')
        WHERE variable IN ({vit_list})
        UNION ALL
        SELECT episode_id AS episode_key, phenotype_track, k, bin_hour,
               CASE WHEN variable = 'INR' THEN 'inr' ELSE variable END,
               value_median, max_event_episode_min,
               t_landmark_offset_min
        FROM read_parquet('{lab}')
        WHERE variable IN ({lab_list})
      )
      SELECT episode_key, phenotype_track, k, variable,
        MIN(value_median) AS min, MEDIAN(value_median) AS median,
        MAX(value_median) AS max, STDDEV(value_median) AS sd,
        COUNT(*) AS n_obs,
        ANY_VALUE(value_median ORDER BY max_event_episode_min DESC)
          AS last_value,
        (MAX(t_landmark_offset_min) - MAX(max_event_episode_min)) / 60.0
          AS last_obs_age_h
      FROM src
      GROUP BY episode_key, phenotype_track, k, variable
    """
    summ = con.execute(q).fetchdf()
    con.close()
    summ["sd"] = summ["sd"].astype(float)
    print(f"[eicu-tab] eICU summary rows: {len(summ):,}", flush=True)
    return summ


def build_eicu_df(track, summ, fit):
    idx = pd.read_parquet(EICU_PKG / track / "index.parquet")
    sub = summ[summ["phenotype_track"] == track]
    wide = sub.pivot_table(
        index=["episode_key", "k"], columns="variable",
        values=["min", "median", "max", "sd", "n_obs", "last_value",
                "last_obs_age_h"])
    wide.columns = [f"{v}_{s}" for s, v in wide.columns]
    wide = wide.reset_index().rename(columns={"k": "landmark_k"})

    stat = idx[["episode_key", "age_num", "gender"]].drop_duplicates(
        "episode_key")
    stat = stat.rename(columns={"age_num": "age"})
    stat["gender"] = stat["gender"].map(
        {"Male": "M", "Female": "F"}).fillna("Unknown")

    df = idx[["episode_key", "subject_key", "landmark_k", "y_24h",
              "weight"]].merge(wide, on=["episode_key", "landmark_k"],
                               how="left")
    df = df.merge(stat, on="episode_key", how="left")
    df["admission_type"] = "Unknown"
    df["icu_type"] = "Unknown"
    df["charlson_prior"] = np.nan
    df["charlson_prior_available"] = False
    df["delta_icu_sepsis_h"] = np.nan
    df = df.reindex(
        columns=["episode_key", "subject_key", "landmark_k", "y_24h",
                 "weight"] + fit["feature_cols"])
    return df


def _auroc(y, s):
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    mid = csum - counts + (counts + 1) / 2.0
    ranks = mid[inv]
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _iauroc(y, s, k):
    aucs = [_auroc(y[k == kk], s[k == kk]) for kk in range(12)]
    if any(np.isnan(a) for a in aucs):
        return np.nan, aucs
    return float(np.mean(aucs)), aucs


def transform(df, fit):
    f = df.drop(columns=[c for c in KEY_COLS if c in df.columns])
    num_cols, medians, means, stds, cats = (
        fit["num_cols"], fit["medians"], fit["means"], fit["stds"],
        fit["cats"])
    Xn = f[num_cols].fillna(medians)
    Xn = (Xn - means) / stds
    parts = [Xn.to_numpy(dtype=np.float32)]
    for c in CAT_COLS:
        codes = f[c].astype(str)
        for cat in cats[c]:
            parts.append((codes == cat).to_numpy(dtype=np.float32)
                         .reshape(-1, 1))
        known = set(cats[c])
        parts.append((~codes.isin(known)).to_numpy(dtype=np.float32)
                     .reshape(-1, 1))
    return np.hstack(parts)


def main():
    fit_and_save_mimic_models()
    with open(OUT / "preprocess_fit.pkl", "rb") as f:
        fit = pickle.load(f)
    import joblib
    lr = joblib.load(OUT / "lr_mimic.joblib")
    import xgboost as xgb
    booster = xgb.Booster()
    booster.load_model(str(OUT / "xgb_mimic.json"))

    summ = build_eicu_summary()
    rng = np.random.default_rng(SEED)
    results = {}
    report = [
        "# External evaluation of frozen tabular models in eICU",
        "",
        "MIMIC-trained logistic regression (C = 1.0) and XGBoost are "
        "applied to eICU without refitting, using the same 166-column "
        "feature schema and the MIMIC-fitted imputation/standardization/"
        "categorical encoders. iAUROC on the primary grid (k = 0-11); "
        "95% CIs from 1,000 patient-level bootstrap resamples. Reference: "
        "frozen GRU-D clinical model iAUROC 0.704 (P-clinical) and 0.707 "
        "(P-explicit).",
        "",
    ]
    for track in ("P-clinical", "P-explicit"):
        df = build_eicu_df(track, summ, fit)
        X = transform(df, fit)
        y = df["y_24h"].to_numpy(dtype=np.float32)
        k = df["landmark_k"].to_numpy(dtype=np.int64)
        subj = df["subject_key"].to_numpy()
        p_lr = lr.predict_proba(X)[:, 1]
        p_xgb = booster.predict(xgb.DMatrix(X))
        m_lr = landmark_metrics(y, p_lr, k)
        m_xgb = landmark_metrics(y, p_xgb, k)

        usubj, inv = np.unique(subj, return_inverse=True)
        n_u = len(usubj)
        inb = np.zeros(n_u, dtype=bool)
        d_lr = np.full(N_BOOT, np.nan)
        d_xgb = np.full(N_BOOT, np.nan)
        for b in range(N_BOOT):
            inb[:] = False
            inb[rng.integers(0, n_u, n_u)] = True
            m = inb[inv]
            a_lr, _ = _iauroc(y[m], p_lr[m], k[m])
            a_xgb, _ = _iauroc(y[m], p_xgb[m], k[m])
            if not np.isnan(a_lr):
                d_lr[b] = a_lr
            if not np.isnan(a_xgb):
                d_xgb[b] = a_xgb
        ci_lr = (float(np.nanpercentile(d_lr, 2.5)),
                 float(np.nanpercentile(d_lr, 97.5)))
        ci_xgb = (float(np.nanpercentile(d_xgb, 2.5)),
                  float(np.nanpercentile(d_xgb, 97.5)))
        results[track] = {
            "n_landmarks": int(len(y)), "n_patients": int(n_u),
            "n_positive": int(y.sum()),
            "lr": {"iauroc": m_lr["iauroc"], "ci95": ci_lr,
                   "brier": m_lr["brier"]},
            "xgb": {"iauroc": m_xgb["iauroc"], "ci95": ci_xgb,
                    "brier": m_xgb["brier"]},
        }
        print(f"[eicu-tab] {track}: LR={m_lr['iauroc']:.4f} "
              f"CI=[{ci_lr[0]:.4f},{ci_lr[1]:.4f}] "
              f"XGB={m_xgb['iauroc']:.4f} "
              f"CI=[{ci_xgb[0]:.4f},{ci_xgb[1]:.4f}]", flush=True)
        report += [
            f"## {track}",
            "",
            f"- n = {len(y):,} landmarks; {int(n_u):,} patients; "
            f"{int(y.sum()):,} positive.",
            f"- Logistic regression: iAUROC {m_lr['iauroc']:.4f} "
            f"(95% CI {ci_lr[0]:.4f} to {ci_lr[1]:.4f}); "
            f"Brier {m_lr['brier']:.4f}.",
            f"- XGBoost: iAUROC {m_xgb['iauroc']:.4f} "
            f"(95% CI {ci_xgb[0]:.4f} to {ci_xgb[1]:.4f}); "
            f"Brier {m_xgb['brier']:.4f}.",
            "",
        ]
    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"[eicu-tab] report -> {OUT / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()
