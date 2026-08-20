"""L3: MIMIC 24h three-state labels + 7d competing-risk labels (v2.4.1 §4).

状态机（A.3）+ 双观察终点（临床观察源白名单，P1-2）+
full_inhospital_followup_24h 边界（P1-3）+ outcome_unknown_reason 枚举 +
label_adjudications 物理分离。
"""
import sys
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def _build_observation_endpoints(con, merge_version=None):
    """双观察终点（§4.1 白名单）。

    last_clinically_observed_time：仅白名单事件/采集时间，封顶 dischtime；
    last_database_available_time：含 storetime，仅 QA。
    临床事件 > dischtime 记时间异常 QA。
    """
    out = config.OUTPUT_DIRS["labels"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    utils.log_step("L3: observation endpoints (whitelist)")

    sql = f"""
    WITH c AS (
      SELECT DISTINCT episode_id, hadm_id, dischtime
      FROM read_parquet('{cohort_path}')
    ),
    ev AS (
      SELECT c.episode_id, c.hadm_id, ce.charttime AS event_time,
             ce.storetime AS db_time
      FROM c JOIN main.chartevents ce ON ce.hadm_id = c.hadm_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, le.charttime, le.storetime
      FROM c JOIN main.labevents le ON le.hadm_id = c.hadm_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, u.charttime, NULL
      FROM c
      JOIN mimic_icu_stays s ON s.hadm_id = c.hadm_id
      JOIN mimiciv_derived.urine_output u ON u.stay_id = s.stay_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, v.starttime, NULL
      FROM c
      JOIN mimic_icu_stays s ON s.hadm_id = c.hadm_id
      JOIN mimiciv_derived.vasoactive_agent v ON v.stay_id = s.stay_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, v.endtime, NULL
      FROM c
      JOIN mimic_icu_stays s ON s.hadm_id = c.hadm_id
      JOIN mimiciv_derived.vasoactive_agent v ON v.stay_id = s.stay_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, v.endtime, NULL
      FROM c
      JOIN mimic_icu_stays s ON s.hadm_id = c.hadm_id
      JOIN mimiciv_derived.ventilation v ON v.stay_id = s.stay_id
      UNION ALL
      SELECT c.episode_id, c.hadm_id, m.charttime, NULL
      FROM c JOIN main.microbiologyevents m ON m.hadm_id = c.hadm_id
      WHERE m.charttime IS NOT NULL
    ),
    agg AS (
      SELECT episode_id,
        MAX(event_time) AS last_clinical_event_ts_raw,
        MAX(db_time) AS last_database_available_ts
      FROM ev GROUP BY episode_id
    )
    SELECT c.episode_id, c.hadm_id, c.dischtime,
      CASE
        WHEN c.dischtime IS NOT NULL AND a.last_clinical_event_ts_raw IS NOT NULL
          THEN LEAST(c.dischtime, a.last_clinical_event_ts_raw)
        WHEN c.dischtime IS NOT NULL THEN c.dischtime
        ELSE a.last_clinical_event_ts_raw
      END AS last_clinically_observed_time,
      a.last_database_available_ts AS last_database_available_time,
      CASE
        WHEN c.dischtime IS NOT NULL
          AND (a.last_clinical_event_ts_raw IS NULL
               OR c.dischtime <= a.last_clinical_event_ts_raw)
          THEN 'discharge'
        WHEN a.last_clinical_event_ts_raw IS NOT NULL THEN 'clinical_event'
        ELSE 'unknown'
      END AS observation_end_source,
      CASE WHEN a.last_clinical_event_ts_raw IS NOT NULL
            AND c.dischtime IS NOT NULL
            AND a.last_clinical_event_ts_raw > c.dischtime
           THEN TRUE ELSE FALSE END AS clinical_event_after_discharge_flag
    FROM c LEFT JOIN agg a USING (episode_id)
    """
    # mimic_icu_stays：episode 内全部 stay（hadm 级即可，白名单事件按 hadm 归集）
    con.execute(f"""
      CREATE OR REPLACE TEMP VIEW mimic_icu_stays AS
      SELECT DISTINCT hadm_id, stay_id FROM main.icustays
      WHERE hadm_id IN (SELECT hadm_id FROM read_parquet('{cohort_path}'))
    """)
    n = utils.write_duckdb_table(con, sql,
                                 out / "observation_endpoints_v2.parquet")
    print(f"  observation_endpoints_v2: {n:,} episodes")
    return n


def run_labels_24h(con, merge_version=None):
    out = config.OUTPUT_DIRS["labels"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    obs_path = str(out / "observation_endpoints_v2.parquet")
    acute_locs = "','".join(config.ACUTE_TRANSFER_LOCS)
    alive_locs = "','".join(config.ALIVE_DISCHARGE_LOCS)
    utils.log_step("L3: labels_24h_v2 state machine")

    sql = f"""
    WITH base AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts,
        l.t_landmark_ts + INTERVAL '24 hours' AS w_end,
        c.hadm_id, c.subject_id,
        a.hospital_expire_flag, a.deathtime, a.dischtime, a.discharge_location,
        CASE WHEN a.discharge_location IN ('{acute_locs}')
             THEN a.dischtime END AS acute_transfer_time,
        CASE WHEN a.discharge_location IN ('{alive_locs}')
             THEN a.dischtime END AS alive_discharge_time,
        o.last_clinically_observed_time, o.last_database_available_time,
        o.observation_end_source, o.clinical_event_after_discharge_flag
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
      JOIN main.admissions a ON c.hadm_id = a.hadm_id
      LEFT JOIN read_parquet('{obs_path}') o ON l.episode_id = o.episode_id
    ),
    state AS (
      SELECT *,
        CASE
          WHEN deathtime IS NOT NULL AND deathtime <= t_landmark_ts
            THEN 'invalid_input'
          WHEN deathtime IS NOT NULL AND hospital_expire_flag = 0
            THEN 'status_conflict'
          WHEN hospital_expire_flag = 1 AND deathtime IS NULL
            THEN 'death_time_missing'
          WHEN deathtime > t_landmark_ts AND deathtime <= w_end
            THEN 'event'
          WHEN acute_transfer_time > t_landmark_ts
               AND acute_transfer_time <= w_end
            THEN 'acute_transfer'
          WHEN alive_discharge_time > t_landmark_ts
               AND alive_discharge_time <= w_end
            THEN 'non_event_alive_discharge'
          WHEN last_clinically_observed_time >= w_end
            THEN 'non_event_observed'
          ELSE 'missing_status_left_observation'
        END AS label_state
      FROM base
    )
    SELECT episode_id, k, t_landmark_ts, w_end,
      CASE WHEN label_state = 'event' THEN 1
           WHEN label_state IN ('non_event_observed',
                                'non_event_alive_discharge')
           THEN 0 ELSE NULL END AS y_24h,
      CASE WHEN label_state = 'event' THEN 'event'
           WHEN label_state IN ('non_event_observed',
                                'non_event_alive_discharge')
           THEN 'non_event' ELSE 'unknown' END AS label_status,
      label_state AS label_reason,
      (label_state IN ('event', 'non_event_observed',
                       'non_event_alive_discharge'))
        AS outcome_ascertainable,
      CASE
        WHEN label_state = 'non_event_observed' THEN TRUE     -- P1-3 边界
        WHEN label_state = 'non_event_alive_discharge'
         AND alive_discharge_time >= w_end THEN TRUE
        ELSE FALSE END AS full_inhospital_followup_24h,
      CASE
        WHEN label_state = 'acute_transfer' THEN 'acute_transfer'
        WHEN label_state = 'death_time_missing' THEN 'death_time_missing'
        WHEN label_state = 'status_conflict' THEN 'status_conflict'
        WHEN label_state = 'invalid_input' THEN 'invalid_input'
        WHEN clinical_event_after_discharge_flag
          AND label_state = 'missing_status_left_observation'
          THEN 'time_anomaly'
        WHEN label_state = 'missing_status_left_observation'
          THEN 'missing_status_left_observation'
        ELSE NULL END AS outcome_unknown_reason,
      last_clinically_observed_time, last_database_available_time,
      observation_end_source,
      deathtime, dischtime, acute_transfer_time, alive_discharge_time
    FROM state
    ORDER BY episode_id, k
    """
    n = utils.write_duckdb_table(con, sql, out / "labels_24h_v2.parquet")
    print(f"  labels_24h_v2: {n:,} rows")

    # adjudications：冲突与缺失状态进人工裁决表（物理分离）
    adj_sql = f"""
    SELECT episode_id, k,
      label_reason AS label_preliminary_status,
      NULL AS label_final_status,
      'pending' AS label_adjudication_status,
      NULL AS label_adjudication_source
    FROM read_parquet('{out / "labels_24h_v2.parquet"}')
    WHERE label_reason IN ('status_conflict', 'death_time_missing',
                           'invalid_input')
    ORDER BY episode_id, k
    """
    m = utils.write_duckdb_table(con, adj_sql,
                                 out / "label_adjudications.parquet")
    print(f"  label_adjudications (pending): {m:,} rows")
    return n


def run_labels_competing_7d(con, merge_version=None):
    """§4.2 次要结局：7 天竞争风险（四类事件）。"""
    out = config.OUTPUT_DIRS["labels"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    obs_path = str(out / "observation_endpoints_v2.parquet")
    acute_locs = "','".join(config.ACUTE_TRANSFER_LOCS)
    alive_locs = "','".join(config.ALIVE_DISCHARGE_LOCS)
    utils.log_step("L3: labels_competing_7d_v2")

    sql = f"""
    WITH base AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts,
        l.t_landmark_ts + INTERVAL '168 hours' AS w7_end,
        a.deathtime, a.dischtime,
        CASE WHEN a.discharge_location IN ('{acute_locs}')
             THEN a.dischtime END AS acute_transfer_time,
        CASE WHEN a.discharge_location IN ('{alive_locs}')
             THEN a.dischtime END AS alive_discharge_time,
        o.last_clinically_observed_time
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
      JOIN main.admissions a ON c.hadm_id = a.hadm_id
      LEFT JOIN read_parquet('{obs_path}') o ON l.episode_id = o.episode_id
    ),
    ev AS (
      SELECT *,
        -- 同时刻优先级：死亡 > 急性转出 > 存活出院 > 删失
        CASE
          WHEN deathtime > t_landmark_ts AND deathtime <= w7_end THEN 1
          WHEN acute_transfer_time > t_landmark_ts
               AND acute_transfer_time <= w7_end THEN 3
          WHEN alive_discharge_time > t_landmark_ts
               AND alive_discharge_time <= w7_end THEN 2
          ELSE 0
        END AS event_type
      FROM base
    )
    SELECT episode_id, k, t_landmark_ts, w7_end,
      event_type,
      CASE event_type
        WHEN 1 THEN deathtime
        WHEN 3 THEN acute_transfer_time
        WHEN 2 THEN alive_discharge_time
        ELSE LEAST(w7_end, COALESCE(last_clinically_observed_time, w7_end))
      END AS event_or_censor_time,
      CASE WHEN event_type = 0
            AND last_clinically_observed_time < w7_end
           THEN 'left_observation' ELSE 'window_end' END AS censor_type
    FROM ev
    ORDER BY episode_id, k
    """
    n = utils.write_duckdb_table(con, sql,
                                 out / "labels_competing_7d_v2.parquet")
    print(f"  labels_competing_7d_v2: {n:,} rows")
    return n


def run_labels_pipeline(con, merge_version=None):
    _build_observation_endpoints(con, merge_version)
    run_labels_24h(con, merge_version)
    run_labels_competing_7d(con, merge_version)
