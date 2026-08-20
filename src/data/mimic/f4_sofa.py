"""F4: MIMIC SOFA components, realtime (v2.4.1 §5.4; R11/R15/R28/R34)。

purpose × evidence_track 二维：
  realtime_feature × strict_24h（主分析）
  realtime_feature × carryforward（敏感性；胆红素/肌酐/血小板 ≤48h，GCS/PF ≤24h）

- 不直接使用 mimiciv_derived.sofa 总分（R11）；
- 六组分从原始窗内输入按经典规则计算（心血管为修正阈值 + 最大分值）；
- 缺失组分不计 0；sofa_total_complete 仅 6/6；5/6 仅 partial。
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

_MAP_IDS = config.MIMIC_VITAL_ITEMIDS["mbp"]


def _prepare(con):
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    ep_path = str(config.OUTPUT_DIRS["episodes"]
                  / "mimic_icu_episode_map_final.parquet")
    mv = config.DEFAULT_MERGE_VERSION
    # SOFA 相关 labs（48h 覆盖 carryforward 轨）
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW sofa_lab_src AS
    WITH c AS (
      SELECT DISTINCT episode_id, hadm_id FROM read_parquet('{cohort_path}')
    ),
    bounds AS (
      SELECT c.hadm_id,
             MIN(l.t_landmark_ts) - INTERVAL '49 hours' AS t_lo,
             MAX(l.t_landmark_ts) AS t_hi
      FROM c JOIN read_parquet('{landmarks_path}') l
        ON l.episode_id = c.episode_id
      GROUP BY c.hadm_id
    )
    SELECT le.hadm_id, le.charttime AS event_time, le.storetime,
      CASE WHEN le.itemid IN (51265) THEN 'platelets'
           WHEN le.itemid IN (50885) THEN 'bilirubin'
           WHEN le.itemid IN (50912) THEN 'creatinine' END AS lab_name,
      le.valuenum
    FROM main.labevents le
    JOIN bounds b ON le.hadm_id = b.hadm_id
    WHERE le.itemid IN (51265, 50885, 50912)
      AND le.valuenum IS NOT NULL AND le.valuenum > 0
      AND le.charttime >= b.t_lo AND le.charttime <= b.t_hi
    """)
    # MAP（chartevents；strict 用 storetime）
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW sofa_map_src AS
    WITH c AS (
      SELECT DISTINCT episode_id, hadm_id FROM read_parquet('{cohort_path}')
    ),
    bounds AS (
      SELECT c.hadm_id,
             MIN(l.t_landmark_ts) - INTERVAL '25 hours' AS t_lo,
             MAX(l.t_landmark_ts) AS t_hi
      FROM c JOIN read_parquet('{landmarks_path}') l
        ON l.episode_id = c.episode_id
      GROUP BY c.hadm_id
    )
    SELECT ce.hadm_id, ce.charttime AS event_time, ce.storetime, ce.valuenum
    FROM main.chartevents ce
    JOIN bounds b ON ce.hadm_id = b.hadm_id
    WHERE ce.itemid IN {_MAP_IDS}
      AND ce.valuenum IS NOT NULL AND ce.valuenum > 0
      AND ce.charttime >= b.t_lo AND ce.charttime <= b.t_hi
    """)
    # episode stays 视图
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW sofa_stays AS
    SELECT episode_id, stay_id FROM read_parquet('{ep_path}')
    WHERE episode_mapping_version = '{mv}'
    """)


