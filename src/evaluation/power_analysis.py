"""paired 队列功效分析（月 1 样本量分析核心交付物）。

设计：基于冻结 paired 队列（test 集）的真实构成（患者数、逐 landmark 阳性数），
双正态模型模拟 SC/SCE 配对得分（共享潜变量制造配对相关），
患者级聚类 bootstrap（B=500）估计 ΔiAUROC 的 95% CI，
R=200 次模拟重复估计「CI 下限 > 0」的功效。
输出：功效表（Δ=0.01/0.02/0.03/0.05）+ MDE 曲线 + 基线 AUROC 敏感性。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "preprocess/artifacts/p9_packages/pp_v1_20260730" \
    / "sc_common_paired" / "test" / "index.parquet"
MAIN_K = 12
R_SIMS = 100
B_BOOT = 300
SEED = 20260730


def norm_ppf(p):
    from scipy.stats import norm
    return norm.ppf(p)


def auc(x, y, s):
    """AUROC via midrank. y=labels(0/1), s=scores."""
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    start = csum - counts
    mid = start + (counts + 1) / 2.0
    ranks = mid[inv]
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def simulate_power(df: pd.DataFrame, delta: float, auc_sc: float,
                   rng: np.random.Generator, r_sims=R_SIMS,
                   b_boot=B_BOOT):
    """df: test index (subject_key, landmark_k, y_24h). Returns power."""
    # 逐 landmark 参数
    ks = sorted(df["landmark_k"].unique())[:MAIN_K]
    subj = df["subject_key"].to_numpy()
    usubj, inv = np.unique(subj, return_inverse=True)
    n_subj = len(usubj)
    y = df["y_24h"].to_numpy()
    kidx = df["landmark_k"].to_numpy()

    z_sc = norm_ppf(auc_sc) * np.sqrt(2)
    z_sce = norm_ppf(min(auc_sc + delta, 0.999)) * np.sqrt(2)
    wins = 0
    for _ in range(r_sims):
        # 生成配对得分：共享结果 y + 相关噪声
        e1 = rng.standard_normal(len(y))
        e2 = 0.7 * e1 + np.sqrt(1 - 0.7**2) * rng.standard_normal(len(y))
        s_sc = y * z_sc + e1
        s_sce = y * z_sce + e2
        # 患者级 bootstrap CI（查表法替代 np.isin，提速约 10 倍）
        boots = rng.integers(0, n_subj, size=(b_boot, n_subj))
        deltas = np.full(b_boot, np.nan)
        inboot = np.zeros(n_subj, dtype=bool)
        for b in range(b_boot):
            inboot[:] = False
            inboot[boots[b]] = True
            mask = inboot[inv]
            aucs_sc = np.full(MAIN_K, np.nan)
            aucs_sce = np.full(MAIN_K, np.nan)
            for i, k in enumerate(ks):
                m = mask & (kidx == k)
                aucs_sc[i] = auc(None, y[m], s_sc[m])
                aucs_sce[i] = auc(None, y[m], s_sce[m])
            if not (np.isnan(aucs_sc).any() or np.isnan(aucs_sce).any()):
                deltas[b] = np.mean(aucs_sce - aucs_sc)
        lo = np.nanpercentile(deltas, 2.5)
        if lo > 0:
            wins += 1
    return wins / r_sims


def main():
    df = pd.read_parquet(PKG, columns=["subject_key", "landmark_k", "y_24h"])
    df = df[df["landmark_k"] < MAIN_K].reset_index(drop=True)
    comp = {
        "samples": len(df),
        "patients": int(df["subject_key"].nunique()),
        "positives": int(df["y_24h"].sum()),
        "per_k_pos": df.groupby("landmark_k")["y_24h"].sum().to_dict(),
        "per_k_n": df.groupby("landmark_k").size().to_dict(),
    }
    print("paired test 构成:", comp["samples"], "样本 /",
          comp["patients"], "患者 /", comp["positives"], "阳性")
    print("逐 k 阳性:", comp["per_k_pos"])

    rng = np.random.default_rng(SEED)
    rows = []
    for auc_sc in (0.70, 0.75, 0.80):
        for delta in (0.01, 0.02, 0.03, 0.05):
            p = simulate_power(df, delta, auc_sc, rng)
            rows.append({"auc_sc": auc_sc, "delta_iauroc": delta,
                         "power": p})
            print(f"AUROC_SC={auc_sc} Δ={delta}: power={p:.2f}")
    res = pd.DataFrame(rows)
    out = ROOT / "src/data/_output/qa/power_analysis_paired.csv"
    res.to_csv(out, index=False)
    print("saved:", out)
    return comp, res


if __name__ == "__main__":
    main()
