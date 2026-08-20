# -*- coding: utf-8 -*-
"""Subgroup (fairness) audit: sex, age, race/ethnicity.

Reviewer-driven, post hoc analysis using the frozen test scores and
demographics from baseline_static (age, sex) plus race/ethnicity from the
source MIMIC-IV admissions table.  Reports iAUROC (primary grid k = 0-11)
for SC and SCE in the paired and deployment test sets by subgroup, with
patient-level bootstrap 95% CIs for the SCE-vs-SC difference.

Outputs:
    src/models/runs/fairness/REPORT.md
    src/models/runs/fairness/results.json
"""

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "models" / "runs" / "fairness"
OUT.mkdir(parents=True, exist_ok=True)
PKG = ROOT / "preprocess" / "artifacts" / "p9_packages" / "pp_v1_20260730" \
    / "baseline_tabular" / "test" / "features.parquet"
CAL = ROOT / "src" / "models" / "runs" / "main_calibration"
DB = r"E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb"
N_BOOT = 1000
SEED = 20260730
MAIN_K = 12

RACE_MAP = {
    "WHITE": "White",
    "WHITE - RUSSIAN": "White",
    "WHITE - OTHER EUROPEAN": "White",
    "WHITE - BRAZILIAN": "White",
    "WHITE - EASTERN EUROPEAN": "White",
    "WHITE - MIDDLE EASTERN": "White",
    "BLACK/AFRICAN AMERICAN": "Black",
    "BLACK/CAPE VERDEAN": "Black",
    "BLACK/AFRICAN": "Black",
    "BLACK/CARIBBEAN ISLAND": "Black",
    "ASIAN": "Asian",
    "ASIAN - CHINESE": "Asian",
    "ASIAN - ASIAN INDIAN": "Asian",
    "ASIAN - VIETNAMESE": "Asian",
    "ASIAN - FILIPINO": "Asian",
    "ASIAN - OTHER": "Asian",
    "ASIAN - JAPANESE": "Asian",
    "ASIAN - KOREAN": "Asian",
    "ASIAN - THAI": "Asian",
    "ASIAN - CAMBODIAN": "Asian",
    "HISPANIC OR LATINO": "Hispanic",
    "HISPANIC/LATINO - PUERTO RICAN": "Hispanic",
    "HISPANIC/LATINO - DOMINICAN": "Hispanic",
    "HISPANIC/LATINO - GUATEMALAN": "Hispanic",
    "HISPANIC/LATINO - CUBAN": "Hispanic",
    "HISPANIC/LATINO - SALVADORAN": "Hispanic",
    "HISPANIC/LATINO - CENTRAL AMERICAN": "Hispanic",
    "HISPANIC/LATINO - COLUMBIAN": "Hispanic",
    "HISPANIC/LATINO - MEXICAN": "Hispanic",
    "HISPANIC/LATINO - OTHER": "Hispanic",
    "NATIVE HAWAIIAN OR OTHER PACIFIC ISLANDER": "Other",
    "AMERICAN INDIAN/ALASKA NATIVE": "Other",
    "MULTIPLE RACE/ETHNICITY": "Other",
    "OTHER": "Other",
    "UNKNOWN": "Other/Unknown",
    "UNABLE TO OBTAIN": "Other/Unknown",
    "PATIENT DECLINED TO ANSWER": "Other/Unknown",
}


def _auc(y, s):
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
    aucs = [_auc(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan, aucs
    return float(np.mean(aucs)), aucs


def _boot_delta(y, s_sc, s_sce, k, subj, rng):
    usubj, inv = np.unique(subj, return_inverse=True)
    n_u = len(usubj)
    inb = np.zeros(n_u, dtype=bool)
    d = np.full(N_BOOT, np.nan)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n_u, n_u)] = True
        m = inb[inv]
        a_sc, _ = _iauroc(y[m], s_sc[m], k[m])
        a_sce, _ = _iauroc(y[m], s_sce[m], k[m])
        if not (np.isnan(a_sc) or np.isnan(a_sce)):
            d[b] = a_sce - a_sc
    return (float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5))), int(np.isfinite(d).sum())


def load_demographics():
    df = pd.read_parquet(PKG)
    con = duckdb.connect(DB, read_only=True)
    adm = con.execute(
        "SELECT subject_id, hadm_id, race FROM main.admissions"
    ).fetchdf()
    con.close()
    adm["race_group"] = adm["race"].map(
        lambda x: RACE_MAP.get(str(x).upper(), "Other/Unknown"))
    adm = adm[["subject_id", "hadm_id", "race", "race_group"]]
    df = df.rename(columns={"subject_key": "subject_id"})
    df = df.merge(adm, on=["subject_id", "hadm_id"], how="left")
    df["race_group"] = df["race_group"].fillna("Other/Unknown")
    df["sex"] = df["gender"].astype(str).str.upper()
    df["age_group"] = pd.cut(
        df["age"], bins=[0, 65, 80, 200],
        labels=["<65", "65-79", ">=80"]).astype(str)
    return df


