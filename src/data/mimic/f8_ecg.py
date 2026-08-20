"""F8: MIMIC ECG index (v2.4.1 §5.8; 技术文档 §7)。

- 显式 OR 就诊归属（same_hospitalization / auditable_pre_admission_encounter /
  other_encounter / out_of_window）
- 四时间字段：ecg_acquisition_time、recording_duration_s（WFDB header）、
  ecg_available_time_assumed = 采集完成时间、ecg_selection_time
- 五层级 availability：found_raw → same_encounter → structurally_valid →
  pass_frozen_qc → selected_for_model
- 时效窗：主 24h；48h/72h 标志（敏感性）；多份取最近
"""
import sys
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


def _parse_wfdb_header(rec_path: Path):
    """Parse one WFDB header. Returns dict or None on failure."""
    try:
        import wfdb
        rec = wfdb.rdheader(str(rec_path))
        dur = rec.sig_len / rec.fs if rec.fs else None
        return {
            "fs": rec.fs, "n_sig": rec.n_sig, "sig_len": rec.sig_len,
            "recording_duration_s": dur,
            "sig_names": ",".join(rec.sig_name or []),
        }
    except Exception:
        return None


def run_f8_recording_durations(con, force=False):
    """为队列 subject 的 ECG 解析 WFDB header（断点续跑缓存）。"""
    out = config.OUTPUT_DIRS["ecg_index"]
    cache = out / "ecg_recording_duration.parquet"
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    utils.log_step("F8: ECG recording durations (WFDB headers)")

    cand = con.execute(f"""
      SELECT DISTINCT e.subject_id, e.study_id, e.path, e.ecg_time
      FROM main.ecg_records e
      JOIN (SELECT DISTINCT subject_id FROM read_parquet('{cohort_path}')) c
        ON e.subject_id = c.subject_id
    """).fetchdf()
    print(f"  candidate ECGs (cohort subjects): {len(cand):,}")

    done = pd.DataFrame(columns=["study_id"])
    if cache.exists() and not force:
        done = pd.read_parquet(cache)[["study_id"]]
    done_ids = set(done["study_id"].tolist())
    todo = cand[~cand["study_id"].isin(done_ids)]
    print(f"  to parse: {len(todo):,} (cached: {len(done_ids):,})")

    rows = []
    for i, r in enumerate(todo.itertuples(index=False)):
        rec_path = config.ECG_WFDB_ROOT / str(r.path)
        info = _parse_wfdb_header(rec_path)
        if info is None:
            rows.append({"study_id": r.study_id, "fs": None, "n_sig": None,
                         "sig_len": None, "recording_duration_s": None,
                         "sig_names": None, "header_ok": False})
        else:
            rows.append({"study_id": r.study_id, **info, "header_ok": True})
        if (i + 1) % 10000 == 0:
            print(f"    parsed {i + 1:,}/{len(todo):,}")
    new_df = pd.DataFrame.from_records(rows)
    if cache.exists() and not force and len(new_df):
        old = pd.read_parquet(cache)
        new_df = pd.concat([old, new_df], ignore_index=True)
    if len(new_df):
        utils.write_parquet(new_df, cache)
    ok = int(new_df["header_ok"].sum()) if len(new_df) else 0
    print(f"  ecg_recording_duration: {len(new_df):,} rows, header_ok={ok:,}")
    return len(new_df)


