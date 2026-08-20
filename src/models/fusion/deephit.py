"""DeepHit 竞争风险（技术文档 §2.2 次要分析）。

4 类事件 × 28 个 6h 离散区间（(t, t+168h]）：
  0 删失 / 1 院内死亡 / 2 存活出院 / 3 急性转出（同时刻优先级：1>3>2>0）
编码器复用 GRU-D；DeepHit 头输出 (cause, bin) 联合 PMF（softmax）。
损失：NLL（观测事件 bin）+ σ 加权 ranking（pycox DeepHit 近似）。
"""
import numpy as np
import torch
import torch.nn as nn

N_CAUSES = 4       # 含删失（索引 0 不参与事件头）
N_EVENT_CAUSES = 3 # 1=death, 2=alive_discharge, 3=acute_transfer


class DeepHitHead(nn.Module):
    """输入 encoder 维度 → (N_EVENT_CAUSES × n_bins) logits。"""

    def __init__(self, in_dim: int, n_bins: int = 28, hidden: int = 64):
        super().__init__()
        self.n_bins = n_bins
        self.head = nn.Sequential(
            nn.Linear(in_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, N_EVENT_CAUSES * n_bins))

    def forward(self, h):
        z = self.head(h)                       # [B, K*T]
        return z.view(-1, N_EVENT_CAUSES, self.n_bins)


def deephit_loss(logits, event_type, event_bin, weight,
                 alpha=1.0, beta=0.1, sigma=0.1):
    """logits: [B, K, T]; event_type: 0 censor / 1..3 causes; event_bin ∈ [1,28]

    关键：softmax 必须作用在 (cause × time) 联合维度上，得到
    P(cause=k, time=t)（全部 K×T 求和为 1），而非逐 cause 对 time 归一。
    """
    B, K, T = logits.shape
    logpmf = torch.log_softmax(logits.view(B, K * T), dim=-1).view(B, K, T)
    pmf = logpmf.exp()
    idx = (event_bin - 1).clamp(0, T - 1)        # 事件 bin → 0 基
    is_censor = event_type == 0
    cause = (event_type - 1).clamp(0, K - 1)     # 1..3 → 0..2

    # NLL：事件者取 log pmf[cause, bin]；删失者取 log(1 - Σ_k CIF_k(bin))
    nll_event = -logpmf[torch.arange(B, device=logits.device),
                        cause, idx]
    cif_all = pmf.sum(dim=1)                     # [B, T] 全部 cause 合计 CIF
    surv = (1 - cif_all.gather(1, idx.unsqueeze(1)).squeeze(1)).clamp(min=1e-8)
    nll_censor = -torch.log(surv)
    nll = torch.where(is_censor, nll_censor, nll_event)

    # ranking：事件者 i 在其事件 bin 的 cause 特异 CIF 应高于他人
    cif_k = torch.cumsum(pmf, dim=-1)            # [B, K, T]
    cif_at = cif_k[torch.arange(B, device=logits.device), cause, idx]
    rank = torch.zeros(B, device=logits.device)
    ev = ~is_censor
    if ev.any():
        ci = cif_at[ev].unsqueeze(1)             # [E,1]
        cj = cif_at[ev].unsqueeze(0)             # [1,E]
        eta = ci - cj                            # [E,E] i 应 > j
        eye = torch.eye(int(ev.sum()), device=logits.device).bool()
        rank_ev = torch.log(1 + torch.exp(-eta / sigma))
        rank_ev = rank_ev.masked_fill(eye, 0.0)
        rank[ev] = rank_ev.mean(dim=1)
    loss = (alpha * nll + beta * rank) * weight
    return loss.sum() / weight.sum().clamp(min=1e-9)


def cif_at_horizon(logits, cause: int, horizon_bin: int) -> torch.Tensor:
    """P(event of cause ≤ horizon) = sum pmf[cause, ≤bin]（cause 0 基）。

    与 loss 一致：joint softmax over (cause × time)。
    """
    B, K, T = logits.shape
    pmf = torch.softmax(logits.view(B, K * T), dim=-1).view(B, K, T)
    return pmf[:, cause, :horizon_bin].sum(dim=-1)


def td_cindex(y_type, y_bin, risk, cause: int) -> float:
    """time-dependent C-index（竞争风险校正近似）：
    事件者(cause)应比所有在其 bin 后仍未事件者风险高。"""
    ev = y_type == cause
    if ev.sum() == 0:
        return float("nan")
    conc, disc = 0.0, 0.0
    for i in np.where(ev)[0]:
        later = (y_bin > y_bin[i]) | is_censored_later(y_type, y_bin, y_bin[i])
        conc += (risk[i] > risk[later]).sum()
        disc += (risk[i] < risk[later]).sum()
    tot = conc + disc
    return float(conc / tot) if tot > 0 else float("nan")


def is_censored_later(y_type, y_bin, t):
    return (y_type == 0) & (y_bin >= t)
