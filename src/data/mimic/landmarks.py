"""L1/L2: MIMIC landmark grid + risk set (v2.4.1 §3.2/§3.3, 阻断项 3)。

主分析硬门槛：episode_outtime_status = 'ok' AND episode_outtime_ts IS NOT NULL
（missing_or_open episode 不生成主分析 landmark，进 QA）。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_landmarks(con, merge_version=None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["landmarks"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    utils.log_step("L1/L2: MIMIC landmarks")

    sql = f"""
    WITH grid AS (
      SELECT c.episode_id, c.subject_id, k,
        c.t_sepsis_ts
          + (k * {config.LANDMARK_INTERVAL_HOURS}) * INTERVAL '1 hour'
          AS t_landmark_ts,
        c.episode_outtime_ts, c.deathtime,
        GREATEST(0, CEIL(
          EPOCH(c.episode_intime_ts - c.t_sepsis_ts)
          / ({config.LANDMARK_INTERVAL_HOURS} * 3600.0)))::INTEGER AS k0
      FROM read_parquet('{cohort_path}') c
      CROSS JOIN generate_series(0, {config.LANDMARK_MAX_K}) AS t(k)
      WHERE c.episode_outtime_status = 'ok'            -- 阻断项 3 硬门槛
        AND c.episode_outtime_ts IS NOT NULL
        AND k >= GREATEST(0, CEIL(
             EPOCH(c.episode_intime_ts - c.t_sepsis_ts)
             / ({config.LANDMARK_INTERVAL_HOURS} * 3600.0)))
        AND c.t_sepsis_ts
            + (k * {config.LANDMARK_INTERVAL_HOURS}) * INTERVAL '1 hour'
            < c.episode_outtime_ts
        AND (c.deathtime IS NULL
             OR c.t_sepsis_ts
                + (k * {config.LANDMARK_INTERVAL_HOURS}) * INTERVAL '1 hour'
                < c.deathtime)
    )
    SELECT episode_id, subject_id, k, t_landmark_ts,
      (k * {config.LANDMARK_INTERVAL_HOURS})::DOUBLE AS hours_since_sepsis,
      TRUE AS in_risk_set,                              -- L2：生成条件即风险集
      (k <= {config.LANDMARK_MAIN_GRID_MAX_K}) AS in_main_grid,
      k0,
      '{mv}' AS episode_mapping_version
    FROM grid
    ORDER BY episode_id, k
    """
    n = utils.write_duckdb_table(con, sql, out / "landmarks_v2.parquet")

    # missing_or_open episode 计数（QA）
    qa = con.execute(f"""
      SELECT episode_outtime_status, COUNT(*) FROM read_parquet('{cohort_path}')
      GROUP BY 1
    """).fetchall()
    print(f"  landmarks_v2: {n:,} rows; episode_outtime_status dist: {qa}")
    return n
