"""
SEPSIS-MM-DYN Data Extraction Configuration (v2.4.1 rewrite).

All paths, parameters, and constants derived from:
  - 《技术文档 v1.9》
  - 《数据提取方案 v2.4.1》（冻结审核修补版）

本 config 中每个常量块标注方案章节来源。凡属「待 PI/审计锁定」的参数
（D0 时间原点、出院去向清单、eICU 表型规则、ECG 数据驱动 QC）均以
`*_PENDING` 字段显式登记，与方案 §11「冻结前许可：原型提取」一致。
"""
from pathlib import Path
import os
import json

# ================================================================
# Database paths (read-only)
# ----------------------------------------------------------------
# Reproducible override via environment variables:
#   MIMIC_DB        path to the MIMIC-IV v3.1 DuckDB database
#   EICU_DB         path to the eICU-CRD v2.0 DuckDB database
#   ECG_WFDB_ROOT   directory containing MIMIC-IV-ECG WFDB records
# The fallback values below reflect the original authoring machine;
# override them for any other deployment.
# ================================================================
MIMIC_DB = os.environ.get(
    "MIMIC_DB", r"E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb")
EICU_DB = os.environ.get(
    "EICU_DB", r"E:\clinical_research\eICUdatabase\eicu_crd.duckdb")
ECG_WFDB_ROOT = Path(os.environ.get(
    "ECG_WFDB_ROOT", r"E:\clinical_research\MIMIC_IV_3.1\ecg"))

# ================================================================
# Output root (extraction plan v2.4.1 §8)
# ================================================================
OUTPUT_ROOT = Path(__file__).resolve().parent / "_output"

OUTPUT_DIRS = {
    "cohorts":    OUTPUT_ROOT / "cohorts",
    "episodes":   OUTPUT_ROOT / "episodes",
    "phenotypes": OUTPUT_ROOT / "phenotypes",
    "splits":     OUTPUT_ROOT / "splits",
    "landmarks":  OUTPUT_ROOT / "landmarks",
    "labels":     OUTPUT_ROOT / "labels",
    "features":   OUTPUT_ROOT / "features",
    "contracts":  OUTPUT_ROOT / "contracts",
    "ecg_index":  OUTPUT_ROOT / "ecg_index",
    "qa":         OUTPUT_ROOT / "qa",
    "_meta":      OUTPUT_ROOT / "_meta",
}

# ================================================================
# C0: Episode merge thresholds (§2.1 C0, P1-7; 参数化独立版本)
# ================================================================
EPISODE_MERGE_THRESHOLDS = {
    "main_tau0": 0,
    "sensitivity_tau30": 30,
    "sensitivity_tau60": 60,
}
DEFAULT_MERGE_VERSION = "main_tau0"

# ICU careunit classification for transfer path audit (§2.1 C0 zero-gap 序列核验)
ICU_CAREUNIT_PATTERNS = (
    "%Intensive Care Unit%", "%CCU%",
)
ED_CAREUNIT_PATTERNS = (
    "%Emergency Department%",
)
# 合法路径 ICU_A → internal_transfer_placeholder → ICU_B：
# 两个 ICU 单元之间的短时长非 ICU 记录（如 Discharge Lounge 秒级/分钟级行政记录）
# 视为内部转移占位而非真实 ward 停留（参数候选，QA 实测后预登记）
PLACEHOLDER_MAX_MIN = 30

# ================================================================
# Landmark grid (§3.2; 技术文档 §2.1/§5.1)
# ================================================================
LANDMARK_INTERVAL_HOURS = 6
LANDMARK_MAX_K = 27              # [0h, 168h) 半开区间最多 28 个
LANDMARK_MAIN_GRID_MAX_K = 11    # 主积分网格 [0h, 72h)
LANDMARK_MIN_AGE = 18

# ================================================================
# D0 决策门状态（§3.1）—— 未锁定；操作口径见 d0_decision.json
# ================================================================
D0_STATUS = "decided"
D0_PRIMARY_T_SEPSIS_FIELD = "suspected_infection_time"   # D0 出口 B（amendment）
D0_OPERATIONAL_T_SEPSIS_FIELD = "suspected_infection_time"
D0_SECONDARY_ORIGINS = ["max(sofa_time, suspected_infection_time)",
                        "icu_admission"]
D0_SOURCE_TABLE = "mimiciv_derived.sepsis3"
D0_SOURCE_CODE_COMMIT = "a0af19c18a66b6d96935058ebfa830608989bd7c"
D0_PI_APPROVAL_DATE = "2026-07-30"

# ================================================================
# Internal temporal split (§2.4; 技术文档 §12.2)
# ================================================================
SPLIT_MAP = {
    "2008 - 2010": "train",
    "2011 - 2013": "train",
    "2014 - 2016": "validation",
    "2017 - 2019": "test",
    "2020 - 2022": "excluded_amendment_pending",
}

