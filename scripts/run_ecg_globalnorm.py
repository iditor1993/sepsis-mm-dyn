"""ECG 归一化敏感性：global_train_stats（训练方案 §2.3 预设次要分析）。

主方案 per-record z-score 抹掉振幅信息（低电压/QRS 振幅变化的预后价值）；
本模式用训练集全局均值/SD 归一化重建缓存并重训 SCE，与主方案同报。

流程：
  ① 训练集 study 估计逐导联全局 mean/sd（原始 WFDB，fitted_on=train）
  ② 重建 global 归一化缓存 ecg_cache_global.npy（36,316 × [12,5000]）
  ③ 用该缓存重训 SCE × SEEDS，对比 per-record 主结果（ΔiAUROC）

VSCode：改 CONFIG → ▶ Run；或终端 python scripts/run_ecg_globalnorm.py --mode full
输出：preprocess/artifacts/p5_ecg_cache/pp_v1_20260730/ecg_cache_global.npy
      src/models/runs/sensitivity/ecg_globalnorm/{result.json, REPORT.md}
"""
import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocess" / "src"))

# ============================ CONFIG（只改这里） ============================
MODE = "full"          # cache_only(只建缓存) / full(缓存+训练) / train_only
SEEDS = [1, 2, 3, 4, 5]
WORKERS = 6
# ===========================================================================

from lib import io  # noqa: E402

warnings.filterwarnings("ignore")
OUT = ROOT / "src" / "models" / "runs" / "sensitivity" / "ecg_globalnorm"
OUT.mkdir(parents=True, exist_ok=True)

# 模块级全局（multiprocessing 经 initializer 注入，Windows spawn 兼容）
_G_MEAN = None
_G_SD = None


def _stats_accum(path):
    """worker：返回单文件的逐导联 (sums, sums2, cnt)。"""
    import os
    import sys as _sys
    _sys.stdout = open(os.devnull, "w")
    _sys.stderr = open(os.devnull, "w")
    import wfdb
    rec = wfdb.rdrecord(path)
    sig = np.nan_to_num(rec.p_signal.astype(np.float64), nan=0.0)
    n = min(sig.shape[1], 12)
    sums = np.zeros(12); sums2 = np.zeros(12); cnt = np.zeros(12)
    sums[:n] += sig.sum(axis=0)
    sums2[:n] += (sig ** 2).sum(axis=0)
    cnt[:n] += sig.shape[0]
    return sums, sums2, cnt


def _init_norm(mean, sd):
    global _G_MEAN, _G_SD
    _G_MEAN = mean
    _G_SD = sd


def _norm_one(path):
    """worker：读取并按全局 mean/sd 归一化（返回 [12,5000]）。"""
    import os
    import sys as _sys
    _sys.stdout = open(os.devnull, "w")
    _sys.stderr = open(os.devnull, "w")
    import wfdb
    from lib.ecg import LEADS, TARGET_FS, DUR_S
    rec = wfdb.rdrecord(path)
    sig = np.nan_to_num(rec.p_signal.astype(np.float32), nan=0.0)
    out = np.zeros((12, TARGET_FS * DUR_S), dtype=np.float32)
    n2i = {str(nm).strip(): i for i, nm in enumerate(rec.sig_name)}
    for li, lead in enumerate(LEADS):
        if lead in n2i:
            s = sig[:, n2i[lead]][: TARGET_FS * DUR_S]
            if len(s) == TARGET_FS * DUR_S:
                out[li] = (s - _G_MEAN[li]) / (_G_SD[li] + 1e-8)
    return out


