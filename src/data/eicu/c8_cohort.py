"""C8/C9/C10: eICU 入排初筛 + index episode + 队列事实表 cohort_eicu_v2
（v2.4.1 §2.2 C8/C9/C10）。

- 年龄：patient.age VARCHAR 数值化（'> 89' → 90 并打标 age_was_capped；
  空/不可解析 → NULL）；age_num ≥ 18 保留（config.LANDMARK_MIN_AGE）。
  实测（2026-07-30）：'> 89' 7,081 行；NULL 95 行；其余为整数字符串。
- 每个 phenotype_track 内按 uniquepid 取首个 episode，排序键：
  t_sepsis_offset_min NULLS LAST, hospitaladmitoffset,
  episode_start_hospital_min, episode_id（§2.2 C8）。
- 抗生素时间源 episode 级标志（§2.2 C9/C10）来自 c6b 事件表；
  administration_confirmation_availability = 'structurally_unavailable'
  ⇒ episode_has_administration_confirmed 恒 FALSE。
- 敏感性标志（候选，冻结清单外；PPV 抽查前不作正式排除）：
  flag_transfer_from_outside（首 stay unitadmitsource = 'Other Hospital'）、
  unresolved_conservative_split、episode_time_anomaly_flag。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def run_c8(con):
    out = config.OUTPUT_DIRS["cohorts"]
    eps = config.OUTPUT_DIRS["episodes"]
    phe = config.OUTPUT_DIRS["phenotypes"]
    final_map = str(eps / "eicu_episode_map_final.parquet").replace("\\", "/")
    timeline = str(eps / "eicu_unitstay_timeline.parquet").replace("\\", "/")
    tracks = str(phe / "eicu_phenotype_tracks_v2.parquet").replace("\\", "/")
    event = str(phe / "eicu_phenotype_event_v2.parquet").replace("\\", "/")
    abx = str(phe / "eicu_antibiotic_events.parquet").replace("\\", "/")
    utils.log_step("C8-C10: cohort_eicu_v2")

    sql = f"""
    WITH tr AS (
      SELECT episode_id, phenotype_track, feasibility_only
      FROM read_parquet('{tracks}') WHERE member
    ),
    pe AS (
      -- P-strict 每 episode 多行（每 pair 一行，t_sepsis 均 NULL）→ 聚合唯一
      SELECT episode_id, phenotype_track,
        MIN(t_sepsis_offset_min) AS t_sepsis_offset_min
      FROM read_parquet('{event}')
      GROUP BY 1, 2
    ),
    first_stay AS (
      SELECT f.episode_id,
        f.patientunitstayid AS index_patientunitstayid,
        f.patienthealthsystemstayid, f.uniquepid,
        f.episode_start_hospital_min,
        (f.episode_end_hospital_min - f.episode_start_hospital_min)::BIGINT
          AS episode_end_offset_min,
        f.episode_time_anomaly_flag,
        tl.hospitaladmitoffset,
        (tl.hospitaldischargeoffset - f.episode_start_hospital_min)::BIGINT
          AS hospital_discharge_episode_min,
        tl.hospitaldischargestatus, tl.hospitaldischargelocation,
        tl.age, tl.gender, tl.unittype, tl.hospitalid, tl.unitadmitsource
      FROM read_parquet('{final_map}') f
      JOIN read_parquet('{timeline}') tl
        ON tl.patientunitstayid = f.patientunitstayid
      WHERE f.stay_seq_in_episode = 1
    ),
    flags AS (
      SELECT episode_id,
        MAX(CASE WHEN unresolved_conservative_split THEN 1 ELSE 0 END)
          AS unresolved_conservative_split
      FROM read_parquet('{final_map}') GROUP BY 1
    ),
    abx_flags AS (
      SELECT episode_id,
        MAX(CASE WHEN source_level = 'infusion_recorded' THEN 1 ELSE 0 END)
          AS has_infusion,
        MAX(CASE WHEN source_level = 'scheduled_start' THEN 1 ELSE 0 END)
          AS has_sched,
        MAX(CASE WHEN source_level = 'order_time' THEN 1 ELSE 0 END)
          AS has_order
      FROM read_parquet('{abx}') GROUP BY 1
    ),
    base AS (
      SELECT tr.episode_id, tr.phenotype_track, tr.feasibility_only,
        pe.t_sepsis_offset_min,
        fs.index_patientunitstayid, fs.patienthealthsystemstayid,
        fs.uniquepid,
        fs.episode_start_hospital_min, fs.episode_end_offset_min,
        fs.episode_time_anomaly_flag,
        fs.hospitaladmitoffset, fs.hospital_discharge_episode_min,
        fs.hospitaldischargestatus, fs.hospitaldischargelocation,
        fs.age, fs.gender, fs.unittype, fs.hospitalid, fs.unitadmitsource,
        fl.unresolved_conservative_split,
        COALESCE(af.has_infusion, 0) AS has_infusion,
        COALESCE(af.has_sched, 0) AS has_sched,
        COALESCE(af.has_order, 0) AS has_order
      FROM tr
      JOIN pe ON pe.episode_id = tr.episode_id
             AND pe.phenotype_track = tr.phenotype_track
      JOIN first_stay fs ON fs.episode_id = tr.episode_id
      JOIN flags fl ON fl.episode_id = tr.episode_id
      LEFT JOIN abx_flags af ON af.episode_id = tr.episode_id
    ),
    aged AS (
      SELECT *,
        CASE WHEN age = '> 89' THEN 90
             WHEN REGEXP_MATCHES(age, '^[0-9]+$') THEN CAST(age AS INTEGER)
             ELSE NULL END AS age_num,
        (age = '> 89') AS age_was_capped
      FROM base
    ),
    ranked AS (
      SELECT *,
        ROW_NUMBER() OVER (
          PARTITION BY uniquepid, phenotype_track
          ORDER BY t_sepsis_offset_min NULLS LAST, hospitaladmitoffset,
                   episode_start_hospital_min, episode_id) AS rn
      FROM aged
      WHERE age_num >= {config.LANDMARK_MIN_AGE}
    )
    SELECT episode_id, index_patientunitstayid, patienthealthsystemstayid,
      uniquepid, t_sepsis_offset_min,
      0::BIGINT AS episode_start_offset_min,
      episode_end_offset_min,
      hospitaladmitoffset, hospital_discharge_episode_min,
      hospitaldischargestatus, hospitaldischargelocation,
      age_num, age_was_capped, gender, unittype, hospitalid,
      phenotype_track,
      -- 抗生素时间源 episode 级标志（C9/C10；structurally_unavailable）
      FALSE AS episode_has_administration_confirmed,
      (has_infusion = 1) AS episode_has_infusion_recorded,
      (has_infusion = 0 AND has_sched = 1)
        AS episode_has_scheduled_start_only,
      (has_infusion = 0 AND has_sched = 0 AND has_order = 1)
        AS episode_has_order_time_only,
      (has_infusion = 1) AS episode_has_reliable_antibiotic_time,
      CASE WHEN has_infusion = 1 THEN 'infusion_recorded'
           WHEN has_sched = 1 THEN 'scheduled_start'
           WHEN has_order = 1 THEN 'order_time'
           ELSE NULL END AS selected_antibiotic_time_source,
      -- 敏感性标志（候选，冻结外）
      (unitadmitsource = 'Other Hospital') AS flag_transfer_from_outside,
      (unresolved_conservative_split = 1) AS unresolved_conservative_split,
      episode_time_anomaly_flag,
      feasibility_only
    FROM ranked WHERE rn = 1
    ORDER BY phenotype_track, uniquepid
    """
    n = utils.write_duckdb_table(con, sql, out / "cohort_eicu_v2.parquet")
    dist = con.execute(f"""
      SELECT phenotype_track, COUNT(*) episodes,
             COUNT(DISTINCT uniquepid) patients,
             COUNT(DISTINCT hospitalid) hospitals
      FROM read_parquet('{str(out / "cohort_eicu_v2.parquet").replace(chr(92), "/")}')
      GROUP BY 1 ORDER BY 1
    """).fetchall()
    print(f"  cohort_eicu_v2: {n:,} index episodes")
    for row in dist:
        print(f"    {row[0]}: episodes={row[1]:,} patients={row[2]:,} "
              f"hospitals={row[3]:,}")
    return n
