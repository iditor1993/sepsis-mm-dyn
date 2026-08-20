"""eICU 特征提取（v2.4.1 §5.2/§5.3/§5.6/§5.7；charttime_fallback 语义）。

每 landmark 24h 窗（t_landmark_offset_min-1440, t_landmark_offset_min]，
(start, end] 半开）1h 分箱：
  bin_hour = LEAST(FLOOR((t_landmark - event)/60), 23)   -- bin0 = 最近一小时
  bin_end_offset_min   = t_landmark - bin_hour * 60
  bin_start_offset_min = t_landmark - (bin_hour + 1) * 60
  （事件满足 bin_start < event ≤ bin_end；bin_hour 语义与 MIMIC 一致，
    bin 边界公式按 bin0=最近 口径自洽——MIMIC 侧 bin_start/bin_end 公式
    与其 bin0=最近 docstring 不一致，差异已登记上报，待统一修订）

eICU 无 storetime/结果可用时间概念（lab revised offset 语义审计 C-2
pending），全部产物 source_time_type = 'charttime_fallback'
（§5.0：不得并入 strict 轨，声明 retrospective chart-time prediction）。

输出（长表 bin 聚合；按 cohort_eicu_v2 的 episode 过滤）：
  features/eicu_vitals_v2.parquet  pivoted_vital（有创血压优先于无创）
  features/eicu_labs_v2.parquet    pivoted_lab 长表化 + pivoted_bg(pao2,fio2)
  features/eicu_gcs_v2.parquet     pivoted_gcs
  features/eicu_urine_v2.parquet   pivoted_uo（另给 value_sum：区间流量）
  features/eicu_support_v2.parquet vent/vaso 窗内 0/1（每 landmark 一行）

实测登记（2026-07-30 冒烟）：
  treatment 通气命中（'%ventilat%' OR '%intubat%'）：413,568 行 / 71,195 stay
    （最高频串 'pulmonary|ventilation and oxygenation|mechanical ventilation'
     117,481 行）。
  infusion_drug 血管活性药（六药正则）：1,066,069 行 / 25,838 stay。
  pivoted_bg.fio2 量纲 0.2–1.0。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

_VASO_REGEX = ("norepinephrine|epinephrine|dopamine|dobutamine|"
               "phenylephrine|vasopressin")

# pivoted_vital 变量映射（有创 ibp 优先于无创 nibp，§5.2）
_VITAL_SELECT = """
  SELECT patientunitstayid, chartoffset,
    heartrate AS hr, "RespiratoryRate" AS rr, spo2,
    COALESCE(ibp_systolic, nibp_systolic) AS sbp,
    COALESCE(ibp_diastolic, nibp_diastolic) AS dbp,
    COALESCE(ibp_mean, nibp_mean) AS mbp,
    temperature AS temp
  FROM main.pivoted_vital
