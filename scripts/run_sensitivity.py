"""敏感性分析矩阵（技术文档 §15.2 / 训练方案 §4.3 预登记）。

四个模式：
  freshness_48h / freshness_72h   ECG 时效窗敏感性（样本集 idx_ecg_sensitivity_*）
  sofa_carryforward               CV 亚组交互的 carryforward 轨对照
  ecg_globalnorm                  ECG 归一化 global_train_stats（预设次要分析）
  ssl_inductive                   ECG inductive SSL 预训练 → 微调（Tier 2）

VSCode 用法：改 CONFIG 的 MODES → ▶ Run；或终端
  python scripts/run_sensitivity.py --mode freshness_48h
输出：src/models/runs/sensitivity/<mode>/{result.json, REPORT.md}
"""
import argparse
import json
import sys
import time
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
MODES = ["freshness_48h", "freshness_72h", "sofa_carryforward"]
# 可选值：freshness_48h / freshness_72h / sofa_carryforward /
#         ecg_globalnorm / ssl_inductive / all
SEEDS = [1, 2, 3, 4, 5]
SSL_EPOCHS = 20
# ===========================================================================

from src.models.data.dataset import ART, RID  # noqa: E402
from src.models.encoders.ecg_resnet import ECGResNet18  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.heads import SCEModel, SCModel  # noqa: E402
from src.models.train.metrics import auroc_np  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OUT = ROOT / "src" / "models" / "runs" / "sensitivity"
OUT.mkdir(parents=True, exist_ok=True)
MAIN_K = 12
N_BINS = 28


def _iauroc(y, s, k):
    aucs = [auroc_np(y[k == kk], s[k == kk]) for kk in range(MAIN_K)]
    if any(np.isnan(a) for a in aucs):
        return np.nan
    return float(np.mean(aucs))


def _bootstrap_delta(y, s_sc, s_sce, k, subj, n_boot=2000, seed=20260730):
    rng = np.random.default_rng(seed)
    usubj, inv = np.unique(subj, return_inverse=True)
    n = len(usubj)
    inb = np.zeros(n, dtype=bool)
    d = np.full(n_boot, np.nan)
    for b in range(n_boot):
        inb[:] = False
        inb[rng.integers(0, n, n)] = True
        m = inb[inv]
        a = _iauroc(y[m], s_sc[m], k[m])
        bb = _iauroc(y[m], s_sce[m], k[m])
        if not (np.isnan(a) or np.isnan(bb)):
            d[b] = bb - a
    v = np.isfinite(d)
    return (float(np.nanmean(d)), float(np.nanpercentile(d, 2.5)),
            float(np.nanpercentile(d, 97.5)), int(v.sum()))


