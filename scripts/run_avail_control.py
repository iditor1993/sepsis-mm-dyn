# -*- coding: utf-8 -*-
"""Availability-only control model (SC + ECG-availability indicator).

Reviewer-driven analysis: the deployment comparison (SCE-deployment vs
SC-common-all) is not paired and may be confounded by ECG availability
itself.  This script trains a clinical-only GRU-D that additionally receives
the binary ECG-availability indicator (ecg_selected_for_model, i.e. a 24-h
freshness + two-layer QC ECG is available at the landmark) on the full
deployment cohort, using exactly the same hyper-parameters, sample weights,
and per-seed RNG seeds as the frozen SC-common-all model.

Outputs:
    src/models/runs/avail_control/REPORT.md
    src/models/runs/avail_control/result.json
    src/models/runs/avail_control/grud/seed_*/{model.pt,result.json,predictions.npz}

The analysis is post hoc (revision-added) and is reported as exploratory in
the manuscript.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models.data.dataset import ART, RID, ClinicalDataset  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.heads import SCModel  # noqa: E402
from src.models.train.metrics import landmark_metrics, predict  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
PKG = ART / "p9_packages" / RID / "sc_avail_common_all"
SRC_PKG = ART / "p9_packages" / RID / "sc_common_all"
DEP_PKG = ART / "p9_packages" / RID / "sce_deployment"
TENSOR_DIR = ART / "p7_fitted" / RID
RAW_DIR = ART / "p2_clinical" / RID / "master"
RUNS = ROOT / "src" / "models" / "runs"
OUT = RUNS / "avail_control"
MAIN_K = 12
N_BOOT = 2000
BOOT_SEED = 20260730
SEEDS = [1, 2, 3, 4, 5]


class ClinicalAvailDataset(ClinicalDataset):
    """ClinicalDataset whose static.npy already carries the availability
    indicator as the last column (static dim = 45)."""

    def __init__(self, pkg, tensor_dir, raw_tensor_dir=None):
        super().__init__(pkg, tensor_dir, raw_tensor_dir)
        self.static = np.load(Path(pkg) / "static.npy", mmap_mode="r")


def build_packages():
    """Create sc_avail_common_all from sc_common_all + availability flag."""
    for split in ("train", "validation", "test"):
        src = SRC_PKG / split
        dep = DEP_PKG / split
        dst = PKG / split
        if dst.exists() and (dst / "static.npy").exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        idx = pd.read_parquet(src / "index.parquet")
        dep_idx = pd.read_parquet(dep / "index.parquet")
        assert (idx["row_idx"].to_numpy()
                == dep_idx["row_idx"].to_numpy()).all(), \
            f"row mismatch in {split}"
        idx = idx.copy()
        idx["ecg_available"] = dep_idx["ecg_selected_for_model"].fillna(
            False).astype(bool)
        idx.to_parquet(dst / "index.parquet", index=False)
        static = np.load(src / "static.npy", mmap_mode="r")
        flag = idx["ecg_available"].to_numpy(dtype=np.float32).reshape(-1, 1)
        static_ext = np.hstack([np.asarray(static), flag])
        np.save(dst / "static.npy", static_ext.astype(np.float32))
        shutil.copy2(src / "manifest.json", dst / "manifest.json")
        print(f"[avail] package {split}: {idx.shape} "
              f"ecg_available={int(idx['ecg_available'].sum())}", flush=True)


def get_rng_seed(seed: int) -> int:
    """Reuse the exact RNG seed of the frozen sc_common_all/grud run so the
    control model differs only by the added availability feature."""
    p = RUNS / "sc_common_all" / "grud" / f"seed_{seed}" / "result.json"
    if not p.exists():
        raise FileNotFoundError(p)
    r = json.loads(p.read_text(encoding="utf-8"))
    return int(r["rng_seed"])


def train_seed(seed: int):
    node = f"grud/sc_avail_common_all/seed_{seed}"
    rng_seed = get_rng_seed(seed)
    torch.manual_seed(rng_seed)
    np.random.seed(rng_seed)
    print(f"[avail] {node} rng_seed={rng_seed} device={DEVICE}", flush=True)

    tr_ds = ClinicalAvailDataset(PKG / "train", TENSOR_DIR, RAW_DIR)
    va_ds = ClinicalAvailDataset(PKG / "validation", TENSOR_DIR, RAW_DIR)
    bs = 512
    tr_ld = DataLoader(tr_ds, batch_size=bs, shuffle=True,
                       num_workers=0, pin_memory=True)
    va_ld = DataLoader(va_ds, batch_size=bs, shuffle=False,
                       num_workers=0, pin_memory=True)

    ch_params = json.loads(
        (TENSOR_DIR / "scaler_clinical_seq.json").read_text(encoding="utf-8"))
    channels = json.loads(
        (PKG / "train" / "manifest.json").read_text(encoding="utf-8")
    )["channels"]
    means = torch.tensor([ch_params["channels"][c]["mean"]
                          for c in channels], dtype=torch.float32)
    static_dim = tr_ds.static.shape[1]
    enc = GRUDEncoder(17, 128, static_dim, 64, means)
    model = SCModel(enc, 128 + 64)
    model.to(DEVICE)

    lr = 1e-3
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    p = float(tr_ds.idx["y_24h"].mean())
    pw = (1 - p) / max(p, 1e-9)
    bce = torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(pw, device=DEVICE), reduction="none")
    patience, max_epochs = 10, 100
    best_ia, best_state, bad = -1.0, None, 0
    for ep in range(1, max_epochs + 1):
        model.train()
        t0 = time.time()
        tot, wsum = 0.0, 0.0
        for batch in tr_ld:
            x = batch["x"].to(DEVICE, non_blocking=True)
            m = batch["m"].to(DEVICE, non_blocking=True)
            d = batch["d"].to(DEVICE, non_blocking=True)
            st = batch["static"].to(DEVICE, non_blocking=True)
            y = batch["y"].to(DEVICE, non_blocking=True)
            w = batch["w"].to(DEVICE, non_blocking=True)
            s = model(x, m, d, st)
            loss = (bce(s, y) * w).sum() / w.sum().clamp(min=1e-9)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * float(w.sum())
            wsum += float(w.sum())
        vy, vs, vk = predict(model, va_ld, DEVICE, ecg=False)
        met = landmark_metrics(vy, vs, vk)
        ia = met["iauroc_partial"] or 0.0
        print(f"[avail] ep{ep} loss={tot / max(wsum, 1e-9):.4f} "
              f"val_iAUROC={ia:.4f} ({time.time() - t0:.0f}s)", flush=True)
        if ia > best_ia:
            best_ia, bad = ia, 0
            best_state = {k: v.detach().clone()
                          for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                print(f"[avail] early stop at ep{ep} best={best_ia:.4f}",
                      flush=True)
                break

    out_dir = OUT / "grud" / f"seed_{seed}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if best_state is not None:
        model.load_state_dict(best_state)
    torch.save(model.state_dict(), out_dir / "model.pt")

    te_ds = ClinicalAvailDataset(PKG / "test", TENSOR_DIR, RAW_DIR)
    te_ld = DataLoader(te_ds, batch_size=bs, shuffle=False, num_workers=0)
    ty, ts, tk = predict(model, te_ld, DEVICE, ecg=False)
    tmet = landmark_metrics(ty, ts, tk)
    result = {"node": node, "rng_seed": rng_seed,
              "best_val_iauroc": best_ia, "test": tmet,
              "pos_weight": pw}
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")
    np.savez_compressed(
        out_dir / "predictions.npz",
        subject_key=te_ds.idx["subject_key"].to_numpy(),
        landmark_k=tk, y_24h=ty, y_score=ts)
    print(f"[avail] test iAUROC={tmet['iauroc']} "
          f"({tmet['n_estimable']}/12) -> {out_dir}", flush=True)
    return result


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


def load_deployment_scores(pkg, model):
    """Return ensemble mean scores for a package/model across existing seeds."""
    scores = []
    for sd in SEEDS:
        p = RUNS / pkg / model / f"seed_{sd}" / "predictions.npz"
        if not p.exists():
            return None
        z = np.load(p, allow_pickle=True)
        scores.append(z["y_score"])
    return np.mean(scores, axis=0)


def aggregate():
    idx = pd.read_parquet(PKG / "test" / "index.parquet")
    dep_idx = pd.read_parquet(DEP_PKG / "test" / "index.parquet")
    assert (idx["row_idx"].to_numpy() == dep_idx["row_idx"].to_numpy()).all()
    df = idx[["episode_key", "subject_key", "landmark_k", "y_24h"]].copy()
    df["ecg_available"] = dep_idx["ecg_selected_for_model"].fillna(
        False).astype(bool).to_numpy()
    y = df["y_24h"].to_numpy()
    k = df["landmark_k"].to_numpy()
    avail = df["ecg_available"].to_numpy()

    s_avail = load_deployment_scores("avail_control", "grud")
    s_sc = load_deployment_scores("sc_common_all", "grud")
    s_sce = load_deployment_scores("sce_deployment", "sce_grud")
    if s_avail is None:
        print("[avail] control predictions not complete; skip aggregate")
        return

    ia_avail, _ = _iauroc(y, s_avail, k)
    ia_sc, _ = _iauroc(y, s_sc, k)
    ia_sce, _ = _iauroc(y, s_sce, k)
    # route = SCE where ECG available, else SC (unchanged deployment route)
    s_route = np.where(avail, s_sce, s_sc)
    ia_route, _ = _iauroc(y, s_route, k)
    # availability-control route = control where ECG available, else SC
    s_route_ctrl = np.where(avail, s_avail, s_sc)
    ia_route_ctrl, _ = _iauroc(y, s_route_ctrl, k)

    # ECG-available subset (primary grid) comparisons
    m_avail = avail & (k < MAIN_K)
    ia_sc_av, _ = _iauroc(y[m_avail], s_sc[m_avail], k[m_avail])
    ia_sce_av, _ = _iauroc(y[m_avail], s_sce[m_avail], k[m_avail])
    ia_avail_av, _ = _iauroc(y[m_avail], s_avail[m_avail], k[m_avail])

    # Per-seed deltas (control - SC, SCE - SC, SCE - control)
    per_seed = []
    for sd in SEEDS:
        pa = RUNS / "avail_control" / "grud" / f"seed_{sd}" / "predictions.npz"
        psc = RUNS / "sc_common_all" / "grud" / f"seed_{sd}" / "predictions.npz"
        psce = RUNS / "sce_deployment" / "sce_grud" / f"seed_{sd}" / "predictions.npz"
        if not (pa.exists() and psc.exists() and psce.exists()):
            continue
        za, zsc, zsce = (np.load(p, allow_pickle=True)
                         for p in (pa, psc, psce))
        ia_a, _ = _iauroc(za["y_24h"], za["y_score"], za["landmark_k"])
        ia_c, _ = _iauroc(zsc["y_24h"], zsc["y_score"], zsc["landmark_k"])
        ia_e, _ = _iauroc(zsce["y_24h"], zsce["y_score"], zsce["landmark_k"])
        per_seed.append({"seed": sd, "sc": ia_c, "control": ia_a,
                         "sce": ia_e,
                         "delta_control_vs_sc": ia_a - ia_c,
                         "delta_sce_vs_sc": ia_e - ia_c,
                         "delta_sce_vs_control": ia_e - ia_a})

    subj = df["subject_key"].to_numpy()
    usubj, inv = np.unique(subj, return_inverse=True)
    n_u = len(usubj)
    rng = np.random.default_rng(BOOT_SEED)
    d_ctrl = np.full(N_BOOT, np.nan)   # avail-control minus SC
    d_sce = np.full(N_BOOT, np.nan)    # SCE minus SC
    d_wave = np.full(N_BOOT, np.nan)   # SCE minus control (residual waveform)
    d_wave_av = np.full(N_BOOT, np.nan)  # SCE minus control, ECG-available grid
    inb = np.zeros(n_u, dtype=bool)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n_u, n_u)] = True
        m = inb[inv]
        a_sc, _ = _iauroc(y[m], s_sc[m], k[m])
        a_ctrl, _ = _iauroc(y[m], s_avail[m], k[m])
        a_sce, _ = _iauroc(y[m], s_sce[m], k[m])
        av_m = m & m_avail
        a_ctrl_av, _ = _iauroc(y[av_m], s_avail[av_m], k[av_m])
        a_sce_av, _ = _iauroc(y[av_m], s_sce[av_m], k[av_m])
        if not (np.isnan(a_sc) or np.isnan(a_ctrl) or np.isnan(a_sce)):
            d_ctrl[b] = a_ctrl - a_sc
            d_sce[b] = a_sce - a_sc
            d_wave[b] = a_sce - a_ctrl
            if not (np.isnan(a_ctrl_av) or np.isnan(a_sce_av)):
                d_wave_av[b] = a_sce_av - a_ctrl_av

    ps = pd.DataFrame(per_seed)
    ps.to_csv(OUT / "per_seed.csv", index=False)

    result = {
        "per_seed": per_seed,
        "per_seed_mean": {
            "delta_control_vs_sc": float(ps["delta_control_vs_sc"].mean()),
            "delta_control_vs_sc_sd": float(ps["delta_control_vs_sc"].std()),
            "delta_sce_vs_sc": float(ps["delta_sce_vs_sc"].mean()),
            "delta_sce_vs_control": float(ps["delta_sce_vs_control"].mean()),
        },
        "ensemble": {
            "sc_common_all": ia_sc,
            "sc_avail_control": ia_avail,
            "sce_deployment": ia_sce,
            "delta_control_vs_sc": ia_avail - ia_sc,
            "delta_sce_vs_sc": ia_sce - ia_sc,
            "residual_waveform_signal":
                (ia_sce - ia_sc) - (ia_avail - ia_sc),
            "deployment_route": ia_route,
            "avail_control_route": ia_route_ctrl,
            "ecg_available_subset_primary_grid": {
                "n": int(m_avail.sum()),
                "sc": ia_sc_av, "sce": ia_sce_av,
                "sc_avail_control": ia_avail_av,
                "delta_sce_vs_sc": ia_sce_av - ia_sc_av,
                "delta_control_vs_sc": ia_avail_av - ia_sc_av,
            },
            "n_test": int(len(df)), "n_positive": int(y.sum()),
            "n_ecg_available": int(avail.sum()),
        },
        "delta_control_vs_sc_bootstrap_ci95": (
            float(np.nanpercentile(d_ctrl, 2.5)),
            float(np.nanpercentile(d_ctrl, 97.5))),
        "delta_sce_vs_sc_bootstrap_ci95": (
            float(np.nanpercentile(d_sce, 2.5)),
            float(np.nanpercentile(d_sce, 97.5))),
        "delta_sce_vs_control_bootstrap_ci95": (
            float(np.nanpercentile(d_wave, 2.5)),
            float(np.nanpercentile(d_wave, 97.5))),
        "delta_sce_vs_control_ecg_available_bootstrap_ci95": (
            float(np.nanpercentile(d_wave_av, 2.5)),
            float(np.nanpercentile(d_wave_av, 97.5))),
        "bootstrap_valid": int(np.isfinite(d_ctrl).sum()),
    }
    (OUT / "result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")

    e = result["ensemble"]
    lines = [
        "# Availability-only control model (SC + ECG-availability indicator)",
        "",
        "Post hoc, revision-added analysis on the full deployment cohort. "
        "The control model is a clinical-only GRU-D with the binary "
        "ECG-availability indicator (`ecg_selected_for_model`, 24-h "
        "freshness + two-layer QC) added to the static features, trained "
        "with the same hyper-parameters, weights, and per-seed RNG as the "
        "frozen SC-common-all model.",
        "",
        f"Test set: {e['n_test']:,} landmarks, {e['n_positive']:,} positive, "
        f"{e['n_ecg_available']:,} ECG-available.",
        "",
        "## Ensemble iAUROC (primary grid k=0-11)",
        "",
        "| Model | iAUROC |",
        "|---|---|",
        f"| SC-common-all | {ia_sc:.4f} |",
        f"| SC + availability indicator | {ia_avail:.4f} |",
        f"| SCE-deployment | {ia_sce:.4f} |",
        "",
        f"- Δ(control − SC) = {ia_avail - ia_sc:+.4f} "
        f"(95% CI {result['delta_control_vs_sc_bootstrap_ci95'][0]:+.4f} to "
        f"{result['delta_control_vs_sc_bootstrap_ci95'][1]:+.4f})",
        f"- Δ(SCE − SC) = {ia_sce - ia_sc:+.4f} "
        f"(95% CI {result['delta_sce_vs_sc_bootstrap_ci95'][0]:+.4f} to "
        f"{result['delta_sce_vs_sc_bootstrap_ci95'][1]:+.4f})",
        f"- Δ(SCE − control) = {ia_sce - ia_avail:+.4f} "
        f"(95% CI "
        f"{result['delta_sce_vs_control_bootstrap_ci95'][0]:+.4f} to "
        f"{result['delta_sce_vs_control_bootstrap_ci95'][1]:+.4f}) = "
        f"residual waveform signal beyond availability.",
        f"- Per-seed Δ(control − SC) mean ± SD = "
        f"{result['per_seed_mean']['delta_control_vs_sc']:+.4f} ± "
        f"{result['per_seed_mean']['delta_control_vs_sc_sd']:.4f}.",
        "",
        "## Deployment route",
        "",
        f"- Route with SCE where ECG available: {ia_route:.4f}",
        f"- Route with control where ECG available: {ia_route_ctrl:.4f}",
        "",
        "## ECG-available subset (primary grid)",
        "",
        f"- n = {e['ecg_available_subset_primary_grid']['n']:,}",
        f"- SC = {ia_sc_av:.4f}; control = {ia_avail_av:.4f}; "
        f"SCE = {ia_sce_av:.4f}",
        f"- Δ(control − SC) = {ia_avail_av - ia_sc_av:+.4f}",
        f"- Δ(SCE − SC) = {ia_sce_av - ia_sc_av:+.4f}",
        f"- Δ(SCE − control) = {ia_sce_av - ia_avail_av:+.4f} "
        f"(95% CI "
        f"{result['delta_sce_vs_control_ecg_available_bootstrap_ci95'][0]:+.4f}"
        f" to "
        f"{result['delta_sce_vs_control_ecg_available_bootstrap_ci95'][1]:+.4f}"
        f").",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[avail] aggregate written to {OUT / 'REPORT.md'}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="full",
                    choices=["smoke", "full", "aggregate_only"])
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    build_packages()
    if args.mode != "aggregate_only":
        seeds = [args.seed] if args.seed else SEEDS
        for sd in seeds:
            t0 = time.time()
            try:
                train_seed(sd)
                print(f"[avail] seed {sd} done in {time.time() - t0:.0f}s",
                      flush=True)
            except Exception as e:
                print(f"[avail] seed {sd} FAILED: {e}", flush=True)
                raise
    aggregate()


if __name__ == "__main__":
    main()
