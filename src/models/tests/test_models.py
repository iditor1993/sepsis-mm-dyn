"""模型单元测试（形状/前向/mask 处理/loss 健康）。"""
import sys
import unittest
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.models.encoders.ecg_resnet import ECGResNet18  # noqa: E402
from src.models.encoders.grud import GRUDEncoder  # noqa: E402
from src.models.encoders.tpc import TPCEncoder  # noqa: E402
from src.models.fusion.heads import SCEModel, SCModel  # noqa: E402
from src.models.train.metrics import auroc_np, landmark_metrics  # noqa: E402
import numpy as np  # noqa: E402


class TestEncoders(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(0)
        self.b, self.v, self.t = 4, 17, 24
        self.x = torch.randn(self.b, self.v, self.t)
        self.m = (torch.rand(self.b, self.v, self.t) > 0.3)
        self.d = torch.rand(self.b, self.v, self.t) * 48
        self.static = torch.randn(self.b, 44)

    def test_grud(self):
        enc = GRUDEncoder(self.v, 128, 44, 64)
        h = enc(self.x, self.m, self.d, self.static)
        self.assertEqual(h.shape, (self.b, 192))
        self.assertFalse(torch.isnan(h).any())

    def test_tpc(self):
        enc = TPCEncoder(34, 16, 64, 44, 64)
        h = enc(self.x, self.m, self.d, self.static)
        self.assertEqual(h.shape, (self.b, 128))
        self.assertFalse(torch.isnan(h).any())

    def test_ecg_resnet(self):
        enc = ECGResNet18(12, 64, 512)
        h = enc(torch.randn(2, 12, 5000))
        self.assertEqual(h.shape, (2, 512))
        self.assertFalse(torch.isnan(h).any())

    def test_sc_model(self):
        model = SCModel(GRUDEncoder(self.v, 128, 44, 64), 192)
        s = model(self.x, self.m, self.d, self.static)
        self.assertEqual(s.shape, (self.b,))

    def test_sce_model_deployment(self):
        model = SCEModel(GRUDEncoder(self.v, 128, 44, 64),
                         ECGResNet18(12, 64, 512), 192, 512, 64,
                         deployment=True)
        ecg = torch.randn(self.b, 12, 5000)
        avail = torch.tensor([1.0, 1.0, 0.0, 0.0])
        s = model(self.x, self.m, self.d, self.static, ecg, avail)
        self.assertEqual(s.shape, (self.b,))
        self.assertFalse(torch.isnan(s).any())

    def test_metrics(self):
        y = np.array([0, 0, 1, 1, 0, 1])
        s = np.array([0.1, 0.2, 0.9, 0.8, 0.3, 0.4])
        a = auroc_np(y, s)
        self.assertAlmostEqual(a, 1.0, places=2)
        met = landmark_metrics(y, s, np.array([0, 0, 0, 0, 1, 1]),
                               main_k=2)
        self.assertEqual(met["iauroc"], 1.0)


if __name__ == "__main__":
    unittest.main()
