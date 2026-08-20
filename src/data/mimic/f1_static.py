"""F1: MIMIC static features (v2.4.1 §5.1).

baseline_static_v2：每 episode 一行；charlson_prior NULL 口径（仅既往住院，
无既往 → NULL + available=FALSE + prior_hospital_count=0；R16）。
landmark_context_v2：每 episode × landmark 一行（landmark 前最近体重/身高、
Δ_ICU-sepsis、当前支持状态）。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_f1_static(con):
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    utils.log_step("F1: baseline_static_v2 (charlson_prior NULL 口径)")

    sql = f"""
    WITH c AS (SELECT * FROM read_parquet('{cohort_path}')),
    prior AS (
      SELECT c.episode_id,
        COUNT(pa.hadm_id) AS prior_hospital_count,
        ANY_VALUE(ch.charlson_comorbidity_index ORDER BY pa.admittime DESC)
          AS charlson_prior_latest
      FROM c
      LEFT JOIN main.admissions pa
        ON pa.subject_id = c.subject_id AND pa.admittime < c.admittime
      LEFT JOIN mimiciv_derived.charlson ch ON ch.hadm_id = pa.hadm_id
      GROUP BY c.episode_id
    )
    SELECT c.episode_id, c.subject_id, c.hadm_id,
      c.admission_age AS age, c.gender,
      c.admission_type, c.admission_location,
      c.first_careunit AS icu_type,
      p.charlson_prior_latest AS charlson_prior,
      CASE WHEN p.prior_hospital_count > 0
            AND p.charlson_prior_latest IS NOT NULL
           THEN TRUE ELSE FALSE END AS charlson_prior_available,
      COALESCE(p.prior_hospital_count, 0) AS prior_hospital_count,
      CASE WHEN p.prior_hospital_count > 0
           THEN 'prior_admissions_final_icd' ELSE NULL END
        AS prior_icd_observation_window,
      CASE WHEN c.edregtime IS NOT NULL THEN 'ED' ELSE 'direct' END
        AS admission_route,
      c.anchor_year_group,
      c.flag_transfer_from_outside, c.flag_ecmo_before_first_landmark,
      c.flag_solid_organ_transplant_90d, c.flag_dnr_cco_before_first_landmark
    FROM c JOIN prior p ON c.episode_id = p.episode_id
    ORDER BY c.episode_id
    """
    n = utils.write_duckdb_table(con, sql, out / "baseline_static_v2.parquet")
    print(f"  baseline_static_v2: {n:,} rows")
    return n


def run_f1_landmark_context(con, merge_version=None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    utils.log_step("F1: landmark_context_v2")

    sql = f"""
    WITH l AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id,
             c.t_sepsis_ts, c.episode_intime_ts
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    ),
    stays AS (
      SELECT episode_id, stay_id FROM read_parquet('{ep_path}')
      WHERE episode_mapping_version = '{mv}'
    ),
    weight_ctx AS (
      SELECT l.episode_id, l.k,
        ANY_VALUE(w.weight ORDER BY w.starttime DESC) AS weight_kg,
        ANY_VALUE(w.weight_type ORDER BY w.starttime DESC) AS weight_type,
        MAX(w.starttime) AS weight_time
      FROM l
      JOIN stays s ON l.episode_id = s.episode_id
      JOIN mimiciv_derived.weight_durations w ON w.stay_id = s.stay_id
      WHERE w.starttime <= l.t_landmark_ts
      GROUP BY l.episode_id, l.k
    ),
    height_ctx AS (
      SELECT l.episode_id, l.k,
        ANY_VALUE(h.height ORDER BY h.charttime DESC) AS height_cm
      FROM l
      JOIN stays s ON l.episode_id = s.episode_id
      JOIN mimiciv_derived.height h ON h.stay_id = s.stay_id
      WHERE h.charttime <= l.t_landmark_ts
      GROUP BY l.episode_id, l.k
    ),
    vent_now AS (
      SELECT l.episode_id, l.k,
        MAX(CASE WHEN v.ventilation_status IN ('InvasiveVent','Tracheostomy')
                 THEN 1 ELSE 0 END) AS invasive_vent_current
      FROM l
      JOIN stays s ON l.episode_id = s.episode_id
      JOIN mimiciv_derived.ventilation v ON v.stay_id = s.stay_id
      WHERE v.starttime <= l.t_landmark_ts AND v.endtime > l.t_landmark_ts
      GROUP BY l.episode_id, l.k
    ),
    vaso_now AS (
      SELECT l.episode_id, l.k,
        MAX(n.norepinephrine_equivalent_dose) AS nee_current
      FROM l
      JOIN stays s ON l.episode_id = s.episode_id
      JOIN mimiciv_derived.norepinephrine_equivalent_dose n
        ON n.stay_id = s.stay_id
      WHERE n.starttime <= l.t_landmark_ts AND n.endtime > l.t_landmark_ts
      GROUP BY l.episode_id, l.k
    )
    SELECT l.episode_id, l.k, l.t_landmark_ts,
      EPOCH(l.episode_intime_ts - l.t_sepsis_ts) / 3600.0 AS delta_icu_sepsis_h,
      w.weight_kg, w.weight_type, w.weight_time,
      CASE WHEN w.weight_kg IS NULL THEN TRUE ELSE FALSE END AS weight_missing,
      CASE WHEN w.weight_kg < 40 OR w.weight_kg > 150
           THEN TRUE ELSE FALSE END AS extreme_weight_flag,
      h.height_cm,
      COALESCE(vn.invasive_vent_current, 0) AS invasive_vent_current,
      vs.nee_current,
      CASE WHEN vs.nee_current IS NOT NULL AND vs.nee_current > 0
           THEN 1 ELSE 0 END AS vaso_current
    FROM l
    LEFT JOIN weight_ctx w ON l.episode_id = w.episode_id AND l.k = w.k
    LEFT JOIN height_ctx h ON l.episode_id = h.episode_id AND l.k = h.k
    LEFT JOIN vent_now vn ON l.episode_id = vn.episode_id AND l.k = vn.k
    LEFT JOIN vaso_now vs ON l.episode_id = vs.episode_id AND l.k = vs.k
    ORDER BY l.episode_id, l.k
    """
    n = utils.write_duckdb_table(con, sql, out / "landmark_context_v2.parquet")
    print(f"  landmark_context_v2: {n:,} rows")
    return n


def run_f1_pipeline(con, merge_version=None):
    run_f1_static(con)
    run_f1_landmark_context(con, merge_version)
