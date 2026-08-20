"""C6a: eICU episode 四表 + 住院级统一时间坐标 + canonical 事件标识
（v2.4.1 §2.2 C6a / A.4，评审 P1-1 / 阻断项 2；R14/R26/R32）。

坐标换算（eICU 原始事件时间为相对各 unit stay 入科的分钟 offset）：
  hospital_offset_min = -hospitaladmitoffset + event_offset
  episode_offset_min  = hospital_offset_min - episode_start_hospital_min
  （episode_start = 同一 final episode 内最早 unit_start）

episode 合并主规则（τ_merge_eicu = 0 min，单版本；§2.2 C6a）：
  同一 patienthealthsystemstayid 内按 unit_start 排序的相邻 stay 边：
    gap < 0                    → edge_path_class='offset_overlap'，pending_review
    gap ≤ 0 且 icu_to_icu      → merged
    其余                       → split（保守）
  final decision 显式 CASE 与 MIMIC C0 一致：pending_review 未裁决 →
  split + unresolved_conservative_split = TRUE；裁决范围仅 pending 边。

canonical 事件标识（阻断项 2，A.4；canonical JSON + SHA-256）：
  raw_row_fingerprint            = SHA256(原始值 struct JSON)
  canonical_clinical_fingerprint = SHA256(字符串列 trim+lower 后 struct JSON)
  source_event_id                = canonical_clinical_fingerprint
  双计数（两级聚合）：raw 指纹先行分组计数，canonical 指纹再聚合：
    raw_exact_duplicate_count = SUM(每 raw 指纹行数)
    canonical_duplicate_count = 组内物理行数
  按此定义两计数数值恒等（canonical 折叠合并 raw 变体时均累计全部物理行）；
  canonical 组含多个 raw 变体时 raw_row_fingerprint 取组内最小值（代表值），
  事件主标识为 source_event_id。守恒断言（Q1-11）：
    SUM(raw_exact_duplicate_count) = 物理源行数。
  浮点列的规范十进制表示未冻结（B-4 pending），当前按 DuckDB to_json 输出。

实测登记（2026-07-30，eicu_crd.duckdb main schema，只读冒烟）：
  patient 200,859 行；hospitaladmitoffset / unitdischargeoffset /
    hospitaldischargeoffset / patienthealthsystemstayid / uniquepid 均无 NULL；
    age 95 行 NULL；unit stay 数/住院：1=138,720; 2=23,151; 3=2,907; 4=1,127;
    5+=506（最多 17）；相邻 stay 边 34,494 条。
  边分类器实测命中分布（含下列实测增补值）：
    icu_to_icu                     zero 10,232 / pos 1,110
    icu_to_stepdown_or_ward_to_icu zero 13,730 / pos 7,303
    icu_to_or_procedure_to_icu     zero 48     / pos 1,741
    cross_hospital_transfer        zero 3      / pos 11
    offset_overlap                 261（gap<0）
    unknown                        zero 37     / pos 28
  unitdischargelocation DISTINCT：Floor(93,121), Step-Down Unit (SDU)(26,945),
    Acute Care/Floor(20,279), Home(18,880), Telemetry(10,995), Death(10,907),
    Other ICU(6,208), Other Hospital(4,164), Other External(2,789),
    Skilled Nursing Facility(2,025), Other(1,548), ICU(1,274),
    Rehabilitation(769), NULL(334), Nursing Home(301), Other Internal(301),
    Operating Room(13), Other ICU (CABG)(6)
  unitadmitsource DISTINCT：Emergency Department(89,594), Floor(24,368),
    Operating Room(24,305), ICU to SDU(13,827), Direct Admit(12,672),
    Recovery Room(7,844), Acute Care/Floor(5,604), Step-Down Unit (SDU)(5,450),
    ICU(5,439), Other Hospital(4,323), Other ICU(4,264), PACU(1,714),
    NULL(1,090), Chest Pain Center(336), Observation(19), Other(10)
  分类清单实测增补（pending 审核，§9 R9 / 冻结清单 B-7）：
    - 'OR to ICU' 未在实测中出现；'Operating Room'/'PACU' 为其实测等价，
      并入 icu_to_or_procedure_to_icu；
    - 'ICU'/'Other ICU (CABG)' 实测存在，并入 icu_to_icu；
    - 'Step-Down Unit' 未实测（实测为 'Step-Down Unit (SDU)'）；
      prev 侧增补 'Step-Down Unit (SDU)'/'Acute Care/Floor'，curr 侧增补 'Floor'。
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

# eICU episode 合并阈值（§2.2 C6a 主规则 τ=0；敏感性版本未建，pending）
EICU_EPISODE_MERGE_TAU_MIN = 0

# ----------------------------------------------------------------
# edge_path_class 清单（实测值见模块 docstring；增补值 pending 审核）
# ----------------------------------------------------------------
_ICU_PREV_DISCH = ("Other ICU", "ICU", "Other ICU (CABG)")
_ICU_CURR_ADMIT = ("Other ICU", "ICU")
_OR_PROC_CURR_ADMIT = ("OR to ICU", "Recovery Room", "Operating Room", "PACU")
_CROSS_HOSPITAL = ("Other Hospital",)
_WARD_PREV_DISCH = ("Floor", "Step-Down Unit", "Step-Down Unit (SDU)",
                    "Telemetry", "Acute Care/Floor")
_WARD_CURR_ADMIT = ("Acute Care/Floor", "Step-Down Unit (SDU)", "Floor")

# ----------------------------------------------------------------
# canonical 事件指纹字段（逐表 schema 冻结候选，B-4 pending；
# (列名, 是否字符串列)；字符串列 canonical = trim+lower）
# medication 字段顺序按 A.4 冻结示例
# ----------------------------------------------------------------
_TIME_MAP_SPECS = {
    "medication": {
        "table": "main.medication",
        # 主键时间列：drugorderoffset；start/stop 的 episode 坐标 =
        # episode_offset_min + (drugstart/stopoffset - drugorderoffset)
        #（同一 unit stay 内 offset 差与坐标系无关）
        "offset_col": "drugorderoffset",
        "fields": [("patientunitstayid", False), ("drugorderoffset", False),
                   ("drugstartoffset", False), ("drugstopoffset", False),
                   ("drugname", True), ("routeadmin", True),
                   ("dosage", True), ("frequency", True)],
        "out": "eicu_medication_time_map.parquet",
    },
    "lab": {
        "table": "main.lab",
        "offset_col": "labresultoffset",
        # labresultrevisedoffset 计入原始身份；语义审计 C-2 关闭前不单独处理
        "fields": [("patientunitstayid", False), ("labresultoffset", False),
                   ("labtypeid", False), ("labname", True),
                   ("labresult", False), ("labresulttext", True),
                   ("labmeasurenamesystem", True),
                   ("labmeasurenameinterface", True),
                   ("labresultrevisedoffset", False)],
        "out": "eicu_lab_time_map.parquet",
    },
    "micro_lab": {
        "table": "main.micro_lab",
        "offset_col": "culturetakenoffset",
        "fields": [("patientunitstayid", False), ("culturetakenoffset", False),
                   ("culturesite", True), ("organism", True),
                   ("antibiotic", True), ("sensitivitylevel", True)],
        "out": "eicu_microbiology_time_map.parquet",
    },
    "infusion_drug": {
        "table": "main.infusion_drug",
        "offset_col": "infusionoffset",
        "fields": [("patientunitstayid", False), ("infusionoffset", False),
                   ("drugname", True), ("drugrate", True),
                   ("infusionrate", True), ("drugamount", False),
                   ("volumeoffluid", False), ("patientweight", False)],
        "out": "eicu_infusion_time_map.parquet",
    },
}


# ----------------------------------------------------------------
# eicu_unitstay_timeline
# ----------------------------------------------------------------

def run_c6a_unitstay_timeline(con):
    """patient 表派生 unit stay 时间线（hospital 分钟坐标）。"""
    out = config.OUTPUT_DIRS["episodes"]
    utils.log_step("C6a: eicu_unitstay_timeline")
    sql = """
    SELECT patientunitstayid, patienthealthsystemstayid, uniquepid, hospitalid,
      hospitaladmitoffset,
      (-hospitaladmitoffset)::BIGINT AS unit_start_hospital_min,
      (-hospitaladmitoffset + unitdischargeoffset)::BIGINT
        AS unit_end_hospital_min,
      unitdischargeoffset,
      -- 负 LOS 数据异常（实测 2 条 unitdischargeoffset<0，2026-07-30）：
      -- 不修复原始值，仅打标进 QA（类比 MIMIC episode_outtime_status）
      (unitdischargeoffset < 0) AS unit_los_anomaly_flag,
      (-hospitaladmitoffset + hospitaldischargeoffset)::BIGINT
        AS hospital_discharge_hospital_min,
      hospitaldischargeoffset,
      hospitaldischargestatus, hospitaldischargelocation,
      CASE WHEN hospitaldischargestatus = 'Expired' THEN 1 ELSE 0 END
        AS hosp_mort,
      unittype, unitadmitsource, unitstaytype, unitvisitnumber,
      unitdischargelocation, unitdischargestatus,
      gender, age, ethnicity, admissionheight, admissionweight,
      hospitaladmitsource
    FROM main.patient
    ORDER BY patienthealthsystemstayid, unit_start_hospital_min,
             patientunitstayid
    """
    n = utils.write_duckdb_table(con, sql,
                                 out / "eicu_unitstay_timeline.parquet")
    print(f"  eicu_unitstay_timeline: {n:,} rows")
    return n


# ----------------------------------------------------------------
# 边级分类与 preliminary 输出
# ----------------------------------------------------------------

def _build_base_edges(con) -> pd.DataFrame:
    utils.log_step("C6a: build adjacent-stay edges")
    sql = """
    WITH s AS (
      SELECT patientunitstayid, patienthealthsystemstayid,
             -hospitaladmitoffset AS unit_start_hospital_min,
             -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min,
             unitdischargelocation, unitadmitsource, unitstaytype
      FROM main.patient
    ), e AS (
      SELECT patienthealthsystemstayid,
        LAG(patientunitstayid) OVER w AS previous_patientunitstayid,
        patientunitstayid AS current_patientunitstayid,
        LAG(unit_end_hospital_min) OVER w AS previous_unit_end_hospital_min,
        unit_start_hospital_min AS current_unit_start_hospital_min,
        LAG(unitdischargelocation) OVER w AS previous_unitdischargelocation,
        unitadmitsource AS current_unitadmitsource,
        LAG(unitstaytype) OVER w AS previous_unitstaytype,
        unitstaytype AS current_unitstaytype
      FROM s
      WINDOW w AS (PARTITION BY patienthealthsystemstayid
                   ORDER BY unit_start_hospital_min, patientunitstayid)
    )
    SELECT *,
      current_unit_start_hospital_min - previous_unit_end_hospital_min
        AS gap_minutes
    FROM e
    WHERE previous_patientunitstayid IS NOT NULL
    ORDER BY patienthealthsystemstayid, current_unit_start_hospital_min,
             current_patientunitstayid
    """
    df = con.execute(sql).fetchdf()
    print(f"  base edges: {len(df):,} rows")
    return df


def _classify_edge_path(gap_minutes, prev_disch, curr_admit) -> str:
    """edge_path_class 六类（§2.2 C6a；清单实测增补见 docstring）。"""
    if gap_minutes is not None and not pd.isna(gap_minutes) and gap_minutes < 0:
        return "offset_overlap"
    if prev_disch in _ICU_PREV_DISCH or curr_admit in _ICU_CURR_ADMIT:
        return "icu_to_icu"
    if curr_admit in _OR_PROC_CURR_ADMIT:
        return "icu_to_or_procedure_to_icu"
    if prev_disch in _CROSS_HOSPITAL or curr_admit in _CROSS_HOSPITAL:
        return "cross_hospital_transfer"
    if prev_disch in _WARD_PREV_DISCH or curr_admit in _WARD_CURR_ADMIT:
        return "icu_to_stepdown_or_ward_to_icu"
    return "unknown"


def _edge_decision(gap_minutes, path_class) -> str:
    """preliminary_decision（§2.2 C6a 合并主规则）。"""
    if gap_minutes is not None and not pd.isna(gap_minutes) and gap_minutes < 0:
        return "pending_review"
    if (gap_minutes is not None and not pd.isna(gap_minutes)
            and gap_minutes <= EICU_EPISODE_MERGE_TAU_MIN
            and path_class == "icu_to_icu"):
        return "merged"
    return "split"


def run_c6a_edges(con) -> pd.DataFrame:
    out = config.OUTPUT_DIRS["episodes"]
    edges = _build_base_edges(con)
    utils.log_step("C6a: classify edge paths")
    edges["overlap_flag"] = edges["gap_minutes"].lt(0).fillna(False)
    edges["edge_path_class"] = [
        _classify_edge_path(g, p, c) for g, p, c in zip(
            edges["gap_minutes"], edges["previous_unitdischargelocation"],
            edges["current_unitadmitsource"])
    ]
    edges["preliminary_decision"] = [
        _edge_decision(g, c) for g, c in zip(
            edges["gap_minutes"], edges["edge_path_class"])
    ]
    edges["episode_merge_threshold_min"] = EICU_EPISODE_MERGE_TAU_MIN
    utils.write_parquet(edges, out / "eicu_episode_edges_preliminary.parquet")
    dist = edges.groupby(["edge_path_class", "preliminary_decision"]).size()
    print(f"  edges_preliminary: {len(edges):,} rows\n{dist}")
    return edges


# ----------------------------------------------------------------
# preliminary / final map（与 mimic/c0_episodes.py 同一累积分组法）
# ----------------------------------------------------------------

_EDGE_KEY = ["patienthealthsystemstayid", "previous_patientunitstayid",
             "current_patientunitstayid"]


def _decision_with_adjudication(prelim_decision, adj_status, adj_final):
    """显式 final decision CASE（冻结；非法值 ⇒ pipeline failure）。"""
    if prelim_decision == "pending_review":
        if adj_status == "adjudicated" and adj_final in ("merged", "split"):
            return adj_final, "adjudicated", False
        return "split", "unresolved_conservative_split", True
    if prelim_decision in ("merged", "split"):
        return prelim_decision, "clean", False
    raise ValueError(f"Illegal preliminary_decision: {prelim_decision!r} "
                     "(final_decision IS NULL => pipeline failure)")


def _build_map(stays: pd.DataFrame, edges: pd.DataFrame,
               adjudications: pd.DataFrame, stage: str) -> pd.DataFrame:
    ev = edges.copy()
    if stage == "final":
        adj = adjudications[_EDGE_KEY + ["adjudication_status",
                                         "final_decision"]]
        ev = ev.merge(adj, on=_EDGE_KEY, how="left")
        decisions = [
            _decision_with_adjudication(p, a, f) for p, a, f in zip(
                ev["preliminary_decision"], ev["adjudication_status"],
                ev["final_decision"])
        ]
        ev["edge_final_decision"] = [d[0] for d in decisions]
        ev["edge_mapping_status"] = [d[1] for d in decisions]
        ev["unresolved_conservative_split"] = [d[2] for d in decisions]
    else:
        # preliminary：pending_review 保守拆分（显式标 unresolved）
        ev["edge_final_decision"] = np.where(
            ev["preliminary_decision"] == "merged", "merged", "split")
        ev["edge_mapping_status"] = "preliminary"
        ev["unresolved_conservative_split"] = (
            ev["preliminary_decision"] == "pending_review")

    m = stays.merge(
        ev[_EDGE_KEY + ["edge_final_decision", "edge_mapping_status",
                        "unresolved_conservative_split", "gap_minutes",
                        "overlap_flag", "edge_path_class"]],
        left_on=["patienthealthsystemstayid", "patientunitstayid"],
        right_on=["patienthealthsystemstayid", "current_patientunitstayid"],
        how="left")
    m = m.sort_values(["patienthealthsystemstayid", "unit_start_hospital_min",
                       "patientunitstayid"]).reset_index(drop=True)
    m["is_merged_from_prev"] = m["edge_final_decision"].eq("merged") \
        .fillna(False)
    m["episode_seq"] = m.groupby("patienthealthsystemstayid")[
        "is_merged_from_prev"].transform(lambda s: (~s).cumsum())
    m["stay_seq_in_episode"] = m.groupby(
        ["patienthealthsystemstayid", "episode_seq"]).cumcount() + 1
    m["episode_id"] = ("EICU_" + m["patienthealthsystemstayid"].astype(str)
                       + "_" + m["episode_seq"].astype(str))

    grp = m.groupby("episode_id", sort=False)
    ep_times = grp.agg(
        episode_start_hospital_min=("unit_start_hospital_min", "min"),
        episode_end_hospital_min=("unit_end_hospital_min", "max"),
        episode_time_anomaly_flag=("unit_los_anomaly_flag", "max"),
    ).reset_index()
    m = m.merge(ep_times, on="episode_id", how="left")
    m["episode_merge_decision"] = np.where(
        m["stay_seq_in_episode"] == 1, "episode_start",
        m["edge_final_decision"].fillna("split"))
    m["episode_mapping_status"] = np.where(
        m["stay_seq_in_episode"] == 1, "episode_start",
        m["edge_mapping_status"].fillna("episode_start"))
    # split 边以 edge_path_class 作为排除原因留痕（merged/首 stay 为 NULL）
    m["episode_merge_exclusion_reason"] = np.where(
        m["episode_merge_decision"] == "split", m["edge_path_class"], None)

    cols = ["patientunitstayid", "patienthealthsystemstayid", "uniquepid",
            "episode_id", "stay_seq_in_episode",
            "episode_start_hospital_min", "episode_end_hospital_min",
            "episode_time_anomaly_flag", "gap_minutes",
            "episode_merge_decision",
            "episode_merge_exclusion_reason", "overlap_flag",
            "edge_path_class", "episode_mapping_status",
            "unresolved_conservative_split"]
    out = m[cols].rename(
        columns={"gap_minutes": "gap_minutes_from_prev_stay"})
    return out


def run_c6a_maps(con, edges: pd.DataFrame):
    out = config.OUTPUT_DIRS["episodes"]
    utils.log_step("C6a: preliminary/final episode maps")

    # 阶段③：adjudications —— 仅 pending_review 边（裁决范围冻结，同 MIMIC）
    pend = edges[edges["preliminary_decision"] == "pending_review"][
        _EDGE_KEY].drop_duplicates().copy()
    pend["adjudication_status"] = "pending"
    pend["final_decision"] = None
    pend["adjudication_source"] = None
    utils.write_parquet(pend, out / "eicu_episode_merge_adjudications.parquet")
    print(f"  adjudications (pending edges): {len(pend):,} rows")

    timeline_path = str(out / "eicu_unitstay_timeline.parquet")
    stays = pd.read_parquet(timeline_path, columns=[
        "patientunitstayid", "patienthealthsystemstayid", "uniquepid",
        "unit_start_hospital_min", "unit_end_hospital_min",
        "unit_los_anomaly_flag"])

    prelim = _build_map(stays, edges, None, stage="preliminary")
    utils.write_parquet(prelim, out / "eicu_episode_map_preliminary.parquet")
    print(f"  map_preliminary: {len(prelim):,} rows")

    final = _build_map(stays, edges, pend, stage="final")
    utils.write_parquet(final, out / "eicu_episode_map_final.parquet")
    print(f"  map_final: {len(final):,} rows")

    # Q1 eICU episode 约束断言（§2.2 C6a final map 约束）
    # ① 每 patientunitstayid 恰好一个 final episode_id
    assert len(final) == len(stays), "final map 行数 != patient stay 数"
    assert final.groupby("patientunitstayid")["episode_id"].nunique().max() == 1, \
        "patientunitstayid in multiple episodes"
    # ② (episode_id, patientunitstayid) 无重复行
    assert not final.duplicated(["episode_id", "patientunitstayid"]).any(), \
        "duplicate (episode_id, patientunitstayid) rows"
    # ③ 同一 final episode 不跨 patienthealthsystemstayid
    assert final.groupby("episode_id")["patienthealthsystemstayid"] \
        .nunique().max() == 1, "episode_id spans patienthealthsystemstayid"
    # ④ final decision 状态空间合法
    assert final["episode_merge_decision"].isin(
        ["episode_start", "merged", "split"]).all(), \
        "illegal final decision state"
    # ⑤ episode hospital-time 区间单调（负 LOS 异常 episode 除外——
    #   实测 2 条 unitdischargeoffset<0，已打 episode_time_anomaly_flag 进 QA）
    ok = final[~final["episode_time_anomaly_flag"].fillna(False)]
    assert (ok["episode_start_hospital_min"]
            <= ok["episode_end_hospital_min"]).all(), \
        "episode_start_hospital_min > episode_end_hospital_min"
    n_anom = int(final["episode_time_anomaly_flag"].fillna(False).sum())
    print(f"  episode_time_anomaly_flag stays: {n_anom} "
          f"(negative unit LOS, 进 QA)")
    print("  Q1 eICU episode uniqueness/state-space assertions passed")
    return len(final)


# ----------------------------------------------------------------
# canonical 事件时间映射表（四张）
# ----------------------------------------------------------------

def _time_map_sql(spec: dict, final_map_path: str) -> str:
    fields = spec["fields"]
    cols = [f"t.{name}" for name, _ in fields]
    raw_fields = {"source_table":
                  f"'{spec['table'].split('.')[-1]}'"}
    canon_fields = {"source_table":
                    f"'{spec['table'].split('.')[-1]}'"}
    for name, is_str in fields:
        raw_fields[name] = f"t.{name}"
        canon_fields[name] = (utils.canonicalize_str_sql(f"t.{name}")
                              if is_str else f"t.{name}")
    raw_json = utils.struct_json_sql(raw_fields)
    canon_json = utils.struct_json_sql(canon_fields)
    min_cols = ",\n         ".join(
        f"MIN({name}) AS {name}" for name, _ in fields)
    sql = f"""
    WITH fp AS (
      SELECT {", ".join(cols)},
        (-p.hospitaladmitoffset + t.{spec['offset_col']})::BIGINT
          AS hospital_offset_min,
        SHA256({raw_json}::VARCHAR)   AS raw_row_fingerprint,
        SHA256({canon_json}::VARCHAR) AS canonical_clinical_fingerprint
      FROM {spec['table']} t
      JOIN main.patient p USING (patientunitstayid)
    ),
    raw_grp AS (
      -- 第一级：每 raw 指纹行数（内容列全部参与分组 = raw 身份）
      SELECT {", ".join(name for name, _ in fields)},
        hospital_offset_min, canonical_clinical_fingerprint,
        raw_row_fingerprint,
        COUNT(*) AS raw_n
      FROM fp
      GROUP BY {", ".join(name for name, _ in fields)},
               hospital_offset_min, canonical_clinical_fingerprint,
               raw_row_fingerprint
    ),
    canon_grp AS (
      -- 第二级：canonical 唯一事件；raw_exact_duplicate_count = SUM(raw 行数)
      SELECT canonical_clinical_fingerprint,
        MIN(raw_row_fingerprint) AS raw_row_fingerprint,
        {min_cols},
        MIN(hospital_offset_min) AS hospital_offset_min,
        SUM(raw_n) AS raw_exact_duplicate_count,
        SUM(raw_n) AS canonical_duplicate_count
      FROM raw_grp
      GROUP BY canonical_clinical_fingerprint
    )
    SELECT c.*,
      f.episode_id,
      (c.hospital_offset_min - f.episode_start_hospital_min)::BIGINT
        AS episode_offset_min,
      c.canonical_clinical_fingerprint AS source_event_id,
      '{config.SOURCE_EVENT_ID_VERSION}' AS source_event_id_version,
      '{config.CANONICALIZATION_RULE_VERSION}' AS canonicalization_rule_version
    FROM canon_grp c
    JOIN read_parquet('{final_map_path}') f
      ON f.patientunitstayid = c.patientunitstayid
    """
    return sql


def run_c6a_time_maps(con):
    out = config.OUTPUT_DIRS["episodes"]
    final_map_path = str(out / "eicu_episode_map_final.parquet") \
        .replace("\\", "/")
    total = 0
    for name, spec in _TIME_MAP_SPECS.items():
        utils.log_step(f"C6a: {spec['out']} (canonical event time map)")
        path = out / spec["out"]
        n = utils.write_duckdb_table_direct(
            con, _time_map_sql(spec, final_map_path), path)
        # 守恒断言（Q1-11）：SUM(raw_exact_duplicate_count) = 物理源行数
        n_physical = con.execute(
            f"SELECT COUNT(*) FROM {spec['table']}").fetchone()[0]
        p = str(path).replace("\\", "/")
        n_sum, n_sum2, n_null_ep = con.execute(f"""
          SELECT SUM(raw_exact_duplicate_count),
                 SUM(canonical_duplicate_count),
                 SUM(CASE WHEN episode_id IS NULL THEN 1 ELSE 0 END)
          FROM read_parquet('{p}')
        """).fetchone()
        assert n_sum == n_physical, \
            f"{name}: 守恒断言失败 SUM(raw_exact_duplicate_count)={n_sum} " \
            f"!= 物理行数 {n_physical}"
        assert n_sum2 == n_physical, \
            f"{name}: canonical_duplicate_count 守恒失败"
        assert n_null_ep == 0, f"{name}: {n_null_ep} 事件未映射 episode"
        print(f"  {spec['out']}: {n:,} unique events "
              f"(physical {n_physical:,}, 守恒 OK)")
        total += n
    return total


# ----------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------

def run_c6a(con):
    run_c6a_unitstay_timeline(con)
    edges = run_c6a_edges(con)
    run_c6a_maps(con, edges)
    run_c6a_time_maps(con)
