# -*- coding: utf-8 -*-
"""ECG + tabular baselines (the prespecified ECG+LR "trial" from the
training plan v1.1, section 3.3).

Adds ECG features to the frozen 166-column baseline_tabular package:
  1. machine measurements from MIMIC-IV `ecg_measurements` (one row per
     study): RR interval, heart rate, P/QRS/T onsets/offsets, axes, and
     derived QRS/PR/QT durations and QTc (Bazett);
  2. per-lead mean/SD waveform statistics from the frozen ECG cache
     (12 leads x 2 statistics = 24 features);
  3. the binary ECG-availability indicator (24-h freshness + two-layer QC).

Two model families are trained:
  - paired (ECG features on all landmarks of the paired ECG cohort);
  - deployment (ECG features only where `ecg_selected_for_model` is true,
    missing elsewhere, with the availability indicator).

Clinical-only LR/XGBoost are re-fit on the same matrices so that the
ECG-vs-clinical difference is computed on identical rows.  iAUROC is
computed on the primary grid (k = 0-11); 95% CIs for the difference use
2,000 patient-level bootstrap resamples.
"""

import json
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
PAIRED = ART / "p9_packages" / RID / "sce_common_paired"
MOD_IDX = ART / "p6_modality" / RID / "modality_index.parquet"
ECG_CACHE = ART / "p5_ecg_cache" / RID / "ecg_cache_v2.npy"
ECG_CACHE_IDX = ART / "p5_ecg_cache" / RID / "ecg_cache_index_v2.parquet"
DB = r"E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb"
OUT = ROOT / "src" / "models" / "runs" / "ecg_tabular"
OUT.mkdir(parents=True, exist_ok=True)

KEY_COLS = [
    "episode_key", "subject_key", "landmark_k", "t_landmark_ts",
    "hours_since_sepsis", "in_risk_set", "in_main_grid", "k0",
    "episode_mapping_version", "hadm_id", "anchor_year_group", "set_name",
    "row_idx", "episode_id", "k", "y_24h", "label_status",
    "outcome_ascertainable", "weight",
]
CAT_COLS = ["gender", "admission_type", "icu_type"]
ECG_MEASURE_COLS = ["rr_interval", "p_onset", "p_end", "qrs_onset",
                    "qrs_end", "t_end", "p_axis", "qrs_axis", "t_axis"]
ECG_DERIVED_COLS = ["ecg_hr", "ecg_qrs_dur", "ecg_pr", "ecg_qt",
                    "ecg_qtc_bazett"]
ECG_WF_COLS = [f"ecg_lead{i}_mean" for i in range(12)] + \
              [f"ecg_lead{i}_sd" for i in range(12)]
ECG_FEATURES = ECG_MEASURE_COLS + ECG_DERIVED_COLS + ECG_WF_COLS
MAIN_K = 12
N_BOOT = 2000
SEED = 20260730


