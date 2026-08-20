"""Unit tests (stdlib unittest；pytest 未安装的替代登记，方案 §15.1)。"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lib import grid, leakage, scalers, static as lib_static  # noqa: E402
from lib import labels as lib_labels  # noqa: E402


class TestGrid(unittest.TestCase):
    def test_delta_basic(self):
        m = np.zeros((1, 1, 24), dtype=bool)
        m[0, 0, [3, 7, 20]] = True
        d = grid.compute_delta(m)
        self.assertEqual(d[0, 0, 3], 0.0)
        self.assertEqual(d[0, 0, 7], 0.0)
        self.assertEqual(d[0, 0, 8], 1.0)
        self.assertEqual(d[0, 0, 19], 12.0)
        self.assertEqual(d[0, 0, 0], 48.0)   # 无历史 → cap
        self.assertEqual(d[0, 0, 2], 48.0)

    def test_delta_no_history(self):
        m = np.zeros((2, 3, 24), dtype=bool)
        d = grid.compute_delta(m)
        self.assertTrue((d == 48.0).all())

    def test_delta_cap(self):
        m = np.zeros((1, 1, 24), dtype=bool)
        m[0, 0, 0] = True
        d = grid.compute_delta(m)
        self.assertEqual(d[0, 0, 23], 23.0)   # 不超 cap
        self.assertLessEqual(d.max(), 48.0)


class TestScalers(unittest.TestCase):
    def test_fit_apply_channel(self):
        rng = np.random.default_rng(0)
        x = rng.normal(80, 10, size=(50, 2, 24)).astype(np.float32)
        m = rng.random((50, 2, 24)) > 0.3
        p = scalers.fit_channel_scaler(x, m, ["hr", "rr"])
        self.assertEqual(p["fitted_on"], "train")
        xs = x.copy()
        scalers.apply_channel_scaler(xs, m, p, ["hr", "rr"])
        vals = xs[:, 0, :][m[:, 0, :]]
        self.assertAlmostEqual(float(vals.mean()), 0.0, places=4)
        self.assertTrue((xs[~m] == 0).all())


class TestStatic(unittest.TestCase):
    def test_encoder_unknown_bucket(self):
        df = pd.DataFrame({"g": ["M"] * 90 + ["F"] * 9 + ["X"]})
        enc = lib_static.fit_categorical_encoder(df, "g")
        self.assertIn("Unknown", enc["keep"])
        new = pd.DataFrame({"g": ["M", "ZZZ", None]})
        oh, n_unknown = lib_static.apply_categorical_encoder(new, "g", enc)
        self.assertEqual(n_unknown, 1)   # ZZZ → Unknown
        self.assertIn("g_Unknown", oh.columns)

    def test_imputer(self):
        df = pd.DataFrame({"w": [60.0, 70.0, np.nan, 80.0]})
        imp = lib_static.fit_static_imputer(df, ["w"])
        out = lib_static.apply_static_imputer(df, imp)
        self.assertEqual(out.loc[2, "w"], 70.0)
        self.assertEqual(out.loc[2, "w_missing"], 1.0)


class TestLabels(unittest.TestCase):
    def test_patient_weights_sum_one(self):
        df = pd.DataFrame({
            "subject_key": [1, 1, 1, 2, 2],
            "landmark_k": [0, 1, 2, 0, 1],
            "episode_key": ["a", "a", "a", "b", "b"]})
        w = lib_labels.add_patient_weights(df)
        sums = w.groupby("subject_key")["weight"].sum()
        self.assertTrue((sums.sub(1.0).abs() < 1e-9).all())


class TestLeakage(unittest.TestCase):
    def test_split_purity(self):
        df = pd.DataFrame({"subject_key": [1, 1, 2],
                           "set_name": ["train", "train", "test"]})
        leakage.assert_split_purity(df)
        bad = pd.DataFrame({"subject_key": [1, 1, 2],
                            "set_name": ["train", "test", "test"]})
        with self.assertRaises(AssertionError):
            leakage.assert_split_purity(bad)

    def test_mask_nan_policy(self):
        x = np.zeros((4, 2, 24), dtype=np.float32)
        m = np.zeros((4, 2, 24), dtype=bool)
        m[:, :, ::3] = True
        leakage.assert_mask_nan_policy(x, m)
        x[0, 0, 1] = np.nan
        with self.assertRaises(AssertionError):
            leakage.assert_mask_nan_policy(x, m)

    def test_train_fitted(self):
        leakage.assert_train_fitted({"fitted_on": "train"})
        with self.assertRaises(AssertionError):
            leakage.assert_train_fitted({"fitted_on": "validation"})


if __name__ == "__main__":
    unittest.main()
