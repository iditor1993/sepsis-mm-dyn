"""L1/L2: eICU landmark 网格（v2.4.1 §3.2/§3.3；episode 分钟坐标）。

k0 = max(0, ceil((0 - t_sepsis_offset_min) / 360))
k ∈ [k0, config.LANDMARK_MAX_K=27]；t_landmark_offset_min = t_sepsis_offset_min + 360k
终止规则（阻断项 3 对齐）：episode_end_offset_min 非空 且
  t_landmark < episode_end_offset_min 且
  （非 Expired 或 t_landmark < death_episode_min，
    death_episode_min = hospitaldischargeoffset - episode_start_hospital_min
    = cohort.hospital_discharge_episode_min）
风险集（L2）：生成条件即风险集（t 时刻存活且 episode 未结束）。

注：P-strict track 的 t_sepsis_offset_min 恒 NULL（锁定选对函数 pending，
R33/B-5），不生成 landmark——属预期行为，QA 报告中显式标注。
hospitaldischargestatus NULL（实测 1,751 条）按非 Expired 处理，
标签层以观察终点状态机兜底（§4.1）。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_landmarks(con):
    out = config.OUTPUT_DIRS["landmarks"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_eicu_v2.parquet")
    step_min = config.LANDMARK_INTERVAL_HOURS * 60  # 360
    utils.log_step("L1/L2: eICU landmarks")

    sql = f"""
    WITH grid AS (
      SELECT c.episode_id, c.uniquepid, c.phenotype_track, k,
        (c.t_sepsis_offset_min + {step_min} * k)::BIGINT
          AS t_landmark_offset_min,
        c.episode_end_offset_min, c.hospitaldischargestatus,
        c.hospital_discharge_episode_min,
        GREATEST(0, CEIL((0 - c.t_sepsis_offset_min)
                         / {step_min}.0))::INTEGER AS k0
      FROM read_parquet('{cohort_path}') c
      CROSS JOIN generate_series(0, {config.LANDMARK_MAX_K}) AS t(k)
      WHERE c.t_sepsis_offset_min IS NOT NULL      -- P-strict pending（见 docstring）
        AND c.episode_end_offset_min IS NOT NULL   -- 阻断项 3 硬门槛
        AND k >= GREATEST(0, CEIL((0 - c.t_sepsis_offset_min)
                                  / {step_min}.0))
        AND c.t_sepsis_offset_min + {step_min} * k
            < c.episode_end_offset_min
        AND (c.hospitaldischargestatus IS NULL
             OR c.hospitaldischargestatus <> 'Expired'
             OR c.t_sepsis_offset_min + {step_min} * k
                < c.hospital_discharge_episode_min)
    )
    SELECT episode_id, uniquepid, k, t_landmark_offset_min,
      (k * {config.LANDMARK_INTERVAL_HOURS})::DOUBLE AS hours_since_sepsis,
      TRUE AS in_risk_set,
      (k <= {config.LANDMARK_MAIN_GRID_MAX_K}) AS in_main_grid,
      k0, phenotype_track
    FROM grid
    ORDER BY episode_id, phenotype_track, k
    """
    n = utils.write_duckdb_table(con, sql, out / "eicu_landmarks_v2.parquet")
    dist = con.execute(f"""
      SELECT phenotype_track, COUNT(*) rows_, COUNT(DISTINCT episode_id) eps
      FROM read_parquet('{str(out / "eicu_landmarks_v2.parquet").replace(chr(92), "/")}')
      GROUP BY 1 ORDER BY 1
    """).fetchall()
    print(f"  eicu_landmarks_v2: {n:,} rows; per track: {dist}")
    return n
