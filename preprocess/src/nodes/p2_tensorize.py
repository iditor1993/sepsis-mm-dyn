"""P2: MIMIC 临床时序张量化（方案 §4；v1.1 通道清单）。

输出：p2_clinical/<run_id>/master/{X_seq,M_seq,D_seq}.npy + summary_features.parquet
"""
import numpy as np
import pandas as pd

from lib import grid, io

VITALS_MAP = {"hr": "hr", "sbp": "sbp", "dbp": "dbp", "mbp": "mbp",
              "rr": "rr", "spo2": "spo2", "temp": "temp"}


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p2_clinical") / "master"
    out.mkdir(parents=True, exist_ok=True)
    master = pd.read_parquet(
        io.artifact_dir(cfg, "p1_validate") / "master_index.parquet")
    n_rows = len(master)

    channels = (cfg["channels"]["vitals"] + cfg["channels"]["labs"]
                + cfg["channels"]["extra_mimic_only"])
    v_of = {name: i for i, name in enumerate(channels)}
    print(f"[P2] rows={n_rows:,} channels={len(channels)} {channels}")

    x, m, d = grid.alloc_tensors(
        n_rows, len(channels),
        out / "X_seq.npy", out / "M_seq.npy", out / "D_seq.npy")

    # --- vitals strict long table ---
    print("[P2] fill vitals ...")
    vit = pd.read_parquet(
        root / "features/vitals_realtime_strict_v2.parquet",
        columns=["episode_id", "k", "bin_hour", "variable", "value_median"])
    for name, var in VITALS_MAP.items():
        sub = vit[vit["variable"] == var]
        n = grid.fill_channel(master, sub, x, m, v_of[name],
                              val_col="value_median")
        print(f"    {name}: {n:,} cells")
    del vit

    # --- labs strict track ---
    print("[P2] fill labs ...")
    labs = pd.read_parquet(
        root / "features/labs_hourly_v2.parquet",
        columns=["episode_id", "k", "time_track", "bin_hour", "lab_name",
                 "value_median"])
    labs = labs[labs["time_track"] == "strict_available_time"]
    for name in cfg["channels"]["labs"]:
        sub = labs[labs["lab_name"] == name]
        n = grid.fill_channel(master, sub, x, m, v_of[name],
                              val_col="value_median")
        print(f"    {name}: {n:,} cells")
    del labs

    # --- nee_current（v1.1 §4.4：P2 按窗重建，bin 内 max） ---
    print("[P2] rebuild nee_current bins ...")
    nee = _rebuild_nee(cfg, master)
    n = grid.fill_channel(master, nee, x, m, v_of["nee_current"],
                          val_col="value_median")
    print(f"    nee_current: {n:,} cells")
    del nee

    # --- Δt ---
    print("[P2] compute delta_t ...")
    d[:] = grid.compute_delta(m)

    x.flush(); m.flush(); d.flush()
    del x, m, d

    # --- summary features ---
    print("[P2] summary features ...")
    summary = _build_summary(cfg, master)
    summary.to_parquet(
        io.artifact_dir(cfg, "p2_clinical") / "summary_features.parquet",
        index=False)
    print(f"[P2] summary_features: {len(summary):,} rows")

    meta = {"channels": channels, "n_rows": n_rows,
            "tensors": ["X_seq.npy", "M_seq.npy", "D_seq.npy"],
            "grid": cfg["grid"]}
    io.write_json(meta, out / "tensor_meta.json")
    return meta


def _rebuild_nee(cfg: dict, master: pd.DataFrame) -> pd.DataFrame:
    """NEE 逐小时 bin（bin 内 max；窗口 (t-24h, t]）。"""
    import duckdb
    con = duckdb.connect(cfg["paths"]["mimic_db"], read_only=True)
    con.execute("SET threads TO 4")
    con.execute("SET memory_limit = '12GB'")
    con.execute("SET preserve_insertion_order = false")
    lm_path = str(io.artifact_dir(cfg, "p1_validate")
                  / "master_index.parquet").replace("\\", "/")
    ep_path = str(io.data_root(cfg)
                  / "episodes/mimic_icu_episode_map_final.parquet") \
        .replace("\\", "/")
    mv = cfg["episode_mapping_version"]
    df = con.execute(f"""
      WITH lm AS (
        SELECT episode_id, k, t_landmark_ts FROM read_parquet('{lm_path}')
      ),
      stays AS (
        SELECT episode_id, stay_id FROM read_parquet('{ep_path}')
        WHERE episode_mapping_version = '{mv}'
      ),
      win AS (
        SELECT lm.episode_id, lm.k, n.starttime, n.endtime,
               n.norepinephrine_equivalent_dose AS dose,
               EPOCH(lm.t_landmark_ts - n.starttime) / 3600.0 AS hb_start
        FROM lm
        JOIN stays s ON s.episode_id = lm.episode_id
        JOIN mimiciv_derived.norepinephrine_equivalent_dose n
          ON n.stay_id = s.stay_id
        WHERE n.starttime <= lm.t_landmark_ts
          AND n.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
      )
      SELECT episode_id, k,
        LEAST(FLOOR(hb_start)::INTEGER, 23) AS bin_hour,
        MAX(dose) AS value_median
      FROM win
      GROUP BY episode_id, k, LEAST(FLOOR(hb_start)::INTEGER, 23)
    """).fetchdf()
    con.close()
    return df


def _build_summary(cfg: dict, master: pd.DataFrame) -> pd.DataFrame:
    """每 (episode,k,variable) 汇总特征（基线包用；strict 轨）。"""
    import duckdb
    con = duckdb.connect()
    root = str(io.data_root(cfg)).replace("\\", "/")
    lm_path = str(io.artifact_dir(cfg, "p1_validate")
                  / "master_index.parquet").replace("\\", "/")
    vit = f"SELECT episode_id, k, bin_hour, variable, value_median, max_event_time, max_available_time FROM read_parquet('{root}/features/vitals_realtime_strict_v2.parquet')"
    lab = f"""SELECT episode_id, k, bin_hour, lab_name AS variable, value_median, max_event_time, max_available_time
              FROM read_parquet('{root}/features/labs_hourly_v2.parquet')
              WHERE time_track = 'strict_available_time'"""
    df = con.execute(f"""
      WITH lm AS (
        SELECT episode_id, k, t_landmark_ts FROM read_parquet('{lm_path}')
      ),
      src AS (
        SELECT * FROM ({vit})
        UNION ALL
        SELECT * FROM ({lab})
      ),
      j AS (
        SELECT s.*, lm.t_landmark_ts
        FROM src s JOIN lm ON lm.episode_id = s.episode_id AND lm.k = s.k
      )
      SELECT episode_id, k, variable,
        MIN(value_median) AS min, MEDIAN(value_median) AS median,
        MAX(value_median) AS max, STDDEV(value_median) AS sd,
        COUNT(*) AS n_obs,
        ANY_VALUE(value_median ORDER BY max_event_time DESC) AS last_value,
        EPOCH(MAX(t_landmark_ts) - MAX(max_event_time)) / 3600.0
          AS last_obs_age_h
      FROM j
      GROUP BY episode_id, k, variable
    """).fetchdf()
    con.close()
    return df
