"""Contracts: sc_common_variable_contract_v2 + clinical_observation_whitelist_v2.

（v2.4.1 §6 变量级等价性合同 + §4.1 临床观察源白名单；评级为候选，待 C2 阶段核验）
"""
import sys
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


# §6 A/B/C 层候选合同（等价性评级为候选，C2 阶段逐变量核验后锁定）
_SC_COMMON = [
    # concept, mimic_source, eicu_source, unit, layer, grade, leak_risk
    ("age", "icustay_detail.admission_age", "patient.age", "years", "A", "A", "low"),
    ("gender", "patients.gender", "patient.gender", "-", "A", "A", "low"),
    ("hr", "chartevents(220045)", "pivoted_vital.heartrate", "bpm", "A", "A", "low"),
    ("map", "chartevents(mbp itemids)", "pivoted_vital ibp/nibp_mean", "mmHg", "A", "A", "low"),
    ("sbp", "chartevents(sbp itemids)", "pivoted_vital ibp/nibp_systolic", "mmHg", "A", "A", "low"),
    ("dbp", "chartevents(dbp itemids)", "pivoted_vital ibp/nibp_diastolic", "mmHg", "A", "A", "low"),
    ("rr", "chartevents(rr itemids)", "pivoted_vital.RespiratoryRate", "/min", "A", "A", "low"),
    ("spo2", "chartevents(220277)", "pivoted_vital.spo2", "%", "A", "A", "low"),
    ("temp", "chartevents(temp itemids)", "pivoted_vital.temperature", "C", "A", "A", "low"),
    ("creatinine", "labevents(50912)", "pivoted_lab.creatinine", "mg/dL", "A", "A", "low"),
    ("bilirubin", "labevents(50885)", "pivoted_lab.bilirubin", "mg/dL", "A", "A", "low"),
    ("platelets", "labevents(51265)", "pivoted_lab.platelets", "K/uL", "A", "A", "low"),
    ("lactate", "labevents(50813)", "pivoted_lab.lactate", "mmol/L", "A", "A", "low"),
    ("wbc", "labevents(51301,51300)", "pivoted_lab.wbc", "K/uL", "A", "A", "low"),
    ("hemoglobin", "labevents(51222)", "pivoted_lab.hemoglobin", "g/dL", "A", "A", "low"),
    ("glucose", "labevents(50931)", "pivoted_lab.glucose", "mg/dL", "A", "A", "low"),
    ("sodium", "labevents(50983)", "pivoted_lab.sodium", "mmol/L", "A", "A", "low"),
    ("potassium", "labevents(50971)", "pivoted_lab.potassium", "mmol/L", "A", "A", "low"),
    ("bicarbonate", "labevents(50882)", "pivoted_lab.bicarbonate", "mmol/L", "A", "A", "low"),
    ("gcs", "derived.gcs", "pivoted_gcs", "-", "B", "B", "medium"),
    ("pao2_fio2", "labevents+chartevents pairing", "pivoted_bg", "-", "B", "B", "medium"),
    ("urine_output_24h", "derived.urine_output", "pivoted_uo", "mL", "B", "B", "low"),
    ("mechanical_ventilation", "derived.ventilation", "respiratory_care/treatment", "-", "B", "B", "medium"),
    ("vasoactive_use", "vasoactive_agent", "infusion_drug", "0/1", "B", "B", "medium"),
    ("inr", "labevents(51237)", "pivoted_lab.INR", "-", "B", "B", "low"),
    ("nee_dose", "norepinephrine_equivalent_dose", "infusion_drug parse", "ug/kg/min", "C", "C", "high"),
    ("charlson_prior", "prior admissions charlson", "past_history (不同构)", "-", "C", "C", "medium"),
    ("icu_type", "icustays.first_careunit", "patient.unittype", "-", "C", "C", "low"),
    ("admission_source", "admissions.admission_location", "hospitaladmitsource", "-", "C", "C", "low"),
]


def run_contracts():
    out = config.OUTPUT_DIRS["contracts"]
    utils.log_step("Contracts: sc_common_variable_contract_v2")
    df = pd.DataFrame(_SC_COMMON, columns=[
        "concept_name", "mimic_source", "eicu_source", "unit",
        "layer", "cross_database_equivalence_grade", "leakage_risk"])
    df["conversion_rule"] = None
    df["priority_rule"] = "primary_then_fallback"
    df["event_time_rule"] = "chart_or_event_time"
    df["available_time_rule"] = "strict_available_time"
    df["missing_rule"] = "keep_missing_with_mask"
    df["physiologic_range"] = None
    df["contract_status"] = "candidate_pending_c2_audit"
    df["contract_version"] = "v2_candidate"
    utils.write_parquet(df, out / "sc_common_variable_contract_v2.parquet")
    print(f"  sc_common_variable_contract_v2: {len(df)} variables")

    wl = pd.DataFrame(config.CLINICAL_OBSERVATION_WHITELIST, columns=[
        "source_table", "clinical_time_field", "time_semantics",
        "eligible_for_last_clinical_observation"])
    wl["whitelist_version"] = "v1_frozen_candidate"
    utils.write_parquet(wl, out / "clinical_observation_whitelist_v2.parquet")
    print(f"  clinical_observation_whitelist_v2: {len(wl)} rows")