def build_study_features():
    """Study-level ECG features: measurements + waveform per-lead stats."""
    feat_path = OUT / "ecg_study_features.parquet"
    if feat_path.exists():
        return pd.read_parquet(feat_path)

    t0 = time.time()
    con = duckdb.connect(DB, read_only=True)
    em = con.execute(
        "select study_id, rr_interval, p_onset, p_end, qrs_onset, qrs_end, "
        "t_end, p_axis, qrs_axis, t_axis from main.ecg_measurements"
    ).fetchdf()
    con.close()
    em = em.drop_duplicates(subset="study_id")
    em["ecg_hr"] = np.where(em["rr_interval"] > 0,
                            60000.0 / em["rr_interval"], np.nan)
    em["ecg_qrs_dur"] = em["qrs_end"] - em["qrs_onset"]
    em["ecg_pr"] = em["qrs_onset"] - em["p_onset"]
    em["ecg_qt"] = em["t_end"] - em["qrs_onset"]
    em.loc[em["ecg_qrs_dur"] <= 0, "ecg_qrs_dur"] = np.nan
    em.loc[em["ecg_pr"] <= 0, "ecg_pr"] = np.nan
    em.loc[em["ecg_qt"] <= 0, "ecg_qt"] = np.nan
    em["ecg_qtc_bazett"] = np.where(
        em["rr_interval"] > 0,
        em["ecg_qt"] / np.sqrt(em["rr_interval"] / 1000.0), np.nan)
    em = em[["study_id", "rr_interval", "ecg_hr", "p_onset", "p_end",
             "qrs_onset", "qrs_end", "t_end", "p_axis", "qrs_axis",
             "t_axis", "ecg_qrs_dur", "ecg_pr", "ecg_qt",
             "ecg_qtc_bazett"]]

    ci = pd.read_parquet(ECG_CACHE_IDX)
    em = em.merge(ci[["study_id", "cache_row"]], on="study_id", how="left")
    em = em.dropna(subset=["cache_row"])
    em["cache_row"] = em["cache_row"].astype(int)
    em = em.sort_values("cache_row").reset_index(drop=True)

    arr = np.load(ECG_CACHE, mmap_mode="r")
    rows = em["cache_row"].to_numpy()
    # Compute per-lead statistics over the full memmap (streamed), then
    # select the study rows; avoids materializing an 8.7 GB fancy-index copy.
    means_full = arr.mean(axis=2, dtype=np.float64)
    stds_full = arr.std(axis=2, dtype=np.float64)
    means = means_full[rows]
    stds = stds_full[rows]
    lead_cols = [f"ecg_lead{i}_mean" for i in range(12)] + \
                [f"ecg_lead{i}_sd" for i in range(12)]
    wf = pd.DataFrame(np.hstack([means, stds]), columns=lead_cols)
    em = pd.concat([em.reset_index(drop=True), wf], axis=1)
    em = em.drop(columns=["cache_row"])
    em.to_parquet(feat_path, index=False)
    print(f"[ecg-tab] study features built: {em.shape} "
          f"({time.time() - t0:.0f}s)", flush=True)
    return em


def load_clinical(split):
    return pd.read_parquet(BT / split / "features.parquet")


def paired_keys(split):
    idx = pd.read_parquet(PAIRED / split / "index.parquet",
                          columns=["episode_key", "landmark_k"])
    return set(zip(idx["episode_key"], idx["landmark_k"]))


def clinical_matrix(df, fit=None):
    """Same preprocessing as the clinical-only tabular baselines."""
    feat = df.drop(columns=[c for c in KEY_COLS if c in df.columns])
    if fit is None:
        num_cols = [c for c in feat.columns if c not in CAT_COLS]
        medians = feat[num_cols].median(numeric_only=True)
        means = feat[num_cols].mean(numeric_only=True)
        stds = feat[num_cols].std(numeric_only=True).replace(0, 1.0)
        cats = {c: sorted(feat[c].dropna().astype(str).unique().tolist())
                for c in CAT_COLS}
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
        known = set(cats[c])
        parts.append((~codes.isin(known)).to_numpy(dtype=np.float32)
                     .reshape(-1, 1))
    return np.hstack(parts), fit


def merge_ecg(df, study_feat):
    mod = pd.read_parquet(
        MOD_IDX, columns=["episode_key", "landmark_k", "study_id",
                          "ecg_selected_for_model"])
    df = df.merge(mod, on=["episode_key", "landmark_k"], how="left")
    df["ecg_selected"] = df["ecg_selected_for_model"].fillna(False).astype(bool)
    sid = df["study_id"].astype("float64").to_numpy()
    df = df.drop(columns=["study_id", "ecg_selected_for_model"])
    feat = study_feat.set_index(
        study_feat["study_id"].astype("float64")).drop(columns=["study_id"])
    feat_sub = feat.reindex(sid).reset_index(drop=True)
    df = pd.concat([df.reset_index(drop=True), feat_sub], axis=1)
    return df


