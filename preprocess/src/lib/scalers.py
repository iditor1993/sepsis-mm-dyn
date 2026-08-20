"""Train-only scalers (方案 §9.2, 附录 A.2)."""
import hashlib
import json

import numpy as np


def fit_channel_scaler(x: np.ndarray, m: np.ndarray,
                       channel_names: list) -> dict:
    """x: [N, V, T], m: [N, V, T]（仅训练集）。按通道用有观测值估计。"""
    params = {"channels": {}, "fitted_on": "train"}
    for v, name in enumerate(channel_names):
        vals = x[:, v, :][m[:, v, :]]
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            params["channels"][name] = {"mean": 0.0, "sd": 1.0,
                                        "n_observed": 0}
        else:
            params["channels"][name] = {
                "mean": float(vals.mean()),
                "sd": float(vals.std() + 1e-8),
                "n_observed": int(len(vals))}
    blob = json.dumps(params, sort_keys=True).encode()
    params["content_hash"] = hashlib.sha256(blob).hexdigest()
    return params


def apply_channel_scaler(x: np.ndarray, m: np.ndarray, params: dict,
                         channel_names: list) -> np.ndarray:
    """Apply frozen scaler in place (missing positions stay 0)."""
    for v, name in enumerate(channel_names):
        p = params["channels"][name]
        x[:, v, :] = np.where(m[:, v, :],
                              (x[:, v, :] - p["mean"]) / p["sd"], 0.0)
    return x


def fit_static_scaler(df, numeric_cols: list) -> dict:
    params = {"cols": {}, "fitted_on": "train"}
    for c in numeric_cols:
        vals = df[c].dropna().values.astype(float)
        if len(vals) == 0:
            params["cols"][c] = {"mean": 0.0, "sd": 1.0, "n": 0}
        else:
            params["cols"][c] = {"mean": float(vals.mean()),
                                 "sd": float(vals.std() + 1e-8),
                                 "n": int(len(vals))}
    blob = json.dumps(params, sort_keys=True).encode()
    params["content_hash"] = hashlib.sha256(blob).hexdigest()
    return params