def run_f4_sofa(con):
    """逐组分窗口聚合 → 经典规则计分 → 两轨输出。"""
    out = config.OUTPUT_DIRS["features"]
    cohort_path = str(config.OUTPUT_DIRS["cohorts"] / "cohort_mimic_v2.parquet")
    landmarks_path = str(config.OUTPUT_DIRS["landmarks"] / "landmarks_v2.parquet")
    pf_path = str(out / "pf_ratio_v2.parquet")
    utils.log_step("F4: sofa realtime components")

    sql = f"""
    WITH lm AS (
      SELECT l.episode_id, l.k, l.t_landmark_ts, c.hadm_id
      FROM read_parquet('{landmarks_path}') l
      JOIN read_parquet('{cohort_path}') c ON l.episode_id = c.episode_id
    ),
    -- 呼吸：最近合格 PF（pf_ratio_v2 strict）+ 24h 通气状态
    resp AS (
      SELECT lm.episode_id, lm.k,
        p.pf_ratio, p.pf_available_time
      FROM lm
      JOIN read_parquet('{pf_path}') p
        ON p.episode_id = lm.episode_id AND p.k = lm.k
       AND p.pf_strict_eligible
    ),
    vent AS (
      SELECT lm.episode_id, lm.k,
        MAX(CASE WHEN v.ventilation_status IN ('InvasiveVent','Tracheostomy')
                 THEN 1 ELSE 0 END) AS invasive_24h
      FROM lm
      JOIN sofa_stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.ventilation v ON v.stay_id = s.stay_id
      WHERE v.starttime <= lm.t_landmark_ts
        AND v.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
      GROUP BY lm.episode_id, lm.k
    ),
    labs AS (
      SELECT lm.episode_id, lm.k,
        MIN(CASE WHEN s.lab_name = 'platelets'
                  AND EPOCH(lm.t_landmark_ts - s.event_time)/3600.0 <= 24
                 THEN s.valuenum END) AS platelets_min_24h,
        MIN(CASE WHEN s.lab_name = 'platelets' THEN s.valuenum END)
          AS platelets_min_48h,
        MAX(CASE WHEN s.lab_name = 'bilirubin'
                  AND EPOCH(lm.t_landmark_ts - s.event_time)/3600.0 <= 24
                 THEN s.valuenum END) AS bilirubin_max_24h,
        MAX(CASE WHEN s.lab_name = 'bilirubin' THEN s.valuenum END)
          AS bilirubin_max_48h,
        MAX(CASE WHEN s.lab_name = 'creatinine'
                  AND EPOCH(lm.t_landmark_ts - s.event_time)/3600.0 <= 24
                 THEN s.valuenum END) AS creatinine_max_24h,
        MAX(CASE WHEN s.lab_name = 'creatinine' THEN s.valuenum END)
          AS creatinine_max_48h
      FROM lm JOIN sofa_lab_src s ON s.hadm_id = lm.hadm_id
      WHERE s.event_time <= lm.t_landmark_ts
        AND s.event_time > lm.t_landmark_ts - INTERVAL '48 hours'
        AND COALESCE(s.storetime, s.event_time) <= lm.t_landmark_ts
      GROUP BY lm.episode_id, lm.k
    ),
    map_agg AS (
      SELECT lm.episode_id, lm.k, MIN(m.valuenum) AS map_min_24h
      FROM lm JOIN sofa_map_src m ON m.hadm_id = lm.hadm_id
      WHERE m.event_time <= lm.t_landmark_ts
        AND m.event_time > lm.t_landmark_ts - INTERVAL '24 hours'
        AND COALESCE(m.storetime, m.event_time) <= lm.t_landmark_ts
      GROUP BY lm.episode_id, lm.k
    ),
    vaso AS (
      SELECT lm.episode_id, lm.k,
        MAX(va.dopamine) AS dopamine_max,
        MAX(va.dobutamine) AS dobutamine_max,
        MAX(va.epinephrine) AS epinephrine_max,
        MAX(va.norepinephrine) AS norepinephrine_max
      FROM lm
      JOIN sofa_stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.vasoactive_agent va ON va.stay_id = s.stay_id
      WHERE va.starttime <= lm.t_landmark_ts
        AND va.endtime > lm.t_landmark_ts - INTERVAL '24 hours'
      GROUP BY lm.episode_id, lm.k
    ),
    gcs AS (
      SELECT lm.episode_id, lm.k, MIN(g.gcs) AS gcs_min_24h
      FROM lm
      JOIN sofa_stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.gcs g ON g.stay_id = s.stay_id
      WHERE g.charttime <= lm.t_landmark_ts
        AND g.charttime > lm.t_landmark_ts - INTERVAL '24 hours'
        AND COALESCE(g.gcs_unable, 0) = 0
      GROUP BY lm.episode_id, lm.k
    ),
    uo AS (
      SELECT lm.episode_id, lm.k, SUM(u.urineoutput) AS urine_24h_ml
      FROM lm
      JOIN sofa_stays s ON s.episode_id = lm.episode_id
      JOIN mimiciv_derived.urine_output u ON u.stay_id = s.stay_id
      WHERE u.charttime <= lm.t_landmark_ts
        AND u.charttime > lm.t_landmark_ts - INTERVAL '24 hours'
      GROUP BY lm.episode_id, lm.k
    ),
    assembled AS (
      SELECT lm.episode_id, lm.k, lm.t_landmark_ts,
        r.pf_ratio, COALESCE(v.invasive_24h, 0) AS invasive_24h,
        lb.platelets_min_24h, lb.platelets_min_48h,
        lb.bilirubin_max_24h, lb.bilirubin_max_48h,
        lb.creatinine_max_24h, lb.creatinine_max_48h,
        m.map_min_24h,
        va.dopamine_max, va.dobutamine_max,
        va.epinephrine_max, va.norepinephrine_max,
        g.gcs_min_24h, u.urine_24h_ml
      FROM lm
      LEFT JOIN resp r ON r.episode_id = lm.episode_id AND r.k = lm.k
      LEFT JOIN vent v ON v.episode_id = lm.episode_id AND v.k = lm.k
      LEFT JOIN labs lb ON lb.episode_id = lm.episode_id AND lb.k = lm.k
      LEFT JOIN map_agg m ON m.episode_id = lm.episode_id AND m.k = lm.k
      LEFT JOIN vaso va ON va.episode_id = lm.episode_id AND va.k = lm.k
      LEFT JOIN gcs g ON g.episode_id = lm.episode_id AND g.k = lm.k
      LEFT JOIN uo u ON u.episode_id = lm.episode_id AND u.k = lm.k
    )
    SELECT * FROM assembled ORDER BY episode_id, k
    """
    df = con.execute(sql).fetchdf()
    print(f"  assembled component inputs: {len(df):,} rows")
    _score_and_write(df, out)
    return len(df)


