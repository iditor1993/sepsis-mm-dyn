"""F3: MIMIC labs (v2.4.1 §5.3) + PaO2/FiO2 pairing.

labevents 重建（charttime = event_time；storetime = available_time 主轨，
缺失降级 charttime 打标）。双轨合并于 labs_hourly_v2（time_track 列）。
pf_ratio_v2：P/F 双时间、pf_available_time = max(两者)、gap、fio2_source。
性能：窗口连接物化一次（labs_win），双轨分别聚合，COPY TO 直写。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

_LAB_CASE = "\n".join(
    f"      WHEN le.itemid IN ({', '.join(str(i) for i in ids)}) THEN '{name}'"
    for name, ids in config.MIMIC_LAB_ITEMIDS.items())
_ALL_LAB_IDS = tuple(
    i for ids in config.MIMIC_LAB_ITEMIDS.values() for i in ids)
_FIO2_IDS = tuple(int(i) for i in config.MIMIC_FIO2_ITEMIDS.keys())
_FIO2_CASE = "\n".join(
    f"      WHEN ce.itemid = {iid} THEN '{src}'"
    for iid, src in config.MIMIC_FIO2_ITEMIDS.items())


def _prepare_sources(con):
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    utils.log_step("F3: materialize labs sources + win")
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW lab_src AS
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
    SELECT le.hadm_id, le.charttime AS event_time, le.storetime,
      CASE
{_LAB_CASE}
      END AS lab_name,
      le.valuenum, le.valueuom
    FROM main.labevents le
    JOIN bounds b ON le.hadm_id = b.hadm_id
    WHERE le.itemid IN {_ALL_LAB_IDS}
      AND le.valuenum IS NOT NULL AND le.valuenum > 0
      AND le.charttime >= b.t_lo AND le.charttime <= b.t_hi
    """)
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW fio2_src AS
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
      ce.valuenum,
      CASE
{_FIO2_CASE}
      END AS fio2_source
    FROM main.chartevents ce
    JOIN bounds b ON ce.hadm_id = b.hadm_id
    WHERE ce.itemid IN {_FIO2_IDS}
      AND ce.valuenum IS NOT NULL AND ce.valuenum > 0
      AND ce.charttime >= b.t_lo AND ce.charttime <= b.t_hi
    """)
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE labs_win AS
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    )
    SELECT lm.episode_id, lm.k, lm.t_landmark_ts, s.lab_name,
      s.event_time,
      COALESCE(s.storetime, s.event_time) AS available_time,
      s.valuenum,
      EPOCH(lm.t_landmark_ts - s.event_time) / 3600.0 AS hours_before,
      CASE WHEN s.storetime IS NOT NULL THEN 'result_available'
           ELSE 'charttime_fallback' END AS source_time_type
    FROM lm JOIN lab_src s ON s.hadm_id = lm.hadm_id
    WHERE s.event_time <= lm.t_landmark_ts
      AND s.event_time > lm.t_landmark_ts - INTERVAL '24 hours'
    """)
    n = con.execute("SELECT COUNT(*) FROM labs_win").fetchone()[0]
    print(f"  labs_win: {n:,} rows")


_AGG = """
    SELECT episode_id, k, t_landmark_ts,
      '{track}' AS time_track,
      LEAST(FLOOR(hours_before)::INTEGER, 23) AS bin_hour,
      -- bin0 = 最近一小时：[t_lm-1h, t_lm]；bin b: [t_lm-(b+1)h, t_lm-bh)
      t_landmark_ts
        - (LEAST(FLOOR(hours_before)::INTEGER, 23) + 1) * INTERVAL '1 hour'
        AS bin_start,
      t_landmark_ts
        - LEAST(FLOOR(hours_before)::INTEGER, 23) * INTERVAL '1 hour' AS bin_end,
      lab_name,
      MEDIAN(valuenum) AS value_median,
      COUNT(*) AS n_source_records,
      MIN(event_time) AS min_event_time,
      MAX(event_time) AS max_event_time,
      MAX(available_time) AS max_available_time,
      'median' AS aggregation_method,
      'labevents' AS source_table_set,
      MIN(source_time_type) AS source_time_type
    FROM labs_win
    {where}
    GROUP BY episode_id, k, t_landmark_ts,
             LEAST(FLOOR(hours_before)::INTEGER, 23), lab_name
"""


