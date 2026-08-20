"""1D ResNet-18 ECG 编码器（训练方案 v1.1 §2.3，从头训练，锁定）。

输入 [12, 5000]（500Hz × 10s）→ 512 维 embedding。
"""
import torch
import torch.nn as nn


class BasicBlock1d(nn.Module):
    def __init__(self, cin, cout, stride=1):
        super().__init__()
        self.conv1 = nn.Conv1d(cin, cout, 7, stride=stride, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm1d(cout)
        self.conv2 = nn.Conv1d(cout, cout, 7, padding=3, bias=False)
        self.bn2 = nn.BatchNorm1d(cout)
        self.relu = nn.ReLU(inplace=True)
        self.down = None
        if stride != 1 or cin != cout:
            self.down = nn.Sequential(
                nn.Conv1d(cin, cout, 1, stride=stride, bias=False),
                nn.BatchNorm1d(cout))

    def forward(self, x):
        idn = x if self.down is None else self.down(x)
        z = self.relu(self.bn1(self.conv1(x)))
        z = self.bn2(self.conv2(z))
        return self.relu(z + idn)


class ECGResNet18(nn.Module):
    def __init__(self, in_ch: int = 12, base: int = 64, out_dim: int = 512):
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv1d(in_ch, base, 15, stride=2, padding=7, bias=False),
            nn.BatchNorm1d(base), nn.ReLU(inplace=True),
            nn.MaxPool1d(3, stride=2))
        chs = [base, base * 2, base * 4, base * 8]
        layers = []
        cin = base
        for i, c in enumerate(chs):
            layers.append(BasicBlock1d(cin, c, stride=2 if i > 0 else 1))
            layers.append(BasicBlock1d(c, c, stride=1))
            cin = c
        self.stages = nn.Sequential(*layers)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.out_dim = chs[-1]

    def forward(self, x):
        z = self.stem(x)
        z = self.stages(z)
        return self.pool(z).squeeze(-1)