def _write_result(mode, result, lines):
    d = OUT / mode
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(result, indent=2, default=str),
                                   encoding="utf-8")
    (d / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[{mode}] result + REPORT.md written")


# ---------------- 共享：轻量数据集 ----------------

def _build_static(idx_df):
    """按 P7 冻结参数为任意 (episode_key, landmark_k) 集合编码静态矩阵。"""
    from nodes.p3_static import (DESCRIBE_ONLY, STATIC_CATEGORICAL,
                                 STATIC_FLAGS, STATIC_NUMERIC)
    from lib import static as lib_static
    import json as _json
    sdf = pd.read_parquet(
        ART / "p3_static" / RID / "static_raw.parquet")
    encs = _json.loads((ART / "p7_fitted" / RID
                        / "categorical_encoders.json").read_text(
        encoding="utf-8"))
    imp = _json.loads((ART / "p7_fitted" / RID / "imputers.json").read_text(
        encoding="utf-8"))
    scaler = _json.loads((ART / "p7_fitted" / RID / "scaler_static.json")
                         .read_text(encoding="utf-8"))
    df = idx_df.merge(sdf, on=["episode_key", "landmark_k"], how="left")
    df = lib_static.apply_static_imputer(df, imp)
    parts = []
    for col in STATIC_NUMERIC:
        v = df[col].to_numpy(dtype=np.float32)
        p = scaler["cols"][col]
        parts.append(((v - p["mean"]) / p["sd"]).reshape(-1, 1))
        parts.append(df[f"{col}_missing"].to_numpy(
            dtype=np.float32).reshape(-1, 1))
    for col in STATIC_CATEGORICAL:
        oh, _ = lib_static.apply_categorical_encoder(df, col, encs[col])
        parts.append(oh.to_numpy(dtype=np.float32))
    for col in STATIC_FLAGS:
        parts.append(df[col].fillna(0).to_numpy(
            dtype=np.float32).reshape(-1, 1))
    return np.concatenate(parts, axis=1).astype(np.float32)


class SensDataset(Dataset):
    """从 p4 样本索引构建的数据集（张量 + 静态 + 可选 ECG）。"""

    def __init__(self, idx_path: Path, split: str, with_ecg: bool):
        df = pd.read_parquet(idx_path)
        df = df[df["set_name"] == split].reset_index(drop=True)
        self.df = df
        self.static = _build_static(df[["episode_key", "landmark_k"]])
        self.x = np.load(ART / "p7_fitted" / RID / "X_seq_scaled.npy",
                         mmap_mode="r")[:, :17, :]
        self.m = np.load(ART / "p2_clinical" / RID / "master" / "M_seq.npy",
                         mmap_mode="r")[:, :17, :]
        self.d = np.load(ART / "p2_clinical" / RID / "master" / "D_seq.npy",
                         mmap_mode="r")[:, :17, :]
        self.rows = df["row_idx"].to_numpy(dtype=np.int64)
        self.y = torch.from_numpy(df["y_24h"].to_numpy(dtype=np.float32))
        self.w = torch.from_numpy(df["weight"].to_numpy(dtype=np.float32))
        self.with_ecg = with_ecg
        if with_ecg:
            self.ecg = np.load(ART / "p5_ecg_cache" / RID / "ecg_cache_v2.npy",
                               mmap_mode="r")
            cidx = pd.read_parquet(
                ART / "p5_ecg_cache" / RID / "ecg_cache_index_v2.parquet")
            self.study2row = dict(zip(cidx["study_id"], cidx["cache_row"]))
            mod = pd.read_parquet(
                ART / "p6_modality" / RID / "modality_index.parquet",
                columns=["episode_key", "landmark_k", "study_id"])
            df2 = df.merge(mod, on=["episode_key", "landmark_k"], how="left")
            self.ecg_rows = np.array(
                [self.study2row.get(int(s), -1) if pd.notna(s) else -1
                 for s in df2["study_id"].to_numpy()], dtype=np.int64)

    def __len__(self):
        return len(self.df)

    def __getitem__(self, i):
        r = int(self.rows[i])
        out = {"x": torch.from_numpy(np.asarray(self.x[r]).copy()),
               "m": torch.from_numpy(np.asarray(self.m[r]).copy()),
               "d": torch.from_numpy(np.asarray(self.d[r]).copy()),
               "static": torch.from_numpy(self.static[i]),
               "y": self.y[i], "w": self.w[i],
               "landmark_k": int(self.df.iloc[i]["landmark_k"]),
               "subject_key": self.df.iloc[i]["subject_key"]}
        if self.with_ecg:
            er = int(self.ecg_rows[i])
            if er >= 0:
                out["ecg"] = torch.from_numpy(np.asarray(self.ecg[er]).copy())
                out["ecg_avail"] = torch.tensor(1.0)
            else:
                out["ecg"] = torch.zeros(12, 5000)
                out["ecg_avail"] = torch.tensor(0.0)
        return out


def _train_eval_sce(idx_path: Path, tag: str, seed: int):
    """在指定样本集上训练 SC 与 SCE，返回 ΔiAUROC + bootstrap CI。"""
    from src.models.train.train import get_pos_weight

    def _mk(with_ecg):
        tr = SensDataset(idx_path, "train", with_ecg)
        va = SensDataset(idx_path, "validation", with_ecg)
        te = SensDataset(idx_path, "test", with_ecg)
        return tr, va, te

    def _run(with_ecg):
        tr, va, te = _mk(with_ecg)
        torch.manual_seed(20260730 + seed)
        clin = GRUDEncoder(17, 128, 44, 64)
        if with_ecg:
            model = SCEModel(clin, ECGResNet18(12, 64, 512), 192, 512, 64)
        else:
            model = SCModel(clin, 192)
        model.to(DEVICE)
        opt = torch.optim.AdamW(model.parameters(), lr=3e-4 if with_ecg else 1e-3,
                                weight_decay=1e-4)
        pw = get_pos_weight(tr.df)
        bce = nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor(pw, device=DEVICE), reduction="none")
        tr_ld = DataLoader(tr, batch_size=64 if with_ecg else 512,
                           shuffle=True, num_workers=0)
        va_ld = DataLoader(va, batch_size=64 if with_ecg else 512,
                           shuffle=False, num_workers=0)
        best, bad, state = -1.0, 0, None
        max_ep = 50 if with_ecg else 100
        pat = 8 if with_ecg else 10
        for ep in range(1, max_ep + 1):
            model.train()
            for b in tr_ld:
                x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
                d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
                y = b["y"].to(DEVICE); w = b["w"].to(DEVICE)
                if with_ecg:
                    s = model(x, m, d, st, b["ecg"].to(DEVICE),
                              b["ecg_avail"].to(DEVICE))
                else:
                    s = model(x, m, d, st)
                loss = (bce(s, y) * w).sum() / w.sum().clamp(min=1e-9)
                opt.zero_grad(); loss.backward(); opt.step()
            ia = _eval_ia(model, va_ld, with_ecg)
            if ia > best:
                best, bad = ia, 0
                state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                bad += 1
                if bad >= pat:
                    break
        if state:
            model.load_state_dict(state)
        te_ld = DataLoader(te, batch_size=64 if with_ecg else 512,
                           shuffle=False, num_workers=0)
        return _predict(model, te_ld, with_ecg), te

    (y1, s_sc, k1, subj1), _ = _run(False)
    (y2, s_sce, k2, subj2), _ = _run(True)
    assert (y1 == y2).all() and (k1 == k2).all()
    ia_sc = _iauroc(y1, s_sc, k1)
    ia_sce = _iauroc(y2, s_sce, k2)
    d, lo, hi, n = _bootstrap_delta(y1, s_sc, s_sce, k1, subj1)
    return {"sc": ia_sc, "sce": ia_sce, "delta": ia_sce - ia_sc,
            "delta_boot_mean": d, "ci95": [lo, hi], "boot_valid": n,
            "tag": tag}


