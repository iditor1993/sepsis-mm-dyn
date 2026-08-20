"""F5: MIMIC vasoactive agents / NEE stream (v2.4.1 §5.5; R15).

nee_current / 24h 统计与经典 SOFA CV 用药剂量三变量严格分离。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_f5_nee(con, merge_version=None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    utils.log_step("F5: nee_stream_v2")

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
      SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
        n.starttime, n.endtime, n.norepinephrine_equivalent_dose AS nee_dose,
        -- 输注与 (t-24h, t] 窗口的重叠时长（小时）
        EPOCH(LEAST(n.endtime, lm.t_landmark_ts)
              - GREATEST(n.starttime,
                         lm.t_landmark_ts - INTERVAL '24 hours')) / 3600.0
          AS overlap_h
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.norepinephrine_equivalent_dose n
        ON n.stay_id = s.stay_id
      WHERE n.starttime <= lm.t_landmark_ts
        AND n.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
    ),
    vaso_win AS (
      SELECT lm.episode_id, lm.k,
        MAX(va.dopamine) AS dopamine_max_24h,
        MAX(va.dobutamine) AS dobutamine_max_24h,
        MAX(va.epinephrine) AS epinephrine_max_24h,
        MAX(va.norepinephrine) AS norepinephrine_max_24h,
        MAX(va.phenylephrine) AS phenylephrine_max_24h,
        MAX(va.vasopressin) AS vasopressin_max_24h
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.vasoactive_agent va ON va.stay_id = s.stay_id
      WHERE va.starttime <= lm.t_landmark_ts
        AND va.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
      GROUP BY lm.episode_id, lm.k
    ),
    nee_now AS (
      SELECT lm.episode_id, lm.k,
        ANY_VALUE(n.norepinephrine_equivalent_dose
                  ORDER BY n.starttime DESC) AS nee_current
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.norepinephrine_equivalent_dose n
        ON n.stay_id = s.stay_id
      WHERE n.starttime <= lm.t_landmark_ts AND n.endtime > lm.t_landmark_ts
      GROUP BY lm.episode_id, lm.k
    ),
    agg AS (
      SELECT episode_id, k, t_landmark_ts,
        COUNT(*) AS n_infusion_records,
        MAX(nee_dose) AS nee_max_24h,
        MEDIAN(nee_dose) AS nee_median_24h,
        SUM(nee_dose * overlap_h) AS nee_auc_24h
      FROM win
      GROUP BY episode_id, k, t_landmark_ts
    )
    SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
      COALESCE(a.n_infusion_records, 0) AS n_infusion_records,
      a.nee_max_24h, a.nee_median_24h, a.nee_auc_24h,
      nn.nee_current,
      v.dopamine_max_24h, v.dobutamine_max_24h, v.epinephrine_max_24h,
      v.norepinephrine_max_24h, v.phenylephrine_max_24h,
      v.vasopressin_max_24h
    FROM lm
    LEFT JOIN agg a ON a.episode_id = lm.episode_id AND a.k = lm.k
    LEFT JOIN nee_now nn ON nn.episode_id = lm.episode_id
                        AND nn.k = lm.k
    LEFT JOIN vaso_win v ON v.episode_id = lm.episode_id AND v.k = lm.k
    ORDER BY lm.episode_id, lm.k
    """
    n = utils.write_duckdb_table(con, sql, out / "nee_stream_v2.parquet")
    print(f"  nee_stream_v2: {n:,} rows")
    return n