def _global_stats(cfg):
    """训练集 study 的逐导联全局 mean/sd（流式两趟）。"""
    out7 = ROOT / cfg["paths"]["out_root"] / "p7_fitted" / cfg["run_id"]
    cache_dir = ROOT / cfg["paths"]["out_root"] / "p5_ecg_cache" / cfg["run_id"]
    stat_path = out7 / "ecg_global_norm_stats.json"
    if stat_path.exists():
        return json.loads(stat_path.read_text(encoding="utf-8"))
    idx = pd.read_parquet(cache_dir / "ecg_cache_index_v2.parquet")
    splits = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "splits/split_assignments_v2.parquet")
    ecg_rec = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "ecg_index/ecg_landmark_index_v2.parquet",
        columns=["study_id", "episode_id"])
    cohort = pd.read_parquet(
        ROOT / cfg["paths"]["data_pipeline_root"]
        / "cohorts/cohort_mimic_v2.parquet",
        columns=["episode_id", "subject_id"])
    st2sub = ecg_rec.merge(cohort, on="episode_id")[
        ["study_id", "subject_id"]].drop_duplicates("study_id")
    df = idx.merge(st2sub, on="study_id", how="left")
    df = df.merge(splits[["subject_id", "set_name"]],
                  on="subject_id", how="left")
    train = df[df["set_name"] == "train"]
    print(f"[globalnorm] train studies: {len(train):,}")
    root = ROOT / cfg["paths"]["ecg_wfdb_root"]

    from multiprocessing import Pool
    sums = np.zeros(12); sums2 = np.zeros(12); cnt = np.zeros(12)
    with Pool(WORKERS) as pool:
        for i, (s, s2, c) in enumerate(pool.imap_unordered(
                _stats_accum,
                [str(root / str(p)) for p in train["ecg_path"]],
                chunksize=32)):
            sums += s; sums2 += s2; cnt += c
            if (i + 1) % 5000 == 0:
                print(f"[globalnorm] stats {i + 1:,}/{len(train):,}")
    mean = sums / np.maximum(cnt, 1)
    sd = np.sqrt(np.maximum(sums2 / np.maximum(cnt, 1) - mean ** 2, 0.0))
    stats = {"fitted_on": "train",
             "mean": mean.tolist(), "sd": sd.tolist()}
    io.write_json(stats, stat_path)
    return stats


def _build_cache(cfg, stats):
    cache_dir = ROOT / cfg["paths"]["out_root"] / "p5_ecg_cache" / cfg["run_id"]
    idx = pd.read_parquet(cache_dir / "ecg_cache_index_v2.parquet")
    tensor_path = cache_dir / "ecg_cache_global.npy"
    if tensor_path.exists():
        print("[globalnorm] cache exists, skip build")
        return
    mean = np.array(stats["mean"], dtype=np.float32)
    sd = np.array(stats["sd"], dtype=np.float32)
    root = ROOT / cfg["paths"]["ecg_wfdb_root"]
    arr = np.lib.format.open_memmap(
        tensor_path, mode="w+", dtype=np.float32,
        shape=(len(idx), 12, 5000))

    from multiprocessing import Pool
    with Pool(WORKERS, initializer=_init_norm, initargs=(mean, sd)) as pool:
        for i, t in enumerate(pool.imap(
                _norm_one, [str(root / str(p)) for p in idx["ecg_path"]],
                chunksize=32)):
            arr[i] = t
            if (i + 1) % 5000 == 0:
                arr.flush()
                print(f"[globalnorm] cache {i + 1:,}/{len(idx):,}")
    arr.flush()
    print(f"[globalnorm] cache built: {tensor_path}")