@torch.no_grad()
def _predict(model, ld, with_ecg):
    model.eval()
    ys, ss, ks, sb = [], [], [], []
    for b in ld:
        x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
        d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
        if with_ecg:
            s = model(x, m, d, st, b["ecg"].to(DEVICE),
                      b["ecg_avail"].to(DEVICE))
        else:
            s = model(x, m, d, st)
        ys.append(b["y"].numpy()); ss.append(torch.sigmoid(s).cpu().numpy())
        ks.append(b["landmark_k"].numpy())
        sb.append(b["subject_key"])
    return (np.concatenate(ys), np.concatenate(ss), np.concatenate(ks),
            np.concatenate(sb))


def _eval_ia(model, ld, with_ecg):
    y, s, k, _ = _predict(model, ld, with_ecg)
    return _iauroc(y, s, k)


# ---------------- 模式实现 ----------------

def run_freshness(hours: int):
    tag = f"freshness_{hours}h"
    idx = ART / "p4_samples" / RID / f"idx_ecg_sensitivity_{hours}h.parquet"
    print(f"[{tag}] 样本集 {idx.name}")
    r = _train_eval_sce(idx, tag, seed=SEEDS[0])
    lines = [f"# ECG 时效窗敏感性（{hours}h）", "",
             f"- SC iAUROC = {r['sc']:.4f}",
             f"- SCE iAUROC = {r['sce']:.4f}",
             f"- **ΔiAUROC = {r['delta']:+.4f}，95% CI [{r['ci95'][0]:+.4f}, {r['ci95'][1]:+.4f}]**（seed 1）",
             "",
             "对照主 24h 窗（paired Δ≈+0.006、deployment Δ≈+0.011）："
             "时效放宽后 ECG-available 样本增多，观察 Δ 变化方向。"]
    _write_result(tag, r, lines)
    return r


