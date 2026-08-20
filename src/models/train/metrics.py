"""评估指标：landmark-wise AUROC / iAUROC / Brier（训练方案 §5）。"""
import numpy as np
import torch


def auroc_np(y: np.ndarray, s: np.ndarray) -> float:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    mid = csum - counts + (counts + 1) / 2.0
    ranks = mid[inv]
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def landmark_metrics(y_true: np.ndarray, y_score: np.ndarray,
                     landmark_k: np.ndarray, main_k: int = 12) -> dict:
    aucs = {}
    for k in range(main_k):
        m = landmark_k == k
        if m.sum() == 0:
            continue
        aucs[k] = auroc_np(y_true[m], y_score[m])
    valid = [v for v in aucs.values() if not np.isnan(v)]
    return {
        "auroc_per_landmark": aucs,
        "iauroc": float(np.mean(valid)) if len(valid) == main_k else None,
        "iauroc_partial": float(np.mean(valid)) if valid else None,
        "n_estimable": len(valid),
        "brier": float(np.mean((y_score - y_true) ** 2)),
    }


@torch.no_grad()
def predict(model, loader, device, ecg: bool = False):
    model.eval()
    ys, ss, ks = [], [], []
    for batch in loader:
        x = batch["x"].to(device)
        m = batch["m"].to(device)
        d = batch["d"].to(device)
        st = batch["static"].to(device)
        if ecg:
            s = model(x, m, d, st,
                      batch["ecg"].to(device),
                      batch["ecg_avail"].to(device))
        else:
            s = model(x, m, d, st)
        ys.append(batch["y"].numpy())
        ss.append(torch.sigmoid(s).cpu().numpy())
        ks.append(batch["landmark_k"].numpy())
    return (np.concatenate(ys), np.concatenate(ss),
            np.concatenate(ks))
