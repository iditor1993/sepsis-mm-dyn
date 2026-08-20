"""C6b: eICU 统一抗生素事件 + 培养事件 + suspected infection 候选配对
（v2.4.1 §2.2 C6b / A.5，评审 P1-5；R3/R18/R33）。

五步构建（同 v2.4）：
  ① infusion_drug → infusion_recorded（time = infusionoffset）
  ② medication    → scheduled_start（drugstartoffset 非空）/ order_time（否则）
                    drugordercancelled = TRUE 排除
  ③ 同 (patientunitstayid, 规范化药名) 相近时间去重
     （±config.EICU_ABX_DEDUP_WINDOW_MIN=240min 链式归簇；候选参数）
  ④ 来源互斥赋值：簇内优先级 infusion_recorded > scheduled_start > order_time，
     同级取时间最早
  ⑤ 输出 eicu_antibiotic_events（episode 坐标 + administration 双字段）

administration confirmation（P1-5）：eICU 无 MAR 级确认来源 →
  administration_confirmation_availability = 'structurally_unavailable'，
  administration_confirmed 一律 NULL；episode 级
  episode_has_administration_confirmed = FALSE（§2.2 C6b）。

配对（A.5 candidate generation template）：同一 final episode 内
  (ab_time - cx_time) ∈ [0, 4320] 或 (cx_time - ab_time) ∈ [0, 1440]
  （config.EICU_PAIR_*）；仅生成候选 pair，最终选对由
  select_suspected_infection_pairs_locked_v1 完成（pending，R33/B-5）。

实测登记（2026-07-30 冒烟）：
  - medication.drugstartoffset 全量非空（7,301,853/7,301,853）→
    'order_time' 级预计接近 0；drugordercancelled 为 BOOLEAN
    （True 205,273 / False 7,096,580），非 offset 列。
  - 抗生素 pattern（config.EICU_ANTIBIOTIC_PATTERNS，35 个）命中：
    infusion_drug 490 行 / 45 stay；medication 166,569 行 / 54,489 stay。
  - micro_lab 16,996 行 / 2,923 患者，culturetakenoffset 无 NULL。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

# 药名 ILIKE 条件（config.EICU_ANTIBIOTIC_PATTERNS，候选清单）
_ABX_COND = " OR ".join(
    f"drugname ILIKE '%{p}%'" for p in config.EICU_ANTIBIOTIC_PATTERNS)

_SOURCE_PRIORITY = {"infusion_recorded": 1, "scheduled_start": 2,
                    "order_time": 3}


def _paths():
    eps = config.OUTPUT_DIRS["episodes"]
    phe = config.OUTPUT_DIRS["phenotypes"]
    return {
        "timeline": str(eps / "eicu_unitstay_timeline.parquet")
        .replace("\\", "/"),
        "final_map": str(eps / "eicu_episode_map_final.parquet")
        .replace("\\", "/"),
        "abx": phe / "eicu_antibiotic_events.parquet",
        "cx": phe / "eicu_culture_events.parquet",
        "pairs": phe / "eicu_infection_pairs.parquet",
        "summary": phe / "eicu_antibiotic_time_source_summary.parquet",
    }


def run_c6b_antibiotics(con):
    """统一抗生素事件表（五步构建）。"""
    p = _paths()
    utils.log_step("C6b: eicu_antibiotic_events")
    sql = f"""
    WITH abx_raw AS (
      SELECT patientunitstayid, drugname,
        'infusion_recorded' AS source_level,
        1 AS source_priority,
        infusionoffset AS antibiotic_time_min
      FROM main.infusion_drug
      WHERE {_ABX_COND}
      UNION ALL
      SELECT patientunitstayid, drugname,
        CASE WHEN drugstartoffset IS NOT NULL THEN 'scheduled_start'
             ELSE 'order_time' END AS source_level,
        CASE WHEN drugstartoffset IS NOT NULL THEN 2 ELSE 3 END
          AS source_priority,
        COALESCE(drugstartoffset, drugorderoffset) AS antibiotic_time_min
      FROM main.medication
      WHERE NOT COALESCE(drugordercancelled, FALSE)
        AND ({_ABX_COND})
    ),
    norm AS (
      SELECT *, LOWER(TRIM(drugname)) AS drugname_norm
      FROM abx_raw
      WHERE antibiotic_time_min IS NOT NULL
    ),
    lagged AS (
      SELECT *,
        LAG(antibiotic_time_min) OVER (
          PARTITION BY patientunitstayid, drugname_norm
          ORDER BY antibiotic_time_min, source_priority) AS prev_time
      FROM norm
    ),
    islands AS (
      -- 链式归簇：与同（stay, 药名）前一事件间隔 > 去重窗则开新簇
      SELECT *,
        SUM(CASE WHEN prev_time IS NULL
                  OR antibiotic_time_min - prev_time
                     > {config.EICU_ABX_DEDUP_WINDOW_MIN}
                 THEN 1 ELSE 0 END) OVER (
          PARTITION BY patientunitstayid, drugname_norm
          ORDER BY antibiotic_time_min, source_priority) AS dedup_grp
      FROM lagged
    ),
    dedup AS (
      SELECT *,
        ROW_NUMBER() OVER (
          PARTITION BY patientunitstayid, drugname_norm, dedup_grp
          ORDER BY source_priority, antibiotic_time_min) AS keep_rn,
        COUNT(*) OVER (
          PARTITION BY patientunitstayid, drugname_norm, dedup_grp)
          AS n_merged_records
      FROM islands
    )
    SELECT d.patientunitstayid, f.episode_id,
      'EICUABX_' || SHA256(
        d.patientunitstayid::VARCHAR || '|' || d.drugname_norm || '|' ||
        d.antibiotic_time_min::VARCHAR || '|' || d.source_level)
        AS antibiotic_event_id,
      d.drugname AS drugname_raw,
      d.drugname_norm,
      d.source_level,
      d.antibiotic_time_min,
      ((-tl.hospitaladmitoffset + d.antibiotic_time_min)
        - f.episode_start_hospital_min)::BIGINT AS antibiotic_time_episode_min,
      d.n_merged_records,
      {config.EICU_ABX_DEDUP_WINDOW_MIN} AS dedup_window_min,
      -- P1-5 双字段：eICU 无 MAR 级确认来源（§2.2 C6b）
      'structurally_unavailable' AS administration_confirmation_availability,
      CAST(NULL AS BOOLEAN) AS administration_confirmed
    FROM dedup d
    JOIN main.patient tl ON tl.patientunitstayid = d.patientunitstayid
    JOIN read_parquet('{p["final_map"]}') f
      ON f.patientunitstayid = d.patientunitstayid
    WHERE d.keep_rn = 1
    """
    n = utils.write_duckdb_table(con, sql, p["abx"])
    dist = con.execute(f"""
      SELECT source_level, COUNT(*) FROM read_parquet(
        '{str(p["abx"]).replace(chr(92), "/")}') GROUP BY 1 ORDER BY 1
    """).fetchall()
    print(f"  eicu_antibiotic_events: {n:,} rows; source_level dist: {dist}")
    return n


def run_c6b_cultures(con):
    """培养事件表（micro_lab 全表 + episode 坐标）。"""
    p = _paths()
    utils.log_step("C6b: eicu_culture_events")
    sql = f"""
    SELECT m.patientunitstayid, f.episode_id,
      'EICUCX_' || m.microlabid::VARCHAR AS culture_event_id,
      m.culturetakenoffset AS culture_time_min,
      ((-tl.hospitaladmitoffset + m.culturetakenoffset)
        - f.episode_start_hospital_min)::BIGINT AS culture_time_episode_min,
      m.culturesite, m.organism, m.antibiotic, m.sensitivitylevel
    FROM main.micro_lab m
    JOIN main.patient tl ON tl.patientunitstayid = m.patientunitstayid
    JOIN read_parquet('{p["final_map"]}') f
      ON f.patientunitstayid = m.patientunitstayid
    """
    n = utils.write_duckdb_table(con, sql, p["cx"])
    print(f"  eicu_culture_events: {n:,} rows")
    return n


def run_c6b_infection_pairs(con):
    """suspected infection 候选配对（A.5 template；仅候选，非最终事件）。"""
    p = _paths()
    utils.log_step("C6b: eicu_infection_pairs (candidates only)")
    abx_path = str(p["abx"]).replace("\\", "/")
    cx_path = str(p["cx"]).replace("\\", "/")
    sql = f"""
    SELECT ab.episode_id,
      ab.antibiotic_event_id, cx.culture_event_id,
      ab.antibiotic_time_episode_min AS ab_time_episode_min,
      cx.culture_time_episode_min AS cx_time_episode_min,
      ab.antibiotic_event_id || '__' || cx.culture_event_id
        AS infection_pair_id,
      -- 候选 pair：最终选对函数 pending（R33 / 冻结清单 B-5）
      'candidate_only_pending_locked_selection' AS pair_selection_status,
      'select_suspected_infection_pairs_locked_v1'
        AS locked_selection_function
    FROM read_parquet('{abx_path}') ab
    JOIN read_parquet('{cx_path}') cx USING (episode_id)
    WHERE (ab.antibiotic_time_episode_min - cx.culture_time_episode_min)
            BETWEEN 0 AND {config.EICU_PAIR_AB_AFTER_CX_MAX_MIN}
       OR (cx.culture_time_episode_min - ab.antibiotic_time_episode_min)
            BETWEEN 0 AND {config.EICU_PAIR_CX_AFTER_AB_MAX_MIN}
    """
    n = utils.write_duckdb_table(con, sql, p["pairs"])
    n_ep = con.execute(f"""
      SELECT COUNT(DISTINCT episode_id) FROM read_parquet(
        '{str(p["pairs"]).replace(chr(92), "/")}')
    """).fetchone()[0]
    print(f"  eicu_infection_pairs: {n:,} candidate pairs "
          f"in {n_ep:,} episodes")
    return n


def run_c6b_time_source_summary(con):
    """episode 级 source_level 构成 + 每 hospitalid 覆盖率（30% 门槛）。

    覆盖率口径（structurally_unavailable 下的正式门槛，§2.2 C6b）：
      antibiotic_time_source_coverage_rate_episode_level
        = 有 ≥1 条 infusion_recorded 事件的 episode 数
          / 有 ≥1 条抗生素事件（任意来源）的 episode 数。
      validated administration 来源不存在，联合覆盖率退化为 infusion 覆盖。
    """
    p = _paths()
    utils.log_step("C6b: eicu_antibiotic_time_source_summary")
    abx_path = str(p["abx"]).replace("\\", "/")
    sql = f"""
    WITH ep_hosp AS (
      -- 每条抗生素事件唯一归属 (episode_id, 自身 stay) → 每 episode 一行
      SELECT ab.episode_id,
        MAX(tl.hospitalid) AS hospitalid,
        MAX(CASE WHEN ab.source_level = 'infusion_recorded'
                 THEN 1 ELSE 0 END) AS has_infusion_recorded,
        MAX(CASE WHEN ab.source_level = 'scheduled_start'
                 THEN 1 ELSE 0 END) AS has_scheduled_start,
        MAX(CASE WHEN ab.source_level = 'order_time'
                 THEN 1 ELSE 0 END) AS has_order_time,
        SUM(CASE WHEN ab.source_level = 'infusion_recorded'
                 THEN 1 ELSE 0 END) AS n_events_infusion_recorded,
        SUM(CASE WHEN ab.source_level = 'scheduled_start'
                 THEN 1 ELSE 0 END) AS n_events_scheduled_start,
        SUM(CASE WHEN ab.source_level = 'order_time'
                 THEN 1 ELSE 0 END) AS n_events_order_time
      FROM read_parquet('{abx_path}') ab
      JOIN read_parquet('{p["timeline"]}') tl
        ON tl.patientunitstayid = ab.patientunitstayid
      GROUP BY ab.episode_id
    ),
    per_hosp AS (
      SELECT hospitalid::VARCHAR AS hospitalid_grp,
        COUNT(*) AS n_episodes_with_antibiotic,
        SUM(has_infusion_recorded) AS n_episodes_infusion_recorded,
        SUM(CASE WHEN has_infusion_recorded = 0
                  AND has_scheduled_start = 1 THEN 1 ELSE 0 END)
          AS n_episodes_scheduled_start_only,
        SUM(CASE WHEN has_infusion_recorded = 0
                  AND has_scheduled_start = 0
                  AND has_order_time = 1 THEN 1 ELSE 0 END)
          AS n_episodes_order_time_only,
        SUM(n_events_infusion_recorded) AS n_events_infusion_recorded,
        SUM(n_events_scheduled_start) AS n_events_scheduled_start,
        SUM(n_events_order_time) AS n_events_order_time
      FROM ep_hosp GROUP BY hospitalid
    )
    SELECT * FROM per_hosp
    UNION ALL
    SELECT 'ALL',
      COUNT(*),
      SUM(has_infusion_recorded),
      SUM(CASE WHEN has_infusion_recorded = 0
                AND has_scheduled_start = 1 THEN 1 ELSE 0 END),
      SUM(CASE WHEN has_infusion_recorded = 0
                AND has_scheduled_start = 0
                AND has_order_time = 1 THEN 1 ELSE 0 END),
      SUM(n_events_infusion_recorded),
      SUM(n_events_scheduled_start),
      SUM(n_events_order_time)
    FROM ep_hosp
    """
    df = con.execute(sql).fetchdf()
    df["antibiotic_time_source_coverage_rate_episode_level"] = (
        df["n_episodes_infusion_recorded"]
        / df["n_episodes_with_antibiotic"].clip(lower=1))
    df["coverage_threshold"] = 0.30
    df["meets_threshold"] = (
        df["antibiotic_time_source_coverage_rate_episode_level"] >= 0.30)
    df["administration_confirmation_structurally_available"] = False
    df["coverage_denominator"] = "episodes_with_any_antibiotic_event"
    df["coverage_note"] = (
        "structurally_unavailable: Pr(infusion_recorded 或 validated "
        "administration) 退化为 infusion_recorded 覆盖（§2.2 C6b）")
    utils.write_parquet(df, p["summary"])
    allrow = df[df["hospitalid_grp"] == "ALL"].iloc[0]
    print(f"  eicu_antibiotic_time_source_summary: {len(df):,} rows; "
          f"ALL coverage = "
          f"{allrow['antibiotic_time_source_coverage_rate_episode_level']:.4f} "
          f"(threshold 0.30)")
    return len(df)


def run_c6b(con):
    run_c6b_antibiotics(con)
    run_c6b_cultures(con)
    run_c6b_infection_pairs(con)
    run_c6b_time_source_summary(con)