def build_matrices(split, study_feat, ecg_mode, fit):
    """ecg_mode: 'clinical', 'paired', 'deployment'."""
    df = load_clinical(split)
    sub = None
    if ecg_mode in ("paired", "deployment"):
        df = merge_ecg(df, study_feat)
        sub = df[ECG_FEATURES].copy()
        avail = np.ones(len(df), dtype=np.float32)
        if ecg_mode == "deployment":
            sub = sub.where(df["ecg_selected"], np.nan)
            avail = df["ecg_selected"].astype(np.float32).to_numpy()
        df = df.drop(columns=ECG_FEATURES + ["ecg_selected"])
    Xc, fit_ret = clinical_matrix(df, fit)
    y = df["y_24h"].to_numpy(dtype=np.float32)
    k = df["landmark_k"].to_numpy(dtype=np.int64)
    w = df["weight"].to_numpy(dtype=np.float32)
    if ecg_mode == "clinical":
        return Xc, y, k, w, fit_ret
    if fit is None:
        med = sub.median(numeric_only=True)
        mu = sub.mean(numeric_only=True)
        sd = sub.std(numeric_only=True).replace(0, 1.0)
        med = med.fillna(0.0)
        mu = mu.fillna(0.0)
        sd = sd.fillna(1.0)
        fit_ret["ecg_med"] = med
        fit_ret["ecg_mu"] = mu
        fit_ret["ecg_sd"] = sd
    else:
        med, mu, sd = fit["ecg_med"], fit["ecg_mu"], fit["ecg_sd"]
    Xe = (sub.fillna(med) - mu) / sd
    X = np.hstack([Xc, Xe.to_numpy(dtype=np.float32),
                   avail.reshape(-1, 1)])
    return X, y, k, w, fit_ret


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
    aucs = [_auroc(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan, aucs
    return float(np.mean(aucs)), aucs


def _boot_delta(y, s_base, s_ecg, k, subj, rng):
    usubj, inv = np.unique(subj, return_inverse=True)
    n_u = len(usubj)
    inb = np.zeros(n_u, dtype=bool)
    d = np.full(N_BOOT, np.nan)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n_u, n_u)] = True
        m = inb[inv]
        a_base, _ = _iauroc(y[m], s_base[m], k[m])
        a_ecg, _ = _iauroc(y[m], s_ecg[m], k[m])
        if not (np.isnan(a_base) or np.isnan(a_ecg)):
            d[b] = a_ecg - a_base
    return (float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5))), int(np.isfinite(d).sum())


def fit_lr(Xtr, ytr, wtr, Xte, yte, C=1.0):
    from sklearn.linear_model import LogisticRegression
    m = LogisticRegression(max_iter=2000, C=C, class_weight="balanced")
    m.fit(Xtr, ytr, sample_weight=wtr)
    return m.predict_proba(Xte)[:, 1]


def fit_xgb(Xtr, ytr, wtr, Xva, yva, Xte, yte):
    import xgboost as xgb
    prev = float(ytr.mean())
    spw = (1 - prev) / max(prev, 1e-9)
    m = xgb.XGBClassifier(
        n_estimators=500, max_depth=6, eta=0.05, subsample=0.8,
        colsample_bytree=0.8, min_child_weight=1, scale_pos_weight=spw,
        eval_metric="auc", tree_method="hist", n_jobs=8,
        random_state=SEED, enable_categorical=False,
        early_stopping_rounds=20)
    m.fit(Xtr, ytr, sample_weight=wtr, eval_set=[(Xva, yva)],
          verbose=False)
    return m.predict_proba(Xte)[:, 1]