def run_f3_labs(con):
    out = config.OUTPUT_DIRS["features"]
    utils.log_step("F3: labs_hourly_v2 (dual track)")
    parts = []
    for track, where in [
            ("strict_available_time", "WHERE available_time <= t_landmark_ts"),
            ("chart_or_event_time", "")]:
        parts.append(f"COPY ({_AGG.format(track=track, where=where)}) "
                     f"TO '{{p}}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    import tempfile
    tmp_paths = []
    for i, stmt in enumerate(parts):
        p = out / f"_labs_part{i}.parquet"
        tmp_paths.append(str(p).replace("\\", "/"))
        con.execute(stmt.format(p=tmp_paths[-1]))
    sql = " UNION ALL ".join(
        f"SELECT * FROM read_parquet('{p}')" for p in tmp_paths)
    n = utils.write_duckdb_table_direct(
        con, sql + " ORDER BY episode_id, k, time_track, bin_hour, lab_name",
        out / "labs_hourly_v2.parquet")
    for p in tmp_paths:
        Path(p).unlink(missing_ok=True)
    print(f"  labs_hourly_v2: {n:,} rows")
    return n


def run_f3_pf_ratio(con):
    """PaO2/FiO2 配对（双时间；主分析仅用明确记录 FiO2）。"""
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    utils.log_step("F3: pf_ratio_v2")

    sql = f"""
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    ),
    pao2_last AS (
      SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
        s.valuenum AS pao2_value, s.event_time AS pao2_event_time,
        COALESCE(s.storetime, s.event_time) AS pao2_available_time,
        ROW_NUMBER() OVER (PARTITION BY lm.episode_id, lm.k
                           ORDER BY s.event_time DESC) AS rn
      FROM lm JOIN lab_src s ON s.hadm_id = lm.hadm_id
      WHERE s.lab_name = 'pao2'
        AND s.event_time <= lm.t_landmark_ts
        AND s.event_time > lm.t_landmark_ts - INTERVAL '24 hours'
    ),
    fio2_last AS (
      SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
        s.valuenum AS fio2_value_raw, s.event_time AS fio2_event_time,
        COALESCE(s.storetime, s.event_time) AS fio2_available_time,
        s.fio2_source,
        ROW_NUMBER() OVER (PARTITION BY lm.episode_id, lm.k
                           ORDER BY s.event_time DESC) AS rn
      FROM lm JOIN fio2_src s ON s.hadm_id = lm.hadm_id
      WHERE s.event_time <= lm.t_landmark_ts
        AND s.event_time > lm.t_landmark_ts - INTERVAL '24 hours'
    )
    SELECT p.episode_id, p.k, p.t_landmark_ts,
      p.pao2_value, p.pao2_event_time, p.pao2_available_time,
      f.fio2_value_raw,
      CASE WHEN f.fio2_value_raw > 1.5
           THEN f.fio2_value_raw / 100.0 ELSE f.fio2_value_raw END AS fio2_value,
      f.fio2_event_time, f.fio2_available_time, f.fio2_source,
      GREATEST(p.pao2_available_time, f.fio2_available_time)
        AS pf_available_time,
      ABS(EPOCH(p.pao2_event_time - f.fio2_event_time)) / 60.0
        AS pf_pairing_gap_min,
      p.pao2_value / CASE WHEN f.fio2_value_raw > 1.5
                          THEN f.fio2_value_raw / 100.0
                          ELSE f.fio2_value_raw END AS pf_ratio,
      CASE WHEN p.pao2_available_time <= p.t_landmark_ts
            AND f.fio2_available_time <= p.t_landmark_ts
           THEN TRUE ELSE FALSE END AS pf_strict_eligible
    FROM pao2_last p
    JOIN fio2_last f ON p.episode_id = f.episode_id AND p.k = f.k
    WHERE p.rn = 1 AND f.rn = 1
    """
    n = utils.write_duckdb_table_direct(con, sql, out / "pf_ratio_v2.parquet")
    print(f"  pf_ratio_v2: {n:,} rows")
    return n


def run_f3_pipeline(con):
    _prepare_sources(con)
    run_f3_labs(con)
    run_f3_pf_ratio(con)
