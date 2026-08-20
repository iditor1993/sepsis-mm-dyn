"""数据集（训练方案 v1.1 §1.1）：从模型输入包加载张量/静态/ECG。

临床张量全量入 RAM（MIMIC ~2GB，eICU ~1.2GB，memmap 读取）；
ECG 走 memmap 懒加载（8.8GB 不入 RAM）。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RID = "pp_v1_20260730"
ART = PROJECT_ROOT / "preprocess" / "artifacts"
CORE_CHANNELS = 17


def _load_channels(pkg_dir: Path) -> list:
    man = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    return man["channels"]


class ClinicalDataset(Dataset):
    """SC 模型数据集：临床张量 + 静态 + 标签 + 权重。

    pkg: 包目录（含 index.parquet / static.npy / manifest.json）
    tensor_dir: 张量主存目录（p7_fitted 或 eicu_master）
    """

    def __init__(self, pkg: str, tensor_dir: Path,
                 raw_tensor_dir: Path = None, core_only: bool = True):
        """tensor_dir: X_seq_scaled 所在（p7_fitted）；
        raw_tensor_dir: M_seq/D_seq 所在（p2_clinical/master；缺省同 tensor_dir）"""
        self.pkg_dir = Path(pkg)
        self.idx = pd.read_parquet(self.pkg_dir / "index.parquet")
        self.static = np.load(self.pkg_dir / "static.npy")
        self.channels = _load_channels(self.pkg_dir)
        raw_dir = raw_tensor_dir or tensor_dir
        x = np.load(tensor_dir / "X_seq_scaled.npy", mmap_mode="r")
        m = np.load(raw_dir / "M_seq.npy", mmap_mode="r")
        d = np.load(raw_dir / "D_seq.npy", mmap_mode="r")
        if core_only and x.shape[1] > len(self.channels):
            # 只取 SC-common-core 通道（张量按 config 通道序，core 为前 17）
            x = x[:, :CORE_CHANNELS, :]
            m = m[:, :CORE_CHANNELS, :]
            d = d[:, :CORE_CHANNELS, :]
        self.x = torch.from_numpy(np.ascontiguousarray(x))
        self.m = torch.from_numpy(np.ascontiguousarray(m))
        self.d = torch.from_numpy(np.ascontiguousarray(d))
        self.y = torch.from_numpy(
            self.idx["y_24h"].to_numpy(dtype=np.float32))
        self.w = torch.from_numpy(
            self.idx["weight"].to_numpy(dtype=np.float32))
        self.rows = torch.from_numpy(
            self.idx["row_idx"].to_numpy(dtype=np.int64))

    def __len__(self):
        return len(self.idx)

    def __getitem__(self, i):
        r = int(self.rows[i])
        return {
            "x": self.x[r], "m": self.m[r], "d": self.d[r],
            "static": torch.from_numpy(self.static[i]),
            "y": self.y[i], "w": self.w[i],
            "landmark_k": int(self.idx.iloc[i]["landmark_k"]),
            "subject_idx": i,
        }


class SCEDataset(ClinicalDataset):
    """SCE 模型数据集：临床 + ECG（memmap 懒加载）。"""

    def __init__(self, pkg: str, tensor_dir: Path, ecg_dir: Path,
                 raw_tensor_dir: Path = None, core_only: bool = True,
                 use_dropout_group: bool = False,
                 ecg_suffix: str = ""):
        super().__init__(pkg, tensor_dir, raw_tensor_dir, core_only)
        self.ecg = np.load(ecg_dir / f"ecg_cache{ecg_suffix}.npy",
                           mmap_mode="r")
        cache_idx = pd.read_parquet(
            ecg_dir / f"ecg_cache_index{ecg_suffix}.parquet")
        self.study2row = dict(zip(cache_idx["study_id"],
                                  cache_idx["cache_row"]))
        sid = self.idx["study_id"].to_numpy()
        self.ecg_rows = np.array(
            [self.study2row.get(int(s), -1) if pd.notna(s) else -1
             for s in sid], dtype=np.int64)
        self.avail = torch.from_numpy((self.ecg_rows >= 0).astype(np.float32))
        self.use_dropout_group = use_dropout_group
        if use_dropout_group and "modality_dropout_group" in self.idx.columns:
            self.drop_group = (self.idx["modality_dropout_group"]
                               == "drop").to_numpy()
        else:
            self.drop_group = np.zeros(len(self.idx), dtype=bool)

    def __getitem__(self, i):
        out = super().__getitem__(i)
        er = int(self.ecg_rows[i])
        if er >= 0 and not (self.training_dropout and self.drop_group[i]):
            out["ecg"] = torch.nan_to_num(
                torch.from_numpy(np.asarray(self.ecg[er]).copy()),
                nan=0.0)
            out["ecg_avail"] = torch.tensor(1.0)
        else:
            out["ecg"] = torch.zeros(12, 5000, dtype=torch.float32)
            out["ecg_avail"] = torch.tensor(0.0)
        return out

    training_dropout: bool = False


# ------------------------------------------------------------------
# eICU 外部验证数据集（层级 2）
# ------------------------------------------------------------------
STATIC_DIM = 44


class EICUDataset(Dataset):
    """eICU 张量 + MIMIC-schema 静态（缺省插补+缺失指示）+ 标签。

    静态按 MIMIC 44 维 schema 映射：age 用 MIMIC scaler，gender 按 MIMIC 类别，
    其余数值/类别特征用训练集插补值 + missing=1（诚实跨库口径）。
    """

    def __init__(self, track: str):
        pkg = ART / "p9_packages" / RID / "eicu_sc_common" / track
        self.df = pd.read_parquet(pkg / "index.parquet")
        self.static = np.load(pkg / "static.npy")
        self.static44 = self._map_static44()
        self.x = np.load(
            ART / "p2_clinical" / RID / "eicu_master" / "X_seq_scaled.npy",
            mmap_mode="r")
        self.m = np.load(
            ART / "p2_clinical" / RID / "eicu_master" / "M_seq.npy",
            mmap_mode="r")
        self.d = np.load(
            ART / "p2_clinical" / RID / "eicu_master" / "D_seq.npy",
            mmap_mode="r")
        self.rows = self.df["row_idx"].to_numpy(dtype=np.int64)
        self.y = torch.from_numpy(self.df["y_24h"].to_numpy(dtype=np.float32))
        self.w = torch.from_numpy(self.df["weight"].to_numpy(dtype=np.float32))

    def _map_static44(self):
        n = len(self.df)
        out = np.zeros((n, STATIC_DIM), dtype=np.float32)
        from nodes.p3_static import (STATIC_CATEGORICAL, STATIC_FLAGS,
                                     STATIC_NUMERIC)
        import json as _json
        scaler = _json.loads((ART / "p7_fitted" / RID / "scaler_static.json")
                             .read_text(encoding="utf-8"))
        encs = _json.loads((ART / "p7_fitted" / RID
                            / "categorical_encoders.json").read_text(
            encoding="utf-8"))
        imp = _json.loads((ART / "p7_fitted" / RID / "imputers.json")
                          .read_text(encoding="utf-8"))
        pos = 0
        age_raw = self.df["age_num"].to_numpy(dtype=float)
        for col in STATIC_NUMERIC:
            if col == "age":
                vals = np.where(np.isnan(age_raw),
                                imp["cols"]["age"]["median"], age_raw)
                out[:, pos] = (vals - scaler["cols"]["age"]["mean"]) \
                    / scaler["cols"]["age"]["sd"]
                out[:, pos + 1] = np.isnan(age_raw).astype(np.float32)
            else:
                med = imp["cols"][col]["median"] or 0.0
                out[:, pos] = (med - scaler["cols"][col]["mean"]) \
                    / scaler["cols"][col]["sd"]
                out[:, pos + 1] = 1.0
            pos += 2
        for col in STATIC_CATEGORICAL:
            keep = encs[col]["keep"]
            if col == "gender":
                g = self.df["gender"].fillna("Unknown").astype(str).to_numpy()
            else:
                g = np.array(["Unknown"] * n, dtype=object)
            for j, cat in enumerate(keep):
                out[:, pos + j] = (g == cat).astype(np.float32)
            pos += len(keep)
        # flags 全 0（eICU 无）
        return out

    def __len__(self):
        return len(self.df)

    def __getitem__(self, i):
        r = int(self.rows[i])
        return {"x": torch.from_numpy(np.asarray(self.x[r]).copy()),
                "m": torch.from_numpy(np.asarray(self.m[r]).copy()),
                "d": torch.from_numpy(np.asarray(self.d[r]).copy()),
                "static": torch.from_numpy(self.static44[i]),
                "y": self.y[i], "landmark_k": int(self.df.iloc[i]["landmark_k"]),
                "subject_key": self.df.iloc[i]["subject_key"]}
