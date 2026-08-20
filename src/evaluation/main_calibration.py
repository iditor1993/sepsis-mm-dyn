"""主模型（SC/SCE）概率校准评估 + Platt 再校准（预登记增补）。

背景：训练使用 BCE + pos_weight≈43（预登记，解决 2.4% 类别不平衡），
原始输出概率被系统性抬高（test 均值 ~0.38 vs 实际 2.4%），绝对概率
不可直接用于临床阈值。本脚本给出「原始 vs 校准后」两版校准结果：
  ① 原始校准（test，5 seed 概率均值集成）：
     cal-in-the-large、logistic 校准斜率/截距、十分位曲线、Brier、
     landmark 分段（k0-3 / k4-11 / k12+）、患者级 bootstrap 2000 次 95% CI
  ② Platt 再校准：y ~ sigmoid(a + b·logit(p))，**仅在 validation 拟合**
     （需 GPU 加载 4 节点 × 5 seeds 的 model.pt 对 validation 推理），
     应用到 test 后重复上述指标
  ③ 保存 test 逐样本分数（原始+校准后），供 dca.py 决策曲线复用

两个人群：paired test（9,344，主比较人群）、deployment test（72,067，
全院部署人群）；SC=纯临床，SCE=临床+ECG。

VSCode：打开 → ▶ Run；或终端 python src/evaluation/main_calibration.py
运行环境：需 GPU（validation 推理），预计 1–3 小时（sce_deployment
validation 91,655 行为主要耗时）。
输出：src/models/runs/main_calibration/{REPORT.md, calibration_metrics.json,
      platt_params.json, test_scores_paired.npz, test_scores_deployment.npz,
      val_scores_paired.npz, val_scores_deployment.npz}
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

from src.models.data.dataset import (ART, RID, ClinicalDataset,  # noqa: E402
                                     SCEDataset)
from src.models.train.metrics import predict  # noqa: E402
from src.models.train.train import DEVICE, build_model  # noqa: E402

RUNS = ROOT / "src" / "models" / "runs"
OUT = RUNS / "main_calibration"
OUT.mkdir(parents=True, exist_ok=True)

SEEDS = (1, 2, 3, 4, 5)
N_BOOT = 2000
BOOT_SEED = 20260730
SEGMENTS = {"k0-3": lambda k: k <= 3, "k4-11": lambda k: (k >= 4) & (k <= 11),
            "k12+": lambda k: k >= 12}
# 人群 → {arm: (model_name, pkg)}
POPULATIONS = {
    "paired": {"sc": ("grud", "sc_common_paired"),
               "sce": ("sce_grud", "sce_common_paired")},
    "deployment": {"sc": ("grud", "sc_common_all"),
                   "sce": ("sce_grud", "sce_deployment")},
}


# ---------- 工具 ----------

def _logit(p):
    return np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _logistic_irls(x, y, iters=100):
    """y ~ sigmoid(a + b·x)，IRLS 拟合，返回 (a, b)。"""
    X = np.column_stack([np.ones(len(x)), x])
    beta = np.zeros(2)
    beta[0] = _logit(np.clip(y.mean(), 1e-6, 1 - 1e-6))
    for _ in range(iters):
        mu = _sigmoid(X @ beta)
        w = np.clip(mu * (1 - mu), 1e-9, None)
        H = (X.T * w) @ X
        g = X.T @ (y - mu)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def _ensemble_test_scores(model_name, pkg):
    """加载 test predictions.npz，5 seeds 概率均值集成。"""
    scores = None
    ref = None
    for sd in SEEDS:
        z = np.load(RUNS / pkg / model_name / f"seed_{sd}" / "predictions.npz")
        if scores is None:
            scores = z["y_score"].astype(np.float64)
            ref = {k: z[k] for k in ("subject_key", "landmark_k", "y_24h")}
        else:
            scores += z["y_score"].astype(np.float64)
    return ref["subject_key"], ref["landmark_k"], ref["y_24h"], scores / len(SEEDS)


def _channel_means(pkg_dir, tensor_dir):
    ch_params = json.loads((tensor_dir / "scaler_clinical_seq.json")
                           .read_text(encoding="utf-8"))
    channels = json.loads((pkg_dir / "train" / "manifest.json")
                          .read_text(encoding="utf-8"))["channels"]
    return torch.tensor([ch_params["channels"][c]["mean"] for c in channels],
                        dtype=torch.float32)


def _val_inference(model_name, pkg):
    """validation 推理（GPU），5 seeds 概率均值集成。"""
    pkg_dir = ART / "p9_packages" / RID / pkg
    tensor_dir = ART / "p7_fitted" / RID
    raw_dir = ART / "p2_clinical" / RID / "master"
    ecg_dir = ART / "p5_ecg_cache" / RID
    is_sce = model_name.startswith("sce_")
    deployment = pkg == "sce_deployment"
    if is_sce:
        ds = SCEDataset(pkg_dir / "validation", tensor_dir, ecg_dir,
                        raw_tensor_dir=raw_dir, ecg_suffix="_v2")
        bs = 64
    else:
        ds = ClinicalDataset(pkg_dir / "validation", tensor_dir,
                             raw_tensor_dir=raw_dir)
        bs = 512
    ld = DataLoader(ds, batch_size=bs, shuffle=False, num_workers=0,
                    pin_memory=True)
    means = _channel_means(pkg_dir, tensor_dir)
    ens, y_ref = None, None
    for sd in SEEDS:
        model, ecg_mode = build_model(model_name, ds.static.shape[1],
                                      means, deployment)
        model.to(DEVICE)
        sd_path = RUNS / pkg / model_name / f"seed_{sd}" / "model.pt"
        model.load_state_dict(torch.load(sd_path, map_location=DEVICE))
        vy, vs, _ = predict(model, ld, DEVICE, ecg=ecg_mode)
        ens = vs.astype(np.float64) if ens is None else ens + vs
        y_ref = vy
        print(f"[calibration] {pkg}/{model_name}/seed_{sd} val 推理完成",
              flush=True)
        del model
        torch.cuda.empty_cache()
    return (ds.idx["subject_key"].to_numpy(),
            ds.idx["landmark_k"].to_numpy(), y_ref, ens / len(SEEDS))


def _align(sc, sce):
    """按 (subject_key, landmark_k) 对齐 SC 与 SCE 分数。"""
    a = pd.DataFrame({"subject_key": sc[0], "landmark_k": sc[1],
                      "y": sc[2], "p_sc": sc[3]})
    b = pd.DataFrame({"subject_key": sce[0], "landmark_k": sce[1],
                      "y_sce": sce[2], "p_sce": sce[3]})
    df = a.merge(b, on=["subject_key", "landmark_k"], how="inner")
    assert len(df) == len(a) == len(b), \
        f"对齐失败：sc={len(a)} sce={len(b)} merged={len(df)}"
    assert np.allclose(df["y"], df["y_sce"]), "y 不一致"
    return (df["subject_key"].to_numpy(), df["landmark_k"].to_numpy(),
            df["y"].to_numpy(), df["p_sc"].to_numpy(),
            df["p_sce"].to_numpy())


def _cal_metrics(y, p):
    pred = float(np.mean(p))
    obs = float(np.mean(y))
    a, b = _logistic_irls(_logit(p), y)
    return {"pred_mean": pred, "obs_rate": obs,
            "cal_in_large": pred - obs,
            "slope": b, "intercept": a,
            "brier": float(np.mean((p - y) ** 2)), "n": int(len(y))}


def _decile_curve(y, p):
    qs = np.quantile(p, np.linspace(0, 1, 11))
    rows = []
    for i in range(10):
        m = (p >= qs[i]) & (p <= qs[i + 1] if i == 9 else p < qs[i + 1])
        if m.sum() == 0:
            continue
        rows.append({"group": i + 1, "pred_mean": float(p[m].mean()),
                     "obs_rate": float(y[m].mean()), "n": int(m.sum())})
    return rows


def _boot_cil(y, p, subj):
    rng = np.random.default_rng(BOOT_SEED)
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    inb = np.zeros(n, dtype=bool)
    d = np.full(N_BOOT, np.nan)
    for b_ in range(N_BOOT):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        if m.sum() > 0:
            d[b_] = float(p[m].mean() - y[m].mean())
    return (float(np.nanpercentile(d, 2.5)), float(np.nanpercentile(d, 97.5)))


# ---------- 主流程 ----------

def run():
    print(f"[calibration] device={DEVICE}", flush=True)
    metrics, platt = {}, {}
    report = ["# 主模型（SC/SCE）概率校准报告", "",
              f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
              "5 seed 概率均值集成；Platt 再校准仅 validation 拟合；"
              "bootstrap 患者级 2000 次", "",
              "背景：训练使用 BCE + pos_weight≈43（预登记不平衡处理），"
              "原始概率被系统性抬高，绝对概率不可直接用于临床阈值——"
              "决策相关分析（DCA）以校准后概率为主。", ""]

    for pop, arms in POPULATIONS.items():
        print(f"[calibration] ===== {pop} =====", flush=True)
        # test 原始分数
        sc_t = _ensemble_test_scores(*arms["sc"])
        sce_t = _ensemble_test_scores(*arms["sce"])
        subj, k, y, p_sc, p_sce = _align(sc_t, sce_t)
        # validation 推理 + Platt 拟合
        sc_v = _val_inference(*arms["sc"])
        sce_v = _val_inference(*arms["sce"])
        vsubj, vk, vy, pv_sc, pv_sce = _align(sc_v, sce_v)
        a_sc, b_sc = _logistic_irls(_logit(pv_sc), vy)
        a_sce, b_sce = _logistic_irls(_logit(pv_sce), vy)
        p_sc_cal = _sigmoid(a_sc + b_sc * _logit(p_sc))
        p_sce_cal = _sigmoid(a_sce + b_sce * _logit(p_sce))
        platt[pop] = {"sc": {"a": a_sc, "b": b_sc},
                      "sce": {"a": a_sce, "b": b_sce}}
        # 保存分数（dca.py 复用）
        np.savez_compressed(
            OUT / f"test_scores_{pop}.npz",
            subject_key=subj, landmark_k=k, y=y,
            sc_raw=p_sc, sce_raw=p_sce, sc_cal=p_sc_cal, sce_cal=p_sce_cal)
        np.savez_compressed(
            OUT / f"val_scores_{pop}.npz",
            subject_key=vsubj, landmark_k=vk, y=vy,
            sc_raw=pv_sc, sce_raw=pv_sce)

        metrics[pop] = {}
        prev = float(y.mean())
        report += [f"## {pop}（test n={len(y):,}，阳性 {int(y.sum()):,}，"
                   f"事件率 {prev:.4f}）", "",
                   "### ① 总体校准（原始 vs Platt 校准后）", "",
                   "| 模型 | 版本 | pred_mean | cal-in-large [95% CI] | "
                   "校准斜率 | 截距 | Brier |",
                   "|---|---|---|---|---|---|---|",
                   f"| 空模型（患病率） | - | {prev:.4f} | - | - | - | "
                   f"{np.mean((prev - y) ** 2):.4f} |"]
        for arm, p_raw, p_cal in (("SC", p_sc, p_sc_cal),
                                  ("SCE", p_sce, p_sce_cal)):
            for ver, p in (("原始", p_raw), ("校准后", p_cal)):
                r = _cal_metrics(y, p)
                lo, hi = _boot_cil(y, p, subj)
                r["cil_ci95"] = [lo, hi]
                metrics[pop][f"{arm}_{ver}"] = r
                report.append(
                    f"| {arm} | {ver} | {r['pred_mean']:.4f} | "
                    f"{r['cal_in_large']:+.4f} [{lo:+.4f}, {hi:+.4f}] | "
                    f"{r['slope']:.3f} | {r['intercept']:+.4f} | "
                    f"{r['brier']:.4f} |")
        report += ["", f"Platt 参数：SC a={a_sc:+.4f} b={b_sc:.4f}；"
                   f"SCE a={a_sce:+.4f} b={b_sce:.4f}（validation 拟合）", "",
                   "### ② 十分位校准曲线（校准后）", "",
                   "| 组 | SC pred | SC obs | SCE pred | SCE obs | n |",
                   "|---|---|---|---|---|---|"]
        cur_sc = _decile_curve(y, p_sc_cal)
        cur_sce = _decile_curve(y, p_sce_cal)
        metrics[pop]["decile_cal"] = {"sc": cur_sc, "sce": cur_sce}
        for r1, r2 in zip(cur_sc, cur_sce):
            report.append(
                f"| {r1['group']} | {r1['pred_mean']:.4f} | "
                f"{r1['obs_rate']:.4f} | {r2['pred_mean']:.4f} | "
                f"{r2['obs_rate']:.4f} | {r1['n']:,} |")
        report += ["", "### ③ landmark 分段 cal-in-the-large（校准后）", "",
                   "| 段 | SC | SCE |", "|---|---|---|"]
        metrics[pop]["segments_cal"] = {}
        for seg, fn in SEGMENTS.items():
            m = fn(k)
            if m.sum() == 0:
                continue
            c_sc = float(p_sc_cal[m].mean() - y[m].mean())
            c_sce = float(p_sce_cal[m].mean() - y[m].mean())
            metrics[pop]["segments_cal"][seg] = {"sc": c_sc, "sce": c_sce,
                                                 "n": int(m.sum())}
            report.append(f"| {seg} | {c_sc:+.4f} | {c_sce:+.4f} |")
        report.append("")

    report += [
        "## 解读提示（预登记口径）", "",
        "- 原始概率被 pos_weight 系统性抬高属预期行为，**不是模型缺陷**；"
        "排序指标（iAUROC/C-index）不受影响；",
        "- 校准后 cal-in-large ≈0 且斜率 ≈1 → 概率可用于阈值决策与 DCA；"
        "若校准后仍失准，报告失准区段并讨论，不做二次再校准；",
        "- Platt 仅 validation 拟合、test 只应用不拟合——test 上的残余"
        "失准是真实泛化误差，如实报告；",
        "- Brier 以空模型（患病率）为参照，校准后应明显优于空模型；",
        "- landmark 分段失准若集中于 k12+（稀疏段），报告并讨论分布漂移。"]

    (OUT / "calibration_metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    (OUT / "platt_params.json").write_text(
        json.dumps(platt, indent=2), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"[calibration] metrics + platt + REPORT.md → {OUT}", flush=True)


if __name__ == "__main__":
    run()
