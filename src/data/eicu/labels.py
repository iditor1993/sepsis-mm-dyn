"""L3: eICU 24h 三态标签 + 7d 竞争风险标签（v2.4.1 §4；全 *_episode_min）。

与 mimic/labels.py 同构的状态机（24h = 1440min），差异：
- 时间一律 episode 坐标 INTEGER 分钟（§4.1 eICU 统一坐标）；
- 死亡：hospitaldischargestatus = 'Expired' 且
  death_episode_min = hospital_discharge_episode_min ∈ (t, t+1440]；
- status_conflict 类比：hospitaldischargelocation = 'Death' 但
  status <> 'Expired'（实测 2026-07-30：Death 18,004 与 Expired 18,004
  完全一致，预期 0 命中；保留防护分支）；
- hospitaldischargestatus NULL（实测 1,751）按分支 5 以
  last_clinically_observed_episode_min 判定（§4.1）。

出院去向清单（D-3 pending；2026-07-30 实测 hospitaldischargelocation
DISTINCT 写入下方注释，冻结前预登记，§9 R9）：
  Home(116,816), Skilled Nursing Facility(27,582), Death(18,004),
  Other External(9,757), Rehabilitation(8,905), Other(7,888),
  Other Hospital(7,807), Nursing Home(2,067), NULL(2,033)
  - acute：'Other Hospital' 实测命中；'Telemetry'/'Step-Down Unit' 为
    unit 级取值（unitdischargelocation），hospital 级未实测——保留待 D-3；
  - alive：实测命中 Home/Rehabilitation/SNF/Nursing Home；
    'Long Term Care Hospital'/'Assisted Living' 未实测——保留待 D-3；
  - 'Other External'(9,757)/'Other'(7,888) 含义不明 → 不进 alive
    （保守：落入分支 5 观察终点判定）。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

# 出院去向清单（D-3 pending；实测分布见模块 docstring）
ACUTE_TRANSFER_LOCS_EICU = ("Other Hospital", "Telemetry", "Step-Down Unit")
ALIVE_DISCHARGE_LOCS_EICU = ("Home", "Rehabilitation",
                             "Skilled Nursing Facility", "Nursing Home",
                             "Long Term Care Hospital", "Assisted Living")

_WINDOW_24H_MIN = 1440
_WINDOW_7D_MIN = 10080


def _paths():
    return {
        "cohort": str(config.OUTPUT_DIRS["cohorts"] / "cohort_eicu_v2.parquet")
        .replace("\\", "/"),
        "landmarks": str(config.OUTPUT_DIRS["landmarks"]
                         / "eicu_landmarks_v2.parquet").replace("\\", "/"),
        "final_map": str(config.OUTPUT_DIRS["episodes"]
                         / "eicu_episode_map_final.parquet").replace("\\", "/"),
        "obs": config.OUTPUT_DIRS["labels"]
        / "eicu_observation_endpoints_v2.parquet",
        "labels24": config.OUTPUT_DIRS["labels"] / "eicu_labels_24h_v2.parquet",
        "adj": config.OUTPUT_DIRS["labels"] / "eicu_label_adjudications.parquet",
        "comp7d": config.OUTPUT_DIRS["labels"]
        / "eicu_labels_competing_7d_v2.parquet",
    }


def _build_observation_endpoints(con):
    """每 episode 临床观察终点（§4.1 白名单 eICU 版）。

    last_clinically_observed_episode_min = max（白名单事件 episode 坐标），
    封顶 hospital_discharge_episode_min；observation_end_source 同 MIMIC
    （discharge / clinical_event / unknown）。
    白名单：pivoted_vital.chartoffset、lab.labresultoffset、
    pivoted_lab/pivoted_bg/pivoted_gcs/pivoted_uo.chartoffset、
    infusion_drug.infusionoffset（均为事件/采集时间；eICU 无 storetime 概念，
    last_database_available_time 不适用，不生成）。
    """
    p = _paths()
    utils.log_step("L3: eicu_observation_endpoints_v2 (whitelist)")

    def _events(src_table, off_col):
        return f"""
          SELECT s.episode_id,
            ((-s.hospitaladmitoffset + e.{off_col})
              - s.episode_start_hospital_min) AS event_episode_min
          FROM stays s
          JOIN main.{src_table} e ON e.patientunitstayid = s.patientunitstayid
        """

    sql = f"""
    WITH co AS (
      SELECT DISTINCT episode_id, hospital_discharge_episode_min
      FROM read_parquet('{p["cohort"]}')
    ),
    stays AS (
      SELECT f.episode_id, f.patientunitstayid,
        f.episode_start_hospital_min, pt.hospitaladmitoffset
      FROM read_parquet('{p["final_map"]}') f
      JOIN main.patient pt ON pt.patientunitstayid = f.patientunitstayid
      JOIN co ON co.episode_id = f.episode_id
    ),
    ev AS (
      {_events("pivoted_vital", "chartoffset")}
      UNION ALL {_events("lab", "labresultoffset")}
      UNION ALL {_events("pivoted_lab", "chartoffset")}
      UNION ALL {_events("pivoted_bg", "chartoffset")}
      UNION ALL {_events("pivoted_gcs", "chartoffset")}
      UNION ALL {_events("pivoted_uo", "chartoffset")}
      UNION ALL {_events("infusion_drug", "infusionoffset")}
    ),
    agg AS (
      SELECT episode_id, MAX(event_episode_min) AS last_event_raw
      FROM ev GROUP BY episode_id
    )
    SELECT co.episode_id, co.hospital_discharge_episode_min,
      CASE
        WHEN co.hospital_discharge_episode_min IS NOT NULL
          AND a.last_event_raw IS NOT NULL
          THEN LEAST(co.hospital_discharge_episode_min, a.last_event_raw)
        WHEN co.hospital_discharge_episode_min IS NOT NULL
          THEN co.hospital_discharge_episode_min
        ELSE a.last_event_raw
      END AS last_clinically_observed_episode_min,
      CASE
        WHEN co.hospital_discharge_episode_min IS NOT NULL
          AND (a.last_event_raw IS NULL
               OR co.hospital_discharge_episode_min <= a.last_event_raw)
          THEN 'discharge'
        WHEN a.last_event_raw IS NOT NULL THEN 'clinical_event'
        ELSE 'unknown'
      END AS observation_end_source,
      CASE WHEN a.last_event_raw IS NOT NULL
            AND co.hospital_discharge_episode_min IS NOT NULL
            AND a.last_event_raw > co.hospital_discharge_episode_min
           THEN TRUE ELSE FALSE END AS clinical_event_after_discharge_flag
    FROM co LEFT JOIN agg a USING (episode_id)
    """
    n = utils.write_duckdb_table(con, sql, p["obs"])
    print(f"  eicu_observation_endpoints_v2: {n:,} episodes")
    return n


def _base_cte(p, window_min):
    acute = "','".join(ACUTE_TRANSFER_LOCS_EICU)
    alive = "','".join(ALIVE_DISCHARGE_LOCS_EICU)
    return f"""
    WITH base AS (
      SELECT l.episode_id, l.phenotype_track, l.uniquepid, l.k,
        l.t_landmark_offset_min,
        (l.t_landmark_offset_min + {window_min})::BIGINT AS w_end_episode_min,
        c.hospitaldischargestatus, c.hospitaldischargelocation,
        c.hospital_discharge_episode_min,
        CASE WHEN c.hospitaldischargestatus = 'Expired'
             THEN c.hospital_discharge_episode_min END AS death_episode_min,
        CASE WHEN c.hospitaldischargelocation IN ('{acute}')
             THEN c.hospital_discharge_episode_min END
          AS acute_transfer_episode_min,
        CASE WHEN c.hospitaldischargelocation IN ('{alive}')
             THEN c.hospital_discharge_episode_min END
          AS alive_discharge_episode_min,
        o.last_clinically_observed_episode_min,
        o.observation_end_source, o.clinical_event_after_discharge_flag
      FROM read_parquet('{p["landmarks"]}') l
      JOIN read_parquet('{p["cohort"]}') c
        ON c.episode_id = l.episode_id
       AND c.phenotype_track = l.phenotype_track
      LEFT JOIN read_parquet('{str(p["obs"]).replace(chr(92), "/")}') o
        ON o.episode_id = l.episode_id
    )
    """


def run_labels_24h(con):
    p = _paths()
    utils.log_step("L3: eicu_labels_24h_v2 state machine")
    sql = _base_cte(p, _WINDOW_24H_MIN) + """
    , state AS (
      SELECT *,
        CASE
          WHEN death_episode_min IS NOT NULL
            AND death_episode_min <= t_landmark_offset_min
            THEN 'invalid_input'
          WHEN hospitaldischargelocation = 'Death'
            AND (hospitaldischargestatus IS NULL
                 OR hospitaldischargestatus <> 'Expired')
            THEN 'status_conflict'
          WHEN hospitaldischargestatus = 'Expired'
            AND hospital_discharge_episode_min IS NULL
            THEN 'death_time_missing'
          WHEN death_episode_min > t_landmark_offset_min
            AND death_episode_min <= w_end_episode_min
            THEN 'event'
          WHEN acute_transfer_episode_min > t_landmark_offset_min
            AND acute_transfer_episode_min <= w_end_episode_min
            THEN 'acute_transfer'
          WHEN alive_discharge_episode_min > t_landmark_offset_min
            AND alive_discharge_episode_min <= w_end_episode_min
            THEN 'non_event_alive_discharge'
          WHEN last_clinically_observed_episode_min >= w_end_episode_min
            THEN 'non_event_observed'
          ELSE 'missing_status_left_observation'
        END AS label_state
      FROM base
    )
    SELECT episode_id, phenotype_track, uniquepid, k,
      t_landmark_offset_min, w_end_episode_min,
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
        WHEN label_state = 'non_event_observed' THEN TRUE   -- P1-3 边界
        WHEN label_state = 'non_event_alive_discharge'
         AND alive_discharge_episode_min >= w_end_episode_min THEN TRUE
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
      last_clinically_observed_episode_min, observation_end_source,
      death_episode_min, hospital_discharge_episode_min,
      acute_transfer_episode_min, alive_discharge_episode_min,
      hospitaldischargestatus, hospitaldischargelocation
    FROM state
    ORDER BY episode_id, phenotype_track, k
    """
    n = utils.write_duckdb_table(con, sql, p["labels24"])
    print(f"  eicu_labels_24h_v2: {n:,} rows")

    # adjudications：冲突与缺失状态进人工裁决表（物理分离）
    adj_sql = f"""
    SELECT episode_id, phenotype_track, k,
      label_reason AS label_preliminary_status,
      NULL AS label_final_status,
      'pending' AS label_adjudication_status,
      NULL AS label_adjudication_source
    FROM read_parquet('{str(p["labels24"]).replace(chr(92), "/")}')
    WHERE label_reason IN ('status_conflict', 'death_time_missing',
                           'invalid_input')
    ORDER BY episode_id, phenotype_track, k
    """
    m = utils.write_duckdb_table(con, adj_sql, p["adj"])
    print(f"  eicu_label_adjudications (pending): {m:,} rows")
    return n


def run_labels_competing_7d(con):
    """§4.2 次要结局：7 天竞争风险（四类事件；全 *_episode_min）。"""
    p = _paths()
    utils.log_step("L3: eicu_labels_competing_7d_v2")
    sql = _base_cte(p, _WINDOW_7D_MIN) + """
    , ev AS (
      SELECT *,
        -- 同时刻优先级：死亡 > 急性转出 > 存活出院 > 删失
        CASE
          WHEN death_episode_min > t_landmark_offset_min
            AND death_episode_min <= w_end_episode_min THEN 1
          WHEN acute_transfer_episode_min > t_landmark_offset_min
            AND acute_transfer_episode_min <= w_end_episode_min THEN 3
          WHEN alive_discharge_episode_min > t_landmark_offset_min
            AND alive_discharge_episode_min <= w_end_episode_min THEN 2
          ELSE 0
        END AS event_type
      FROM base
    )
    SELECT episode_id, phenotype_track, uniquepid, k,
      t_landmark_offset_min, w_end_episode_min,
      event_type,
      CASE event_type
        WHEN 1 THEN death_episode_min
        WHEN 3 THEN acute_transfer_episode_min
        WHEN 2 THEN alive_discharge_episode_min
        ELSE LEAST(w_end_episode_min,
                   COALESCE(last_clinically_observed_episode_min,
                            w_end_episode_min))
      END AS event_or_censor_episode_min,
      CASE WHEN event_type = 0
            AND last_clinically_observed_episode_min < w_end_episode_min
           THEN 'left_observation' ELSE 'window_end' END AS censor_type
    FROM ev
    ORDER BY episode_id, phenotype_track, k
    """
    n = utils.write_duckdb_table(con, sql, p["comp7d"])
    print(f"  eicu_labels_competing_7d_v2: {n:,} rows")
    return n


def run_labels_pipeline(con):
    _build_observation_endpoints(con)
    run_labels_24h(con)
    run_labels_competing_7d(con)
