"""决策曲线分析（DCA / net benefit，预登记增补）。

将 SCE 相对 SC 的 +0.01 iAUROC 转换为临床可读的净获益：
  NB(pt) = TP/n − FP/n × pt/(1−pt)
  - pt = 阈值概率（临床医生对「24h 内死亡」采取干预的风险容忍度）
  - 净获益单位可解释为「每 1000 个 landmark 观测中，净增加的真阳性数」

预登记口径：
  - **主分析用 Platt 校准后概率**（原始概率被 pos_weight 抬高，
    原始 DCA 在低阈值区 ≈ 全干预线，属预期假象，仅作附图）；
  - 阈值网格 0.5%–20%（步长 0.5%，围绕 2.4% 患病率的临床合理区）；
  - 定点报告 2% / 5% / 10%：每 1000 人的 TP、FP、ΔTP、ΔFP、净 TP；
  - ΔNB = NB_SCE − NB_SC，患者级 bootstrap 2000 次 95% CI；
  - 分段：overall + k0-3 / k4-11 / k12+（效应早期集中的验证）；
  - 观测单位为 landmark（与 iAUROC estimand 一致），bootstrap 患者级。

依赖：先运行 src/evaluation/main_calibration.py 生成 test_scores_*.npz。
VSCode：打开 → ▶ Run；或终端 python src/evaluation/dca.py
输出：src/models/runs/dca/{REPORT.md, dca_metrics.json, dca_curves.npz}
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CAL = ROOT / "src" / "models" / "runs" / "main_calibration"
OUT = ROOT / "src" / "models" / "runs" / "dca"
OUT.mkdir(parents=True, exist_ok=True)

PTS = np.round(np.arange(0.005, 0.2001, 0.005), 3)   # 0.5%–20%，40 点
FIXED_PTS = (0.02, 0.05, 0.10)
N_BOOT = 2000
BOOT_SEED = 20260730
SEGMENTS = {"k0-3": lambda k: k <= 3, "k4-11": lambda k: (k >= 4) & (k <= 11),
            "k12+": lambda k: k >= 12}
POPULATIONS = ("paired", "deployment")


def _nb(y, p, pt, n=None):
    """net benefit；n 可显式传入（bootstrap 子样本）。"""
    if n is None:
        n = len(y)
    pred = p >= pt
    tp = float((pred & (y == 1)).sum())
    fp = float((pred & (y == 0)).sum())
    return tp / n - fp / n * pt / (1 - pt)


def _nb_treat_all(y, pt):
    prev = float(y.mean())
    return prev - (1 - prev) * pt / (1 - pt)


def _curve(y, p, mask=None):
    if mask is not None:
        y, p = y[mask], p[mask]
    return np.array([_nb(y, p, pt) for pt in PTS])


def _boot_dnb(y, p_sc, p_sce, subj, mask=None):
    """ΔNB = NB_sce − NB_sc 的患者级配对 bootstrap，返回逐阈值 CI。"""
    if mask is not None:
        y, p_sc, p_sce, subj = y[mask], p_sc[mask], p_sce[mask], subj[mask]
    rng = np.random.default_rng(BOOT_SEED)
    usubj, inv = np.unique(subj, return_inverse=True)
    n_u = len(usubj)
    inb = np.zeros(n_u, dtype=bool)
    d = np.full((N_BOOT, len(PTS)), np.nan)
    for b in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n_u, n_u)] = True
        m = inb[inv]
        if m.sum() < 50:
            continue
        yy, p1, p2 = y[m], p_sc[m], p_sce[m]
        nn = int(m.sum())
        for j, pt in enumerate(PTS):
            d[b, j] = _nb(yy, p2, pt, nn) - _nb(yy, p1, pt, nn)
    return (np.nanpercentile(d, 2.5, axis=0),
            np.nanpercentile(d, 50, axis=0),
            np.nanpercentile(d, 97.5, axis=0))


def _fixed_table(y, p_sc, p_sce, mask=None):
    """定点阈值：每 1000 人 TP/FP 与 Δ。"""
    if mask is not None:
        y, p_sc, p_sce = y[mask], p_sc[mask], p_sce[mask]
    n = len(y)
    rows = []
    for pt in FIXED_PTS:
        r = {"pt": pt, "n": n}
        for name, p in (("sc", p_sc), ("sce", p_sce)):
            pred = p >= pt
            r[f"tp_{name}"] = float((pred & (y == 1)).sum()) / n * 1000
            r[f"fp_{name}"] = float((pred & (y == 0)).sum()) / n * 1000
            r[f"nb_{name}"] = _nb(y, p, pt)
        r["d_tp"] = r["tp_sce"] - r["tp_sc"]
        r["d_fp"] = r["fp_sce"] - r["fp_sc"]
        r["d_nb"] = r["nb_sce"] - r["nb_sc"]
        r["nb_all"] = _nb_treat_all(y, pt)
        rows.append(r)
    return rows


def run():
    curves = {}
    metrics = {}
    report = ["# 决策曲线分析（DCA）报告", "",
              f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
              "主分析用 Platt 校准后概率；阈值网格 0.5%–20%；"
              "ΔNB 患者级 bootstrap 2000 次", "",
              "净获益单位解释：NB×1000 = 每 1000 个 landmark 观测中"
              "「净增加的真阳性数」（相当于在该阈值下，与不干预相比，"
              "每 1000 人多正确识别且不多误报的净患者数）。", ""]

    for pop in POPULATIONS:
        f = CAL / f"test_scores_{pop}.npz"
        if not f.exists():
            raise FileNotFoundError(
                f"{f} 不存在——请先运行 src/evaluation/main_calibration.py")
        z = np.load(f)
        subj, k, y = z["subject_key"], z["landmark_k"], z["y"]
        p_sc, p_sce = z["sc_cal"], z["sce_cal"]
        p_sc_raw, p_sce_raw = z["sc_raw"], z["sce_raw"]
        prev = float(y.mean())
        print(f"[dca] ===== {pop}（n={len(y):,}，阳性 {int(y.sum()):,}）=====",
              flush=True)

        # ① 净获益曲线（校准后为主；原始为附图）
        c_sc, c_sce = _curve(y, p_sc), _curve(y, p_sce)
        c_sc_raw, c_sce_raw = _curve(y, p_sc_raw), _curve(y, p_sce_raw)
        c_all = np.array([_nb_treat_all(y, pt) for pt in PTS])
        curves[pop] = {"pt": PTS, "sc_cal": c_sc, "sce_cal": c_sce,
                       "sc_raw": c_sc_raw, "sce_raw": c_sce_raw,
                       "treat_all": c_all}
        lo, mid, hi = _boot_dnb(y, p_sc, p_sce, subj)
        curves[pop]["dnb_lo"], curves[pop]["dnb_mid"], curves[pop]["dnb_hi"] \
            = lo, mid, hi

        # ② 定点表（overall）
        rows = _fixed_table(y, p_sc, p_sce)
        metrics[pop] = {"overall": rows, "segments": {}}
        report += [f"## {pop}（test n={len(y):,}，事件率 {prev:.4f}）", "",
                   "### ① 定点阈值净获益（校准后概率，每 1000 人）", "",
                   "| 阈值 | SC: TP / FP | SCE: TP / FP | 全干预 NB×1000 | "
                   "ΔTP | ΔFP | **ΔNB×1000（净 TP）** |",
                   "|---|---|---|---|---|---|---|"]
        for r in rows:
            report.append(
                f"| {r['pt']:.0%} | {r['tp_sc']:.1f} / {r['fp_sc']:.1f} | "
                f"{r['tp_sce']:.1f} / {r['fp_sce']:.1f} | "
                f"{r['nb_all'] * 1000:.1f} | {r['d_tp']:+.2f} | "
                f"{r['d_fp']:+.2f} | **{r['d_nb'] * 1000:+.2f}** |")
        # ΔNB 曲线显著性摘要（CI 不含 0 的阈值区间）
        sig = PTS[(lo > 0) | (hi < 0)]
        sig_txt = (f"{sig.min():.1%}–{sig.max():.1%}" if len(sig)
                   else "无（全部阈值 CI 跨 0）")
        report += ["",
                   f"ΔNB 曲线 95% CI 不含 0 的阈值区间：**{sig_txt}**", ""]

        # ③ 分段定点（效应早期集中验证）
        report += ["### ② landmark 分段定点 ΔNB×1000（SCE−SC，校准后）",
                   "", "| 段 | n | 阈值 2% | 阈值 5% | 阈值 10% |",
                   "|---|---|---|---|---|"]
        for seg, fn in SEGMENTS.items():
            m = fn(k)
            if m.sum() < 200:
                continue
            srows = _fixed_table(y, p_sc, p_sce, m)
            slo, smid, shi = _boot_dnb(y, p_sc, p_sce, subj, m)
            metrics[pop]["segments"][seg] = {
                "fixed": srows,
                "dnb_ci_fixed": {f"{r['pt']:.0%}": [
                    float(slo[np.argmin(abs(PTS - r['pt']))]),
                    float(smid[np.argmin(abs(PTS - r['pt']))]),
                    float(shi[np.argmin(abs(PTS - r['pt']))])]
                    for r in srows}}
            cells = []
            for r in srows:
                j = np.argmin(abs(PTS - r["pt"]))
                cells.append(f"{r['d_nb'] * 1000:+.2f} "
                             f"[{slo[j] * 1000:+.2f}, {shi[j] * 1000:+.2f}]")
            report.append(f"| {seg} | {int(m.sum()):,} | " + " | ".join(cells)
                          + " |")
        report.append("")

    report += [
        "## 解读提示（预登记口径）", "",
        "- 主看校准后曲线：SCE 曲线在 SC 曲线上方且 ΔNB 95% CI >0 的阈值"
        "区间 = ECG 带来临床净获益的决策区间；",
        "- 原始概率曲线（附图）在低阈值区 ≈ 全干预线是 pos_weight 抬概率"
        "的预期假象，不是模型失败，不作结论依据；",
        "- ΔNB×1000 的临床读法：在阈值 pt 下，每 1000 个 landmark 观测，"
        "SCE 比 SC 净多正确识别 X 人（已扣掉多误报的代价）；",
        "- 分段若显示 k0-3 的 ΔNB 最大，与「ECG 增益早期集中」的判别结果"
        "互相印证，可写入 Discussion；",
        "- NB 的绝对水平受事件率制约（2.4% 患病率下 NB 数值天然小），"
        "比较重点是模型间差异，不是绝对值。"]

    (OUT / "dca_metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    np.savez_compressed(OUT / "dca_curves.npz",
                        **{f"{pop}_{k_}": v for pop, d in curves.items()
                           for k_, v in d.items()})
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"[dca] metrics + curves + REPORT.md → {OUT}", flush=True)


if __name__ == "__main__":
    run()
