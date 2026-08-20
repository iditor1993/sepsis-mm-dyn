"""C1-C5: MIMIC sepsis cohort construction (v2.4.1).

C1 sepsis 相关 episode 池 (mimic_episode_sepsis)
C2 入排初筛（年龄 ≥18、成人 ICU）
C3 index episode（每 subject 首次）
C4 探索性/敏感性标志（外院转入、landmark 前 ECMO、90 天实体器官移植、
   landmark 前 DNR/CCO；PPV 抽查前不作正式排除）
C5 队列事实表 cohort_mimic_v2

D0 未锁定：t_sepsis 操作口径 = suspected_infection_time（config.D0_*）。
"""
import sys
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

# C4 标志用的候选 itemid / ICD 清单（描述性标志，冻结清单外）
ECMO_CHART_ITEMIDS = (224660, 229270)          # 'ECMO', 'Flow (ECMO)'
CODE_STATUS_ITEMIDS = (223758, 228687, 229784)  # Code Status 记录
DNR_VALUE_PATTERNS = ("%do not resuscitate%", "%dnr%", "%dni%",
                      "%comfort measures%", "%comfort care%")
TRANSPLANT_ICD9_PREFIXES = ("V420", "V421", "V426", "V427", "V4283",
                            "V4284", "V4289", "V429")
TRANSPLANT_ICD10_PREFIXES = ("Z940", "Z941", "Z942", "Z943", "Z944",
                             "Z9482", "Z9483", "Z9489", "Z949")
OUTSIDE_TRANSFER_LOCS = ("TRANSFER FROM HOSPITAL",
                         "TRANSFER FROM SKILLED NURSING FACILITY")


def run_c1_episode_sepsis(con, merge_version=None):
    """C1: episode 级脓毒症聚合（每 episode 一行）。"""
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["episodes"]
    utils.log_step("C1: mimic_episode_sepsis")
    ep_path = str(out / "mimic_icu_episode_map_final.parquet")

    sql = f"""
    WITH sepsis AS (
      SELECT s.subject_id, i.hadm_id, s.stay_id,
             s.{config.D0_OPERATIONAL_T_SEPSIS_FIELD} AS t_sepsis,
             s.sofa_time, s.sofa_score
      FROM mimiciv_derived.sepsis3 s
      JOIN main.icustays i ON s.stay_id = i.stay_id
      WHERE s.sepsis3
    ),
    ep AS (
      SELECT DISTINCT episode_id, subject_id, hadm_id, stay_id
      FROM read_parquet('{ep_path}')
      WHERE episode_mapping_version = '{mv}'
    ),
    ep_ranked AS (
      SELECT e.episode_id, s.stay_id, s.t_sepsis, s.sofa_score,
        COUNT(*) OVER (PARTITION BY e.episode_id) AS qualifying_sepsis_count,
        ROW_NUMBER() OVER (
          PARTITION BY e.episode_id
          ORDER BY s.t_sepsis NULLS LAST, s.stay_id) AS rn
      FROM sepsis s
      JOIN ep e ON e.subject_id = s.subject_id
               AND e.hadm_id = s.hadm_id AND e.stay_id = s.stay_id
    )
    SELECT episode_id, qualifying_sepsis_count,
      t_sepsis AS t_sepsis_ts,
      stay_id AS t_sepsis_source_stay_id,
      'min_t_sepsis_within_episode' AS t_sepsis_selection_rule,
      CASE WHEN t_sepsis IS NULL THEN 'missing' ELSE 'ok' END AS t_sepsis_status,
      sofa_score AS t_sepsis_sofa
    FROM ep_ranked WHERE rn = 1
    """
    n = utils.write_duckdb_table(con, sql, out / "mimic_episode_sepsis.parquet")
    print(f"  mimic_episode_sepsis: {n:,} episodes")
    return n