def evaluate_subgroup(y, sc, sce, k, subj, rng):
    m_grid = k < MAIN_K
    ia_sc, _ = _iauroc(y[m_grid], sc[m_grid], k[m_grid])
    ia_sce, _ = _iauroc(y[m_grid], sce[m_grid], k[m_grid])
    if np.isnan(ia_sc) or np.isnan(ia_sce):
        ci, valid = [None, None], 0
    else:
        ci, valid = _boot_delta(y[m_grid], sc[m_grid], sce[m_grid],
                                k[m_grid], subj[m_grid], rng)
    return {
        "n_landmarks": int(len(y)),
        "n_grid_landmarks": int(m_grid.sum()),
        "n_patients": int(pd.Series(subj).nunique()),
        "events": int(y.sum()),
        "events_grid": int(y[m_grid].sum()),
        "iauroc_sc": ia_sc, "iauroc_sce": ia_sce,
        "delta_iauroc": None if np.isnan(ia_sce - ia_sc)
        else ia_sce - ia_sc,
        "delta_ci95": ci, "boot_valid": valid,
    }


def run():
    demo = load_demographics()
    rng = np.random.default_rng(SEED)
    results = {}
    lines = [
        "# Subgroup (fairness) audit - sex, age, race/ethnicity",
        "",
        "Post hoc, revision-added analysis. iAUROC on the primary grid "
        "(k = 0-11); SC/SCE use the five-seed ensemble scores from the "
        "frozen models. 95% CIs are patient-level bootstrap (1,000 "
        "resamples) for the SCE-vs-SC difference.",
        "",
    ]
    for pop in ("paired", "deployment"):
        z = np.load(CAL / f"test_scores_{pop}.npz", allow_pickle=True)
        sdf = pd.DataFrame({
            "subject_key": z["subject_key"],
            "landmark_k": z["landmark_k"],
            "y": z["y"],
            "sc": z["sc_raw"], "sce": z["sce_raw"],
        })
        merged = sdf.merge(
            demo[["subject_id", "landmark_k", "sex", "age_group",
                  "race_group"]],
            left_on=["subject_key", "landmark_k"],
            right_on=["subject_id", "landmark_k"], how="left")
        merged["sex"] = merged["sex"].fillna("Unknown")
        merged["age_group"] = merged["age_group"].fillna("Unknown")
        n_matched = int(merged["race_group"].notna().sum())
        results[pop] = {"n_matched": n_matched, "subgroups": {}}
        lines += [f"## {pop} test (matched {n_matched:,} landmarks)", ""]
        groups = {
            "sex": sorted(merged["sex"].unique()),
            "age_group": ["<65", "65-79", ">=80", "Unknown"],
            "race_group": ["White", "Black", "Asian", "Hispanic",
                           "Other/Unknown"],
        }
        for dim, levels in groups.items():
            lines.append(f"### {dim}")
            lines.append("| Subgroup | n landmarks | n patients | events "
                         "| SC iAUROC | SCE iAUROC | Δ (95% CI) |")
            lines.append("|---|---|---|---|---|---|---|")
            for lvl in levels:
                m = merged[dim] == lvl
                if m.sum() < 100:
                    lines.append(f"| {lvl} | {int(m.sum()):,} | - | - | "
                                 "n<100 | - | - |")
                    continue
                r = evaluate_subgroup(
                    merged.loc[m, "y"].to_numpy(dtype=np.float32),
                    merged.loc[m, "sc"].to_numpy(),
                    merged.loc[m, "sce"].to_numpy(),
                    merged.loc[m, "landmark_k"].to_numpy(),
                    merged.loc[m, "subject_key"].to_numpy(), rng)
                results[pop]["subgroups"][f"{dim}={lvl}"] = r
                if r["iauroc_sc"] is None or np.isnan(r["iauroc_sc"]):
                    cells = (lvl, f"{r['n_landmarks']:,}", r["n_patients"],
                             r["events"], "n/a", "n/a", "n/a")
                else:
                    ci = r["delta_ci95"]
                    citxt = (f"({ci[0]:+.3f}, {ci[1]:+.3f})"
                             if ci and ci[0] is not None else "n/a")
                    cells = (lvl, f"{r['n_landmarks']:,}", r["n_patients"],
                             r["events"], f"{r['iauroc_sc']:.3f}",
                             f"{r['iauroc_sce']:.3f}",
                             f"{r['delta_iauroc']:+.3f} {citxt}")
                lines.append("| " + " | ".join(map(str, cells)) + " |")
            lines.append("")
    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[fairness] -> {OUT / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    run()