def _cv_score(row):
    """经典心血管规则（修正阈值 + 最大分值）。"""
    map_min = row["map_min_24h"]
    dopa = row["dopamine_max"] or 0.0
    dobu = row["dobutamine_max"] or 0.0
    epi = row["epinephrine_max"] or 0.0
    ne = row["norepinephrine_max"] or 0.0
    any_vaso = (dopa > 0) or (dobu > 0) or (epi > 0) or (ne > 0)
    if map_min is None and not any_vaso:
        return None, "map_and_vaso_missing"
    scores = [0]
    if map_min is not None:
        scores.append(0 if map_min >= 70 else 1)
    if dopa > 0 and dopa <= 5:
        scores.append(2)
    if dobu > 0:
        scores.append(2)
    if (dopa > 5 and dopa <= 15) or (epi > 0 and epi <= 0.1) \
            or (ne > 0 and ne <= 0.1):
        scores.append(3)
    if dopa > 15 or epi > 0.1 or ne > 0.1:
        scores.append(4)
    if map_min is None and any_vaso and max(scores) < 2:
        # 无 MAP 但有血管活性药（低于 2 分阈值）→ 至少 2 分不适用，保持药量分
        pass
    return max(scores), None


def _resp_score(pf, vent):
    if pf is None:
        return None, "pf_pair_missing"
    if pf >= 400:
        return 0, None
    if pf >= 300:
        return 1, None
    if pf >= 200:
        return 2, None
    if pf >= 100:
        return (3, None) if vent else (2, None)
    return (4, None) if vent else (2, None)


def _coag_score(plt):
    if plt is None:
        return None, "platelets_missing"
    if plt >= 150:
        return 0, None
    if plt >= 100:
        return 1, None
    if plt >= 50:
        return 2, None
    if plt >= 20:
        return 3, None
    return 4, None


def _liver_score(bili):
    if bili is None:
        return None, "bilirubin_missing"
    if bili < 1.2:
        return 0, None
    if bili < 2.0:
        return 1, None
    if bili < 6.0:
        return 2, None
    if bili < 12.0:
        return 3, None
    return 4, None


def _cns_score(gcs):
    if gcs is None:
        return None, "gcs_unable_or_missing"
    if gcs >= 15:
        return 0, None
    if gcs >= 13:
        return 1, None
    if gcs >= 10:
        return 2, None
    if gcs >= 6:
        return 3, None
    return 4, None


def _renal_score(creat, uo):
    if creat is None and uo is None:
        return None, "creatinine_and_urine_missing"
    scores = [0]
    if creat is not None:
        if creat < 1.2:
            scores.append(0)
        elif creat < 2.0:
            scores.append(1)
        elif creat < 3.5:
            scores.append(2)
        elif creat < 5.0:
            scores.append(3)
        else:
            scores.append(4)
    if uo is not None:
        if uo < 200:
            scores.append(4)
        elif uo < 500:
            scores.append(3)
    return max(scores), None


