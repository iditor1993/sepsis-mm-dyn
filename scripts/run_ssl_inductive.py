"""ECG inductive SSL 预训练 → 微调（技术文档 §10.5 Tier 2 预设次要分析）。

仅训练患者 ECG（inductive，测试患者完全不参与预训练）。
SimCLR（NT-Xent）对比预训练 ResNet-18 编码器（投影头 128 维）；
增强（训练方案 §3.2 已登记）：时间平移 ≤0.5s、振幅缩放 0.9–1.1、轻噪、
lead dropout p=0.1（同步清 lead_mask）。
随后用 SSL 权重初始化 SCE 微调，对比从头训练主结果。

VSCode：改 CONFIG → ▶ Run；或终端 python scripts/run_ssl_inductive.py --mode full
输出：src/models/runs/sensitivity/ssl_inductive/{ssl_encoder.pt, result.json, REPORT.md}
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
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ============================ CONFIG（只改这里） ============================
MODE = "full"         # ssl_only(只预训练) / full(预训练+微调) / finetune_only
SEEDS = [1, 2, 3, 4, 5]
SSL_EPOCHS = 20
SSL_BATCH = 128
SSL_LR = 3e-4
# ===========================================================================

from src.models.data.dataset import ART, RID, SCEDataset  # noqa: E402
from src.models.encoders.ecg_resnet import ECGResNet18  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.heads import SCEModel  # noqa: E402

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OUT = ROOT / "src" / "models" / "runs" / "sensitivity" / "ssl_inductive"
OUT.mkdir(parents=True, exist_ok=True)


def _augment(x, rng):
    """x: [B, 12, 5000]。时间平移 ≤0.5s、振幅 0.9–1.1、轻噪、lead dropout。"""
    b = x.clone()
    B = b.shape[0]
    shift = int(rng.integers(-250, 250))
    if shift != 0:
        b = torch.roll(b, shifts=shift, dims=-1)
    scale = torch.from_numpy(rng.uniform(0.9, 1.1, (B, 1, 1))).to(x.dtype)
    b = b * scale.to(b.device)
    b = b + torch.from_numpy(
        rng.normal(0, 0.02, b.shape)).to(x.dtype).to(b.device)
    drop = torch.from_numpy(rng.random((B, 12, 1)) < 0.1).to(b.device)
    b = b.masked_fill(drop, 0.0)
    return b


class SSLDataset(Dataset):
    """训练患者 ECG（inductive）。"""

    def __init__(self):
        cache_dir = ART / "p5_ecg_cache" / RID
        idx = pd.read_parquet(cache_dir / "ecg_cache_index_v2.parquet")
        splits = pd.read_parquet(
            ROOT / "src/data/_output/splits/split_assignments_v2.parquet")
        ecg_rec = pd.read_parquet(
            ROOT / "src/data/_output/ecg_index/ecg_landmark_index_v2.parquet",
            columns=["study_id", "episode_id"])
        cohort = pd.read_parquet(
            ROOT / "src/data/_output/cohorts/cohort_mimic_v2.parquet",
            columns=["episode_id", "subject_id"])
        st2sub = ecg_rec.merge(cohort, on="episode_id")[
            ["study_id", "subject_id"]].drop_duplicates("study_id")
        df = idx.merge(st2sub, on="study_id", how="left")
        df = df.merge(splits[["subject_id", "set_name"]],
                      on="subject_id", how="left")
        self.df = df[df["set_name"] == "train"].reset_index(drop=True)
        self.ecg = np.load(cache_dir / "ecg_cache_v2.npy", mmap_mode="r")
        self.rows = self.df["cache_row"].to_numpy(dtype=np.int64)

    def __len__(self):
        return len(self.df)

    def __getitem__(self, i):
        return torch.from_numpy(np.asarray(self.ecg[int(self.rows[i])]).copy())


class ProjectionHead(nn.Module):
    def __init__(self, in_dim=512, out_dim=128):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(in_dim, 256), nn.ReLU(),
                                 nn.Linear(256, out_dim))

    def forward(self, x):
        return F.normalize(self.net(x), dim=-1)


def ssl_pretrain():
    ds = SSLDataset()
    print(f"[ssl] train-patient ECG: {len(ds):,}（仅训练集，inductive）")
    ld = DataLoader(ds, batch_size=SSL_BATCH, shuffle=True, num_workers=0,
                    drop_last=True)
    enc = ECGResNet18(12, 64, 512).to(DEVICE)
    proj = ProjectionHead(512, 128).to(DEVICE)
    params = list(enc.parameters()) + list(proj.parameters())
    opt = torch.optim.AdamW(params, lr=SSL_LR, weight_decay=1e-4)
    rng = np.random.default_rng(20260730)
    temp = 0.1
    for ep in range(1, SSL_EPOCHS + 1):
        enc.train(); proj.train()
        t0 = time.time()
        tot, nb = 0.0, 0
        for x in ld:
            x = x.to(DEVICE)
            v1 = _augment(x, rng)
            v2 = _augment(x, rng)
            z1 = proj(enc(v1))
            z2 = proj(enc(v2))
            logits = z1 @ z2.T / temp
            labels = torch.arange(z1.shape[0], device=DEVICE)
            loss = (F.cross_entropy(logits, labels)
                    + F.cross_entropy(logits.T, labels)) / 2
            opt.zero_grad(); loss.backward(); opt.step()
            tot += float(loss); nb += 1
        print(f"[ssl] ep{ep} nt-xent={tot/max(nb,1):.4f} "
              f"({time.time()-t0:.0f}s)")
    torch.save(enc.state_dict(), OUT / "ssl_encoder.pt")
    print(f"[ssl] encoder saved → {OUT / 'ssl_encoder.pt'}")
    return enc.state_dict()


def _finetune(init_state=None, tag="ssl"):
    from src.models.train.train import get_pos_weight
    from src.models.train.metrics import landmark_metrics, predict
    from src.models.data.dataset import ART as _ART, RID as _RID
    results = []
    for seed in SEEDS:
        tr = SCEDataset(_ART / "p9_packages" / _RID / "sce_common_paired"
                        / "train", _ART / "p7_fitted" / _RID,
                        _ART / "p5_ecg_cache" / _RID,
                        raw_tensor_dir=_ART / "p2_clinical" / _RID / "master",
                        ecg_suffix="_v2")
        va = SCEDataset(_ART / "p9_packages" / _RID / "sce_common_paired"
                        / "validation", _ART / "p7_fitted" / _RID,
                        _ART / "p5_ecg_cache" / _RID,
                        raw_tensor_dir=_ART / "p2_clinical" / _RID / "master",
                        ecg_suffix="_v2")
        te = SCEDataset(_ART / "p9_packages" / _RID / "sce_common_paired"
                        / "test", _ART / "p7_fitted" / _RID,
                        _ART / "p5_ecg_cache" / _RID,
                        raw_tensor_dir=_ART / "p2_clinical" / _RID / "master",
                        ecg_suffix="_v2")
        torch.manual_seed(20260730 + seed)
        model = SCEModel(GRUDEncoder(17, 128, 44, 64),
                         ECGResNet18(12, 64, 512), 192, 512, 64).to(DEVICE)
        if init_state is not None:
            missing, unexpected = model.ecg_encoder.load_state_dict(
                init_state, strict=False)
            print(f"[ssl-ft] seed{seed} 载入 SSL 权重"
                  f"（missing {len(missing)}, unexpected {len(unexpected)}）")
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
            vy, vs, vk = predict(model, va_ld, DEVICE, ecg=True)
            ia = landmark_metrics(vy, vs, vk)["iauroc_partial"] or 0
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
        results.append({"seed": seed, "test_iauroc": tmet["iauroc"]})
        print(f"[ssl-ft] {tag} seed{seed} test iAUROC={tmet['iauroc']}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=MODE,
                    choices=["ssl_only", "full", "finetune_only"])
    args = ap.parse_args()
    init = None
    if args.mode in ("ssl_only", "full"):
        init = ssl_pretrain()
    elif args.mode == "finetune_only":
        init = torch.load(OUT / "ssl_encoder.pt", map_location=DEVICE)
    if args.mode in ("full", "finetune_only"):
        results = _finetune(init, tag="ssl")
        ias = [r["test_iauroc"] for r in results if r["test_iauroc"]]
        lines = ["# ECG inductive SSL 预训练→微调（Tier 2）", "",
                 f"- SSL：SimCLR（NT-Xent, T=0.1），{SSL_EPOCHS} epochs，"
                 f"仅训练患者 ECG（inductive）",
                 f"- 微调逐 seed test iAUROC：" +
                 ", ".join(f"{r['test_iauroc']:.4f}" for r in results),
                 f"- **均值±SD：{np.mean(ias):.4f} ± {np.std(ias):.4f}**",
                 "",
                 "对照从头训练主结果（SCE paired test iAUROC≈0.823±0.010）："
                 "评估 SSL 初始化是否带来增益。"]
        (OUT / "result.json").write_text(
            json.dumps({"per_seed": results, "mean": float(np.mean(ias)),
                        "sd": float(np.std(ias))}, indent=2),
            encoding="utf-8")
        (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"[ssl-ft] 均值={np.mean(ias):.4f}，REPORT.md written")


if __name__ == "__main__":
    main()