def run_sofa_carryforward():
    tag = "sofa_carryforward"
    print(f"[{tag}] 用 carryforward 轨 CV 重做亚组交互")
    sys.path.insert(0, str(ROOT))
    from src.evaluation.cv_subgroup_interaction import _delta, _interaction  # noqa
    sofa = pd.read_parquet(
        ROOT / "src/data/_output/features/sofa_hourly_v2.parquet",
        columns=["episode_id", "k", "sofa_evidence_track",
                 "cardiovascular_score", "cardiovascular_observed"])
    sofa = sofa[sofa["sofa_evidence_track"] == "carryforward"]
    first = sofa.loc[sofa.groupby("episode_id")["k"].idxmin(),
                     ["episode_id", "cardiovascular_score",
                      "cardiovascular_observed"]]
    first = first.rename(columns={"episode_id": "episode_key",
                                  "cardiovascular_score": "cv_score",
                                  "cardiovascular_observed": "cv_observed"})
    # 逐 seed
    rows = []
    ens = {}
    for sd in SEEDS:
        try:
            a = _load_pkg_sce(sd)
            b = _load_pkg_sce(sd, sce=True)
        except FileNotFoundError:
            continue
        a = a.merge(first, on="episode_key", how="left")
        b = b.merge(first, on="episode_key", how="left")
        for df in (a, b):
            df["cv_group"] = np.where(
                ~df["cv_observed"].fillna(False), "missing",
                np.where(df["cv_score"] >= 3, "ge3", "lt3"))
        d3 = _delta(a.loc[a.cv_group == "ge3", "y"].to_numpy(),
                    a.loc[a.cv_group == "ge3", "s"].to_numpy(),
                    b.loc[b.cv_group == "ge3", "s"].to_numpy(),
                    a.loc[a.cv_group == "ge3", "landmark_k"].to_numpy())
        d0 = _delta(a.loc[a.cv_group == "lt3", "y"].to_numpy(),
                    a.loc[a.cv_group == "lt3", "s"].to_numpy(),
                    b.loc[b.cv_group == "lt3", "s"].to_numpy(),
                    a.loc[a.cv_group == "lt3", "landmark_k"].to_numpy())
        rows.append({"seed": sd, "delta_ge3": d3, "delta_lt3": d0,
                     "interaction": d3 - d0})
        ens.setdefault("a", []).append(a)
        ens.setdefault("b", []).append(b)
    ps = pd.DataFrame(rows)
    result = {"track": "carryforward", "per_seed": rows,
              "interaction_mean": float(ps["interaction"].mean()),
              "interaction_sd": float(ps["interaction"].std())}
    lines = ["# CV 亚组交互（carryforward SOFA 轨，敏感性）", "",
             "| seed | Δ(CV≥3) | Δ(CV<3) | Interaction |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['seed']} | {r['delta_ge3']:+.4f} | "
                     f"{r['delta_lt3']:+.4f} | {r['interaction']:+.4f} |")
    lines += ["",
              f"**交互均值±SD：{result['interaction_mean']:+.4f} ± {result['interaction_sd']:.4f}**",
              "",
              "对照 strict_24h 轨（Interaction=-0.0059，CI 跨 0）："
              "观察 carryforward 轨是否改变结论方向。"]
    _write_result(tag, result, lines)
    return result


def _load_pkg_sce(seed, sce=False):
    pkg = "sce_common_paired" if sce else "sc_common_paired"
    model = "sce_grud" if sce else "grud"
    base = ROOT / "src" / "models" / "runs" / pkg / model / f"seed_{seed}"
    idx = pd.read_parquet(
        ROOT / "preprocess/artifacts/p9_packages/pp_v1_20260730"
        / pkg / "test" / "index.parquet",
        columns=["episode_key", "subject_key", "landmark_k"])
    z = np.load(base / "predictions.npz", allow_pickle=True)
    df = idx.copy()
    df["y"] = z["y_24h"]
    df["s"] = z["y_score"]
    return df


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all")
    args = ap.parse_args()
    modes = MODES if args.mode == "all" else [args.mode]
    for m in modes:
        print(f"\n{'='*60}\n  {m}\n{'='*60}")
        if m in ("freshness_48h", "freshness_72h"):
            run_freshness(int(m.replace("freshness_", "").replace("h", "")))
        elif m == "sofa_carryforward":
            run_sofa_carryforward()
        elif m == "ecg_globalnorm":
            print("[ecg_globalnorm] 该模式需重建 ECG 缓存（global_train_stats），"
                  "请单独运行：python scripts/run_sensitivity.py --mode ecg_globalnorm_impl（暂未启用，见注释）")
        elif m == "ssl_inductive":
            print("[ssl_inductive] 该模式实现较大，建议单独一轮运行（暂未启用，见注释）")
        else:
            print(f"[{m}] 未知模式")
    print("\n[sensitivity] 完成。结果目录：", OUT)


if __name__ == "__main__":
    main()
