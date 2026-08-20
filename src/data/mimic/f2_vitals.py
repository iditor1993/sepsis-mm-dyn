"""F2: MIMIC vital signs, dual-track (v2.4.1 §5.2, §5.0).

chartevents 重建（itemid 白名单）：
  strict 轨：available_time = storetime（缺失降级 charttime 并打标）
  retro  轨：available_time = event_time = charttime
分箱约定：hours_before_landmark = (t_landmark - charttime)/3600 ∈ [0, 24]，
bin_hour = LEAST(FLOOR(hours_before), 23)（bin0 = 最近一小时）。

性能：窗口连接只物化一次（vitals_win 临时表），两轨分别聚合；
大结果集经 COPY TO 直接写 Parquet。
输出：
  vitals_realtime_strict_v2.parquet / vitals_charttime_retro_v2.parquet（长表）
  vitals_hourly_v2.parquet（strict 轨宽表，主分析用）
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

_ITEM_CASE = "\n".join(
    f"      WHEN ce.itemid IN ({', '.join(str(i) for i in ids)}) THEN '{name}'"
    for name, ids in config.MIMIC_VITAL_ITEMIDS.items())
_ALL_IDS = tuple(i for ids in config.MIMIC_VITAL_ITEMIDS.values() for i in ids)

_AGG_SELECT = """
    SELECT episode_id, k, t_landmark_ts,
      LEAST(FLOOR(hours_before)::INTEGER, 23) AS bin_hour,
      -- bin0 = 最近一小时：[t_lm-1h, t_lm]；bin b: [t_lm-(b+1)h, t_lm-bh)
      t_landmark_ts
        - (LEAST(FLOOR(hours_before)::INTEGER, 23) + 1) * INTERVAL '1 hour'
        AS bin_start,
      t_landmark_ts
        - LEAST(FLOOR(hours_before)::INTEGER, 23) * INTERVAL '1 hour' AS bin_end,
      variable,
      MEDIAN(valuenum) AS value_median,
      COUNT(*) AS n_source_records,
      MIN(event_time) AS min_event_time,
      MAX(event_time) AS max_event_time,
      MAX(available_time) AS max_available_time,
      'median' AS aggregation_method,
      'chartevents' AS source_table_set,
      MIN(source_time_type) AS source_time_type
    FROM vitals_win
    {where}
    GROUP BY episode_id, k, t_landmark_ts,
             LEAST(FLOOR(hours_before)::INTEGER, 23), variable
"""


def _prepare_win(con):
    """cohort hadm 过滤 + 单次窗口连接物化。"""
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    utils.log_step("F2: materialize vitals_win")
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW vitals_src AS
    WITH c AS (
      SELECT DISTINCT episode_id, hadm_id FROM read_parquet('{cohort_path}')
    ),
    bounds AS (
      SELECT c.hadm_id,
             MIN(l.t_landmark_ts) - INTERVAL '25 hours' AS t_lo,
             MAX(l.t_landmark_ts) AS t_hi
      FROM c JOIN read_parquet('{landmarks_path}') l
        ON l.episode_id = c.episode_id
      GROUP BY c.hadm_id
    )
    SELECT ce.hadm_id, ce.charttime AS event_time, ce.storetime,
      CASE
{_ITEM_CASE}
      END AS variable,
      -- C2 审计修正（2026-07-31）：223761 'Temperature Fahrenheit' 转 °C
      CASE WHEN ce.itemid = 223761
           THEN (ce.valuenum - 32.0) * 5.0 / 9.0
           ELSE ce.valuenum END AS valuenum
    FROM main.chartevents ce
    JOIN bounds b ON ce.hadm_id = b.hadm_id
    WHERE ce.itemid IN {_ALL_IDS}
      AND ce.valuenum IS NOT NULL AND ce.valuenum > 0
      AND ce.charttime >= b.t_lo AND ce.charttime <= b.t_hi
    """)
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE vitals_win AS
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    )
    SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
      v.variable, v.event_time,
      COALESCE(v.storetime, v.event_time) AS available_time,
      v.valuenum,
      EPOCH(lm.t_landmark_ts - v.event_time) / 3600.0 AS hours_before,
      CASE WHEN v.storetime IS NOT NULL THEN 'entry_verified'
           ELSE 'charttime_fallback' END AS source_time_type
    FROM lm JOIN vitals_src v ON v.hadm_id = lm.hadm_id
    WHERE v.event_time <= lm.t_landmark_ts
      AND v.event_time > lm.t_landmark_ts - INTERVAL '24 hours'
    """)
    n = con.execute("SELECT COUNT(*) FROM vitals_win").fetchone()[0]
    print(f"  vitals_win: {n:,} rows")


def _aggregate_track(con, track: str):
    out = config.OUTPUT_DIRS["features"]
    if track == "strict":
        where = "WHERE available_time <= t_landmark_ts"
        fname = "vitals_realtime_strict_v2.parquet"
    else:
        where = ""
        fname = "vitals_charttime_retro_v2.parquet"
    utils.log_step(f"F2: aggregate {track} track")
    n = utils.write_duckdb_table_direct(
        con, _AGG_SELECT.format(where=where), out / fname)
    print(f"  {fname}: {n:,} rows")
    return n


def _write_hourly_wide(con):
    out = config.OUTPUT_DIRS["features"]
    strict_path = str(out / "vitals_realtime_strict_v2.parquet")
    utils.log_step("F2: vitals_hourly_v2 (strict wide)")
    sql = f"""
    SELECT episode_id, k, t_landmark_ts, bin_hour, bin_start, bin_end,
      SUM(n_source_records) AS n_source_records,
      MAX(CASE WHEN variable = 'hr'  THEN value_median END) AS hr_median,
      MAX(CASE WHEN variable = 'sbp' THEN value_median END) AS sbp_median,
      MAX(CASE WHEN variable = 'dbp' THEN value_median END) AS dbp_median,
      MAX(CASE WHEN variable = 'mbp' THEN value_median END) AS mbp_median,
      MAX(CASE WHEN variable = 'rr'  THEN value_median END) AS rr_median,
      MAX(CASE WHEN variable = 'spo2' THEN value_median END) AS spo2_median,
      MAX(CASE WHEN variable = 'temp' THEN value_median END) AS temp_median,
      MAX(max_available_time) AS max_available_time,
      'median' AS aggregation_method,
      'chartevents' AS source_table_set
    FROM read_parquet('{strict_path}')
    GROUP BY episode_id, k, t_landmark_ts, bin_hour, bin_start, bin_end
    """
    n = utils.write_duckdb_table_direct(
        con, sql, out / "vitals_hourly_v2.parquet")
    print(f"  vitals_hourly_v2: {n:,} rows")
    return n


def run_f2_pipeline(con):
    _prepare_win(con)
    n_strict = _aggregate_track(con, "strict")
    _aggregate_track(con, "retro")
    _write_hourly_wide(con)
    return n_strict