def _train_eval(cfg, stats):
    """用 global 缓存重训 SCE，对比 per-record 主结果。"""
    sys.path.insert(0, str(ROOT))
    import torch
    from src.models.data.dataset import ART, RID, SCEDataset
    from src.models.encoders.ecg_resnet import ECGResNet18
    from src.models.encoders.grud import GRUDEncoder
    from src.models.fusion.heads import SCEModel
    from src.models.train.train import get_pos_weight
    from torch.utils.data import DataLoader
    import torch.nn as nn

    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    results = []
    for seed in SEEDS:
        # 用 global 缓存（v2 索引 + 覆盖 .ecg 为 global 归一化张量）
        tr = SCEDataset(ART / "p9_packages" / RID / "sce_common_paired"
                        / "train", ART / "p7_fitted" / RID,
                        ART / "p5_ecg_cache" / RID,
                        raw_tensor_dir=ART / "p2_clinical" / RID / "master",
                        ecg_suffix="_v2")
        tr.ecg = np.load(ART / "p5_ecg_cache" / RID
                         / "ecg_cache_global.npy", mmap_mode="r")
        va = SCEDataset(ART / "p9_packages" / RID / "sce_common_paired"
                        / "validation", ART / "p7_fitted" / RID,
                        ART / "p5_ecg_cache" / RID,
                        raw_tensor_dir=ART / "p2_clinical" / RID / "master",
                        ecg_suffix="_v2")
        va.ecg = tr.ecg
        te = SCEDataset(ART / "p9_packages" / RID / "sce_common_paired"
                        / "test", ART / "p7_fitted" / RID,
                        ART / "p5_ecg_cache" / RID,
                        raw_tensor_dir=ART / "p2_clinical" / RID / "master",
                        ecg_suffix="_v2")
        te.ecg = tr.ecg
        torch.manual_seed(20260730 + seed)
        model = SCEModel(GRUDEncoder(17, 128, 44, 64),
                         ECGResNet18(12, 64, 512), 192, 512, 64).to(DEVICE)
        opt = torch.optim.AdamW(model.parameters(), lr=3e-4,
                                weight_decay=1e-4)
        pw = get_pos_weight(tr.idx)
        bce = nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor(pw, device=DEVICE), reduction="none")
        tr_ld = DataLoader(tr, batch_size=64, shuffle=True, num_workers=0)
        va_ld = DataLoader(va, batch_size=64, shuffle=False, num_workers=0)
        te_ld = DataLoader(te, batch_size=64, shuffle=False, num_workers=0)
        best, bad, state = -1.0, 0, None
        for ep in range(1, 51):
            model.train()
            for b in tr_ld:
                s = model(b["x"].to(DEVICE), b["m"].to(DEVICE),
                          b["d"].to(DEVICE), b["static"].to(DEVICE),
                          b["ecg"].to(DEVICE), b["ecg_avail"].to(DEVICE))
                loss = (bce(s, b["y"].to(DEVICE))
                        * b["w"].to(DEVICE)).sum() / b["w"].to(DEVICE).sum().clamp(min=1e-9)
                opt.zero_grad(); loss.backward(); opt.step()
            from src.models.train.metrics import predict, landmark_metrics
            vy, vs, vk = predict(model, va_ld, DEVICE, ecg=True)
            ia = landmark_metrics(vy, vs, vk)["iauroc_partial"] or 0
            print(f"[globalnorm] seed{seed} ep{ep} val={ia:.4f}")
            if ia > best:
                best, bad = ia, 0
                state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                bad += 1
                if bad >= 8:
                    break
        if state:
            model.load_state_dict(state)
        ty, ts, tk = predict(model, te_ld, DEVICE, ecg=True)
        tmet = landmark_metrics(ty, ts, tk)
        results.append({"seed": seed, "best_val": best,
                        "test_iauroc": tmet["iauroc"]})
        torch.save(model.state_dict(), OUT / f"model_seed{seed}.pt")
        print(f"[globalnorm] seed{seed} test iAUROC={tmet['iauroc']}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=MODE,
                    choices=["cache_only", "full", "train_only"])
    args = ap.parse_args()
    cfg = io.load_config()
    stats = _global_stats(cfg)
    if args.mode in ("cache_only", "full"):
        _build_cache(cfg, stats)
    if args.mode in ("full", "train_only"):
        results = _train_eval(cfg, stats)
        import numpy as np
        ias = [r["test_iauroc"] for r in results if r["test_iauroc"]]
        lines = ["# ECG 归一化敏感性：global_train_stats", "",
                 f"- 全局归一化（训练集 mean/sd，fitted_on=train）SCE 重训",
                 f"- 逐 seed test iAUROC：" +
                 ", ".join(f"{r['test_iauroc']:.4f}" for r in results),
                 f"- **均值±SD：{np.mean(ias):.4f} ± {np.std(ias):.4f}**",
                 "",
                 "对照 per-record z-score 主结果（SCE paired test "
                 "iAUROC≈0.823±0.010）：评估保留振幅信息的增量/损失。"]
        (OUT / "result.json").write_text(
            json.dumps({"per_seed": results, "mean": float(np.mean(ias)),
                        "sd": float(np.std(ias))}, indent=2),
            encoding="utf-8")
        (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"[globalnorm] 均值={np.mean(ias):.4f}，REPORT.md written")


if __name__ == "__main__":
    main()
