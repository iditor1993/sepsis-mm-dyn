"""训练入口（训练方案 v1.1 §3）。

用法：
    python -m src.models.train.train --model grud --pkg sc_common_paired --seed 1
    python -m src.models.train.train --model sce_grud --pkg sce_common_paired --seed 1
    python -m src.models.train.train --model grud --pkg sc_common_all --seed 1
规则（预登记）：
    - BCE + pos_weight = (1-p̄)/p̄（p̄ 按训练集阳性率，逐样本集计算）
    - 患者等权 weight 列与 pos_weight 正交叠加
    - early stop 按 validation iAUROC（临床 patience=10 / SCE patience=8）
    - 超参固定（训练方案 §3.1/§3.2），不做网格搜索
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.models.data.dataset import (ART, RID, ClinicalDataset,
                                     SCEDataset)  # noqa: E402
from src.models.encoders.ecg_resnet import ECGResNet18  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.encoders.tpc import TPCEncoder  # noqa: E402
from src.models.fusion.heads import SCEModel, SCModel  # noqa: E402
from src.models.train.metrics import landmark_metrics, predict  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SEED_ROOT = 20260730


def seed_for(node: str) -> int:
    g = np.random.Generator(np.random.PCG64(
        [SEED_ROOT, abs(hash(node)) % (2**31)]))
    return int(g.integers(0, 2**31 - 1))


def build_model(name: str, static_dim: int, channel_means, deployment=False):
    if name == "grud":
        enc = GRUDEncoder(17, 128, static_dim, 64, channel_means)
        return SCModel(enc, 128 + 64), False
    if name == "tpc":
        enc = TPCEncoder(34, 16, 64, static_dim, 64)
        return SCModel(enc, 64 + 64), False
    if name == "sce_grud":
        clin = GRUDEncoder(17, 128, static_dim, 64, channel_means)
        ecg = ECGResNet18(12, 64, 512)
        return SCEModel(clin, ecg, 128 + 64, 512, 64, deployment), True
    if name == "sce_tpc":
        clin = TPCEncoder(34, 16, 64, static_dim, 64)
        ecg = ECGResNet18(12, 64, 512)
        return SCEModel(clin, ecg, 64 + 64, 512, 64, deployment), True
    raise ValueError(name)


def get_pos_weight(idx) -> float:
    p = float(idx["y_24h"].mean())
    return (1 - p) / max(p, 1e-9)


def train_one(args):
    pkg_dir = ART / "p9_packages" / RID / args.pkg
    tensor_dir = ART / "p7_fitted" / RID
    raw_dir = ART / "p2_clinical" / RID / "master"
    ecg_dir = ART / "p5_ecg_cache" / RID
    node = f"{args.model}/{args.pkg}/seed_{args.seed}"
    rng = seed_for(node)
    torch.manual_seed(rng)
    np.random.seed(rng)
    print(f"[train] {node} device={DEVICE}")

    is_sce = args.model.startswith("sce_")
    deployment = args.pkg == "sce_deployment"
    if is_sce:
        tr_ds = SCEDataset(pkg_dir / "train", tensor_dir, ecg_dir,
                           raw_tensor_dir=raw_dir,
                           use_dropout_group=deployment, ecg_suffix="_v2")
        va_ds = SCEDataset(pkg_dir / "validation", tensor_dir, ecg_dir,
                           raw_tensor_dir=raw_dir, ecg_suffix="_v2")
        tr_ds.training_dropout = deployment
    else:
        tr_ds = ClinicalDataset(pkg_dir / "train", tensor_dir,
                                raw_tensor_dir=raw_dir)
        va_ds = ClinicalDataset(pkg_dir / "validation", tensor_dir,
                                raw_tensor_dir=raw_dir)

    bs = 64 if is_sce else 512
    # num_workers=0：数据集已在主进程持有全部张量（RAM），
    # worker 进程只会复制内存并在 Windows 上触发 pickle 失败；
    # ECG 走 memmap + OS 页缓存，主进程读取足够快（GPU 为瓶颈）。
    tr_ld = DataLoader(tr_ds, batch_size=bs, shuffle=True,
                       num_workers=0, pin_memory=True,
                       drop_last=False)
    va_ld = DataLoader(va_ds, batch_size=bs, shuffle=False,
                       num_workers=0, pin_memory=True)

    # 通道均值（P7 工件，GRU-D 用）
    ch_params = json.loads((tensor_dir / "scaler_clinical_seq.json")
                           .read_text(encoding="utf-8"))
    channels = json.loads((pkg_dir / "train" / "manifest.json")
                          .read_text(encoding="utf-8"))["channels"]
    means = torch.tensor([ch_params["channels"][c]["mean"]
                          for c in channels], dtype=torch.float32)

    model, ecg_mode = build_model(args.model, tr_ds.static.shape[1],
                                  means, deployment)
    model.to(DEVICE)
    lr = 3e-4 if is_sce else 1e-3
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    pw = get_pos_weight(tr_ds.idx)
    print(f"[train] pos_weight={pw:.2f} (train prev={tr_ds.idx['y_24h'].mean():.4f})")
    bce = nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(pw, device=DEVICE), reduction="none")

    patience = 8 if is_sce else 10
    max_epochs = 50 if is_sce else 100
    best_iauroc, best_state, bad = -1.0, None, 0
    hist = []
    for ep in range(1, max_epochs + 1):
        model.train()
        t0 = time.time()
        tot, wsum = 0.0, 0.0
        for batch in tr_ld:
            x = batch["x"].to(DEVICE, non_blocking=True)
            m = batch["m"].to(DEVICE, non_blocking=True)
            d = batch["d"].to(DEVICE, non_blocking=True)
            st = batch["static"].to(DEVICE, non_blocking=True)
            y = batch["y"].to(DEVICE, non_blocking=True)
            w = batch["w"].to(DEVICE, non_blocking=True)
            if ecg_mode:
                s = model(x, m, d, st,
                          batch["ecg"].to(DEVICE, non_blocking=True),
                          batch["ecg_avail"].to(DEVICE, non_blocking=True))
            else:
                s = model(x, m, d, st)
            loss = (bce(s, y) * w).sum() / w.sum().clamp(min=1e-9)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * float(w.sum())
            wsum += float(w.sum())
        vy, vs, vk = predict(model, va_ld, DEVICE, ecg=ecg_mode)
        met = landmark_metrics(vy, vs, vk)
        ia = met["iauroc_partial"] or 0.0
        hist.append({"epoch": ep, "loss": tot / max(wsum, 1e-9),
                     "val_iauroc": ia, "val_brier": met["brier"]})
        print(f"[train] ep{ep} loss={tot / max(wsum,1e-9):.4f} "
              f"val_iAUROC={ia:.4f} brier={met['brier']:.4f} "
              f"({time.time() - t0:.0f}s)")
        if ia > best_iauroc:
            best_iauroc, bad = ia, 0
            best_state = {k: v.detach().clone() for k, v
                          in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                print(f"[train] early stop at ep{ep} (best={best_iauroc:.4f})")
                break

    out_dir = ROOT / "src" / "models" / "runs" / args.pkg / args.model \
        / f"seed_{args.seed}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if best_state is not None:
        model.load_state_dict(best_state)
    torch.save(model.state_dict(), out_dir / "model.pt")

    # test 评估（冻结 best 权重，仅一次）
    if is_sce:
        te_ds = SCEDataset(pkg_dir / "test", tensor_dir, ecg_dir,
                           raw_tensor_dir=raw_dir, ecg_suffix="_v2")
    else:
        te_ds = ClinicalDataset(pkg_dir / "test", tensor_dir,
                                raw_tensor_dir=raw_dir)
    te_ld = DataLoader(te_ds, batch_size=bs, shuffle=False,
                       num_workers=0)
    ty, ts, tk = predict(model, te_ld, DEVICE, ecg=ecg_mode)
    tmet = landmark_metrics(ty, ts, tk)
    result = {"node": node, "best_val_iauroc": best_iauroc,
              "test": tmet, "pos_weight": pw,
              "history": hist, "rng_seed": rng}
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")
    # 保存逐样本测试预测（主比较 ΔiAUROC 患者级 bootstrap 用）
    np.savez_compressed(
        out_dir / "predictions.npz",
        subject_key=te_ds.idx["subject_key"].to_numpy(),
        landmark_k=tk, y_24h=ty, y_score=ts)
    print(f"[train] test iAUROC={tmet['iauroc']} "
          f"(estimable {tmet['n_estimable']}/12) saved {out_dir}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True,
                    choices=["grud", "tpc", "sce_grud", "sce_tpc"])
    ap.add_argument("--pkg", required=True)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    train_one(args)


if __name__ == "__main__":
    main()
