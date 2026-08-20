"""eICU-CRD extraction pipeline orchestrator (v2.4.1 rewrite).

DAG: c6a -> c6b -> c7 -> c8 -> landmarks -> labels -> features -> qa

  c6a        episode 四表 + 时间坐标 + canonical 事件时间映射
  c6b        抗生素事件 / 培养事件 / 候选配对 / 时间源汇总
  c7         三套可行性表型（P-strict / P-clinical / P-explicit）
  c8         入排 + index episode + cohort_eicu_v2
  landmarks  landmark 网格（episode 分钟坐标）
  labels     24h 三态标签 + 7d 竞争风险
  features   vitals / labs / gcs / urine / support（charttime_fallback）
  qa         eicu_go_nogo_v2.md
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

from eicu.c6a_episodes import run_c6a              # noqa: E402
from eicu.c6b_antibiotics import run_c6b           # noqa: E402
from eicu.c7_phenotypes import run_c7              # noqa: E402
from eicu.c8_cohort import run_c8                  # noqa: E402
from eicu.landmarks import run_landmarks           # noqa: E402
from eicu.labels import run_labels_pipeline        # noqa: E402
from eicu.features import run_features             # noqa: E402
from eicu.qa import run_qa_eicu                    # noqa: E402

STEPS = ["c6a", "c6b", "c7", "c8",
         "landmarks", "labels", "features", "qa"]


def run_full_eicu_pipeline(step: str = None):
    if step is not None and step not in STEPS:
        raise ValueError(f"unknown eICU step: {step!r}; valid: {STEPS}")
    config.ensure_output_dirs()
    con = utils.connect_eicu()
    try:
        if step in (None, "c6a"):
            run_c6a(con)
        if step in (None, "c6b"):
            run_c6b(con)
        if step in (None, "c7"):
            run_c7(con)
        if step in (None, "c8"):
            run_c8(con)
        if step in (None, "landmarks"):
            run_landmarks(con)
        if step in (None, "labels"):
            run_labels_pipeline(con)
        if step in (None, "features"):
            run_features(con)
        if step in (None, "qa"):
            run_qa_eicu(con)
    finally:
        con.close()
    utils.log_step("eICU pipeline done")
