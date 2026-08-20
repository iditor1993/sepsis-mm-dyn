"""部署队列分析（技术文档 §1.2 关键次要分析 / §7.1 概念 A）。

全体部署队列（不要求 ECG）：SC-common-all（无 ECG 分支）vs SCE-deployment
（modality dropout + availability embedding），并评估部署策略
（有 ECG 走 SCE、无 ECG 走 SC）的系统级性能。

两阶段：
  阶段 1 训练：grud/sc_common_all 与 sce_grud/sce_deployment × SEEDS
  阶段 2 聚合：逐 seed iAUROC、ΔiAUROC + 患者级 bootstrap 95% CI、
               部署策略 iAUROC、ECG 可用性分层、逐 landmark 表

VSCode 用法：改下面 CONFIG 区 → 右上角 ▶ Run 或 F5。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ============================ CONFIG（只改这里） ============================
MODE = "full"        # smoke / quick / full / aggregate_only
SEEDS = [1, 2, 3, 4, 5]
JOBS = [("grud", "sc_common_all"),
        ("sce_grud", "sce_deployment")]
# ===========================================================================

ROOT2 = Path(__file__).resolve().parents[1]
RUNS = ROOT2 / "src" / "models" / "runs"
OUT = RUNS / "deployment"
OUT.mkdir(parents=True, exist_ok=True)
PKGS = ROOT2 / "preprocess" / "artifacts" / "p9_packages" / "pp_v1_20260730"
MOD_IDX = (ROOT2 / "preprocess" / "artifacts" / "p6_modality"
           / "pp_v1_20260730" / "modality_index.parquet")
MAIN_K = 12
N_BOOT = 2000


def train_all(mode: str):
    from src.models.train.train import train_one

    class A:
        pass

    combos = []
    if mode == "smoke":
        combos = [("grud", "sc_common_all", 1)]
    elif mode == "quick":
        combos = [(m, p, 1) for m, p in JOBS]
    elif mode == "full":
        combos = [(m, p, s) for m, p in JOBS for s in SEEDS]
    print(f"[deploy] mode={mode}, {len(combos)} 个训练任务")
    failures = []
    for i, (m, p, s) in enumerate(combos, 1):
        a = A()
        a.model, a.pkg, a.seed = m, p, s
        print(f"\n[deploy] ===== ({i}/{len(combos)}) {m}/{p}/seed_{s} =====")
        t0 = time.time()
        try:
            train_one(a)
            print(f"[deploy] done in {time.time() - t0:.0f}s")
        except Exception as e:
            print(f"[deploy] FAILED {m}/{p}/seed_{s}: {e}")
            failures.append((m, p, s, str(e)))
    return failures


# ---------------- 指标 ----------------

def _auroc(y, s):
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    mid = csum - counts + (counts + 1) / 2.0
    ranks = mid[inv]
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def _iauroc(y, s, k):
    aucs = [_auroc(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan, aucs
    return float(np.mean(aucs)), aucs


def _load(model, pkg, seed):
    base = RUNS / pkg / model / f"seed_{seed}"
    if not (base / "predictions.npz").exists():
        return None
    idx = pd.read_parquet(PKGS / pkg / "test" / "index.parquet",
                          columns=["episode_key", "subject_key", "landmark_k"])
    z = np.load(base / "predictions.npz", allow_pickle=True)
    df = idx.copy()
    df["y"] = z["y_24h"]
    df["s"] = z["y_score"]
    return df


def _ecg_availability(df):
    mod = pd.read_parquet(
        MOD_IDX, columns=["episode_key", "landmark_k",
                          "ecg_selected_for_model"])
    df = df.merge(mod, on=["episode_key", "landmark_k"], how="left")
    df["ecg_available"] = df["ecg_selected_for_model"].fillna(False)
    return df


def aggregate(rng):
    seeds = [s for s in SEEDS
             if _load(JOBS[0][0], JOBS[0][1], s) is not None
             and _load(JOBS[1][0], JOBS[1][1], s) is not None]
    if not seeds:
        print("[deploy] 尚无配对预测，先运行训练模式")
        return
    rows, per_seed = [], []
    for sd in seeds:
        sc = _load(JOBS[0][0], JOBS[0][1], sd)
        sce = _load(JOBS[1][0], JOBS[1][1], sd)
        ia_sc, _ = _iauroc(sc["y"].to_numpy(), sc["s"].to_numpy(),
                           sc["landmark_k"].to_numpy())
        ia_sce, _ = _iauroc(sce["y"].to_numpy(), sce["s"].to_numpy(),
                            sce["landmark_k"].to_numpy())
        per_seed.append({"seed": sd, "sc_all": ia_sc,
                         "sce_deployment": ia_sce,
                         "delta": ia_sce - ia_sc})
    ps = pd.DataFrame(per_seed)

    # 集成分数
    sc = _load(JOBS[0][0], JOBS[0][1], seeds[0])
    sce_s = np.mean([_load(JOBS[1][0], JOBS[1][1], sd)["s"].to_numpy()
                     for sd in seeds], axis=0)
    sc_s = np.mean([_load(JOBS[0][0], JOBS[0][1], sd)["s"].to_numpy()
                    for sd in seeds], axis=0)
    df = sc[["episode_key", "subject_key", "landmark_k", "y"]].copy()
    df["s_sc"] = sc_s
    df["s_sce"] = sce_s
    df = _ecg_availability(df)

    y = df["y"].to_numpy()
    k = df["landmark_k"].to_numpy()
    s_sc = df["s_sc"].to_numpy()
    s_sce = df["s_sce"].to_numpy()
    avail = df["ecg_available"].to_numpy()

    ia_sc, aucs_sc = _iauroc(y, s_sc, k)
    ia_sce, aucs_sce = _iauroc(y, s_sce, k)
    # 部署策略：有 ECG 走 SCE，无 ECG 走 SC
    s_route = np.where(avail, s_sce, s_sc)
    ia_route, aucs_route = _iauroc(y, s_route, k)
    # ECG 分层
    ia_sc_av, _ = _iauroc(y[avail], s_sc[avail], k[avail])
    ia_sce_av, _ = _iauroc(y[avail], s_sce[avail], k[avail])
    ia_sc_na, _ = _iauroc(y[~avail], s_sc[~avail], k[~avail])

    # 患者级 bootstrap：ΔiAUROC 与部署策略增益
    subj = df["subject_key"].to_numpy()
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    d_delta = np.full(N_BOOT, np.nan)
    d_route = np.full(N_BOOT, np.nan)
    inb = np.zeros(n, dtype=bool)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        a_sc, _ = _iauroc(y[m], s_sc[m], k[m])
        a_sce, _ = _iauroc(y[m], s_sce[m], k[m])
        a_rt, _ = _iauroc(y[m], s_route[m], k[m])
        if not (np.isnan(a_sc) or np.isnan(a_sce) or np.isnan(a_rt)):
            d_delta[b] = a_sce - a_sc
            d_route[b] = a_rt - a_sc
    ci_delta = (float(np.nanpercentile(d_delta, 2.5)),
                float(np.nanpercentile(d_delta, 97.5)))
    ci_route = (float(np.nanpercentile(d_route, 2.5)),
                float(np.nanpercentile(d_route, 97.5)))
    valid = int(np.isfinite(d_delta).sum())

    result = {
        "seeds": per_seed,
        "per_seed_delta_mean": float(ps["delta"].mean()),
        "per_seed_delta_sd": float(ps["delta"].std()),
        "ensemble": {
            "sc_common_all": ia_sc, "sce_deployment": ia_sce,
            "delta_iauroc": ia_sce - ia_sc,
            "deployment_route": ia_route,
            "route_gain_vs_sc": ia_route - ia_sc,
            "ecg_available_subset": {"sc": ia_sc_av, "sce": ia_sce_av,
                                     "delta": ia_sce_av - ia_sc_av,
                                     "n": int(avail.sum())},
            "ecg_unavailable_subset": {"sc": ia_sc_na,
                                       "n": int((~avail).sum())},
        },
        "delta_bootstrap_ci95": ci_delta,
        "route_bootstrap_ci95": ci_route,
        "bootstrap_valid": valid,
        "n_test": int(len(df)),
        "n_positive": int(y.sum()),
        "auroc_per_landmark": {"sc": aucs_sc, "sce": aucs_sce,
                               "route": aucs_route},
    }
    (OUT / "deployment_result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")

    lines = ["# 部署队列分析报告（全体部署队列，关键次要分析）",
             "",
             f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
             f"测试集 {result['n_test']:,} landmark / 阳性 {result['n_positive']:,}",
             "",
             "## 逐 seed",
             "",
             "| seed | SC-common-all | SCE-deployment | Δ |",
             "|---|---|---|---|"]
    for r in per_seed:
        lines.append(f"| {r['seed']} | {r['sc_all']:.4f} | "
                     f"{r['sce_deployment']:.4f} | {r['delta']:+.4f} |")
    e = result["ensemble"]
    lines += [
        "",
        f"**逐 seed Δ 均值±SD：{result['per_seed_delta_mean']:+.4f} ± {result['per_seed_delta_sd']:.4f}**",
        "",
        "## 集成分数（主结果）",
        "",
        f"- SC-common-all iAUROC = {e['sc_common_all']:.4f}",
        f"- SCE-deployment iAUROC = {e['sce_deployment']:.4f}",
        f"- **ΔiAUROC = {e['delta_iauroc']:+.4f}，95% CI [{ci_delta[0]:+.4f}, {ci_delta[1]:+.4f}]**（有效重复 {valid}/2000）",
        f"- **部署策略（有 ECG 走 SCE / 无 ECG 走 SC）iAUROC = {e['deployment_route']:.4f}，相对 SC 增益 {e['route_gain_vs_sc']:+.4f}，95% CI [{ci_route[0]:+.4f}, {ci_route[1]:+.4f}]**",
        "",
        "## ECG 可用性分层",
        "",
        f"- ECG-available（n={e['ecg_available_subset']['n']:,}）：SC={e['ecg_available_subset']['sc']:.4f} / SCE={e['ecg_available_subset']['sce']:.4f} / Δ={e['ecg_available_subset']['delta']:+.4f}",
        f"- ECG-unavailable（n={e['ecg_unavailable_subset']['n']:,}）：SC={e['ecg_unavailable_subset']['sc']:.4f}",
        "",
        "逐 landmark AUROC：",
        "",
        "| k | SC-all | SCE-deployment | 部署策略 |",
        "|---|---|---|---|"]
    for kk in range(MAIN_K):
        lines.append(f"| {kk} | {aucs_sc[kk]:.3f} | {aucs_sce[kk]:.3f} | {aucs_route[kk]:.3f} |")
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[deploy] 报告已写入 {OUT / 'REPORT.md'}")
    print(f"[deploy] ΔiAUROC={e['delta_iauroc']:+.4f} CI[{ci_delta[0]:+.4f},{ci_delta[1]:+.4f}]")
    print(f"[deploy] 部署策略 iAUROC={e['deployment_route']:.4f} 增益 {e['route_gain_vs_sc']:+.4f}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=MODE,
                    choices=["smoke", "quick", "full", "aggregate_only"])
    args = ap.parse_args()
    rng = np.random.default_rng(20260730)
    failures = []
    if args.mode != "aggregate_only":
        failures = train_all(args.mode)
    if failures:
        print("[deploy] 失败任务：", failures)
    aggregate(rng)


if __name__ == "__main__":
    main()