"""

# pivoted_lab 22 项（§5.3 项目清单 + 其余列全部保留长表化）
_LAB_COLS = ["albumin", "bilirubin", "BUN", "calcium", "chloride",
             "creatinine", "glucose", "bicarbonate", "TotalCO2",
             "hematocrit", "hemoglobin", "INR", "lactate", "platelets",
             "potassium", "ptt", "sodium", "wbc", "bands",
             "alt", "ast", "alp"]

_GCS_COLS = ["gcs", "gcsmotor", "gcsverbal", "gcseyes"]


def _paths():
    return {
        "cohort": str(config.OUTPUT_DIRS["cohorts"] / "cohort_eicu_v2.parquet")
        .replace("\\", "/"),
        "landmarks": str(config.OUTPUT_DIRS["landmarks"]
                         / "eicu_landmarks_v2.parquet").replace("\\", "/"),
        "final_map": str(config.OUTPUT_DIRS["episodes"]
                         / "eicu_episode_map_final.parquet").replace("\\", "/"),
        "out": config.OUTPUT_DIRS["features"],
    }


def _stays_cte(p):
    """cohort episode 的组成 stay（含坐标换算所需列）。"""
    return f"""
    stays AS (
      SELECT f.episode_id, f.patientunitstayid,
        f.episode_start_hospital_min, pt.hospitaladmitoffset
      FROM read_parquet('{p["final_map"]}') f
      JOIN main.patient pt ON pt.patientunitstayid = f.patientunitstayid
      JOIN (SELECT DISTINCT episode_id FROM read_parquet('{p["cohort"]}')) co
        ON co.episode_id = f.episode_id
    )
    """


def _lm_cte(p):
    return f"""
    lm AS (
      SELECT episode_id, phenotype_track, k, t_landmark_offset_min
      FROM read_parquet('{p["landmarks"]}')
    )
    """


def _win_agg_sql(p, unpivot_src, unpivot_cols, source_table_set,
                 extra_value_cols=""):
    """长表窗口聚合通用骨架：UNPIVOT → 窗口连接 → bin 聚合。"""
    cols = ", ".join(unpivot_cols)
    return f"""
    WITH {_lm_cte(p)},
    {_stays_cte(p)},
    src AS (
      SELECT patientunitstayid, chartoffset, variable, value
      FROM (UNPIVOT (
        {unpivot_src}
      ) ON {cols}
      INTO NAME variable VALUE value)
    ),
    win AS (
      SELECT lm.episode_id, lm.phenotype_track, lm.k,
        lm.t_landmark_offset_min,
        src.variable, src.value,
        ((-s.hospitaladmitoffset + src.chartoffset)
          - s.episode_start_hospital_min)::BIGINT AS event_episode_min
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN src ON src.patientunitstayid = s.patientunitstayid
      WHERE src.value IS NOT NULL
        AND ((-s.hospitaladmitoffset + src.chartoffset)
              - s.episode_start_hospital_min)
            <= lm.t_landmark_offset_min
        AND ((-s.hospitaladmitoffset + src.chartoffset)
              - s.episode_start_hospital_min)
            > lm.t_landmark_offset_min - 1440
    )
    SELECT episode_id, phenotype_track, k, t_landmark_offset_min,
      LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                  / 60.0)::INTEGER, 23) AS bin_hour,
      (t_landmark_offset_min
        - (LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                       / 60.0)::INTEGER, 23) + 1) * 60)::BIGINT
        AS bin_start_offset_min,
      (t_landmark_offset_min
        - LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                      / 60.0)::INTEGER, 23) * 60)::BIGINT
        AS bin_end_offset_min,
      variable,
      MEDIAN(value) AS value_median,
      {extra_value_cols}
      COUNT(*) AS n_source_records,
      MIN(event_episode_min) AS min_event_episode_min,
      MAX(event_episode_min) AS max_event_episode_min,
      'median' AS aggregation_method,
      '{source_table_set}' AS source_table_set,
      'charttime_fallback' AS source_time_type
    FROM win
    GROUP BY episode_id, phenotype_track, k, t_landmark_offset_min,
      LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                  / 60.0)::INTEGER, 23), variable
    """


def run_vitals(con):
    p = _paths()
    utils.log_step("Features: eicu_vitals_v2")
    sql = _win_agg_sql(p, _VITAL_SELECT,
                       ["hr", "rr", "spo2", "sbp", "dbp", "mbp", "temp"],
                       "pivoted_vital")
    n = utils.write_duckdb_table_direct(con, sql, p["out"]
                                        / "eicu_vitals_v2.parquet")
    print(f"  eicu_vitals_v2: {n:,} rows")
    return n


def run_labs(con):
    p = _paths()
    utils.log_step("Features: eicu_labs_v2 (pivoted_lab + pivoted_bg)")
    lab_cols = ", ".join(_LAB_COLS)
    # 两表列不同 → 分别长表化再 UNION
    lab_unpivot = f"""
      SELECT patientunitstayid, chartoffset, variable, value
      FROM (UNPIVOT (SELECT patientunitstayid, chartoffset, {lab_cols}
                     FROM main.pivoted_lab)
      ON {lab_cols}
      INTO NAME variable VALUE value)
    """
    bg_unpivot = """
      SELECT patientunitstayid, chartoffset, variable, value
      FROM (UNPIVOT (SELECT patientunitstayid, chartoffset, pao2, fio2
                     FROM main.pivoted_bg)
      ON pao2, fio2
      INTO NAME variable VALUE value)
    """
    combined_src = f"""
      SELECT * FROM ({lab_unpivot})
      UNION ALL
      SELECT * FROM ({bg_unpivot})
    """
    # 通用骨架以 src 子查询为输入；此处 src 已为长表，跳过 UNPIVOT
    sql = f"""
    WITH {_lm_cte(p)},
    {_stays_cte(p)},
    src AS ({combined_src}),
    win AS (
      SELECT lm.episode_id, lm.phenotype_track, lm.k,
        lm.t_landmark_offset_min,
        src.variable, src.value,
        ((-s.hospitaladmitoffset + src.chartoffset)
          - s.episode_start_hospital_min)::BIGINT AS event_episode_min
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN src ON src.patientunitstayid = s.patientunitstayid
      WHERE src.value IS NOT NULL
        AND ((-s.hospitaladmitoffset + src.chartoffset)
              - s.episode_start_hospital_min)
            <= lm.t_landmark_offset_min
        AND ((-s.hospitaladmitoffset + src.chartoffset)
              - s.episode_start_hospital_min)
            > lm.t_landmark_offset_min - 1440
    )
    SELECT episode_id, phenotype_track, k, t_landmark_offset_min,
      LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                  / 60.0)::INTEGER, 23) AS bin_hour,
      (t_landmark_offset_min
        - (LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                       / 60.0)::INTEGER, 23) + 1) * 60)::BIGINT
        AS bin_start_offset_min,
      (t_landmark_offset_min
        - LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                      / 60.0)::INTEGER, 23) * 60)::BIGINT
        AS bin_end_offset_min,
      variable,
      MEDIAN(value) AS value_median,
      COUNT(*) AS n_source_records,
      MIN(event_episode_min) AS min_event_episode_min,
      MAX(event_episode_min) AS max_event_episode_min,
      'median' AS aggregation_method,
      'pivoted_lab+pivoted_bg' AS source_table_set,
      'charttime_fallback' AS source_time_type
    FROM win
    GROUP BY episode_id, phenotype_track, k, t_landmark_offset_min,
      LEAST(FLOOR((t_landmark_offset_min - event_episode_min)
                  / 60.0)::INTEGER, 23), variable
    """
    n = utils.write_duckdb_table_direct(con, sql, p["out"]
                                        / "eicu_labs_v2.parquet")
    print(f"  eicu_labs_v2: {n:,} rows")
    return n


def run_gcs(con):
    p = _paths()
    utils.log_step("Features: eicu_gcs_v2")
    src = ("SELECT patientunitstayid, chartoffset, "
           + ", ".join(_GCS_COLS) + " FROM main.pivoted_gcs")
    sql = _win_agg_sql(p, src, _GCS_COLS, "pivoted_gcs")
    n = utils.write_duckdb_table_direct(con, sql, p["out"]
                                        / "eicu_gcs_v2.parquet")
    print(f"  eicu_gcs_v2: {n:,} rows")
    return n


def run_urine(con):
    p = _paths()
    utils.log_step("Features: eicu_urine_v2")
    src = ("SELECT patientunitstayid, chartoffset, urineoutput "
           "FROM main.pivoted_uo")
    # 尿量为区间流量：median（同构）之外另给 value_sum（§5.7）
    sql = _win_agg_sql(p, src, ["urineoutput"], "pivoted_uo",
                       extra_value_cols="SUM(value) AS value_sum,")
    n = utils.write_duckdb_table_direct(con, sql, p["out"]
                                        / "eicu_urine_v2.parquet")
    print(f"  eicu_urine_v2: {n:,} rows")
    return n


def run_support(con):
    """vent/vaso 窗内 0/1（每 landmark 一行）。"""
    p = _paths()
    utils.log_step("Features: eicu_support_v2")
    coord_t = ("(-s.hospitaladmitoffset + t.treatmentoffset)"
               " - s.episode_start_hospital_min")
    coord_i = ("(-s.hospitaladmitoffset + i.infusionoffset)"
               " - s.episode_start_hospital_min")
    sql = f"""
    WITH {_lm_cte(p)},
    {_stays_cte(p)},
    vent AS (
      SELECT lm.episode_id, lm.phenotype_track, lm.k,
        COUNT(*) AS n_vent_records,
        MIN({coord_t}) AS first_vent_episode_min,
        MAX({coord_t}) AS last_vent_episode_min
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN main.treatment t ON t.patientunitstayid = s.patientunitstayid
      WHERE (t.treatmentstring ILIKE '%ventilat%'
             OR t.treatmentstring ILIKE '%intubat%')
        AND {coord_t} <= lm.t_landmark_offset_min
        AND {coord_t} > lm.t_landmark_offset_min - 1440
      GROUP BY 1, 2, 3
    ),
    vaso AS (
      SELECT lm.episode_id, lm.phenotype_track, lm.k,
        COUNT(*) AS n_vaso_records,
        MIN({coord_i}) AS first_vaso_episode_min,
        MAX({coord_i}) AS last_vaso_episode_min
      FROM lm
      JOIN stays s ON s.episode_id = lm.episode_id
      JOIN main.infusion_drug i ON i.patientunitstayid = s.patientunitstayid
      WHERE REGEXP_MATCHES(LOWER(i.drugname), '{_VASO_REGEX}')
        AND {coord_i} <= lm.t_landmark_offset_min
        AND {coord_i} > lm.t_landmark_offset_min - 1440
      GROUP BY 1, 2, 3
    )
    SELECT lm.episode_id, lm.phenotype_track, lm.k,
      lm.t_landmark_offset_min,
      CASE WHEN v.episode_id IS NOT NULL THEN 1 ELSE 0 END AS vent_24h,
      COALESCE(v.n_vent_records, 0) AS n_vent_records,
      v.first_vent_episode_min, v.last_vent_episode_min,
      CASE WHEN va.episode_id IS NOT NULL THEN 1 ELSE 0 END AS vaso_24h,
      COALESCE(va.n_vaso_records, 0) AS n_vaso_records,
      va.first_vaso_episode_min, va.last_vaso_episode_min,
      'treatment+infusion_drug' AS source_table_set,
      'charttime_fallback' AS source_time_type
    FROM lm
    LEFT JOIN vent v ON v.episode_id = lm.episode_id
                    AND v.phenotype_track = lm.phenotype_track
                    AND v.k = lm.k
    LEFT JOIN vaso va ON va.episode_id = lm.episode_id
                     AND va.phenotype_track = lm.phenotype_track
                     AND va.k = lm.k
    """
    n = utils.write_duckdb_table(con, sql, p["out"]
                                 / "eicu_support_v2.parquet")
    print(f"  eicu_support_v2: {n:,} rows")
    return n


def run_features(con):
    run_vitals(con)
    run_labs(con)
    run_gcs(con)
    run_urine(con)
    run_support(con)
