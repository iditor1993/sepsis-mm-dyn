"""eICU 分层细化：按医院 / 按缺失密度（增补分析方案 v1.0 Part B）。

MIMIC 冻结模型（SC-common-all，5 seeds 集成）对 eICU 两表型推理后：
  ① 按 hospitalid 逐院 iAUROC + 分布 + 最大单院占比 + 失败医院占比
     （可估计阈：n_samples≥500 且 n_positive≥20，预登记）
  ② 按缺失密度三分位（预登记切点，不按结果选）× track 的
     iAUROC/Brier/患者级 bootstrap 95% CI + 通道密度对应
急性转出仅附注。分层仅作机制归因，不作反向优化依据。

VSCode：打开 → ▶ Run；或终端 python src/evaluation/eicu_stratified.py
输出：src/models/runs/eicu_external/stratified/{by_hospital.csv,
      by_hospital_summary.json, by_density.json, REPORT.md}
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocess" / "src"))

# ============================ CONFIG（只改这里） ============================
TRACKS = ["P-clinical", "P-explicit"]
SEEDS = [1, 2, 3, 4, 5]
MIN_HOSP_SAMPLES = 500
MIN_HOSP_POS = 20
N_BOOT = 2000
# ===========================================================================

from src.models.data.dataset import (ART, RID,  # noqa: E402
                                     EICUDataset)
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.heads import SCModel  # noqa: E402
from src.models.train.metrics import auroc_np  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OUT = ROOT / "src" / "models" / "runs" / "eicu_external" / "stratified"
OUT.mkdir(parents=True, exist_ok=True)
MAIN_K = 12
DENS_Q = (1 / 3, 2 / 3)


def _iauroc(y, s, k):
    aucs = [auroc_np(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan
    return float(np.mean(aucs))


def _boot(y, s, k, subj, stat_fn, n_boot=N_BOOT):
    rng = np.random.default_rng(20260730)
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    inb = np.zeros(n, dtype=bool)
    d = np.full(n_boot, np.nan)
    for b in range(n_boot):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        d[b] = stat_fn(y[m], s[m], k[m])
    v = np.isfinite(d)
    return (float(np.nanmean(d)), float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5)), int(v.sum()))


def _load_models():
    models = []
    for sd in SEEDS:
        p = ROOT / "src" / "models" / "runs" / "sc_common_all" / "grud" \
            / f"seed_{sd}" / "model.pt"
        if p.exists():
            mo = SCModel(GRUDEncoder(17, 128, 44, 64), 192).to(DEVICE)
            mo.load_state_dict(torch.load(p, map_location=DEVICE))
            mo.eval()
            models.append(mo)
    return models


@torch.no_grad()
def _predict(models, ds):
    ld = DataLoader(ds, batch_size=512, shuffle=False, num_workers=0)
    ys, ss, ks = [], [], []
    for b in ld:
        x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
        d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
        s = torch.stack([torch.sigmoid(mo(x, m, d, st))
                         for mo in models]).mean(0)
        ys.append(b["y"].numpy()); ss.append(s.cpu().numpy())
        ks.append(b["landmark_k"].numpy())
    return np.concatenate(ys), np.concatenate(ss), np.concatenate(ks)


def run():
    models = _load_models()
    if not models:
        print("[stratified] 无 MIMIC 权重，先跑 deployment 训练")
        return
    print(f"[stratified] {len(models)} seed 权重集成")

    hosp_rows = []
    density_res = {}
    report_lines = ["# eICU 分层细化报告（按医院 / 按缺失密度）", "",
                    f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
                    f"模型：MIMIC SC-common-all（{len(models)} seeds 集成）", ""]

    for track in TRACKS:
        print(f"\n[stratified] ===== {track} =====")
        ds = EICUDataset(track)
        y, s, k = _predict(models, ds)
        df = ds.df.copy()
        df["y"] = y; df["s"] = s; df["k"] = k
        report_lines += [f"## {track}", "", "### ① 按医院",
                         "",
                         f"（可估计阈：n≥{MIN_HOSP_SAMPLES} 且阳性≥{MIN_HOSP_POS}）",
                         "",
                         "| hospitalid | n | 阳性 | iAUROC |",
                         "|---|---|---|---|"]
        # 按医院
        est = 0
        for hid, g in df.groupby("hospitalid"):
            if len(g) < MIN_HOSP_SAMPLES or g["y"].sum() < MIN_HOSP_POS:
                continue
            ia = _iauroc(g["y"].to_numpy(), g["s"].to_numpy(),
                         g["k"].to_numpy())
            if np.isnan(ia):
                continue
            est += 1
            hosp_rows.append({"track": track, "hospitalid": int(hid),
                              "n": len(g), "n_pos": int(g["y"].sum()),
                              "iauroc": ia})
            report_lines.append(
                f"| {int(hid)} | {len(g):,} | {int(g['y'].sum())} | {ia:.4f} |")
        hsub = pd.DataFrame([r for r in hosp_rows if r["track"] == track])
        if len(hsub):
            max_share = float(
                df.groupby("hospitalid").size().max() / len(df))
            fail = float((hsub["iauroc"] < 0.55).mean())
            report_lines += ["",
                             f"- 可估计医院数：{est}（占纳入医院比例）",
                             f"- iAUROC 分布：中位 {hsub['iauroc'].median():.4f}，"
                             f"IQR [{hsub['iauroc'].quantile(0.25):.4f}, "
                             f"{hsub['iauroc'].quantile(0.75):.4f}]",
                             f"- 最大单医院患者占比：{max_share:.1%}（对照 ≤25%）",
                             f"- 失败医院（iAUROC<0.55）占比：{fail:.1%}", ""]
        # 按缺失密度（每样本 17 通道×24h 的 mask 密度均值，axis=(1,2)）
        dens = np.asarray(ds.m[ds.rows]).mean(axis=(1, 2))
        q1, q2 = np.quantile(dens, DENS_Q)
        grp = np.where(dens < q1, "低", np.where(dens < q2, "中", "高"))
        df["density"] = dens; df["grp"] = grp
        report_lines += ["", "### ② 按缺失密度（三分位，预登记切点）", "",
                         "| 组 | n | 阳性 | iAUROC | 95% CI |",
                         "|---|---|---|---|---|"]
        dres = {}
        for gname in ["低", "中", "高"]:
            m = df["grp"] == gname
            gy = df.loc[m, "y"].to_numpy(); gs = df.loc[m, "s"].to_numpy()
            gk = df.loc[m, "k"].to_numpy()
            gsubj = df.loc[m, "subject_key"].to_numpy()
            ia = _iauroc(gy, gs, gk)
            br = float(np.mean((gs - gy) ** 2))
            mean_ia, lo, hi, nv = _boot(gy, gs, gk, gsubj, _iauroc)
            dres[gname] = {"n": int(m.sum()), "n_pos": int(gy.sum()),
                           "iauroc": ia, "brier": br,
                           "ci95": [mean_ia, lo, hi], "boot_valid": nv}
            report_lines.append(
                f"| {gname} | {int(m.sum()):,} | {int(gy.sum())} | "
                f"{ia:.4f} | [{lo:+.4f}, {hi:+.4f}] |")
        report_lines += ["",
                         f"切点：q1={q1:.3f} q2={q2:.3f}（17 通道 mask 密度均值）",
                         ""]
        density_res[track] = {"tertile_edges": [float(q1), float(q2)],
                              "groups": dres}

    # 写产物
    pd.DataFrame(hosp_rows).to_csv(OUT / "by_hospital.csv", index=False)
    hosp_all = pd.DataFrame(hosp_rows)
    summary = {}
    if len(hosp_all):
        for track in TRACKS:
            h = hosp_all[hosp_all["track"] == track]
            if len(h):
                summary[track] = {
                    "n_hospitals_estimable": int(len(h)),
                    "iauroc_median": float(h["iauroc"].median()),
                    "iauroc_iqr": [float(h["iauroc"].quantile(0.25)),
                                   float(h["iauroc"].quantile(0.75))],
                    "fail_hospital_share": float((h["iauroc"] < 0.55).mean())}
    (OUT / "by_hospital_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (OUT / "by_density.json").write_text(
        json.dumps(density_res, indent=2), encoding="utf-8")
    report_lines += [
        "## 解读口径（预登记）",
        "",
        "- 医院分布集中（IQR 窄、无大量 <0.55 医院）→ 外验性能稳健；",
        "- 缺失密度单调改善 → 缺失模式偏移是主衰减机制（GRU-D mask 有效但信息有上限）；",
        "- 三组差异小 → 衰减不主要由缺失驱动，转向变量语义/实践差异；",
        "- 分层仅作机制归因，不作反向优化依据；再校准须走独立 calibration subset。"]
    (OUT / "REPORT.md").write_text("\n".join(report_lines),
                                   encoding="utf-8")
    print(f"\n[stratified] by_hospital.csv + summaries + REPORT.md → {OUT}")


if __name__ == "__main__":
    run()