def run_c2_c5_cohort(con, merge_version=None):
    """C2-C5: 入排初筛 + index episode + 敏感性标志 + 队列事实表。"""
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["cohorts"]
    eps_dir = config.OUTPUT_DIRS["episodes"]
    utils.log_step("C2-C5: cohort_mimic_v2")
    ep_path = str(eps_dir / "mimic_icu_episode_map_final.parquet")
    sepsis_path = str(eps_dir / "mimic_episode_sepsis.parquet")
    outside_locs = "','".join(config.ACUTE_TRANSFER_LOCS)

    sql = f"""
    WITH es AS (
      SELECT * FROM read_parquet('{sepsis_path}')
      WHERE t_sepsis_status = 'ok'
    ),
    ep AS (
      SELECT * FROM read_parquet('{ep_path}')
      WHERE episode_mapping_version = '{mv}'
    ),
    ep_first_stay AS (
      SELECT episode_id,
             ANY_VALUE(first_careunit) FILTER (rn = 1) AS episode_first_careunit,
             MIN(intime) AS episode_intime_chk
      FROM (
        SELECT e.episode_id, i.first_careunit, i.intime,
               ROW_NUMBER() OVER (PARTITION BY e.episode_id
                                  ORDER BY i.intime, i.stay_id) AS rn
        FROM ep e JOIN main.icustays i ON e.stay_id = i.stay_id
      ) GROUP BY episode_id
    ),
    eligible AS (
      SELECT es.episode_id, es.qualifying_sepsis_count, es.t_sepsis_ts,
             es.t_sepsis_source_stay_id, es.t_sepsis_selection_rule,
             es.t_sepsis_status, es.t_sepsis_sofa,
             em.subject_id, em.hadm_id,
             em.episode_intime_ts, em.episode_outtime_ts,
             em.episode_outtime_status,
             a.admittime, a.dischtime, a.deathtime,
             a.hospital_expire_flag, a.admission_type,
             a.admission_location, a.discharge_location,
             a.edregtime, a.edouttime,
             d.admission_age, d.gender, d.hospstay_seq,
             efs.episode_first_careunit,
             p.anchor_year_group
      FROM es
      JOIN (SELECT DISTINCT episode_id, subject_id, hadm_id,
                            episode_intime_ts, episode_outtime_ts,
                            episode_outtime_status
            FROM ep) em ON es.episode_id = em.episode_id
      JOIN main.admissions a ON em.hadm_id = a.hadm_id
      JOIN main.patients p ON em.subject_id = p.subject_id
      JOIN mimiciv_derived.icustay_detail d
        ON d.stay_id = es.t_sepsis_source_stay_id
      JOIN ep_first_stay efs ON efs.episode_id = es.episode_id
      WHERE d.admission_age >= {config.LANDMARK_MIN_AGE}
    ),
    ranked AS (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY subject_id
        ORDER BY t_sepsis_ts NULLS LAST, admittime,
                 episode_intime_ts, episode_id
      ) AS subject_episode_rn
      FROM eligible
    ),
    idx AS (SELECT * FROM ranked WHERE subject_episode_rn = 1),
    flags AS (
      SELECT
        i.episode_id,
        -- 首个有效 landmark 时点（k0 规则与技术文档 §4.4 一致）
        i.t_sepsis_ts
          + GREATEST(0, CEIL(EPOCH(i.episode_intime_ts - i.t_sepsis_ts) / 21600.0))
            * INTERVAL '6 hours' AS t_first_landmark_ts,
        CASE WHEN i.admission_location IN
                  ('{"','".join(OUTSIDE_TRANSFER_LOCS)}')
             THEN TRUE ELSE FALSE END AS flag_transfer_from_outside,
        CASE WHEN EXISTS (
               SELECT 1 FROM main.chartevents ce
               WHERE ce.hadm_id = i.hadm_id
                 AND ce.itemid IN {ECMO_CHART_ITEMIDS}
                 AND ce.charttime <= i.t_sepsis_ts
                   + GREATEST(0, CEIL(EPOCH(i.episode_intime_ts - i.t_sepsis_ts) / 21600.0))
                     * INTERVAL '6 hours')
             THEN TRUE ELSE FALSE END AS flag_ecmo_before_first_landmark,
        CASE WHEN EXISTS (
               SELECT 1 FROM main.diagnoses_icd dx
               JOIN main.admissions pa ON dx.hadm_id = pa.hadm_id
               WHERE pa.subject_id = i.subject_id
                 AND pa.admittime < i.admittime
                 AND pa.admittime >= i.admittime - INTERVAL '90 days'
                 AND (
                   (dx.icd_version = 9 AND (
                     dx.icd_code LIKE 'V420%' OR dx.icd_code LIKE 'V421%'
                     OR dx.icd_code LIKE 'V426%' OR dx.icd_code LIKE 'V427%'
                     OR dx.icd_code LIKE 'V4283%' OR dx.icd_code LIKE 'V4284%'
                     OR dx.icd_code LIKE 'V4289%' OR dx.icd_code LIKE 'V429%'))
                   OR (dx.icd_version = 10 AND (
                     dx.icd_code LIKE 'Z940%' OR dx.icd_code LIKE 'Z941%'
                     OR dx.icd_code LIKE 'Z942%' OR dx.icd_code LIKE 'Z943%'
                     OR dx.icd_code LIKE 'Z944%' OR dx.icd_code LIKE 'Z9482%'
                     OR dx.icd_code LIKE 'Z9483%' OR dx.icd_code LIKE 'Z9489%'
                     OR dx.icd_code LIKE 'Z949%'))))
             THEN TRUE ELSE FALSE END AS flag_solid_organ_transplant_90d,
        CASE WHEN EXISTS (
               SELECT 1 FROM main.chartevents ce
               WHERE ce.hadm_id = i.hadm_id
                 AND ce.itemid IN {CODE_STATUS_ITEMIDS}
                 AND REGEXP_MATCHES(LOWER(COALESCE(ce.value, '')),
                     'do not resuscitate|dnr|dni|dnar|comfort')
                 AND ce.charttime <= i.t_sepsis_ts
                   + GREATEST(0, CEIL(EPOCH(i.episode_intime_ts - i.t_sepsis_ts) / 21600.0))
                     * INTERVAL '6 hours')
             THEN TRUE ELSE FALSE END AS flag_dnr_cco_before_first_landmark
      FROM idx i
    )
    SELECT i.subject_id, i.hadm_id, i.episode_id,
      '{mv}' AS episode_mapping_version,
      i.t_sepsis_source_stay_id, i.t_sepsis_ts,
      i.episode_intime_ts, i.episode_outtime_ts, i.episode_outtime_status,
      i.admittime, i.dischtime, i.deathtime, i.hospital_expire_flag,
      i.admission_type, i.admission_location, i.discharge_location,
      i.edregtime, i.edouttime,
      i.admission_age, i.gender, i.anchor_year_group,
      i.episode_first_careunit AS first_careunit,
      i.hospstay_seq, i.qualifying_sepsis_count,
      i.t_sepsis_selection_rule, i.t_sepsis_sofa,
      TRUE AS is_index_episode,
      f.t_first_landmark_ts,
      f.flag_transfer_from_outside,
      f.flag_ecmo_before_first_landmark,
      f.flag_solid_organ_transplant_90d,
      f.flag_dnr_cco_before_first_landmark
    FROM idx i JOIN flags f ON i.episode_id = f.episode_id
    ORDER BY i.subject_id
    """
    n = utils.write_duckdb_table(con, sql, out / "cohort_mimic_v2.parquet")
    print(f"  cohort_mimic_v2: {n:,} index episodes")
    return n


def run_split_assignments(con):
    """split_assignments_v2（§2.4；患者级，anchor_year_group 固定映射）。"""
    out = config.OUTPUT_DIRS["splits"]
    utils.log_step("Splits: split_assignments_v2")
    mapping = ", ".join(
        f"('{grp}', '{st}')" for grp, st in config.SPLIT_MAP.items())
    sql = f"""
    WITH map(anchor_year_group, set_name) AS (VALUES {mapping})
    SELECT p.subject_id, p.anchor_year_group, m.set_name,
           'v1_anchor_year_group' AS split_version
    FROM main.patients p
    JOIN map m ON p.anchor_year_group = m.anchor_year_group
    ORDER BY p.subject_id
    """
    n = utils.write_duckdb_table(con, sql,
                                 out / "split_assignments_v2.parquet")
    print(f"  split_assignments_v2: {n:,} subjects")
    return n


def run_cohort_pipeline(con, merge_version=None):
    run_c1_episode_sepsis(con, merge_version)
    run_c2_c5_cohort(con, merge_version)
    run_split_assignments(con)
