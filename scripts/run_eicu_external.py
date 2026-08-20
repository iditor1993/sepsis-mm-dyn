"""eICU 外部验证（层级 2：Robustness under phenotype shift）。

MIMIC 冻结模型（SC-common-all，deployment 运行产物）直接评估 eICU 两表型
（P-clinical / P-explicit），原始冻结性能，不重拟合。
静态特征按 MIMIC 44 维 schema 映射（eICU 仅有 age/gender，其余按 MIMIC
训练集插补值 + 缺失指示处理）。
指标：逐 track iAUROC / Brier + 患者级（uniquepid）bootstrap 95% CI +
变量缺失率分层。

VSCode：改 CONFIG → ▶ Run；或终端 python scripts/run_eicu_external.py
输出：src/models/runs/eicu_external/{result.json, REPORT.md}
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocess" / "src"))

# ============================ CONFIG（只改这里） ============================
TRACKS = ["P-clinical", "P-explicit"]
SEEDS = [1, 2, 3, 4, 5]   # 使用哪些 MIMIC seed 权重（集成均值）
# ===========================================================================

from src.models.data.dataset import (ART, RID, STATIC_DIM,  # noqa: E402
                                     EICUDataset)
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.heads import SCModel  # noqa: E402
from src.models.train.metrics import auroc_np  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OUT = ROOT / "src" / "models" / "runs" / "eicu_external"
OUT.mkdir(parents=True, exist_ok=True)
MAIN_K = 12


def _iauroc(y, s, k):
    aucs = [auroc_np(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan
    return float(np.mean(aucs))


def _boot(y, s, k, subj, n_boot=2000):
    rng = np.random.default_rng(20260730)
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    inb = np.zeros(n, dtype=bool)
    d = np.full(n_boot, np.nan)
    for b in range(n_boot):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        d[b] = _iauroc(y[m], s[m], k[m])
    return (float(np.nanmean(d)), float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5)))


@torch.no_grad()
def _predict(models, ld):
    ys, ss, ks, sb = [], [], [], []
    for b in ld:
        x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
        d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
        s_seeds = torch.stack([torch.sigmoid(mo(x, m, d, st))
                               for mo in models]).mean(0)
        ys.append(b["y"].numpy()); ss.append(s_seeds.cpu().numpy())
        ks.append(b["landmark_k"].numpy()); sb.append(b["subject_key"])
    return (np.concatenate(ys), np.concatenate(ss), np.concatenate(ks),
            np.concatenate(sb))


def run():
    # 加载 MIMIC 冻结权重（各 seed 集成）
    models = []
    for sd in SEEDS:
        p = ROOT / "src" / "models" / "runs" / "sc_common_all" / "grud" \
            / f"seed_{sd}" / "model.pt"
        if not p.exists():
            print(f"[eicu] seed_{sd} 权重缺失，跳过")
            continue
        mo = SCModel(GRUDEncoder(17, 128, STATIC_DIM, 64), 192).to(DEVICE)
        mo.load_state_dict(torch.load(p, map_location=DEVICE))
        mo.eval()
        models.append(mo)
    if not models:
        print("[eicu] 无 MIMIC 权重，先跑 deployment 训练")
        return
    print(f"[eicu] 加载 {len(models)} 个 MIMIC seed 权重（集成均值）")

    all_res = {}
    for track in TRACKS:
        print(f"\n[eicu] ===== {track} =====")
        ds = EICUDataset(track)
        ld = DataLoader(ds, batch_size=512, shuffle=False, num_workers=0)
        y, s, k, subj = _predict(models, ld)
        ia = _iauroc(y, s, k)
        brier = float(np.mean((s - y) ** 2))
        mean_ia, lo, hi = _boot(y, s, k, subj)
        # 缺失率分层（按 eICU 通道密度报告）
        mrate = np.asarray(ds.m[ds.rows]).mean(axis=(0, 2))
        res = {"track": track, "n": int(len(y)),
               "n_patients": int(ds.df['subject_key'].nunique()),
               "n_positive": int(y.sum()),
               "iauroc": ia, "brier": brier,
               "boot_mean": mean_ia, "ci95": [lo, hi],
               "channel_obs_density": {
                   c: float(v) for c, v in zip(
                       ["hr", "sbp", "dbp", "mbp", "rr", "spo2", "temp",
                        "creatinine", "bilirubin", "platelets", "lactate",
                        "wbc", "hemoglobin", "glucose", "sodium", "potassium",
                        "bicarbonate"], mrate)}}
        all_res[track] = res
        print(f"[eicu] {track}: n={res['n']:,} 阳性 {res['n_positive']:,} "
              f"iAUROC={ia:.4f} CI[{lo:+.4f},{hi:+.4f}] Brier={brier:.4f}")

    (OUT / "result.json").write_text(json.dumps(all_res, indent=2),
                                     encoding="utf-8")
    lines = ["# eICU 外部验证报告（Robustness under phenotype shift）", "",
             f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
             f"模型：MIMIC SC-common-all 冻结权重（{len(models)} seeds 集成）；"
             "原始冻结性能，未重新校准", ""]
    for track, res in all_res.items():
        lines += [f"## {track}",
                  "",
                  f"- 样本 {res['n']:,} / 患者 {res['n_patients']:,} / 阳性 {res['n_positive']:,}",
                  f"- **iAUROC = {res['iauroc']:.4f}，患者级 bootstrap 95% CI [{res['ci95'][0]:+.4f}, {res['ci95'][1]:+.4f}]**",
                  f"- Brier = {res['brier']:.4f}",
                  "",
                  "变量观测密度（mask 密度，缺失模式偏移证据）：",
                  "",
                  "| 通道 | 密度 |",
                  "|---|---|"]
        for c, v in res["channel_obs_density"].items():
            lines.append(f"| {c} | {v:.3f} |")
        lines.append("")
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[eicu] REPORT.md written → {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", default=",".join(TRACKS))
    args = ap.parse_args()
    run()


if __name__ == "__main__":
    main()
