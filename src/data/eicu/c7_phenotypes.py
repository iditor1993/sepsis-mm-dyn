"""C7: eICU 三套可行性表型（v2.4.1 §2.2 C7，双层结构；A-5/R19 pending）。

三套 track（全部 feasibility_only = TRUE；表型规则表待 PI 签署，
P-strict 锁定选对函数 select_suspected_infection_pairs_locked_v1 pending，
冻结清单 A-5 / B-5）：

  P-explicit：显式 sepsis 诊断证据（diagnosis.diagnosisstring 或
    admission_dx 三列 ILIKE config.EICU_EXPLICIT_SEPSIS_PATTERNS）。
    t_I = 最早证据时间（episode 坐标）；SOFA 不适用（描述性）。
  P-clinical：感染诊断证据（config.EICU_INFECTION_DX_PATTERNS，同两表）
    + 回顾性 ΔSOFA ≥ 2 确认。窗口（episode 分钟）：
      baseline  [t_I-2880, t_I-1440]（(start, end] 半开，同 MIMIC 约定）
      qualifying [t_I-1440, t_I+2880]
    六组分（可行性版，与 MIMIC 经典规则同源；mimic/f4_sofa 计分函数复用）：
      呼吸  pivoted_bg pao2/fio2 最小 PF；通气修饰 = 窗内 treatment
            ILIKE '%ventilat%'（respiratory_care 未并入，pending）
      凝血  pivoted_lab platelets 最小
      肝    pivoted_lab bilirubin 最大
      神经  pivoted_gcs gcs 最小
      心血管 pivoted_vital COALESCE(ibp_mean, nibp_mean) 最小 MAP
            + 窗内 infusion_drug 血管活性药（正则六药）存在 → ≥2
            【剂量未解析，F5/R5 七环节审核 pending；可行性近似】
      肾    pivoted_lab creatinine 最大【尿量组分未用，可行性版 pending】
    总分规则（用户口径）：组分缺失不计 0、记缺失数；
      baseline 无 6/6 时 assumed_zero_by_phenotype_rule = 0 并打标；
      delta_sofa_phenotype = qualifying_total - baseline_value；
      delta ≥ 2 确认资格，t_sepsis = t_I。
    estimand 声明（§2.2 C7）：资格由 t_I 后窗口回顾性确认，仅用于
      phenotype ascertainment，不进任何 landmark 特征。
  P-strict：仅输出候选 pair 引用（eicu_infection_pairs），全部时间字段
    NULL，t_sepsis_rule 标记锁定选对函数 pending。

诊断时间语义（A-5 审计 pending）：
  diagnosis.diagnosisoffset      → 'observed_record_time_pending_audit'
  admission_dx.admitdxenteredoffset → 'assigned_admission_proxy'
  confidence：pending_audit / low_proxy（可行性分层，审计后锁定）。

实测登记（2026-07-30 冒烟）：
  显式 sepsis 命中：diagnosis 150,196 行 / 26,733 stay；
    admission_dx 23,136 行 / 23,136 stay。
  感染证据命中：diagnosis 381,145 行 / 49,145 stay。
  pivoted_bg.fio2 量纲 0.2–1.0（332,734/1,464,012 非空）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

# MIMIC 经典 SOFA 计分函数复用（§5.4；心血管为可行性近似，见 _cv_score_feas）
from mimic.f4_sofa import (_resp_score, _coag_score, _liver_score,  # noqa: E402
                           _cns_score, _renal_score)

# 血管活性药正则（六药；可行性存在性标记，剂量解析 pending F5/R5）
_VASO_REGEX = ("norepinephrine|epinephrine|dopamine|dobutamine|"
               "phenylephrine|vasopressin")

_SOFA_RULE_VERSION = "eicu_feasibility_sofa_v1"
_PHENOTYPE_RULE_VERSION = "feasibility_v1_pending_pi_signoff"

# 合同字段（结构冻结，§2.2 C7 第一层）
_CONTRACT_COLS = [
    "episode_id", "infection_evidence_time", "infection_evidence_type",
    "sofa_baseline_window_start", "sofa_baseline_window_end",
    "sofa_qualifying_window_start", "sofa_qualifying_window_end",
    "baseline_sofa_value", "baseline_sofa_source",
    "baseline_sofa_complete_observed", "baseline_assumed_zero",
    "qualifying_sofa", "delta_sofa_observed_complete",
    "delta_sofa_phenotype", "sofa_qualifying_time",
    "t_sepsis_offset_min", "t_sepsis_rule", "phenotype_track",
    "infection_pair_id", "diagnosis_time", "diagnosis_time_semantics",
    "diagnosis_time_confidence", "feasibility_only",
]
# 扩展审计字段（合同之后追加；非合同冻结层）
_EXTRA_COLS = ["evidence_source_table", "n_evidence_candidates",
               "baseline_sofa_component_count",
               "qualifying_sofa_component_count",
               "p_clinical_qualified", "sofa_rule_version",
               "phenotype_rule_version"]


def _paths():
    eps = config.OUTPUT_DIRS["episodes"]
    phe = config.OUTPUT_DIRS["phenotypes"]
    return {
        "final_map": str(eps / "eicu_episode_map_final.parquet")
        .replace("\\", "/"),
        "pairs": str(phe / "eicu_infection_pairs.parquet").replace("\\", "/"),
        "event": phe / "eicu_phenotype_event_v2.parquet",
        "tracks": phe / "eicu_phenotype_tracks_v2.parquet",
    }


# ----------------------------------------------------------------
# 诊断证据抽取（P-explicit / P-clinical 共用骨架）
# ----------------------------------------------------------------

def _evidence_sql(patterns: list, final_map_path: str) -> str:
    cond_dx = " OR ".join(f"diagnosisstring ILIKE '%{p}%'" for p in patterns)
    cond_adx = " OR ".join(
        f"{col} ILIKE '%{p}%'" for p in patterns
        for col in ("admitdxpath", "admitdxname", "admitdxtext"))
    return f"""
    WITH dx AS (
      SELECT patientunitstayid, diagnosisoffset AS evidence_offset,
        'diagnosis' AS evidence_source_table,
        'observed_record_time_pending_audit' AS diagnosis_time_semantics
      FROM main.diagnosis
      WHERE diagnosisoffset IS NOT NULL AND ({cond_dx})
      UNION ALL
      SELECT patientunitstayid, admitdxenteredoffset,
        'admission_dx',
        'assigned_admission_proxy'
      FROM main.admission_dx
      WHERE admitdxenteredoffset IS NOT NULL AND ({cond_adx})
    ),
    ev AS (
      SELECT dx.*, f.episode_id,
        ((-p.hospitaladmitoffset + dx.evidence_offset)
          - f.episode_start_hospital_min)::BIGINT AS evidence_episode_min
      FROM dx
      JOIN main.patient p ON p.patientunitstayid = dx.patientunitstayid
      JOIN read_parquet('{final_map_path}') f
        ON f.patientunitstayid = dx.patientunitstayid
    ),
    ranked AS (
      SELECT *,
        COUNT(*) OVER (PARTITION BY episode_id) AS n_evidence_candidates,
        ROW_NUMBER() OVER (PARTITION BY episode_id
                           ORDER BY evidence_episode_min,
                                    evidence_source_table) AS rn
      FROM ev
    )
    SELECT episode_id,
      evidence_episode_min AS t_I_episode_min,
      n_evidence_candidates,
      evidence_source_table,
      diagnosis_time_semantics
    FROM ranked WHERE rn = 1
    """


# ----------------------------------------------------------------
# P-clinical SOFA 窗口组分聚合
# ----------------------------------------------------------------

def _sofa_windows_sql(final_map_path: str) -> str:
    # 事件 episode 坐标换算（(w_start, w_end] 半开区间，同 MIMIC (t-24h, t]）
    coord = ("(-s.hospitaladmitoffset + {src}.{off})"
             " - s.episode_start_hospital_min")
    return f"""
    WITH win AS (
      SELECT episode_id, t_I_episode_min,
        'baseline' AS sofa_window,
        (t_I_episode_min - 2880)::BIGINT AS w_start,
        (t_I_episode_min - 1440)::BIGINT AS w_end
      FROM pclinical_ev
      UNION ALL
      SELECT episode_id, t_I_episode_min,
        'qualifying',
        (t_I_episode_min - 1440)::BIGINT,
        (t_I_episode_min + 2880)::BIGINT
      FROM pclinical_ev
    ),
    stays AS (
      SELECT f.episode_id, f.patientunitstayid,
        f.episode_start_hospital_min, p.hospitaladmitoffset
      FROM read_parquet('{final_map_path}') f
      JOIN main.patient p USING (patientunitstayid)
      JOIN pclinical_ev e ON e.episode_id = f.episode_id
    ),
    lab_agg AS (
      SELECT w.episode_id, w.sofa_window,
        MIN(CASE WHEN pl.platelets > 0 THEN pl.platelets END)
          AS platelets_min,
        MAX(pl.bilirubin) AS bilirubin_max,
        MAX(pl.creatinine) AS creatinine_max
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.pivoted_lab pl ON pl.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='pl', off='chartoffset')} > w.w_start
        AND {coord.format(src='pl', off='chartoffset')} <= w.w_end
      GROUP BY 1, 2
    ),
    bg_agg AS (
      SELECT w.episode_id, w.sofa_window,
        MIN(CASE WHEN bg.fio2 > 0 THEN bg.pao2 / bg.fio2 END) AS pf_min
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.pivoted_bg bg ON bg.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='bg', off='chartoffset')} > w.w_start
        AND {coord.format(src='bg', off='chartoffset')} <= w.w_end
      WHERE bg.pao2 IS NOT NULL AND bg.fio2 IS NOT NULL AND bg.fio2 > 0
      GROUP BY 1, 2
    ),
    gcs_agg AS (
      SELECT w.episode_id, w.sofa_window, MIN(g.gcs) AS gcs_min
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.pivoted_gcs g ON g.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='g', off='chartoffset')} > w.w_start
        AND {coord.format(src='g', off='chartoffset')} <= w.w_end
      GROUP BY 1, 2
    ),
    map_agg AS (
      SELECT w.episode_id, w.sofa_window,
        MIN(COALESCE(v.ibp_mean, v.nibp_mean)) AS map_min
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.pivoted_vital v ON v.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='v', off='chartoffset')} > w.w_start
        AND {coord.format(src='v', off='chartoffset')} <= w.w_end
      WHERE COALESCE(v.ibp_mean, v.nibp_mean) IS NOT NULL
      GROUP BY 1, 2
    ),
    vaso_agg AS (
      SELECT w.episode_id, w.sofa_window, 1 AS vaso_any
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.infusion_drug i ON i.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='i', off='infusionoffset')} > w.w_start
        AND {coord.format(src='i', off='infusionoffset')} <= w.w_end
      WHERE REGEXP_MATCHES(LOWER(i.drugname), '{_VASO_REGEX}')
      GROUP BY 1, 2
    ),
    vent_agg AS (
      SELECT w.episode_id, w.sofa_window, 1 AS vent_any
      FROM win w
      JOIN stays s ON s.episode_id = w.episode_id
      JOIN main.treatment t ON t.patientunitstayid = s.patientunitstayid
        AND {coord.format(src='t', off='treatmentoffset')} > w.w_start
        AND {coord.format(src='t', off='treatmentoffset')} <= w.w_end
      WHERE t.treatmentstring ILIKE '%ventilat%'
      GROUP BY 1, 2
    )
    SELECT w.episode_id, w.sofa_window, w.t_I_episode_min,
      w.w_start, w.w_end,
      b.pf_min, COALESCE(vt.vent_any, 0) AS vent_any,
      l.platelets_min, l.bilirubin_max, l.creatinine_max,
      g.gcs_min, v.map_min, COALESCE(va.vaso_any, 0) AS vaso_any
    FROM win w
    LEFT JOIN bg_agg b ON b.episode_id = w.episode_id
                      AND b.sofa_window = w.sofa_window
    LEFT JOIN vent_agg vt ON vt.episode_id = w.episode_id
                         AND vt.sofa_window = w.sofa_window
    LEFT JOIN lab_agg l ON l.episode_id = w.episode_id
                       AND l.sofa_window = w.sofa_window
    LEFT JOIN gcs_agg g ON g.episode_id = w.episode_id
                       AND g.sofa_window = w.sofa_window
    LEFT JOIN map_agg v ON v.episode_id = w.episode_id
                      AND v.sofa_window = w.sofa_window
    LEFT JOIN vaso_agg va ON va.episode_id = w.episode_id
                        AND va.sofa_window = w.sofa_window
    """


# ----------------------------------------------------------------
# 计分（可行性版；组分缺失不计 0、记缺失数）
# ----------------------------------------------------------------

def _cv_score_feas(map_min, vaso_any):
    """心血管可行性近似：MAP 规则 + 任意血管活性药存在 → ≥2。

    剂量未解析（F5/R5 七环节人工审核 pending）；不得与 MIMIC
    经典 CV-SOFA（剂量分层）混用。
    """
    if map_min is None and not vaso_any:
        return None
    scores = [0]
    if map_min is not None:
        scores.append(0 if map_min >= 70 else 1)
    if vaso_any:
        scores.append(2)
    return max(scores)


_COMP_ORDER = ["respiration", "coagulation", "liver",
               "cardiovascular", "cns", "renal"]


def _none_if_nan(v):
    """SQL NULL 经 fetchdf 后为 NaN；经典计分函数以 `is None` 判缺失，
    必须显式转换，否则 NaN 会被当作数值参与阈值比较。"""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def _score_window(row):
    resp_s, _ = _resp_score(_none_if_nan(row["pf_min"]),
                            bool(row["vent_any"]))
    coag_s, _ = _coag_score(_none_if_nan(row["platelets_min"]))
    liv_s, _ = _liver_score(_none_if_nan(row["bilirubin_max"]))
    cns_s, _ = _cns_score(_none_if_nan(row["gcs_min"]))
    cv_s = _cv_score_feas(_none_if_nan(row["map_min"]),
                          bool(row["vaso_any"]))
    # 肾：可行性版仅肌酐（尿量组分 pending）
    renal_s, _ = _renal_score(_none_if_nan(row["creatinine_max"]), None)
    scores = {"respiration": resp_s, "coagulation": coag_s,
              "liver": liv_s, "cardiovascular": cv_s,
              "cns": cns_s, "renal": renal_s}
    n_complete = sum(1 for c in _COMP_ORDER if scores[c] is not None)
    total = sum(scores[c] for c in _COMP_ORDER if scores[c] is not None)
    return total, n_complete


# ----------------------------------------------------------------
# P-clinical
# ----------------------------------------------------------------

def run_c7_pclinical(con) -> pd.DataFrame:
    p = _paths()
    utils.log_step("C7: P-clinical (infection dx + retrospective delta SOFA)")
    ev_sql = _evidence_sql(config.EICU_INFECTION_DX_PATTERNS, p["final_map"])
    con.execute(f"CREATE OR REPLACE TEMP VIEW pclinical_ev AS {ev_sql}")
    n_ev = con.execute("SELECT COUNT(*) FROM pclinical_ev").fetchone()[0]
    print(f"  P-clinical evidence episodes: {n_ev:,}")

    win_df = con.execute(_sofa_windows_sql(p["final_map"])).fetchdf()
    print(f"  sofa window rows: {len(win_df):,}")

    scored = []
    for r in win_df.itertuples(index=False):
        d = r._asdict()
        total, n_complete = _score_window(d)
        d["sofa_total"] = total
        d["sofa_component_count"] = n_complete
        scored.append(d)
    sdf = pd.DataFrame(scored)
    base = sdf[sdf["sofa_window"] == "baseline"].set_index("episode_id")
    qual = sdf[sdf["sofa_window"] == "qualifying"].set_index("episode_id")
    ev_df = con.execute("SELECT * FROM pclinical_ev").fetchdf() \
        .set_index("episode_id")

    rows = []
    for ep_id, e in ev_df.iterrows():
        b = base.loc[ep_id] if ep_id in base.index else None
        q = qual.loc[ep_id] if ep_id in qual.index else None
        b_total = int(b["sofa_total"]) if b is not None else 0
        b_count = int(b["sofa_component_count"]) if b is not None else 0
        q_total = int(q["sofa_total"]) if q is not None else 0
        q_count = int(q["sofa_component_count"]) if q is not None else 0
        b_complete = b_count == 6
        if b_complete:
            b_value, b_source = b_total, "observed_complete"
            b_assumed = False
        else:
            # 表型假设（非完整性事实；§2.2 C7 敏感性排除对象）
            b_value, b_source = 0, "assumed_zero_by_phenotype_rule"
            b_assumed = True
        q_complete = q_count == 6
        delta_phen = q_total - b_value
        delta_obs = (q_total - b_total) if (b_complete and q_complete) \
            else None
        qualified = bool(delta_phen >= 2)
        t_I = int(e["t_I_episode_min"])
        rows.append({
            "episode_id": ep_id,
            "infection_evidence_time": t_I,
            "infection_evidence_type": "infection_dx",
            "sofa_baseline_window_start": t_I - 2880,
            "sofa_baseline_window_end": t_I - 1440,
            "sofa_qualifying_window_start": t_I - 1440,
            "sofa_qualifying_window_end": t_I + 2880,
            "baseline_sofa_value": b_value,
            "baseline_sofa_source": b_source,
            "baseline_sofa_complete_observed": b_complete,
            "baseline_assumed_zero": b_assumed,
            "qualifying_sofa": q_total,
            "delta_sofa_observed_complete": delta_obs,
            "delta_sofa_phenotype": delta_phen,
            # 逐时点合格确认评估 pending（锁定规则）；可行性版以 t_I 代之
            "sofa_qualifying_time": t_I,
            "t_sepsis_offset_min": t_I if qualified else None,
            "t_sepsis_rule":
                "infection_evidence_time_with_qualifying_delta_sofa",
            "phenotype_track": "P-clinical",
            "infection_pair_id": None,
            "diagnosis_time": t_I,
            "diagnosis_time_semantics": e["diagnosis_time_semantics"],
            "diagnosis_time_confidence":
                "pending_audit"
                if e["evidence_source_table"] == "diagnosis"
                else "low_proxy",
            "feasibility_only": True,
            "evidence_source_table": e["evidence_source_table"],
            "n_evidence_candidates": int(e["n_evidence_candidates"]),
            "baseline_sofa_component_count": b_count,
            "qualifying_sofa_component_count": q_count,
            "p_clinical_qualified": qualified,
            "sofa_rule_version": _SOFA_RULE_VERSION,
            "phenotype_rule_version": _PHENOTYPE_RULE_VERSION,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=_CONTRACT_COLS + _EXTRA_COLS)
    print(f"  P-clinical evaluated: {len(df):,}; qualified: "
          f"{int(df['p_clinical_qualified'].sum()):,}; "
          f"baseline assumed_zero: {int(df['baseline_assumed_zero'].sum()):,}")
    return df


# ----------------------------------------------------------------
# P-explicit
# ----------------------------------------------------------------

def run_c7_pexplicit(con) -> pd.DataFrame:
    p = _paths()
    utils.log_step("C7: P-explicit (explicit sepsis dx evidence)")
    ev_sql = _evidence_sql(config.EICU_EXPLICIT_SEPSIS_PATTERNS,
                           p["final_map"])
    ev_df = con.execute(ev_sql).fetchdf()
    rows = []
    for e in ev_df.itertuples(index=False):
        t_I = int(e.t_I_episode_min)
        rows.append({
            "episode_id": e.episode_id,
            "infection_evidence_time": t_I,
            "infection_evidence_type": "explicit_sepsis_dx",
            "sofa_baseline_window_start": None,
            "sofa_baseline_window_end": None,
            "sofa_qualifying_window_start": None,
            "sofa_qualifying_window_end": None,
            # P-explicit 不计算 SOFA（描述性；§2.2 C7 不适用）
            "baseline_sofa_value": None,
            "baseline_sofa_source": "unavailable",
            "baseline_sofa_complete_observed": None,
            "baseline_assumed_zero": None,
            "qualifying_sofa": None,
            "delta_sofa_observed_complete": None,
            "delta_sofa_phenotype": None,
            "sofa_qualifying_time": None,
            "t_sepsis_offset_min": t_I,
            "t_sepsis_rule": "first_explicit_sepsis_dx_evidence_time",
            "phenotype_track": "P-explicit",
            "infection_pair_id": None,
            "diagnosis_time": t_I,
            "diagnosis_time_semantics": e.diagnosis_time_semantics,
            "diagnosis_time_confidence":
                "pending_audit"
                if e.evidence_source_table == "diagnosis"
                else "low_proxy",
            "feasibility_only": True,
            "evidence_source_table": e.evidence_source_table,
            "n_evidence_candidates": int(e.n_evidence_candidates),
            "baseline_sofa_component_count": None,
            "qualifying_sofa_component_count": None,
            "p_clinical_qualified": None,
            "sofa_rule_version": None,
            "phenotype_rule_version": _PHENOTYPE_RULE_VERSION,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=_CONTRACT_COLS + _EXTRA_COLS)
    print(f"  P-explicit episodes: {len(df):,}")
    return df


# ----------------------------------------------------------------
# P-strict（仅候选 pair 引用；锁定选对函数 pending）
# ----------------------------------------------------------------

def run_c7_pstrict(con) -> pd.DataFrame:
    p = _paths()
    utils.log_step("C7: P-strict (candidate pair references only)")
    pairs = con.execute(f"""
      SELECT DISTINCT episode_id, infection_pair_id
      FROM read_parquet('{p["pairs"]}')
    """).fetchdf()
    df = pd.DataFrame({
        "episode_id": pairs["episode_id"],
        "infection_evidence_time": None,
        "infection_evidence_type": "culture_antibiotic_pair_candidate",
        "sofa_baseline_window_start": None,
        "sofa_baseline_window_end": None,
        "sofa_qualifying_window_start": None,
        "sofa_qualifying_window_end": None,
        "baseline_sofa_value": None,
        "baseline_sofa_source": "unavailable",
        "baseline_sofa_complete_observed": None,
        "baseline_assumed_zero": None,
        "qualifying_sofa": None,
        "delta_sofa_observed_complete": None,
        "delta_sofa_phenotype": None,
        "sofa_qualifying_time": None,
        "t_sepsis_offset_min": None,
        "t_sepsis_rule": "pending_select_suspected_infection_pairs_locked_v1",
        "phenotype_track": "P-strict",
        "infection_pair_id": pairs["infection_pair_id"],
        "diagnosis_time": None,
        "diagnosis_time_semantics": None,
        "diagnosis_time_confidence": None,
        "feasibility_only": True,
        "evidence_source_table": None,
        "n_evidence_candidates": None,
        "baseline_sofa_component_count": None,
        "qualifying_sofa_component_count": None,
        "p_clinical_qualified": None,
        "sofa_rule_version": None,
        "phenotype_rule_version": _PHENOTYPE_RULE_VERSION,
    })
    print(f"  P-strict candidate pair rows: {len(df):,} "
          f"(episodes: {df['episode_id'].nunique():,})")
    return df


# ----------------------------------------------------------------
# 汇总输出
# ----------------------------------------------------------------

def run_c7(con):
    p = _paths()
    ev = pd.concat([run_c7_pexplicit(con), run_c7_pclinical(con),
                    run_c7_pstrict(con)], ignore_index=True)
    ev = ev[_CONTRACT_COLS + _EXTRA_COLS]
    utils.write_parquet(ev, p["event"])
    print(f"  eicu_phenotype_event_v2: {len(ev):,} rows")

    # tracks 成员表：P-strict/P-explicit 全部 episode；P-clinical 仅合格者
    tracks = pd.concat([
        ev[ev["phenotype_track"] == "P-explicit"][
            ["episode_id", "phenotype_track"]].drop_duplicates(),
        ev[(ev["phenotype_track"] == "P-clinical")
           & (ev["p_clinical_qualified"] == True)][
            ["episode_id", "phenotype_track"]].drop_duplicates(),
        ev[ev["phenotype_track"] == "P-strict"][
            ["episode_id", "phenotype_track"]].drop_duplicates(),
    ], ignore_index=True)
    tracks["member"] = True
    tracks["feasibility_only"] = True
    tracks["track_rule_version"] = _PHENOTYPE_RULE_VERSION
    tracks["membership_rule"] = tracks["phenotype_track"].map({
        "P-strict": "has_candidate_pair_pending_locked_selection",
        "P-clinical": "infection_dx_and_delta_sofa_phenotype_ge_2",
        "P-explicit": "has_explicit_sepsis_dx_evidence",
    })
    utils.write_parquet(tracks, p["tracks"])
    print(f"  eicu_phenotype_tracks_v2: {len(tracks):,} rows; "
          f"{tracks.groupby('phenotype_track')['episode_id'].nunique().to_dict()}")
    return len(ev)