# ================================================================
# MIMIC lab itemids (§5.3; labevents 重建清单)
# ================================================================
MIMIC_LAB_ITEMIDS = {
    "pao2":        (50821,),
    "bilirubin":   (50885,),
    "platelets":   (51265,),
    "creatinine":  (50912,),
    "lactate":     (50813,),
    "wbc":         (51301, 51300),
    "hemoglobin":  (51222,),
    "glucose":     (50931,),
    "sodium":      (50983,),
    "potassium":   (50971,),
    "bicarbonate": (50882,),
    "inr":         (51237,),
    "pt":          (51274,),
}
# FiO2 单列（fio2_source 分类；224740 O2 Flow 仅敏感性，不入主）
# 实测：本 MIMIC-IV v3.1 实例 chartevents 仅存在 223835（MetaVision），
# 3420/190 为 CareVue 遗留 itemid（0 行），保留映射以备跨版本。
MIMIC_FIO2_ITEMIDS = {
    "3420":   "measured",
    "190":    "measured",
    "223835": "ventilator_setting",
}

# ================================================================
# MIMIC vital itemids for chartevents reconstruction (§5.2)
# ================================================================
MIMIC_VITAL_ITEMIDS = {
    "hr":   (220045,),
    "sbp":  (220050, 220179, 225309),
    "dbp":  (220051, 220180, 225310),
    "mbp":  (220052, 220181, 225312),
    "rr":   (220210, 224689, 224690),
    "spo2": (220277,),
    "temp": (223762, 223761),
}
VITAL_SIGNS = tuple(MIMIC_VITAL_ITEMIDS.keys())

# ================================================================
# SOFA carry-forward limits in hours (§5.4 carryforward 轨)
# ================================================================
SOFA_CF_LIMITS = {
    "bilirubin": 48, "creatinine": 48, "platelets": 48,
    "gcs": 24, "pf_ratio": 24, "cv": 24,
}

# NEE equivalence factors (技术文档 §6.2)
NEE_EQ = {
    "norepinephrine": 1.0,
    "epinephrine": 1.0,
    "dopamine": 0.01,
    "phenylephrine": 0.1,
    "vasopressin": 2.5,
}

# ================================================================
# Discharge location classifications (§4.1 派生字段口径)
# 实测清单（v1 预登记候选，D-3 冻结项；两库分别登记）
# ================================================================
ACUTE_TRANSFER_LOCS = (
    "ACUTE HOSPITAL",
)
ALIVE_DISCHARGE_LOCS = (
    "HOME", "HOME HEALTH CARE", "SKILLED NURSING FACILITY",
    "REHAB", "CHRONIC/LONG TERM ACUTE CARE", "ASSISTED LIVING",
    "AGAINST ADVICE", "HOSPICE", "HEALTHCARE FACILITY",
    "OTHER FACILITY", "PSYCH FACILITY",
)
DISCHARGE_LIST_PENDING = True  # QA 实测后预登记（风险 R9）

# ================================================================
# Clinical observation source whitelist (§4.1 白名单, P1-2)
# ================================================================
CLINICAL_OBSERVATION_WHITELIST = [
    # (source_table, clinical_time_field, time_semantics, eligible)
    ("chartevents",           "charttime",          "charted_at_bedside", True),
    ("labevents",             "charttime",          "specimen_chart_time", True),
    ("urine_output",          "charttime",          "measured",            True),
    ("vasoactive_agent",      "starttime/endtime",  "infusion_actual",     True),
    ("ventilation",           "starttime/endtime",  "device_recorded",     True),
    ("microbiologyevents",    "charttime",          "specimen_collection", True),
    ("labevents",             "storetime",          "database_store",      False),
    ("chartevents",           "storetime",          "database_store",      False),
    ("diagnoses_icd",         "n/a",                "coding_time",         False),
]

# ================================================================
# ECG parameters (§5.8; 技术文档 §7.1/§20)
# ================================================================
ECG_FRESHNESS_MAIN_H = 24
ECG_FRESHNESS_SENSITIVITY_H = (48, 72)
ECG_PRE_ADMISSION_MAX_DAYS = 30        # 入院前可审计窗口（E-2 参数候选）
ECG_ED_GAP_MAX_H = 24                  # ED→入院间隔阈值（E-2 参数候选）
ECG_STRUCT_QC_MIN_LEADS = 8            # 结构性 QC 下限候选（E-3 待冻结）
ECG_STRUCT_QC_MIN_DURATION_S = 9.0     # 结构性 QC：记录时长下限（<10s 判不合格）
ECG_DATA_DRIVEN_QC_PENDING = True      # 数据驱动 QC 阈值待训练集拟合（E-4）

# ================================================================
# eICU antibiotic identification (§2.2 C6b；正则候选清单)
# ================================================================
EICU_ANTIBIOTIC_PATTERNS = [
    "cef", "penicillin", "ampicillin", "piperacillin", "vancomycin",
    "meropenem", "imipenem", "ertapenem", "aztreonam",
    "gentamicin", "tobramycin", "amikacin",
    "ciprofloxacin", "levofloxacin", "moxifloxacin",
    "metronidazole", "clindamycin", "linezolid", "daptomycin",
    "azithromycin", "clarithromycin", "doxycycline", "tigecycline",
    "sulfamethoxazole", "trimethoprim", "ceftriaxone", "cefepime",
    "cefazolin", "fluconazole", "acyclovir", "oseltamivir",
    "amoxicillin", "nafcillin", "oxacillin", "rifampin",
]
EICU_ABX_DEDUP_WINDOW_MIN = 240  # 同药相近时间去重窗口（候选参数）

