"""DeepHit 竞争风险训练与评估（技术文档 §2.2 次要分析）。

用法（VSCode）：改 CONFIG → ▶ Run；或终端 python scripts/run_deephit.py --mode quick
输出：src/models/runs/deephit/{result_seed*.json, REPORT.md, cif_predictions_seed*.npz}
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

# ============================ CONFIG（只改这里） ============================
MODE = "quick"         # quick(1 seed) / full(5 seeds) / aggregate_only
SEEDS = [1, 2, 3, 4, 5]
# ===========================================================================

from src.models.data.dataset import ART, RID  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.fusion.deephit import (DeepHitHead, cif_at_horizon,  # noqa: E402
                                       deephit_loss, td_cindex)

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
OUT = ROOT / "src" / "models" / "runs" / "deephit"
OUT.mkdir(parents=True, exist_ok=True)
N_BINS = 28


class CompetingDataset(Dataset):
    """临床张量 + 静态 + 竞争风险标签（event_type/event_time_bin）。"""

    def __init__(self, pkg_dir: Path, tensor_dir: Path, raw_dir: Path,
                 comp_path: Path):
        from src.models.data.dataset import ClinicalDataset
        base = ClinicalDataset(pkg_dir, tensor_dir,
                               raw_tensor_dir=raw_dir)
        self.base = base
        comp = pd.read_parquet(comp_path)
        idx = base.idx[["episode_key", "landmark_k"]].reset_index()
        j = idx.merge(comp, on=["episode_key", "landmark_k"], how="left")
        self.event_type = torch.from_numpy(
            j["event_type"].fillna(0).to_numpy(dtype=np.int64))
        self.event_bin = torch.from_numpy(
            j["event_time_bin"].fillna(N_BINS).to_numpy(dtype=np.int64))

    def __len__(self):
        return len(self.base)

    def __getitem__(self, i):
        out = self.base[i]
        out["event_type"] = self.event_type[i]
        out["event_bin"] = self.event_bin[i]
        return out


@torch.no_grad()
def eval_cindex(enc, head, ld, return_cif=False):
    enc.eval(); head.eval()
    ets, ebs, risks = [], [], {1: [], 2: [], 3: []}
    for b in ld:
        x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
        d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
        logits = head(enc(x, m, d, st))
        for c in (1, 2, 3):
            risks[c].append(
                cif_at_horizon(logits, c - 1, N_BINS).cpu().numpy())
        ets.append(b["event_type"].numpy())
        ebs.append(b["event_bin"].numpy())
    et = np.concatenate(ets); eb = np.concatenate(ebs)
    names = {1: "death", 2: "alive_discharge", 3: "acute_transfer"}
    out = {nm: td_cindex(et, eb, np.concatenate(risks[c]), c)
           for c, nm in names.items()}
    if return_cif:
        return out, {"event_type": et, "event_bin": eb,
                     **{f"cif_{names[c]}": np.concatenate(risks[c])
                        for c in (1, 2, 3)}}
    return out


def train_deephit(seed: int):
    torch.manual_seed(20260730 + seed)
    tensor_dir = ART / "p7_fitted" / RID
    raw_dir = ART / "p2_clinical" / RID / "master"
    pkg = ART / "p9_packages" / RID / "sc_common_all"
    comp = ART / "p4_samples" / RID / "competing_risk_labels.parquet"

    tr = CompetingDataset(pkg / "train", tensor_dir, raw_dir, comp)
    va = CompetingDataset(pkg / "validation", tensor_dir, raw_dir, comp)
    te = CompetingDataset(pkg / "test", tensor_dir, raw_dir, comp)
    tr_ld = DataLoader(tr, batch_size=256, shuffle=True, num_workers=0)
    va_ld = DataLoader(va, batch_size=256, shuffle=False, num_workers=0)
    te_ld = DataLoader(te, batch_size=256, shuffle=False, num_workers=0)

    enc = GRUDEncoder(17, 128, 44, 64).to(DEVICE)
    head = DeepHitHead(128 + 64, N_BINS, 64).to(DEVICE)
    opt = torch.optim.AdamW(list(enc.parameters()) + list(head.parameters()),
                            lr=1e-3, weight_decay=1e-4)

    best, bad, best_state = -1.0, 0, None
    for ep in range(1, 101):
        enc.train(); head.train()
        t0 = time.time()
        tot = 0.0
        for b in tr_ld:
            x = b["x"].to(DEVICE); m = b["m"].to(DEVICE)
            d = b["d"].to(DEVICE); st = b["static"].to(DEVICE)
            et = b["event_type"].to(DEVICE); eb = b["event_bin"].to(DEVICE)
            w = b["w"].to(DEVICE)
            logits = head(enc(x, m, d, st))
            loss = deephit_loss(logits, et, eb, w)
            opt.zero_grad(); loss.backward(); opt.step()
            tot += float(loss)
        c_va = eval_cindex(enc, head, va_ld)
        print(f"[deephit] ep{ep} loss={tot/len(tr_ld):.4f} "
              f"val C(death)={c_va['death']:.4f} ({time.time()-t0:.0f}s)")
        if c_va["death"] > best:
            best, bad = c_va["death"], 0
            best_state = ({k: v.clone() for k, v in enc.state_dict().items()},
                          {k: v.clone() for k, v in head.state_dict().items()})
        else:
            bad += 1
            if bad >= 10:
                print(f"[deephit] early stop ep{ep} (best C={best:.4f})")
                break
    if best_state:
        enc.load_state_dict(best_state[0])
        head.load_state_dict(best_state[1])
    torch.save({"enc": enc.state_dict(), "head": head.state_dict()},
               OUT / f"model_seed{seed}.pt")

    c_test, cif = eval_cindex(enc, head, te_ld, return_cif=True)
    np.savez_compressed(OUT / f"cif_predictions_seed{seed}.npz", **cif)
    result = {"seed": seed, "best_val_c_death": best,
              "test_cindex": c_test}
    (OUT / f"result_seed{seed}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    print(f"[deephit] seed_{seed} test C: death={c_test['death']:.4f} "
          f"alive_discharge={c_test['alive_discharge']:.4f} "
          f"acute_transfer={c_test['acute_transfer']:.4f}")
    return result


def report(results):
    lines = ["# DeepHit 竞争风险分析报告",
             "",
             f"生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}；"
             "事件：1=院内死亡 / 2=存活出院 / 3=急性转出；28×6h 区间（168h）",
             "",
             "| seed | val C(death) | test C death | test C alive_disch | test C acute_trans |",
             "|---|---|---|---|---|"]
    for r in results:
        t = r["test_cindex"]
        lines.append(f"| {r['seed']} | {r['best_val_c_death']:.4f} | "
                     f"{t['death']:.4f} | {t['alive_discharge']:.4f} | {t['acute_transfer']:.4f} |")
    if len(results) > 1:
        d = [r["test_cindex"]["death"] for r in results]
        a = [r["test_cindex"]["alive_discharge"] for r in results]
        at = [r["test_cindex"]["acute_transfer"] for r in results]
        lines += ["",
                  f"**均值±SD：death {np.mean(d):.4f}±{np.std(d):.4f} / "
                  f"alive_discharge {np.mean(a):.4f}±{np.std(a):.4f} / "
                  f"acute_transfer {np.mean(at):.4f}±{np.std(at):.4f}**"]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("[deephit] REPORT.md written")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=MODE,
                    choices=["quick", "full", "aggregate_only"])
    args = ap.parse_args()
    if args.mode != "aggregate_only":
        seeds = [1] if args.mode == "quick" else SEEDS
        for sd in seeds:
            print(f"\n[deephit] ===== seed_{sd} =====")
            train_deephit(sd)
    results = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted(OUT.glob("result_seed*.json"))]
    if results:
        report(results)
    print("\n[deephit] 全部完成，结果目录：", OUT)


if __name__ == "__main__":
    main()
