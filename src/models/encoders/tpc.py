"""TPC 临床时序编码器（训练方案 v1.1 §2.2，GRU-D 预设对照架构）。

Channel-wise BiLSTM → Temporal Conv → Pointwise Conv → 残差 block ×2
→ 全局池化 + 静态 MLP。
"""
import torch
import torch.nn as nn


class _TPCBlock(nn.Module):
    def __init__(self, n_ch, lstm_hidden=16, conv_out=64, kernel=3):
        super().__init__()
        self.lstm = nn.LSTM(1, lstm_hidden, batch_first=True,
                            bidirectional=True)
        self.temporal = nn.Conv1d(n_ch * lstm_hidden * 2, conv_out,
                                  kernel_size=kernel, padding=kernel // 2)
        self.pointwise = nn.Conv1d(conv_out, conv_out, kernel_size=1)
        self.relu = nn.ReLU()

    def forward(self, x):
        # x: [B, C, T]
        b, c, t = x.shape
        h = x.reshape(b * c, t, 1)
        h, _ = self.lstm(h)                     # [B*C, T, 2H]
        h = h.reshape(b, c, t, -1).permute(0, 1, 3, 2)  # [B, C, 2H, T]
        h = h.reshape(b, c * h.shape[2], t)             # [B, C*2H, T]
        z = self.relu(self.temporal(h))
        z = self.relu(self.pointwise(z))
        if z.shape[1] != x.shape[1]:
            # 通道数不同：用 1x1 投影 x 以对齐残差
            if not hasattr(self, "_proj"):
                self._proj = nn.Conv1d(x.shape[1], z.shape[1],
                                       kernel_size=1).to(z.device)
            x = self._proj(x)
        return self.relu(z + x)


class TPCEncoder(nn.Module):
    def __init__(self, n_channels_in: int = 34, hidden: int = 16,
                 conv_out: int = 64, static_dim: int = 44,
                 static_hidden: int = 64):
        super().__init__()
        self.block1 = _TPCBlock(n_channels_in, hidden, conv_out)
        self.block2 = _TPCBlock(conv_out, hidden, conv_out)
        self.static_mlp = nn.Sequential(
            nn.Linear(static_dim, static_hidden), nn.ReLU())
        self.out_dim = conv_out + static_hidden

    def forward(self, x, m, d, static):
        # 输入 X 与 M 拼接为 34 通道；D 作为附加通道可消融
        z = torch.cat([x, m.float()], dim=1)  # [B, 2V, T]
        z = self.block1(z)
        z = self.block2(z)
        z = z.mean(dim=-1)                    # 全局池化 [B, C]
        s = self.static_mlp(static)
        return torch.cat([z, s], dim=-1)
