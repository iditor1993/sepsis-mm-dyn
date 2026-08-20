"""CV-SOFA≥3 亚组交互检验（技术文档 §10.4/§13.5 预登记）。

Interaction = ΔiAUROC(CV-SOFA≥3) − ΔiAUROC(CV-SOFA<3)
  ΔiAUROC(g) = iAUROC_SCE(g) − iAUROC_SC(g)，主网格 k∈[0,11] 等权
亚组规则：`sofa_realtime_strict_24h_cv` = strict_24h 轨、每 episode 首个有效
landmark（最小 k）的 SOFA 心血管评分；患者级固定，不随后续 landmark 变化；
组分缺失者不进入任何亚组（不回填）。
CI：患者级 bootstrap 2000 次（两亚组联合重采样），percentile 95%。
运行：python src/evaluation/cv_subgroup_interaction.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "src" / "models" / "runs"
OUT = RUNS / "cv_subgroup_interaction"
OUT.mkdir(parents=True, exist_ok=True)
SEEDS = [1, 2, 3, 4, 5]
MAIN_K = 12
N_BOOT = 2000
SC = ("grud", "sc_common_paired")
SCE = ("sce_grud", "sce_common_paired")


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
        return np.nan
    return float(np.mean(aucs))


def _load_cv_groups() -> pd.DataFrame:
    """每 episode 首个有效 landmark 的 CV 评分（strict_24h 轨）。"""
    sofa = pd.read_parquet(
        ROOT / "src/data/_output/features/sofa_hourly_v2.parquet",
        columns=["episode_id", "k", "sofa_evidence_track",
                 "cardiovascular_score", "cardiovascular_observed"])
    sofa = sofa[sofa["sofa_evidence_track"] == "strict_24h"]
    first = sofa.loc[sofa.groupby("episode_id")["k"].idxmin(),
                     ["episode_id", "cardiovascular_score",
                      "cardiovascular_observed"]]
    first = first.rename(columns={
        "episode_id": "episode_key",
        "cardiovascular_score": "cv_score",
        "cardiovascular_observed": "cv_observed"})
    return first


def _load_pkg(model, pkg, seed):
    base = RUNS / pkg / model / f"seed_{seed}"
    idx = pd.read_parquet(
        ROOT / "preprocess/artifacts/p9_packages/pp_v1_20260730"
        / pkg / "test" / "index.parquet",
        columns=["episode_key", "subject_key", "landmark_k"])
    z = np.load(base / "predictions.npz", allow_pickle=True)
    df = idx.copy()
    df["y"] = z["y_24h"]
    df["s"] = z["y_score"]
    return df[["episode_key", "subject_key", "landmark_k", "y", "s"]]


def _delta(y, s_sc, s_sce, k):
    a = _iauroc(y, s_sc, k)
    b = _iauroc(y, s_sce, k)
    if np.isnan(a) or np.isnan(b):
        return np.nan
    return b - a


def _interaction(df_sc, df_sce):
    """联合 bootstrap：患者级重采样，重算两亚组 Δ 的差。"""
    rng = np.random.default_rng(20260730)
    subj = df_sc["subject_key"].to_numpy()
    usubj, inv = np.unique(subj, return_inverse=True)
    y = df_sc["y"].to_numpy()
    k = df_sc["landmark_k"].to_numpy()
    s1 = df_sc["s"].to_numpy()
    s2 = df_sce["s"].to_numpy()
    g3 = (df_sc["cv_group"] == "ge3").to_numpy()
    g0 = (df_sc["cv_group"] == "lt3").to_numpy()
    n = len(usubj)
    out = np.full(N_BOOT, np.nan)
    inb = np.zeros(n, dtype=bool)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        d3 = _delta(y[m & g3], s1[m & g3], s2[m & g3], k[m & g3])
        d0 = _delta(y[m & g0], s1[m & g0], s2[m & g0], k[m & g0])
        if not (np.isnan(d3) or np.isnan(d0)):
            out[b] = d3 - d0
    return out


def run():
    cv = _load_cv_groups()
    print(f"[cv] episodes with CV group: {len(cv):,} "
          f"(missing: {int((~cv.cv_observed).sum()):,})")

    # 逐 seed 交互值
    per_seed = []
    ens = {}
    for sd in SEEDS:
        try:
            dsc = _load_pkg(*SC, sd)
            dsce = _load_pkg(*SCE, sd)
        except FileNotFoundError:
            print(f"[cv] seed_{sd} 预测缺失，跳过")
            continue
        for name, df in (("sc", dsc), ("sce", dsce)):
            df2 = df.merge(cv, on="episode_key", how="left")
            df2["cv_group"] = np.where(
                ~df2["cv_observed"].fillna(False), "missing",
                np.where(df2["cv_score"] >= 3, "ge3", "lt3"))
            if name == "sc":
                dsc = df2
            else:
                dsce = df2
        assert (dsc["subject_key"].to_numpy()
                == dsce["subject_key"].to_numpy()).all()
        assert (dsc["landmark_k"].to_numpy()
                == dsce["landmark_k"].to_numpy()).all()
        row = {"seed": sd}
        for gname, mask in (("ge3", dsc["cv_group"] == "ge3"),
                            ("lt3", dsc["cv_group"] == "lt3")):
            d = _delta(dsc.loc[mask, "y"].to_numpy(),
                       dsc.loc[mask, "s"].to_numpy(),
                       dsce.loc[mask, "s"].to_numpy(),
                       dsc.loc[mask, "landmark_k"].to_numpy())
            row[f"delta_{gname}"] = d
        row["interaction"] = row["delta_ge3"] - row["delta_lt3"]
        row["n_missing"] = int((dsc["cv_group"] == "missing").sum())
        per_seed.append(row)
        # 集成分数（逐 seed 平均）
        ens.setdefault("df", dsc[["subject_key", "landmark_k", "y",
                                  "cv_group"]].copy())
        ens.setdefault("sc", []).append(dsc["s"].to_numpy())
        ens.setdefault("sce", []).append(dsce["s"].to_numpy())

    ps = pd.DataFrame(per_seed)
    ens_df = ens["df"]
    ens_df["s"] = np.mean(ens["sc"], axis=0)
    ens_sce = np.mean(ens["sce"], axis=0)

    # 集成分数上的亚组 Δ 与交互
    point = {}
    for gname, mask in (("ge3", ens_df["cv_group"] == "ge3"),
                        ("lt3", ens_df["cv_group"] == "lt3")):
        point[gname] = {
            "delta": _delta(ens_df.loc[mask, "y"].to_numpy(),
                            ens_df.loc[mask, "s"].to_numpy(),
                            ens_sce[mask],
                            ens_df.loc[mask, "landmark_k"].to_numpy()),
            "n_samples": int(mask.sum()),
            "n_patients": int(ens_df.loc[mask, "subject_key"].nunique()),
            "n_pos": int(ens_df.loc[mask, "y"].sum()),
        }
    point_inter = point["ge3"]["delta"] - point["lt3"]["delta"]

    # bootstrap CI（集成分数）
    boots = _interaction(
        ens_df.rename(columns={"s": "s"}).assign(s=ens_df["s"]),
        ens_df.assign(s=ens_sce))
    ci = (float(np.nanpercentile(boots, 2.5)),
          float(np.nanpercentile(boots, 97.5)))
    valid = int(np.isfinite(boots).sum())

    result = {
        "definition": "Interaction = ΔiAUROC(CV-SOFA>=3) - ΔiAUROC(CV-SOFA<3)",
        "subgroup_rule": "strict_24h 轨首个有效 landmark 的心血管 SOFA 评分；"
                         "缺失不入组",
        "seeds": per_seed,
        "per_seed_interaction_mean": float(ps["interaction"].mean()),
        "per_seed_interaction_sd": float(ps["interaction"].std()),
        "ensemble": {
            "ge3": point["ge3"], "lt3": point["lt3"],
            "interaction": point_inter,
        },
        "bootstrap_ci95": ci,
        "bootstrap_valid": valid,
        "interaction_significant": bool(ci[0] > 0),
        "n_missing_samples": int(ps["n_missing"].sum()),
    }
    (OUT / "interaction_result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")

    # markdown 报告
    lines = [
        "# CV-SOFA≥3 亚组交互检验报告",
        "",
        f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；预登记：技术文档 §10.4/§13.5",
        "",
        f"- 亚组规则：strict_24h 轨、每 episode 首个有效 landmark 的 SOFA 心血管评分；缺失不入组（共 {result['n_missing_samples']:,} 样本）",
        f"- 亚组构成：CV≥3 {point['ge3']['n_samples']:,} 样本 / {point['ge3']['n_pos']} 阳性；CV<3 {point['lt3']['n_samples']:,} 样本 / {point['lt3']['n_pos']} 阳性",
        "",
        "## 逐 seed",
        "",
        "| seed | Δ(CV≥3) | Δ(CV<3) | Interaction |",
        "|---|---|---|---|"]
    for r in per_seed:
        lines.append(f"| {r['seed']} | {r['delta_ge3']:+.4f} | "
                     f"{r['delta_lt3']:+.4f} | {r['interaction']:+.4f} |")
    lines += [
        "",
        f"**逐 seed 交互均值±SD：{result['per_seed_interaction_mean']:+.4f} ± {result['per_seed_interaction_sd']:.4f}**",
        "",
        "## 集成分数（5 seeds 均值）主结果",
        "",
        f"- ΔiAUROC(CV≥3) = {point['ge3']['delta']:+.4f}",
        f"- ΔiAUROC(CV<3) = {point['lt3']['delta']:+.4f}",
        f"- **Interaction = {point_inter:+.4f}**",
        f"- **患者级 bootstrap 95% CI：[{ci[0]:+.4f}, {ci[1]:+.4f}]**（有效重复 {valid}/2000）",
        f"- **ECG 增益 × 亚组交互显著（CI 下限 > 0）：{'是 ✅' if result['interaction_significant'] else '否（CI 跨 0）'}**",
    ]
    (OUT / "interaction_report.md").write_text("\n".join(lines),
                                             encoding="utf-8")
    print("\n".join(lines[-8:]))
    return result


if __name__ == "__main__":
    run()
