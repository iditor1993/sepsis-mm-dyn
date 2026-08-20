"""P2 tensorization: GRU-D triplets + summary features (方案 §4).

性能设计：长表与 master 索引做 hash-join 拿 row_idx 后向量化填充，
不逐行循环；张量主存为 npy memmap。
"""
import numpy as np
import pandas as pd

GRID_T = 24
CAP_HOURS = 48.0


def alloc_tensors(n_rows: int, n_vars: int, x_path, m_path, d_path):
    """Allocate memmap tensors X/M/D."""
    x = np.lib.format.open_memmap(x_path, mode="w+", dtype=np.float32,
                                  shape=(n_rows, n_vars, GRID_T))
    m = np.lib.format.open_memmap(m_path, mode="w+", dtype=np.bool_,
                                  shape=(n_rows, n_vars, GRID_T))
    d = np.lib.format.open_memmap(d_path, mode="w+", dtype=np.float32,
                                  shape=(n_rows, n_vars, GRID_T))
    x[:] = 0.0
    m[:] = False
    d[:] = CAP_HOURS
    return x, m, d


def fill_channel(master: pd.DataFrame, long_df: pd.DataFrame,
                 x: np.ndarray, m: np.ndarray, v_idx: int,
                 ep_col="episode_id", k_col="k", bin_col="bin_hour",
                 val_col="value_median") -> int:
    """Fill one channel from a long-format bin table. Returns filled cells."""
    if len(long_df) == 0:
        return 0
    j = long_df[[ep_col, k_col, bin_col, val_col]].merge(
        master[["episode_id", "k", "row_idx"]],
        left_on=[ep_col, k_col], right_on=["episode_id", "k"],
        how="inner")
    if len(j) == 0:
        return 0
    r = j["row_idx"].to_numpy()
    b = j[bin_col].clip(0, GRID_T - 1).to_numpy()
    v = j[val_col].to_numpy(dtype=np.float32)
    x[r, v_idx, b] = v
    m[r, v_idx, b] = True
    return len(j)


def compute_delta(m: np.ndarray, cap: float = CAP_HOURS) -> np.ndarray:
    """Vectorized Δt per (row, var): hours since last observed bin.

    d[b] = 0 at observed bins; b - last_obs_bin afterwards;
    before the first observation → cap (mask=0 there anyway).
    """
    t_idx = np.arange(m.shape[2], dtype=np.float32)
    last = np.where(m, t_idx, -np.inf)
    last = np.maximum.accumulate(last, axis=2)
    d = t_idx - last
    d = np.where(np.isfinite(d), d, cap).astype(np.float32)
    d = np.minimum(d, cap)
    d[~m & (d == 0)] = cap  # 未观测 bin 的 0 仅允许出现在观测位置之后
    d[m] = 0.0
    return d


def summary_from_long(long_df: pd.DataFrame, var_col: str,
                      ep_col="episode_id", k_col="k",
                      val_col="value_median", time_col="max_event_time",
                      lm_col="t_landmark_ts") -> pd.DataFrame:
    """Per (episode, k, variable) summary for baseline tabular package."""
    g = long_df.groupby([ep_col, k_col, var_col], observed=True)
    out = g.agg(
        **{"min": (val_col, "min"), "median": (val_col, "median"),
           "max": (val_col, "max"), "sd": (val_col, "std"),
           "n_obs": (val_col, "count"),
           "last_value": (val_col, "last")}).reset_index()
    last_t = long_df.groupby([ep_col, k_col, var_col],
                             observed=True)[time_col].max().reset_index()
    lm = long_df[[ep_col, k_col, lm_col]].drop_duplicates([ep_col, k_col])
    out = out.merge(last_t, on=[ep_col, k_col, var_col], how="left")
    out = out.merge(lm, on=[ep_col, k_col], how="left")
    out["last_obs_age_h"] = (
        pd.to_datetime(out[lm_col]) - pd.to_datetime(out[time_col])
    ).dt.total_seconds() / 3600.0
    out = out.drop(columns=[time_col, lm_col])
    return out
