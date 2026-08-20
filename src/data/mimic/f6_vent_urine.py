"""F6/F7: MIMIC mechanical ventilation + urine output (v2.4.1 §5.6/§5.7)."""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_f6_vent(con, merge_version=None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    utils.log_step("F6: ventilation_v2")

    sql = f"""
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    ),
    stays AS (
      SELECT episode_id, stay_id FROM read_parquet('{ep_path}')
      WHERE episode_mapping_version = '{mv}'
    ),
    win AS (
      SELECT lm.episode_id, lm.k, lm.t_landmark_ts, v.ventilation_status,
        EPOCH(LEAST(v.endtime, lm.t_landmark_ts)
              - GREATEST(v.starttime,
                         lm.t_landmark_ts - INTERVAL '24 hours')) / 3600.0
          AS overlap_h
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.ventilation v ON v.stay_id = s.stay_id
      WHERE v.starttime <= lm.t_landmark_ts
        AND v.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
    )
    SELECT episode_id, k, t_landmark_ts,
      COUNT(*) AS n_vent_records,
      MAX(CASE WHEN ventilation_status IN ('InvasiveVent','Tracheostomy')
               THEN 1 ELSE 0 END) AS invasive_vent_24h,
      MAX(CASE WHEN ventilation_status = 'NonInvasiveVent'
               THEN 1 ELSE 0 END) AS noninvasive_vent_24h,
      MAX(CASE WHEN ventilation_status = 'HighFlow'
               THEN 1 ELSE 0 END) AS high_flow_24h,
      SUM(overlap_h) AS vent_duration_24h
    FROM win
    GROUP BY episode_id, k, t_landmark_ts
    ORDER BY episode_id, k
    """
    n = utils.write_duckdb_table(con, sql, out / "ventilation_v2.parquet")
    print(f"  ventilation_v2: {n:,} rows")
    return n


def run_f7_urine(con, merge_version=None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    utils.log_step("F7: urine_output_v2")

    sql = f"""
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    ),
    stays AS (
      SELECT episode_id, stay_id FROM read_parquet('{ep_path}')
      WHERE episode_mapping_version = '{mv}'
    )
    SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
      COUNT(u.urineoutput) AS n_uo_records,
      SUM(u.urineoutput) AS total_urine_24h_ml,
      MEDIAN(u.urineoutput) AS median_uo_ml,
      MAX(u.urineoutput) AS max_uo_ml,
      MAX(u.charttime) AS max_event_time
    FROM lm
    JOIN stays s ON s.episode_id = lm.episode_id
    JOIN mimiciv_derived.urine_output u ON u.stay_id = s.stay_id
    WHERE u.charttime <= lm.t_landmark_ts
      AND u.charttime > lm.t_landmark_ts - INTERVAL '24 hours'
    GROUP BY lm.episode_id, lm.k, lm.t_landmark_ts
    ORDER BY lm.episode_id, lm.k
    """
    n = utils.write_duckdb_table(con, sql, out / "urine_output_v2.parquet")
    print(f"  urine_output_v2: {n:,} rows")
    return n
