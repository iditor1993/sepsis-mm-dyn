"""SEPSIS-MM-DYN 一键训练 + 聚合 + 报告（VSCode 直接运行）。

用法（VSCode）：打开本文件 → 右上角 ▶ Run Python File；或 F5 选 "Run Experiment"。
用法（终端）：python scripts/run_experiment.py [--mode quick]

只改下面 CONFIG 区即可切换组合；规则与《模型训练方案 v1.1》一致，不做任何网格搜索。
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
MODE = "full"          # smoke / quick / clinical / full / aggregate_only
SEEDS = [1, 2, 3, 4, 5]
MODELS = [              # (model, package) 组合；可选 grud / tpc / sce_grud / sce_tpc
    ("grud", "sc_common_paired"),
    ("tpc", "sc_common_paired"),
    ("sce_grud", "sce_common_paired"),
]
MAIN_PAIR = (("grud", "sc_common_paired"),
             ("sce_grud", "sce_common_paired"))   # 主比较 SC vs SCE（镜像样本集）
# ===========================================================================

RUNS = ROOT / "src" / "models" / "runs"
MAIN_K = 12
N_BOOT = 2000


def run_training(mode: str):
    from src.models.train.train import train_one

    class A:
        pass

    combos = []
    if mode == "smoke":
        combos = [("grud", "sc_common_paired", 1)]
    elif mode == "quick":
        combos = [(m, p, 1) for m, p in MODELS]
    elif mode == "clinical":
        combos = [(m, p, s) for m, p in MODELS if not m.startswith("sce_")
                  for s in SEEDS]
    elif mode == "full":
        combos = [(m, p, s) for m, p in MODELS for s in SEEDS]

    print(f"[run] mode={mode}, {len(combos)} 个训练任务")
    failures = []
    for i, (m, p, s) in enumerate(combos, 1):
        a = A()
        a.model, a.pkg, a.seed = m, p, s
        print(f"\n[run] ===== ({i}/{len(combos)}) {m}/{p}/seed_{s} =====")
        t0 = time.time()
        try:
            train_one(a)
            print(f"[run] done in {time.time() - t0:.0f}s")
        except Exception as e:  # 单模型失败不中断其余
            print(f"[run] FAILED {m}/{p}/seed_{s}: {e}")
            failures.append((m, p, s, str(e)))
    return failures


# ---------------- 聚合 ----------------

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


def _load_pred(pkg, model, seed):
    p = RUNS / pkg / model / f"seed_{seed}" / "predictions.npz"
    if not p.exists():
        return None
    z = np.load(p, allow_pickle=True)
    return {k: z[k] for k in z.files}


def main_comparison(rng):
    """SC vs SCE paired：逐 seed ΔiAUROC + 均值预测患者级 bootstrap CI。"""
    (sc_m, sc_pkg), (sce_m, sce_pkg) = MAIN_PAIR
    seeds = []
    for sd in SEEDS:
        if (_load_pred(sc_pkg, sc_m, sd) is not None
                and _load_pred(sce_pkg, sce_m, sd) is not None):
            seeds.append(sd)
    if not seeds:
        return {"status": "no_paired_predictions"}
    per_seed, mean_scores = [], {}
    for sd in seeds:
        a = _load_pred(sc_pkg, sc_m, sd)
        b = _load_pred(sce_pkg, sce_m, sd)
        ia_a, _ = _iauroc(a["y_24h"], a["y_score"], a["landmark_k"])
        ia_b, _ = _iauroc(b["y_24h"], b["y_score"], b["landmark_k"])
        per_seed.append({"seed": sd, "sc": ia_a, "sce": ia_b,
                         "delta": ia_b - ia_a})
        mean_scores.setdefault("y", a["y_24h"])
        mean_scores.setdefault("k", a["landmark_k"])
        mean_scores.setdefault("subj", a["subject_key"])
        mean_scores.setdefault("sc", []).append(a["y_score"])
        mean_scores.setdefault("sce", []).append(b["y_score"])
    y = mean_scores["y"]
    k = mean_scores["k"]
    subj = mean_scores["subj"]
    s_sc = np.mean(mean_scores["sc"], axis=0)
    s_sce = np.mean(mean_scores["sce"], axis=0)

    ia_sc, aucs_sc = _iauroc(y, s_sc, k)
    ia_sce, aucs_sce = _iauroc(y, s_sce, k)
    usubj, inv = np.unique(subj, return_inverse=True)
    n_subj = len(usubj)
    deltas = np.full(N_BOOT, np.nan)
    inb = np.zeros(n_subj, dtype=bool)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n_subj, n_subj)] = True
        m = inb[inv]
        da, _ = _iauroc(y[m], s_sc[m], k[m])
        db, _ = _iauroc(y[m], s_sce[m], k[m])
        if not (np.isnan(da) or np.isnan(db)):
            deltas[b] = db - da
    ci = (float(np.nanpercentile(deltas, 2.5)),
          float(np.nanpercentile(deltas, 97.5)))
    return {
        "status": "ok",
        "n_seeds": len(seeds),
        "per_seed": per_seed,
        "mean_iauroc_sc": ia_sc, "mean_iauroc_sce": ia_sce,
        "delta_iauroc": ia_sce - ia_sc,
        "delta_per_seed_mean": float(np.mean([p["delta"] for p in per_seed])),
        "delta_per_seed_sd": float(np.std([p["delta"] for p in per_seed])),
        "bootstrap_ci95": ci,
        "ci_lower_gt0": bool(ci[0] > 0),
        "auroc_per_landmark": {"sc": aucs_sc, "sce": aucs_sce},
        "n_boot_valid": int(np.isfinite(deltas).sum()),
    }


def _md_table(df) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def aggregate(failures, rng):
    rows = []
    for p in sorted(RUNS.glob("*/*/seed_*/result.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        parts = p.parts
        rows.append({
            "pkg": parts[-4], "model": parts[-3], "seed": parts[-2],
            "best_val_iauroc": r["best_val_iauroc"],
            "test_iauroc": r["test"]["iauroc"],
            "test_iauroc_partial": r["test"]["iauroc_partial"],
            "test_brier": r["test"]["brier"],
            "n_estimable": r["test"]["n_estimable"],
            "pos_weight": r["pos_weight"],
        })
    df = pd.DataFrame(rows)
    if len(df):
        df.to_csv(RUNS / "summary.csv", index=False)
    comp = main_comparison(rng)

    lines = ["# SEPSIS-MM-DYN 训练结果报告",
             "",
             f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
             "",
             "## 1. 汇总（各模型 × seed）",
             "",
             _md_table(df) if len(df) else "（无结果）",
             "",
             "## 2. 唯一主要比较：SCE-common-paired vs SC-common-paired（ΔiAUROC）",
             ""]
    if comp.get("status") == "ok":
        lines += [
            f"- 参与 seed 数：{comp['n_seeds']}（聚合规则：先逐 seed 计算 Δ 再取均值）",
            f"- 逐 seed ΔiAUROC："
            + ", ".join(f"s{p['seed']}={p['delta']:+.4f}"
                        for p in comp["per_seed"]),
            f"- **ΔiAUROC（seed 均值±SD）：{comp['delta_per_seed_mean']:+.4f} ± {comp['delta_per_seed_sd']:.4f}**",
            f"- 集成分数 iAUROC：SC={comp['mean_iauroc_sc']:.4f} / SCE={comp['mean_iauroc_sce']:.4f}（Δ={comp['delta_iauroc']:+.4f}）",
            f"- **患者级 bootstrap 95% CI：[{comp['bootstrap_ci95'][0]:+.4f}, {comp['bootstrap_ci95'][1]:+.4f}]**（有效重复 {comp['n_boot_valid']}）",
            f"- **成功标准（CI 下限 > 0）：{'达成 ✅' if comp['ci_lower_gt0'] else '未达成（CI 跨 0）'}**",
            "",
            "逐 landmark AUROC（k=0..11）：",
            "",
            "| k | SC | SCE | Δ |",
            "|---|---|---|---|"]
        for kk in range(MAIN_K):
            a = comp["auroc_per_landmark"]["sc"][kk]
            b = comp["auroc_per_landmark"]["sce"][kk]
            lines.append(f"| {kk} | {a:.3f} | {b:.3f} | {b - a:+.3f} |")
    else:
        lines.append("（尚无配对的 SC/SCE 预测，先运行 quick/full 模式）")
    if failures:
        lines += ["", "## 3. 失败任务", "",
                  *[f"- {m}/{p}/seed_{s}: {e}" for m, p, s, e in failures]]
    (RUNS / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[aggregate] summary.csv + REPORT.md written to {RUNS}")
    return df, comp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=MODE,
                    choices=["smoke", "quick", "clinical", "full",
                             "aggregate_only"])
    args = ap.parse_args()
    rng = np.random.default_rng(20260730)
    failures = []
    if args.mode != "aggregate_only":
        failures = run_training(args.mode)
    aggregate(failures, rng)
    print("\n[run] 全部完成。结果目录：", RUNS)


if __name__ == "__main__":
    main()
