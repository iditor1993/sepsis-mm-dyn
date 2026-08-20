"""P3 static encoding + P7 fitted encoders/imputers (方案 §5, §9.2)."""
import hashlib
import json

import numpy as np
import pandas as pd

RARE_MIN_FREQ = 0.01   # 低频合并阈值（训练集频率 <1% 并入 Other）


def fit_categorical_encoder(train_df: pd.DataFrame, col: str) -> dict:
    """训练集频率表 + 低频合并 + Unknown 桶。"""
    freq = train_df[col].fillna("Unknown").astype(str) \
        .value_counts(normalize=True)
    keep = sorted(freq[freq >= RARE_MIN_FREQ].index.tolist())
    if "Unknown" not in keep:
        keep.append("Unknown")
    enc = {"col": col, "keep": keep, "rare_threshold": RARE_MIN_FREQ,
           "fitted_on": "train",
           "train_freq": {k: float(v) for k, v in freq.items()}}
    blob = json.dumps(enc, sort_keys=True, ensure_ascii=False).encode()
    enc["content_hash"] = hashlib.sha256(blob).hexdigest()
    return enc


def apply_categorical_encoder(df: pd.DataFrame, col: str, enc: dict):
    """Return (one-hot DataFrame, n_unknown_mapped)."""
    vals = df[col].fillna("Unknown").astype(str)
    mapped = vals.where(vals.isin(enc["keep"]), "Unknown")
    n_unknown = int((~vals.isin(enc["keep"])).sum())
    dummies = pd.get_dummies(mapped, prefix=col)
    for k in enc["keep"]:
        c = f"{col}_{k}"
        if c not in dummies.columns:
            dummies[c] = 0
    dummies = dummies[[f"{col}_{k}" for k in enc["keep"]]]
    return dummies.astype(np.float32), n_unknown


def fit_static_imputer(train_df: pd.DataFrame, numeric_cols: list) -> dict:
    """训练集中位数插补参数 + 指示变量清单（主方案 median_indicator）。"""
    imp = {"cols": {}, "fitted_on": "train", "method": "median_indicator"}
    for c in numeric_cols:
        med = train_df[c].median()
        imp["cols"][c] = {"median": None if pd.isna(med) else float(med)}
    blob = json.dumps(imp, sort_keys=True).encode()
    imp["content_hash"] = hashlib.sha256(blob).hexdigest()
    return imp


def apply_static_imputer(df: pd.DataFrame, imp: dict) -> pd.DataFrame:
    out = df.copy()
    for c, p in imp["cols"].items():
        miss_col = f"{c}_missing"
        out[miss_col] = out[c].isna().astype(np.float32)
        if p["median"] is not None:
            out[c] = out[c].fillna(p["median"])
    return out
