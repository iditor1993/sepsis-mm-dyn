"""MIMIC-IV extraction pipeline orchestrator (v2.4.1 rewrite).

DAG: C0 -> C1-C5 -> splits -> L1/L2 -> L3 -> F1..F8 -> contracts -> QA
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

from mimic.c0_episodes import run_c0                     # noqa: E402
from mimic.c1_cohort import run_cohort_pipeline          # noqa: E402
from mimic.landmarks import run_landmarks                # noqa: E402
from mimic.labels import run_labels_pipeline             # noqa: E402
from mimic.f1_static import run_f1_pipeline              # noqa: E402
from mimic.f2_vitals import run_f2_pipeline              # noqa: E402
from mimic.f3_labs import run_f3_pipeline                # noqa: E402
from mimic.f4_sofa import run_f4_pipeline                # noqa: E402
from mimic.f5_nee import run_f5_nee                      # noqa: E402
from mimic.f6_vent_urine import run_f6_vent, run_f7_urine  # noqa: E402
from mimic.f8_ecg import run_f8_pipeline                 # noqa: E402
from mimic.qa import run_qa_pipeline                     # noqa: E402
from contracts import run_contracts                      # noqa: E402

STEPS = [
    "c0", "cohort", "landmarks", "labels", "f1", "f2", "f3", "f4",
    "f5", "f6", "f7", "f8", "contracts", "qa",
]


def run_full_mimic_pipeline(step: str = None, merge_version: str = None):
    mv = merge_version or config.DEFAULT_MERGE_VERSION
    config.ensure_output_dirs()
    con = utils.connect_mimic()
    try:
        if step in (None, "c0"):
            run_c0(con)
        if step in (None, "cohort"):
            run_cohort_pipeline(con, mv)
        if step in (None, "landmarks"):
            run_landmarks(con, mv)
        if step in (None, "labels"):
            run_labels_pipeline(con, mv)
        if step in (None, "f1"):
            run_f1_pipeline(con, mv)
        if step in (None, "f2"):
            run_f2_pipeline(con)
        if step in (None, "f3"):
            run_f3_pipeline(con)
        if step in (None, "f4"):
            run_f4_pipeline(con)
        if step in (None, "f5"):
            run_f5_nee(con, mv)
        if step in (None, "f6"):
            run_f6_vent(con, mv)
        if step in (None, "f7"):
            run_f7_urine(con, mv)
        if step in (None, "f8"):
            run_f8_pipeline(con)
        if step in (None, "contracts"):
            run_contracts()
        if step in (None, "qa"):
            run_qa_pipeline(con)
    finally:
        con.close()
    utils.log_step("MIMIC pipeline done")