def run_f8_index(con):
    out = config.OUTPUT_DIRS["ecg_index"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    dur_path = str(out / "ecg_recording_duration.parquet")
    fresh = config.ECG_FRESHNESS_MAIN_H
    utils.log_step("F8: ecg_landmark_index_v2")

    sql = f"""
    WITH cand AS (
      SELECT c.episode_id, c.subject_id, c.hadm_id, c.admittime, c.dischtime,
        e.study_id, e.ecg_time AS ecg_acquisition_time, e.path AS ecg_path,
        d.recording_duration_s, d.header_ok, d.n_sig,
        -- 显式 OR 归属（§5.8）
        CASE
          WHEN e.ecg_time >= c.admittime
               AND e.ecg_time <= COALESCE(c.dischtime, e.ecg_time)
            THEN 'same_hospitalization'
          WHEN e.ecg_time < c.admittime
               -- E-2 四条件（2026-07-30 实测登记）：
               -- ① ED 关联（edregtime 非空且 ECG 不早于 ED 登记）
               AND c.edregtime IS NOT NULL
               AND e.ecg_time >= c.edregtime
               -- ② ECG 落在 ED 窗内（不晚于 max(admittime, edouttime)；
               --    实测 edouttime 常晚于 admittime，中位 -1.5h）
               AND e.ecg_time <= GREATEST(c.admittime, c.edouttime)
               -- ③ ED-入院间隔 ≤ 阈值（实测 p99=3.09h）
               AND ABS(EPOCH(c.admittime - c.edouttime) / 3600.0)
                   <= {config.ECG_ED_GAP_MAX_H}
               -- ④ 期间无其他 encounter + 入院前最大允许时长
               AND c.admittime - e.ecg_time
                   <= INTERVAL '{config.ECG_PRE_ADMISSION_MAX_DAYS} days'
               AND NOT EXISTS (
                 SELECT 1 FROM main.admissions a2
                 WHERE a2.subject_id = c.subject_id
                   AND a2.hadm_id <> c.hadm_id
                   AND e.ecg_time >= a2.admittime
                   AND e.ecg_time <= COALESCE(a2.dischtime, e.ecg_time))
            THEN 'auditable_pre_admission_encounter'
          WHEN EXISTS (
                 SELECT 1 FROM main.admissions a3
                 WHERE a3.subject_id = c.subject_id
                   AND a3.hadm_id <> c.hadm_id
                   AND e.ecg_time >= a3.admittime
                   AND e.ecg_time <= COALESCE(a3.dischtime, e.ecg_time))
            THEN 'other_encounter'
          ELSE 'out_of_window'
        END AS ecg_encounter_status
      FROM read_parquet('{cohort_path}') c
      JOIN main.ecg_records e ON e.subject_id = c.subject_id
      LEFT JOIN read_parquet('{dur_path}') d ON d.study_id = e.study_id
      WHERE e.ecg_time <= (
        SELECT MAX(t_landmark_ts) FROM read_parquet('{landmarks_path}'))
    ),
    elig AS (
      SELECT *,
        ecg_acquisition_time + COALESCE(
          recording_duration_s * INTERVAL '1 second', INTERVAL '0 second')
          AS ecg_available_time_assumed,
        (ecg_encounter_status IN ('same_hospitalization',
                                  'auditable_pre_admission_encounter'))
          AS ecg_same_encounter,
        (header_ok AND recording_duration_s >= {config.ECG_STRUCT_QC_MIN_DURATION_S}
         AND n_sig >= {config.ECG_STRUCT_QC_MIN_LEADS})
          AS ecg_structurally_valid
      FROM cand
    ),
    joined AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts,
        e.study_id, e.ecg_acquisition_time, e.recording_duration_s,
        e.ecg_available_time_assumed, e.ecg_encounter_status,
        e.ecg_path, e.ecg_same_encounter, e.ecg_structurally_valid,
        EPOCH(l.t_landmark_ts - e.ecg_available_time_assumed) / 3600.0
          AS hours_before_landmark,
        ROW_NUMBER() OVER (
          PARTITION BY l.episode_id, l.k
          ORDER BY e.ecg_available_time_assumed DESC) AS rn
      FROM read_parquet('{landmarks_path}') l
      JOIN elig e ON e.episode_id = l.episode_id
      WHERE e.ecg_same_encounter
        AND e.ecg_available_time_assumed <= l.t_landmark_ts
        AND e.ecg_available_time_assumed
            > l.t_landmark_ts - INTERVAL '72 hours'
    )
    SELECT episode_id, k, t_landmark_ts,
      study_id, ecg_acquisition_time, recording_duration_s,
      ecg_available_time_assumed,
      ecg_encounter_status,
      (ecg_encounter_status = 'auditable_pre_admission_encounter')
        AS pre_admission_ecg,
      TRUE AS ecg_found_raw,
      ecg_same_encounter,
      ecg_structurally_valid,
      ecg_structurally_valid AS ecg_pass_frozen_qc,   -- E-4 冻结前恒等
      (rn = 1 AND ecg_structurally_valid
       AND hours_before_landmark <= {fresh}) AS ecg_selected_for_model,
      (rn = 1 AND ecg_structurally_valid
       AND hours_before_landmark <= {fresh}) AS ecg_available,
      (ecg_structurally_valid AND hours_before_landmark <= 48) AS within_48h,
      (ecg_structurally_valid AND hours_before_landmark <= 72) AS within_72h,
      hours_before_landmark,
      ecg_path
    FROM joined
    WHERE rn = 1
    ORDER BY episode_id, k
    """
    n = utils.write_duckdb_table_direct(
        con, sql, out / "ecg_landmark_index_v2.parquet")
    print(f"  ecg_landmark_index_v2: {n:,} rows")

    # 患者级 ECG 描述队列（技术文档 §7.1C：t_sepsis ±24h ≥1 份）
    utils.log_step("F8: patient-level ECG describe cohort")
    sql_desc = f"""
    WITH c AS (SELECT * FROM read_parquet('{cohort_path}'))
    SELECT DISTINCT c.subject_id, c.episode_id
    FROM c
    JOIN main.ecg_records e ON e.subject_id = c.subject_id
    WHERE e.ecg_time >= c.t_sepsis_ts - INTERVAL '24 hours'
      AND e.ecg_time <= c.t_sepsis_ts + INTERVAL '24 hours'
    """
    m = utils.write_duckdb_table_direct(
        con, sql_desc, out / "ecg_patient_describe_v2.parquet")
    print(f"  ecg_patient_describe_v2: {m:,} rows")
    return n


def run_f8_pipeline(con, force_reparse=False):
    run_f8_recording_durations(con, force=force_reparse)
    run_f8_index(con)
