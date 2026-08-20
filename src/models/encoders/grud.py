"""GRU-D 临床时序编码器（训练方案 v1.1 §2.1，锁定）。

输入三元组：X（z-score）、M（观测 mask）、D（Δt）。
x̂_v = γ_v(Δt) ⊙ x_v + (1 − γ_v(Δt)) ⊙ m̄_v（m̄_v 训练集通道均值，P7 工件）
"""
import torch
import torch.nn as nn


class GRUDEncoder(nn.Module):
    def __init__(self, n_channels: int = 17, hidden: int = 128,
                 static_dim: int = 44, static_hidden: int = 64,
                 channel_means: torch.Tensor = None):
        super().__init__()
        self.n_channels = n_channels
        self.hidden = hidden
        # Δt 衰减参数（每通道）
        self.w_gamma = nn.Parameter(torch.ones(n_channels) * 0.1)
        self.b_gamma = nn.Parameter(torch.zeros(n_channels))
        # 通道均值（P7 工件，buffer 不参与训练）
        self.register_buffer("channel_means",
                             channel_means if channel_means is not None
                             else torch.zeros(n_channels))
        self.gru = nn.GRU(input_size=n_channels * 2, hidden_size=hidden,
                          batch_first=True)
        self.static_mlp = nn.Sequential(
            nn.Linear(static_dim, static_hidden), nn.ReLU())

    def forward(self, x, m, d, static):
        # x/m/d: [B, V, T] → [B, T, V]
        x = x.permute(0, 2, 1)
        m = m.permute(0, 2, 1).float()
        d = d.permute(0, 2, 1)
        gamma = torch.exp(-torch.relu(
            d * self.w_gamma.abs() + self.b_gamma))
        x_hat = gamma * x + (1 - gamma) * self.channel_means
        inp = torch.cat([x_hat, m], dim=-1)
        out, h = self.gru(inp)
        h_last = h[-1]
        s = self.static_mlp(static)
        return torch.cat([h_last, s], dim=-1)
