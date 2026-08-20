"""Generate draft figures for the SEPSIS-MM-DYN npj Digital Medicine manuscript.

All values are read directly from the project result files listed in the
SOURCE comments below. Paired per-landmark AUROCs are transcribed from
src/models/runs/REPORT.md (its only machine-readable record).

Outputs: manuscript/figures/*.png (300 dpi) and *.pdf
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "src" / "models" / "runs"
OUT = ROOT / "figures"
OUT.mkdir(parents=True, exist_ok=True)

# ---- publication style ---------------------------------------------------
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "legend.fontsize": 7,
        "axes.linewidth": 0.8,
        "lines.linewidth": 1.2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 100,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
    }
)

OKABE = {
    "blue": "#0072B2",
    "orange": "#E69F00",
    "green": "#009E73",
    "red": "#D55E00",
    "sky": "#56B4E9",
    "vermilion": "#D55E00",
    "purple": "#CC79A7",
    "yellow": "#F0E442",
    "grey": "#999999",
    "black": "#000000",
}


def export(fig: plt.Figure, name: str) -> None:
    for ext in ("png", "pdf"):
        target = OUT / f"{name}.{ext}"
        tmp = OUT / f"{name}.{ext}.tmp"
        fig.savefig(tmp, dpi=300, format=ext)
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                import time

                time.sleep(1)
    plt.close(fig)
    print("wrote", name)


# ==========================================================================
# SOURCE: src/models/runs/REPORT.md (paired per-landmark table)
# ==========================================================================
K = np.arange(12)
SC_PAIRED = np.array(
    [0.667, 0.781, 0.865, 0.888, 0.755, 0.908,
     0.916, 0.923, 0.909, 0.709, 0.750, 0.763]
)
SCE_PAIRED = np.array(
    [0.733, 0.812, 0.876, 0.903, 0.778, 0.912,
     0.910, 0.921, 0.902, 0.686, 0.715, 0.785]
)
DELTA_PAIRED = SCE_PAIRED - SC_PAIRED


def fig2_paired_primary() -> None:
    """Paired cohort: per-landmark AUROC and delta."""
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(7.0, 2.7), layout="constrained", gridspec_kw={"width_ratios": [2, 1]}
    )
    ax1.plot(K, SC_PAIRED, marker="o", ms=3.5, ls="-", color=OKABE["blue"], label="SC (clinical)")
    ax1.plot(K, SCE_PAIRED, marker="s", ms=3.5, ls="--", color=OKABE["orange"], label="SCE (multimodal)")
    ax1.axhline(0.5, color=OKABE["grey"], lw=0.7, ls=":")
    ax1.set_xlabel("Landmark k (6-h intervals from sepsis onset)")
    ax1.set_ylabel("Landmark AUROC")
    ax1.set_xticks(K)
    ax1.set_ylim(0.55, 1.0)
    ax1.legend(frameon=False, loc="lower right")

    colors = [OKABE["blue"] if d >= 0 else OKABE["red"] for d in DELTA_PAIRED]
    ax2.bar(K, DELTA_PAIRED, color=colors, width=0.7)
    ax2.axhline(0, color="black", lw=0.8)
    ax2.set_xlabel("Landmark k")
    ax2.set_ylabel(r"$\Delta$AUROC (SCE $-$ SC)")
    ax2.set_xticks(K)
    export(fig, "fig2_paired_primary")


# ==========================================================================
# SOURCE: src/models/runs/deployment/deployment_result.json
# ==========================================================================
def fig3_deployment() -> None:
    dep = json.loads((RUNS / "deployment" / "deployment_result.json").read_text(encoding="utf-8"))
    per = dep["auroc_per_landmark"]
    sc = np.array(per["sc"])
    sce = np.array(per["sce"])
    route = np.array(per["route"])
    fig, ax = plt.subplots(figsize=(3.5, 2.6), layout="constrained")
    ax.plot(K, sc, marker="o", ms=3.5, ls="-", color=OKABE["blue"], label="SC-common-all")
    ax.plot(K, sce, marker="s", ms=3.5, ls="--", color=OKABE["orange"], label="SCE-deployment")
    ax.plot(K, route, marker="^", ms=3.5, ls=":", color=OKABE["green"], label="Deployment route")
    ax.axhline(0.5, color=OKABE["grey"], lw=0.7, ls=":")
    ax.set_xlabel("Landmark k")
    ax.set_ylabel("Landmark AUROC")
    ax.set_xticks(K)
    ax.set_ylim(0.6, 0.95)
    ax.legend(frameon=False, ncol=1, loc="lower right")
    export(fig, "fig3_deployment")


# ==========================================================================
# SOURCE: src/models/runs/REPORT.md; deployment_result.json;
#         sensitivity/freshness_48h.json, freshness_72h.json
# ==========================================================================
def fig4_effect_shape() -> None:
    f48 = json.loads((RUNS / "sensitivity" / "freshness_48h" / "result.json").read_text(encoding="utf-8"))
    f72 = json.loads((RUNS / "sensitivity" / "freshness_72h" / "result.json").read_text(encoding="utf-8"))
    dep = json.loads((RUNS / "deployment" / "deployment_result.json").read_text(encoding="utf-8"))
    labels = ["Primary paired\n(24-h freshness)", "Deployment\ncohort", "Freshness 48 h", "Freshness 72 h"]
    d = [0.0063, dep["ensemble"]["delta_iauroc"], f48["delta"], f72["delta"]]
    lo = [-0.0023, dep["delta_bootstrap_ci95"][0], f48["ci95"][0], f72["ci95"][0]]
    hi = [0.0183, dep["delta_bootstrap_ci95"][1], f48["ci95"][1], f72["ci95"][1]]
    y = np.arange(len(labels))[::-1]
    fig, ax = plt.subplots(figsize=(3.8, 2.5), layout="constrained")
    ax.axvline(0, color="black", lw=0.8)
    for yi, m, l, h in zip(y, d, lo, hi):
        ax.errorbar(m, yi, xerr=[[m - l], [h - m]], fmt="o", ms=4,
                    color=OKABE["blue"], ecolor=OKABE["black"], capsize=2.5, lw=1.1)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel(r"$\Delta$iAUROC (95% CI)")
    ax.set_xlim(-0.02, 0.035)
    export(fig, "fig4_effect_shape")


# ==========================================================================
# SOURCE: src/models/runs/summary.csv; sensitivity/{ecg_globalnorm,ssl_inductive}/result.json
# ==========================================================================
def fig5_ablations() -> None:
    summary = pd.read_csv(RUNS / "summary.csv")
    def mean_sd(pkg, model):
        s = summary[(summary.pkg == pkg) & (summary.model == model)].test_iauroc
        return s.mean(), s.std(ddof=1)

    gd, gds = mean_sd("sc_common_paired", "grud")
    tp, tps = mean_sd("sc_common_paired", "tpc")
    scm, scms = mean_sd("sce_common_paired", "sce_grud")
    glob = json.loads((RUNS / "sensitivity" / "ecg_globalnorm" / "result.json").read_text(encoding="utf-8"))
    ssl = json.loads((RUNS / "sensitivity" / "ssl_inductive" / "result.json").read_text(encoding="utf-8"))

    labels = ["GRU-D (SC)", "TPC (SC)", "SCE per-record", "SCE global-norm", "SCE + SSL init"]
    means = [gd, tp, scm, glob["mean"], ssl["mean"]]
    sds = [gds, tps, scms, glob["sd"], ssl["sd"]]
    fig, ax = plt.subplots(figsize=(4.2, 2.6), layout="constrained")
    x = np.arange(len(labels))
    ax.errorbar(x, means, yerr=sds, fmt="o", ms=4.5, color=OKABE["blue"],
                ecolor=OKABE["black"], capsize=3, lw=1.1)
    ax.axhline(gd, color=OKABE["grey"], lw=0.7, ls=":")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha="right")
    ax.set_ylabel("Test iAUROC (mean ± SD across 5 seeds)")
    ax.set_ylim(0.78, 0.85)
    export(fig, "fig5_ablations")


# ==========================================================================
# SOURCE: src/models/runs/main_calibration/calibration_metrics.json
# ==========================================================================
def fig6_calibration() -> None:
    cal = json.loads((RUNS / "main_calibration" / "calibration_metrics.json").read_text(encoding="utf-8"))
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), layout="constrained", sharey=True)
    for ax, cohort in zip(axes, ("paired", "deployment")):
        sc = cal[cohort]["decile_cal"]["sc"]
        sce = cal[cohort]["decile_cal"]["sce"]
        pm = [r["pred_mean"] for r in sc]
        ob = [r["obs_rate"] for r in sc]
        pm2 = [r["pred_mean"] for r in sce]
        ob2 = [r["obs_rate"] for r in sce]
        ax.plot([0, max(ob + ob2)], [0, max(ob + ob2)], ls=":", color=OKABE["grey"], lw=0.9)
        ax.plot(pm, ob, marker="o", ms=3, ls="", color=OKABE["blue"], label="SC")
        ax.plot(pm2, ob2, marker="s", ms=3, ls="", color=OKABE["orange"], label="SCE")
        ax.set_xlabel("Predicted probability (calibrated)")
        ax.set_title(f"{cohort}")
        ax.legend(frameon=False)
    axes[0].set_ylabel("Observed event rate")
    export(fig, "fig6_calibration")


# ==========================================================================
# SOURCE: src/models/runs/dca/dca_curves.npz
# ==========================================================================
def fig7_dca() -> None:
    z = np.load(RUNS / "dca" / "dca_curves.npz")
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), layout="constrained")
    for ax, tag in zip(axes, ("paired", "deployment")):
        pt = z[f"{tag}_pt"]
        ax.plot(pt, z[f"{tag}_sc_cal"], ls="-", color=OKABE["blue"], lw=1.4, label="SC (calibrated)")
        ax.plot(pt, z[f"{tag}_sce_cal"], ls="--", color=OKABE["orange"], lw=1.4, label="SCE (calibrated)")
        ax.plot(pt, z[f"{tag}_treat_all"], ls=":", color=OKABE["grey"], lw=1.0, label="Treat all")
        ax.fill_between(pt, z[f"{tag}_dnb_lo"], z[f"{tag}_dnb_hi"], color=OKABE["sky"], alpha=0.25)
        ax.axhline(0, color="black", lw=1.0, ls="-", clip_on=False,
                   label="Treat none")
        ax.spines["bottom"].set_color(OKABE["grey"])
        ax.set_ylim(0, 0.025)
        ax.set_xlabel("Threshold probability")
        ax.set_title(f"{tag}")
        ax.legend(frameon=False, fontsize=6.5, loc="upper right")
    axes[0].set_ylabel("Net benefit")
    export(fig, "fig7_dca")


# ==========================================================================
# SOURCE: eicu_external/stratified/{by_hospital.csv,by_density.json,by_hospital_summary.json}
# ==========================================================================
def fig8_eicu() -> None:
    hospitals = pd.read_csv(RUNS / "eicu_external" / "stratified" / "by_hospital.csv")
    density = json.loads((RUNS / "eicu_external" / "stratified" / "by_density.json").read_text(encoding="utf-8"))
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), layout="constrained")

    for ax, track, color in zip(axes, ("P-clinical", "P-explicit"), (OKABE["blue"], OKABE["orange"])):
        vals = hospitals.loc[hospitals.track == track, "iauroc"].dropna()
        bp = ax.boxplot([vals], widths=0.55, patch_artist=True, showfliers=True,
                        medianprops=dict(color="black", lw=1.2),
                        flierprops=dict(marker="o", ms=2.5, markerfacecolor=color, markeredgecolor="none"))
        bp["boxes"][0].set_facecolor(color)
        bp["boxes"][0].set_alpha(0.55)
        ax.scatter(np.random.default_rng(20260730).normal(1, 0.05, len(vals)), vals,
                   s=5, color=OKABE["black"], alpha=0.35, linewidths=0)
        ax.axhline(0.5, color=OKABE["grey"], lw=0.7, ls=":")
        ax.set_xticks([1])
        ax.set_xticklabels([track])
        ax.set_ylim(0.3, 1.0)
    axes[0].set_ylabel("Hospital-level iAUROC")
    axes[0].set_title("Hospital distribution")

    ax = axes[1]
    for track, color, marker in (("P-clinical", OKABE["blue"], "o"), ("P-explicit", OKABE["orange"], "s")):
        groups = density[track]["groups"]
        order = ["\u4f4e", "\u4e2d", "\u9ad8"]
        x = np.arange(3)
        m = [groups[g]["iauroc"] for g in order]
        lo = [groups[g]["ci95"][1] for g in order]
        hi = [groups[g]["ci95"][2] for g in order]
        ax.errorbar(x, m, yerr=[np.array(m) - np.array(lo), np.array(hi) - np.array(m)],
                    marker=marker, ms=4, ls="-", color=color, capsize=3, lw=1.2, label=track)
    ax.set_xticks(x)
    ax.set_xticklabels(["Low\n(most missing)", "Medium", "High\n(least missing)"])
    ax.set_ylabel("iAUROC")
    ax.set_ylim(0.55, 0.85)
    ax.set_title("By observation density")
    ax.legend(frameon=False)
    export(fig, "fig8_eicu")


if __name__ == "__main__":
    fig2_paired_primary()
    fig3_deployment()
    fig4_effect_shape()
    fig5_ablations()
    fig6_calibration()
    fig7_dca()
    fig8_eicu()
