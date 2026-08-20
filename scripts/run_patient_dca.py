# -*- coding: utf-8 -*-
"""Patient-level net-benefit analysis (reviewer-driven, post hoc).

The main decision-curve analysis is landmark-level (one observation per
landmark), and the same patient contributes multiple landmarks, so per-1000-
landmark net benefit can overstate per-patient benefit.  This script
reports patient-level net benefit for:
  1. the first landmark of each patient (k0 / minimum landmark_k), and
  2. the per-patient highest-risk landmark (maximum calibrated SCE
     probability; SC and SCE are scored at that same landmark).

For each population (paired test, deployment test), we report:
  - n patients, events;
  - patient-level iAUC (mean of per-landmark AUROC on the primary grid) is
    not used here; instead per-patient AUC at the selected landmark;
  - NB and dNB*1000 per 1,000 patients at thresholds 2%, 5%, 10% with
    patient-level bootstrap 95% CIs (2,000 resamples).
"""

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "models" / "runs" / "patient_dca"
OUT.mkdir(parents=True, exist_ok=True)
CAL = ROOT / "src" / "models" / "runs" / "main_calibration"
N_BOOT = 2000
SEED = 20260730
PTS = (0.02, 0.05, 0.10)


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


def _nb(y, p, pt):
    pred = p >= pt
    tp = float((pred & (y == 1)).sum())
    fp = float((pred & (y == 0)).sum())
    return tp / len(y) - fp / len(y) * pt / (1 - pt)


def _boot_dnb(y, p_sc, p_sce, pt, rng):
    n = len(y)
    d = np.full(N_BOOT, np.nan)
    for b in range(N_BOOT):
        idx = rng.integers(0, n, n)
        yy, a, c = y[idx], p_sc[idx], p_sce[idx]
        d[b] = _nb(yy, c, pt) - _nb(yy, a, pt)
    return (float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 50)),
            float(np.nanpercentile(d, 97.5)))


def patient_rows(subj, k, y, sc, sce, mode):
    subj = np.asarray(subj)
    k = np.asarray(k)
    y = np.asarray(y)
    sc = np.asarray(sc)
    sce = np.asarray(sce)
    usubj, inv = np.unique(subj, return_inverse=True)
    rows = []
    for u in range(len(usubj)):
        m = np.where(inv == u)[0]
        if mode == "first":
            j = m[int(np.argmin(k[m]))]
        else:  # highest-risk: max SCE calibrated probability
            j = m[int(np.argmax(sce[m]))]
        rows.append((usubj[u], y[j], sc[j], sce[j]))
    arr = np.array(rows, dtype=float)
    return arr[:, 0], arr[:, 1].astype(int), arr[:, 2], arr[:, 3]


def run():
    rng = np.random.default_rng(SEED)
    results = {}
    lines = ["# Patient-level decision-curve analysis (post hoc)", "",
             "Landmark-level net benefit can overstate per-patient benefit "
             "because one patient contributes multiple landmarks. Here NB "
             "is recomputed on one observation per patient: (1) the first "
             "landmark, and (2) the per-patient highest-risk landmark "
             "(max SCE calibrated probability; both models scored at that "
             "landmark). dNB is reported per 1,000 patients with "
             "patient-level bootstrap 95% CIs.", ""]
    for pop in ("paired", "deployment"):
        z = np.load(CAL / f"test_scores_{pop}.npz", allow_pickle=True)
        subj, k, y = z["subject_key"], z["landmark_k"], z["y"]
        sc, sce = z["sc_cal"], z["sce_cal"]
        results[pop] = {}
        lines += [f"## {pop} test", ""]
        for mode in ("first", "highest"):
            pu, py, psc, psce = patient_rows(subj, k, y, sc, sce, mode)
            n = len(pu)
            ev = int(py.sum())
            auc_sc = _auc(py, psc)
            auc_sce = _auc(py, psce)
            rows = []
            for pt in PTS:
                nb_sc = _nb(py, psc, pt)
                nb_sce = _nb(py, psce, pt)
                lo, mid, hi = _boot_dnb(py, psc, psce, pt, rng)
                rows.append({
                    "threshold": pt, "n_patients": n, "events": ev,
                    "nb_sc": nb_sc, "nb_sce": nb_sce,
                    "dnb_per_1000": (nb_sce - nb_sc) * 1000,
                    "ci95": [lo * 1000, hi * 1000],
                    "tp_sc": float(((psc >= pt) & (py == 1)).sum()),
                    "fp_sc": float(((psc >= pt) & (py == 0)).sum()),
                    "tp_sce": float(((psce >= pt) & (py == 1)).sum()),
                    "fp_sce": float(((psce >= pt) & (py == 0)).sum()),
                })
            results[pop][mode] = {
                "n_patients": n, "events": ev, "auc_sc": auc_sc,
                "auc_sce": auc_sce, "thresholds": rows,
            }
            lines += [
                f"### {mode} landmark (n patients = {n:,}, events = {ev:,})",
                "",
                f"- Patient-level AUC: SC = {auc_sc:.4f}; "
                f"SCE = {auc_sce:.4f}.",
                "",
                "| Threshold | SC: TP / FP | SCE: TP / FP | dNB per 1,000 "
                "patients (95% CI) |",
                "|---|---|---|---|",
            ]
            for r in rows:
                lines.append(
                    f"| {r['threshold']:.0%} | {r['tp_sc']:.0f} / "
                    f"{r['fp_sc']:.0f} | {r['tp_sce']:.0f} / "
                    f"{r['fp_sce']:.0f} | "
                    f"{r['dnb_per_1000']:+.2f} "
                    f"({r['ci95'][0]:+.2f}, {r['ci95'][1]:+.2f}) |")
            lines.append("")
    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[patient-dca] -> {OUT / 'REPORT.md'}", flush=True)
    for pop in ("paired", "deployment"):
        for mode in ("first", "highest"):
            r = results[pop][mode]
            print(f"[patient-dca] {pop}/{mode}: n={r['n_patients']} "
                  f"AUC SC={r['auc_sc']:.4f} SCE={r['auc_sce']:.4f}")


if __name__ == "__main__":
    run()