def main():
    study_feat = build_study_features()
    rng = np.random.default_rng(SEED)
    results = {}
    report = [
        "# ECG + tabular baselines (prespecified ECG+LR trial)",
        "",
        "ECG features: machine measurements (RR/HR/P/QRS/T intervals and "
        "axes, derived QRS/PR/QT/QTc) + per-lead mean/SD waveform "
        "statistics + binary ECG-availability indicator. Clinical-only "
        "LR/XGBoost are re-fit on identical rows; iAUROC on the primary "
        "grid (k = 0-11); 95% CIs from 2,000 patient-level bootstrap "
        "resamples for the ECG-minus-clinical difference.",
        "",
    ]

    for mode in ("paired", "deployment"):
        # train/validation/test matrices
        Xtr, ytr, ktr, wtr, fit = build_matrices(
            "train", study_feat, mode, None)
        Xva, yva, kva, wva, _ = build_matrices(
            "validation", study_feat, mode, fit)
        Xte, yte, kte, wte, _ = build_matrices(
            "test", study_feat, mode, fit)
        te_df = load_clinical("test")
        subj = te_df["subject_key"].to_numpy()
        print(f"[ecg-tab] {mode}: train {Xtr.shape} test {Xte.shape}",
              flush=True)

        # paired mode: restrict train/validation/test to paired keys
        if mode == "paired":
            tr_keys = paired_keys("train")
            va_keys = paired_keys("validation")
            te_keys = paired_keys("test")
            tr_df = load_clinical("train")
            va_df = load_clinical("validation")
            tr_m = np.array([(e, k) in tr_keys for e, k in zip(
                tr_df["episode_key"], tr_df["landmark_k"])])
            va_m = np.array([(e, k) in va_keys for e, k in zip(
                va_df["episode_key"], va_df["landmark_k"])])
            te_m = np.array([(e, k) in te_keys for e, k in zip(
                te_df["episode_key"], te_df["landmark_k"])])
            Xtr, ytr, ktr, wtr = (Xtr[tr_m], ytr[tr_m], ktr[tr_m], wtr[tr_m])
            Xva, yva, kva, wva = (Xva[va_m], yva[va_m], kva[va_m], wva[va_m])
            Xte, yte, kte, wte = (Xte[te_m], yte[te_m], kte[te_m], wte[te_m])
            subj = te_df["subject_key"].to_numpy()[te_m]
            print(f"[ecg-tab] paired-restricted: train {Xtr.shape} "
                  f"test {Xte.shape}", flush=True)

        # clinical-only matrices (same rows)
        Xtr_c, _, _, _, fit_c = build_matrices("train", None, "clinical", None)
        Xva_c, yva_c, kva_c, wva_c, _ = build_matrices(
            "validation", None, "clinical", fit_c)
        Xte_c, _, _, _, _ = build_matrices("test", None, "clinical", fit_c)
        if mode == "paired":
            Xtr_c = Xtr_c[tr_m]
            Xva_c, yva_c, kva_c, wva_c = (
                Xva_c[va_m], yva_c[va_m], kva_c[va_m], wva_c[va_m])
            Xte_c = Xte_c[te_m]

        p_lr = fit_lr(Xtr_c, ytr, wtr, Xte_c, yte)
        p_lr_ecg = fit_lr(Xtr, ytr, wtr, Xte, yte)
        p_xgb = fit_xgb(Xtr_c, ytr, wtr, Xva_c, yva_c, Xte_c, yte)
        p_xgb_ecg = fit_xgb(Xtr, ytr, wtr, Xva, yva, Xte, yte)

        for name, pb, pe in (("lr", p_lr, p_lr_ecg),
                             ("xgb", p_xgb, p_xgb_ecg)):
            mb = landmark_metrics(yte, pb, kte)
            me = landmark_metrics(yte, pe, kte)
            ci, valid = _boot_delta(yte, pb, pe, kte, subj, rng)
            results[f"{mode}_{name}"] = {
                "clinical": mb, "ecg": me,
                "delta": me["iauroc"] - mb["iauroc"],
                "delta_ci95": ci, "bootstrap_valid": valid,
                "n_test": int(len(yte)), "n_positive": int(yte.sum()),
            }
            print(f"[ecg-tab] {mode}/{name}: clinical="
                  f"{mb['iauroc']:.4f} ecg={me['iauroc']:.4f} "
                  f"delta={me['iauroc'] - mb['iauroc']:+.4f} "
                  f"CI=[{ci[0]:+.4f},{ci[1]:+.4f}]", flush=True)

        report += [f"## {mode}", "",
                   "| Model | Clinical iAUROC | ECG+clinical iAUROC | "
                   "Δ (95% CI) |",
                   "|---|---|---|---|"]
        for name, label in (("lr", "Logistic regression"),
                            ("xgb", "XGBoost")):
            r = results[f"{mode}_{name}"]
            report.append(
                f"| {label} | {r['clinical']['iauroc']:.4f} | "
                f"{r['ecg']['iauroc']:.4f} | "
                f"{r['delta']:+.4f} ({r['delta_ci95'][0]:+.4f} to "
                f"{r['delta_ci95'][1]:+.4f}) |")
        report.append("")

    # deployment availability-only tabular control (XGB + availability flag)
    # Reuse the deployment clinical matrix, but only clinical features plus
    # the availability flag (no ECG measurements).
    Xtr_d, ytr_d, ktr_d, wtr_d, fit_d = build_matrices(
        "train", study_feat, "deployment", None)
    # rebuild clinical-only for deployment
    Xtr_c, _, _, _, fit_c = build_matrices("train", None, "clinical", None)
    Xva_c, yva_c, kva_c, wva_c, _ = build_matrices(
        "validation", None, "clinical", fit_c)
    Xte_c, _, _, _, _ = build_matrices("test", None, "clinical", fit_c)
    te_df = load_clinical("test")
    subj = te_df["subject_key"].to_numpy()
    mod = pd.read_parquet(
        MOD_IDX, columns=["episode_key", "landmark_k", "ecg_selected_for_model"])
    te_avail = te_df.merge(mod, on=["episode_key", "landmark_k"],
                           how="left")["ecg_selected_for_model"].fillna(
        False).astype(bool).to_numpy()
    tr_avail = load_clinical("train").merge(
        mod, on=["episode_key", "landmark_k"], how="left")[
        "ecg_selected_for_model"].fillna(False).astype(bool).to_numpy()
    va_avail = load_clinical("validation").merge(
        mod, on=["episode_key", "landmark_k"], how="left")[
        "ecg_selected_for_model"].fillna(False).astype(bool).to_numpy()
    Xtr_av = np.hstack([Xtr_c, tr_avail.astype(np.float32).reshape(-1, 1)])
    Xva_av = np.hstack([Xva_c, va_avail.astype(np.float32).reshape(-1, 1)])
    Xte_av = np.hstack([Xte_c, te_avail.astype(np.float32).reshape(-1, 1)])
    p_xgb_av = fit_xgb(Xtr_av, ytr_d, wtr_d, Xva_av, yva_c, Xte_av, yte)
    p_xgb = fit_xgb(Xtr_c, ytr_d, wtr_d, Xva_c, yva_c, Xte_c, yte)
    m_av = landmark_metrics(yte, p_xgb_av, kte)
    m_base = landmark_metrics(yte, p_xgb, kte)
    ci_av, valid_av = _boot_delta(yte, p_xgb, p_xgb_av, kte, subj, rng)
    results["deployment_xgb_avail"] = {
        "clinical": m_base, "ecg": m_av,
        "delta": m_av["iauroc"] - m_base["iauroc"],
        "delta_ci95": ci_av, "bootstrap_valid": valid_av,
        "n_test": int(len(yte)), "n_positive": int(yte.sum()),
    }
    report += [
        "## Deployment, XGBoost + availability flag only (post hoc)",
        "",
        f"- Clinical XGBoost iAUROC = {m_base['iauroc']:.4f}; "
        f"XGBoost + availability = {m_av['iauroc']:.4f}; "
        f"Δ = {m_av['iauroc'] - m_base['iauroc']:+.4f} "
        f"(95% CI {ci_av[0]:+.4f} to {ci_av[1]:+.4f}).",
    ]

    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"[ecg-tab] report -> {OUT / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()
