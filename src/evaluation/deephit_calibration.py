"""DeepHit CIF 校准曲线（增补分析方案 v1.0 Part A，三层校准）。

① 总体校准 calibration-in-the-large（死亡/存活出院 × 24/72/168h）
② 十分位校准曲线（预测 CIF 十分位 × KM 观察累积发生率）
③ landmark 分段校准（k0-3 / k4-11 / k12+）
急性转出仅附注（事件不足，按预登记降级）。
bootstrap：患者级 2000 次，cal-in-large 与校准斜率/截距 95% CI。
测试集只报原始校准，不重新拟合。

VSCode：打开 → ▶ Run；或终端 python src/evaluation/deephit_calibration.py
输出：src/models/runs/deephit/calibration/{calibration_metrics.json,
      calibration_curves.npz, REPORT.md}
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DH = ROOT / "src" / "models" / "runs" / "deephit"
OUT = DH / "calibration"
OUT.mkdir(parents=True, exist_ok=True)
PKGS = ROOT / "preprocess/artifacts/p9_packages/pp_v1_20260730"

CAUSES = {"death": 1, "alive_discharge": 2}
HORIZONS = {"24h": 4, "72h": 12, "168h": 28}
N_BOOT = 2000
N_DECILE = 10
SEGMENTS = {"k0-3": lambda k: k <= 3, "k4-11": lambda k: (k >= 4) & (k <= 11),
            "k12+": lambda k: k >= 12}


def _load():
    cifs, ets, ebs = [], None, None
    for sd in (1, 2, 3, 4, 5):
        z = np.load(DH / f"cif_predictions_seed{sd}.npz")
        cifs.append({c: z[f"cif_{c}"] for c in CAUSES})
        ets = z["event_type"]; ebs = z["event_bin"]
    cif = {c: np.mean([x[c] for x in cifs], axis=0) for c in CAUSES}
    idx = pd.read_parquet(PKGS / "sc_common_all" / "test" / "index.parquet",
                          columns=["subject_key", "landmark_k"])
    return cif, ets, ebs, idx["landmark_k"].to_numpy(), \
        idx["subject_key"].to_numpy()


def _km_cif(event_type, event_bin, cause, horizon):
    """cause 特异 KM 累积发生率（其他事件与删失均作删失处理）。"""
    n = len(event_type)
    t = np.sort(np.unique(event_bin))
    risk = n
    surv = 1.0
    cif = 0.0
    prev_surv = 1.0
    for tt in t:
        if tt > horizon:
            break
        d = ((event_type == cause) & (event_bin == tt)).sum()
        risk = ((event_bin >= tt)).sum()
        if risk > 0:
            surv *= (1 - d / risk)
    return 1 - surv


def _cal_metrics(cif_pred, event_type, event_bin, cause, horizon, mask=None):
    if mask is None:
        mask = np.ones(len(cif_pred), dtype=bool)
    p = cif_pred[mask]
    et = event_type[mask]; eb = event_bin[mask]
    pred = float(np.mean(p))
    obs = _km_cif(et, eb, CAUSES[cause], horizon)
    return {"pred_mean": pred, "obs_rate": obs, "cal_in_large": pred - obs,
            "n": int(mask.sum())}


def _decile_curve(cif_pred, event_type, event_bin, cause, horizon, mask=None):
    if mask is None:
        mask = np.ones(len(cif_pred), dtype=bool)
    p = cif_pred[mask]
    et = event_type[mask]; eb = event_bin[mask]
    qs = np.quantile(p, np.linspace(0, 1, N_DECILE + 1))
    rows = []
    for i in range(N_DECILE):
        m = (p >= qs[i]) & (p <= qs[i + 1] if i == N_DECILE - 1 else p < qs[i + 1])
        if m.sum() == 0:
            continue
        rows.append({
            "group": i + 1,
            "pred_mean": float(p[m].mean()),
            "obs_rate": _km_cif(et[m], eb[m], CAUSES[cause], horizon),
            "n": int(m.sum()),
        })
    return rows


def _slope_intercept(curve):
    x = np.array([r["pred_mean"] for r in curve])
    y = np.array([r["obs_rate"] for r in curve])
    if len(x) < 2:
        return float("nan"), float("nan")
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    slope = float(((x - xm) * (y - ym)).sum() / sxx) if sxx > 0 else float("nan")
    return slope, float(ym - slope * xm)


def _boot_cal(cif, et, eb, cause, horizon, subj, mask_all):
    rng = np.random.default_rng(20260730)
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    inb = np.zeros(n, dtype=bool)
    d = np.full(N_BOOT, np.nan)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv] & mask_all
        r = _cal_metrics(cif, et, eb, cause, horizon, m)
        d[b] = r["cal_in_large"]
    return (float(np.nanmean(d)), float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5)))


def run():
    cif, et, eb, lm_k, subj = _load()
    metrics = {}
    curves = {}
    report_lines = ["# DeepHit CIF 校准报告", "",
                    f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
                    "事件：死亡/存活出院（急性转出仅附注，事件不足）；"
                    "horizon：24h/72h/168h；bootstrap 患者级 2000 次", ""]
    for cause in CAUSES:
        report_lines += [f"## {cause}", "", "### ① 总体校准", "",
                         "| horizon | pred_mean | obs(KM) | cal-in-large | 95% CI |",
                         "|---|---|---|---|---|"]
        metrics[cause] = {"overall": {}, "segments": {}, "decile": {}}
        for hname, hb in HORIZONS.items():
            r = _cal_metrics(cif[cause], et, eb, cause, hb)
            lo, hi_mean, hi = _boot_cal(cif[cause], et, eb, cause, hb,
                                        subj, np.ones(len(et), dtype=bool))
            metrics[cause]["overall"][hname] = {**r,
                                                "ci95": [hi_mean, lo, hi]}
            report_lines.append(
                f"| {hname} | {r['pred_mean']:.4f} | {r['obs_rate']:.4f} | "
                f"{r['cal_in_large']:+.4f} | [{lo:+.4f}, {hi:+.4f}] |")
        report_lines += ["", "### ② 十分位校准（168h）", "",
                         "| 组 | pred_mean | obs(KM) | n |",
                         "|---|---|---|---|"]
        curve = _decile_curve(cif[cause], et, eb, cause, 28)
        curves[cause] = curve
        slope, intercept = _slope_intercept(curve)
        metrics[cause]["decile"] = {"curve": curve, "slope": slope,
                                    "intercept": intercept}
        for r in curve:
            report_lines.append(
                f"| {r['group']} | {r['pred_mean']:.4f} | "
                f"{r['obs_rate']:.4f} | {r['n']:,} |")
        report_lines += ["",
                         f"**校准斜率 = {slope:.3f}，截距 = {intercept:+.4f}**"
                         f"（斜率≈1 且截距≈0 为理想；斜率<1 提示区分度过强）",
                         "", "### ③ landmark 分段（168h，cal-in-large）", "",
                         "| 段 | pred_mean | obs(KM) | cal-in-large |",
                         "|---|---|---|---|"]
        for seg, fn in SEGMENTS.items():
            m = fn(lm_k)
            r = _cal_metrics(cif[cause], et, eb, cause, 28, m)
            metrics[cause]["segments"][seg] = r
            report_lines.append(
                f"| {seg} | {r['pred_mean']:.4f} | {r['obs_rate']:.4f} | "
                f"{r['cal_in_large']:+.4f} |")
        report_lines.append("")
    report_lines += [
        "## 解读提示（预登记口径）",
        "",
        "- cal-in-large >0 = 整体高估事件率，<0 = 低估；",
        "- 斜率≈1 且截距≈0 → 概率可直接用；系统性偏离 → 用 logistic "
        "recalibration（仅 validation 拟合）报「原始 vs 校准后」两版；",
        "- landmark 分段失准 → 报告区段并讨论（晚期稀疏/分布漂移），"
        "不强求全局再校准。"]
    (OUT / "calibration_metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    np.savez_compressed(OUT / "calibration_curves.npz",
                        **{c: np.array([[r["pred_mean"], r["obs_rate"]]
                                        for r in curves[c]])
                           for c in curves})
    (OUT / "REPORT.md").write_text("\n".join(report_lines),
                                   encoding="utf-8")
    print(f"[calibration] metrics + curves + REPORT.md → {OUT}")


if __name__ == "__main__":
    run()