# eICU suspected-infection 配对方向性时间窗（分钟；§2.2 C6b / A.5）
EICU_PAIR_AB_AFTER_CX_MAX_MIN = 4320   # 抗生素在培养后 0–72h
EICU_PAIR_CX_AFTER_AB_MAX_MIN = 1440   # 培养在抗生素后 0–24h

# ================================================================
# eICU P-explicit 显式脓毒症诊断串（§2.2 C7；清单预登记候选）
# ================================================================
EICU_EXPLICIT_SEPSIS_PATTERNS = [
    "sepsis", "severe sepsis", "septic shock", "septicemia",
    "septic % shock",
]

# eICU P-clinical 感染诊断证据（候选清单；待 PI 签署）
EICU_INFECTION_DX_PATTERNS = [
    "pneumonia", "urinary tract infection", "uti", "bacteremia",
    "intraabdominal infection", "peritonitis", "meningitis",
    "endocarditis", "cellulitis", "abscess", "empyema",
    "cholangitis", "pyelonephritis", "infection", "infectious",
    "sepsis", "septic",
]

# ================================================================
# Version metadata
# ================================================================
PIPELINE_VERSION = "v2.4.1"
SOURCE_EVENT_ID_VERSION = "eicu_source_event_sha256_v1"
CANONICALIZATION_RULE_VERSION = "canonical_json_v1"

METADATA = {
    "pipeline_version": PIPELINE_VERSION,
    "mimic_source": "MIMIC-IV v3.1",
    "eicu_source": "eICU-CRD v2.0",
    "merge_version_default": DEFAULT_MERGE_VERSION,
    "landmark_interval_h": LANDMARK_INTERVAL_HOURS,
    "landmark_max_k": LANDMARK_MAX_K,
    "landmark_main_grid_max_k": LANDMARK_MAIN_GRID_MAX_K,
    "adult_age_min": LANDMARK_MIN_AGE,
    "d0_status": D0_STATUS,
    "operational_t_sepsis_field": D0_OPERATIONAL_T_SEPSIS_FIELD,
    "source_event_id_version": SOURCE_EVENT_ID_VERSION,
    "canonicalization_rule_version": CANONICALIZATION_RULE_VERSION,
    "pending_items": [
        "eICU 锁定选对函数 select_suspected_infection_pairs_locked_v1 待 mimic-code blob（A-4）",
        "ECG 陷波/基线滤波/起搏检测待训练集审计（P7/技术文档 §20）",
        "eICU 表型规则表待 PI 签署（feasibility_only）",
        "A-3 2020-2022 amendment 待签署",
        "A-6 Go/No-Go 阈值待 PI 确认",
    ],
}


def ensure_output_dirs():
    for d in OUTPUT_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    meta_path = OUTPUT_DIRS["_meta"] / "code_version.json"
    meta_path.write_text(json.dumps(METADATA, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    d0_path = OUTPUT_DIRS["_meta"] / "d0_decision.json"
    d0_path.write_text(json.dumps({
        "status": D0_STATUS,
        "primary_time_origin": D0_PRIMARY_T_SEPSIS_FIELD,
        "secondary_time_origins": D0_SECONDARY_ORIGINS,
        "source_table": D0_SOURCE_TABLE,
        "source_code_commit": D0_SOURCE_CODE_COMMIT,
        "protocol_amendment_required": True,
        "protocol_amendment_note": (
            "D0 出口 B：主时间原点正式定为 suspected_infection_time。"
            "依据：锁定版 mimic-code（a0af19c）MIMIC-IV sepsis3 概念不输出 "
            "sepsis_time（qa/derived_provenance_v2.md §2）；sofa_time 与 "
            "suspected_infection_time 差值分布 [-48h, +24h]（78.5% 感染疑似更早），"
            "敏感性轨保留 max(sofa_time, suspected_infection_time) 与 "
            "icu_admission 两口径。PI 批准日期 2026-07-30。"),
        "pi_approval_date": D0_PI_APPROVAL_DATE,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    fc_path = OUTPUT_DIRS["_meta"] / "freeze_checklist.json"
    if not fc_path.exists():
        fc_path.write_text(json.dumps({
            "schema_version": "freeze_checklist_v1",
            "note": "31 项冻结清单状态骨架；初始全部未关闭，随 QA 运行更新",
            "A_protocol": {f"A-{i}": False for i in range(1, 7)},
            "B_timeline": {f"B-{i}": False for i in range(1, 8)},
            "C_leakage": {f"C-{i}": False for i in range(1, 8)},
            "D_labels": {f"D-{i}": False for i in range(1, 7)},
            "E_ecg": {f"E-{i}": False for i in range(1, 6)},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
