"""融合与预测头（训练方案 v1.1 §2.4，锁定）。

SC：h_clin → MLP head
SCE：[h_clin ; h_ecg] 晚期拼接 → 2 层 MLP；SCE-deployment 加
modality dropout（P6 预分配组）+ 可学习 availability embedding。
"""
import torch
import torch.nn as nn


class SCModel(nn.Module):
    """临床分支模型（SC-common-paired / SC-common-all）。"""

    def __init__(self, encoder, clin_dim: int, head_hidden: int = 64):
        super().__init__()
        self.encoder = encoder
        self.head = nn.Sequential(
            nn.Linear(clin_dim, head_hidden), nn.ReLU(),
            nn.Linear(head_hidden, 32), nn.ReLU(),
            nn.Linear(32, 1))

    def forward(self, x, m, d, static):
        h = self.encoder(x, m, d, static)
        return self.head(h).squeeze(-1)


class SCEModel(nn.Module):
    """多模态模型（SCE-common-paired / SCE-deployment）。

    deployment=True 时启用可学习 availability embedding；
    ECG 缺失（ecg_avail=0）时 h_ecg 用零向量（P6 dropout 组训练期置零）。
    """

    def __init__(self, clin_encoder, ecg_encoder, clin_dim: int,
                 ecg_dim: int = 512, head_hidden: int = 64,
                 deployment: bool = False):
        super().__init__()
        self.clin_encoder = clin_encoder
        self.ecg_encoder = ecg_encoder
        self.deployment = deployment
        if deployment:
            self.avail_emb = nn.Parameter(torch.zeros(ecg_dim))
        self.head = nn.Sequential(
            nn.Linear(clin_dim + ecg_dim, head_hidden), nn.ReLU(),
            nn.Linear(head_hidden, 1))

    def forward(self, x, m, d, static, ecg=None, ecg_avail=None):
        h_c = self.clin_encoder(x, m, d, static)
        if ecg is None:
            h_e = torch.zeros(x.shape[0], self.head[0].in_features
                              - h_c.shape[1], device=x.device)
        else:
            h_e = self.ecg_encoder(ecg)
        if self.deployment and ecg_avail is not None:
            # availability embedding：缺失时叠加可学习向量区分「真零」
            h_e = h_e * ecg_avail.unsqueeze(-1) + \
                (1 - ecg_avail).unsqueeze(-1) * self.avail_emb
        return self.head(torch.cat([h_c, h_e], dim=-1)).squeeze(-1)