def _score_track(df, track):
    """track ∈ {'strict_24h', 'carryforward'} → 计分 DataFrame。"""
    import pandas as _pd

    def _v(x):
        """NaN → None（缺失语义），其余原样。"""
        if x is None:
            return None
        try:
            return None if _pd.isna(x) else x
        except (TypeError, ValueError):
            return x

    rows = []
    for r in df.itertuples(index=False):
        if track == "strict_24h":
            plt = _v(r.platelets_min_24h)
            bili = _v(r.bilirubin_max_24h)
            creat = _v(r.creatinine_max_24h)
            win_labs = 24
        else:
            plt = _v(r.platelets_min_48h)
            bili = _v(r.bilirubin_max_48h)
            creat = _v(r.creatinine_max_48h)
            win_labs = 48
        resp_s, resp_m = _resp_score(_v(r.pf_ratio), bool(r.invasive_24h))
        coag_s, coag_m = _coag_score(plt)
        liv_s, liv_m = _liver_score(bili)
        cns_s, cns_m = _cns_score(_v(r.gcs_min_24h))
        renal_s, renal_m = _renal_score(creat, _v(r.urine_24h_ml))
        cv_s, cv_m = _cv_score({
            "map_min_24h": _v(r.map_min_24h),
            "dopamine_max": _v(r.dopamine_max),
            "dobutamine_max": _v(r.dobutamine_max),
            "epinephrine_max": _v(r.epinephrine_max),
            "norepinephrine_max": _v(r.norepinephrine_max)})
        scores = {"respiration": resp_s, "coagulation": coag_s,
                  "liver": liv_s, "cardiovascular": cv_s,
                  "cns": cns_s, "renal": renal_s}
        miss = {"respiration": resp_m, "coagulation": coag_m,
                "liver": liv_m, "cardiovascular": cv_m,
                "cns": cns_m, "renal": renal_m}
        comp_order = ["respiration", "coagulation", "liver",
                      "cardiovascular", "cns", "renal"]
        n_complete = sum(1 for c in comp_order if scores[c] is not None)
        mask = "".join("0" if scores[c] is not None else "1"
                       for c in comp_order)
        total = sum(scores[c] for c in comp_order) if n_complete == 6 else None
        if n_complete == 6:
            status = "complete_6_of_6"
        elif n_complete == 5:
            status = "partial_5_of_6"
        else:
            status = f"partial_{n_complete}_of_6"
        rows.append({
            "episode_id": r.episode_id, "k": r.k,
            "t_landmark_ts": r.t_landmark_ts,
            "sofa_purpose": "realtime_feature",
            "sofa_evidence_track": track,
            "respiration_value": r.pf_ratio, "respiration_score": resp_s,
            "respiration_observed": resp_s is not None,
            "respiration_window_h": 24,
            "respiration_missing_reason": resp_m,
            "coagulation_value": plt, "coagulation_score": coag_s,
            "coagulation_observed": coag_s is not None,
            "coagulation_window_h": win_labs,
            "coagulation_missing_reason": coag_m,
            "liver_value": bili, "liver_score": liv_s,
            "liver_observed": liv_s is not None,
            "liver_window_h": win_labs,
            "liver_missing_reason": liv_m,
            "cardiovascular_value": r.map_min_24h,
            "cardiovascular_score": cv_s,
            "cardiovascular_observed": cv_s is not None,
            "cardiovascular_window_h": 24,
            "cardiovascular_missing_reason": cv_m,
            "dopamine_max_24h": r.dopamine_max,
            "dobutamine_max_24h": r.dobutamine_max,
            "epinephrine_max_24h": r.epinephrine_max,
            "norepinephrine_max_24h": r.norepinephrine_max,
            "cns_value": r.gcs_min_24h, "cns_score": cns_s,
            "cns_observed": cns_s is not None,
            "cns_window_h": 24,
            "cns_missing_reason": cns_m,
            "renal_value": creat, "renal_score": renal_s,
            "renal_observed": renal_s is not None,
            "renal_window_h": win_labs,
            "renal_missing_reason": renal_m,
            "urine_24h_ml": r.urine_24h_ml,
            "sofa_component_count": n_complete,
            "sofa_missing_component_mask": mask,
            "sofa_total_complete": total,
            "sofa_total_status": status,
            "sofa_cv_original": cv_s,
            "sofa_rule_version": "classic_sofa_v1_corrected_cv",
        })
    import pandas as pd
    return pd.DataFrame.from_records(rows)


def _score_and_write(df, out):
    import pandas as pd
    strict = _score_track(df, "strict_24h")
    carry = _score_track(df, "carryforward")
    all_df = pd.concat([strict, carry], ignore_index=True)
    utils.write_parquet(all_df, out / "sofa_hourly_v2.parquet")
    print(f"  sofa_hourly_v2: {len(all_df):,} rows "
          f"(strict {len(strict):,}, carryforward {len(carry):,})")
    comp = strict["sofa_total_status"].value_counts().to_dict()
    print(f"  strict track status: {comp}")


def run_f4_pipeline(con):
    _prepare(con)
    run_f4_sofa(con)
