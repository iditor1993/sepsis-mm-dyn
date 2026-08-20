"""D-5: §3.4 标签边界单元测试（合成数据驱动真实状态机 SQL）。

运行：python -m unittest src.data.tests.test_label_boundaries -v
（从项目根目录；与 mimic/labels.py 状态机同构的 SQL 在 DuckDB 内存库执行）
"""
import sys
import unittest
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]

# 与 mimic/labels.py 完全一致的状态机 CASE（复制以保持同步）
STATE_CASE = """
    CASE
      WHEN deathtime IS NOT NULL AND deathtime <= t_landmark_ts
        THEN 'invalid_input'
      WHEN deathtime IS NOT NULL AND hospital_expire_flag = 0
        THEN 'status_conflict'
      WHEN hospital_expire_flag = 1 AND deathtime IS NULL
        THEN 'death_time_missing'
      WHEN deathtime > t_landmark_ts AND deathtime <= w_end
        THEN 'event'
      WHEN acute_transfer_time > t_landmark_ts
           AND acute_transfer_time <= w_end
        THEN 'acute_transfer'
      WHEN alive_discharge_time > t_landmark_ts
           AND alive_discharge_time <= w_end
        THEN 'non_event_alive_discharge'
      WHEN last_clinically_observed_time >= w_end
        THEN 'non_event_observed'
      ELSE 'missing_status_left_observation'
    END
"""


def _run_state_machine(rows):
    """rows: list of dict with the base columns. Returns list of dict with
    label_state, y_24h, full_followup, unknown_reason."""
    df = pd.DataFrame(rows)
    for c in ["t_landmark_ts", "w_end", "deathtime", "dischtime",
              "acute_transfer_time", "alive_discharge_time",
              "last_clinically_observed_time"]:
        df[c] = pd.to_datetime(df[c])
    con = duckdb.connect()
    con.register("base", df)
    out = con.execute(f"""
      SELECT *, {STATE_CASE} AS label_state
      FROM base
    """).fetchdf()
    out["y_24h"] = out["label_state"].map(
        lambda s: 1 if s == "event"
        else (0 if s in ("non_event_observed",
                         "non_event_alive_discharge") else None))
    out["full_followup"] = out.apply(
        lambda r: bool(r["label_state"] == "non_event_observed"
                       or (r["label_state"] == "non_event_alive_discharge"
                           and r["alive_discharge_time"] is not pd.NaT
                           and pd.notna(r["alive_discharge_time"])
                           and r["alive_discharge_time"] >= r["w_end"])),
        axis=1)
    return out


T0 = "2180-01-01 00:00:00"
T12 = "2180-01-01 12:00:00"
T24 = "2180-01-02 00:00:00"
T30 = "2180-01-02 06:00:00"
T48 = "2180-01-03 00:00:00"


def _base(**kw):
    r = {"t_landmark_ts": T0, "w_end": T24, "hospital_expire_flag": 0,
         "deathtime": None, "dischtime": T48, "acute_transfer_time": None,
         "alive_discharge_time": None,
         "last_clinically_observed_time": T48,
         "clinical_event_after_discharge_flag": False}
    r.update(kw)
    return r


class TestLabelBoundaries(unittest.TestCase):
    def test_death_exactly_at_landmark_invalid(self):
        out = _run_state_machine([_base(deathtime=T0,
                                        hospital_expire_flag=1)])
        self.assertEqual(out.iloc[0]["label_state"], "invalid_input")

    def test_death_in_window_inclusive_t24(self):
        out = _run_state_machine([_base(deathtime=T24,
                                        hospital_expire_flag=1)])
        self.assertEqual(out.iloc[0]["label_state"], "event")
        self.assertEqual(out.iloc[0]["y_24h"], 1)

    def test_death_after_window(self):
        out = _run_state_machine([_base(deathtime=T30,
                                        hospital_expire_flag=0,
                                        dischtime=T48)])
        # deathtime 非空 + expire_flag=0 → status_conflict 优先
        self.assertEqual(out.iloc[0]["label_state"], "status_conflict")

    def test_discharge_exactly_t24_full_followup(self):
        out = _run_state_machine([_base(alive_discharge_time=T24,
                                        dischtime=T24)])
        r = out.iloc[0]
        self.assertEqual(r["label_state"], "non_event_alive_discharge")
        self.assertEqual(r["y_24h"], 0)
        self.assertTrue(r["full_followup"])   # P1-3 边界

    def test_discharge_t12_delayed_store_t30(self):
        # 反例固化：t+12h 存活出院 + t+30h 延迟 storetime
        # → non_event_alive_discharge 且 full_followup=FALSE
        out = _run_state_machine([_base(alive_discharge_time=T12,
                                        dischtime=T12,
                                        last_clinically_observed_time=T12)])
        r = out.iloc[0]
        self.assertEqual(r["label_state"], "non_event_alive_discharge")
        self.assertFalse(r["full_followup"])

    def test_acute_transfer_in_window(self):
        out = _run_state_machine([_base(acute_transfer_time=T12,
                                        dischtime=T12)])
        self.assertEqual(out.iloc[0]["label_state"], "acute_transfer")
        self.assertIsNone(out.iloc[0]["y_24h"])

    def test_death_time_missing(self):
        out = _run_state_machine([_base(hospital_expire_flag=1,
                                        deathtime=None)])
        self.assertEqual(out.iloc[0]["label_state"], "death_time_missing")

    def test_status_conflict(self):
        out = _run_state_machine([_base(hospital_expire_flag=0,
                                        deathtime=T30)])
        self.assertEqual(out.iloc[0]["label_state"], "status_conflict")

    def test_coverage_non_event(self):
        out = _run_state_machine([_base(last_clinically_observed_time=T48)])
        r = out.iloc[0]
        self.assertEqual(r["label_state"], "non_event_observed")
        self.assertTrue(r["full_followup"])

    def test_left_observation_unknown(self):
        out = _run_state_machine([_base(last_clinically_observed_time=T12)])
        self.assertEqual(out.iloc[0]["label_state"],
                         "missing_status_left_observation")

    def test_alive_discharge_after_window_is_non_event(self):
        # t+30h 存活出院（窗外）+ 观察到 t+48h → non_event_observed
        out = _run_state_machine([_base(alive_discharge_time=T30,
                                        dischtime=T30,
                                        last_clinically_observed_time=T48)])
        self.assertEqual(out.iloc[0]["label_state"], "non_event_observed")


class TestLandmarkGenerationBoundaries(unittest.TestCase):
    """§3.4 landmark 生成侧边界（与 mimic/landmarks.py 同构规则）。"""

    def test_death_before_landmark_excluded(self):
        # 死亡 ≤ landmark → 不生成（landmarks SQL 中 t_landmark < deathtime）
        # 用断言验证产物中不存在此类行
        lm = pd.read_parquet(
            ROOT / "src/data/_output/landmarks/landmarks_v2.parquet",
            columns=["episode_id", "k", "t_landmark_ts"])
        lab = pd.read_parquet(
            ROOT / "src/data/_output/labels/labels_24h_v2.parquet",
            columns=["episode_id", "k", "deathtime", "label_reason"])
        j = lm.merge(lab, on=["episode_id", "k"], how="inner")
        bad = j[j["deathtime"].notna()
                & (j["deathtime"] <= j["t_landmark_ts"])
                & (j["label_reason"] != "invalid_input")]
        self.assertEqual(len(bad), 0)

    def test_episode_end_before_landmark_excluded(self):
        lm = pd.read_parquet(
            ROOT / "src/data/_output/landmarks/landmarks_v2.parquet")
        co = pd.read_parquet(
            ROOT / "src/data/_output/cohorts/cohort_mimic_v2.parquet",
            columns=["episode_id", "episode_outtime_ts",
                     "episode_outtime_status"])
        j = lm.merge(co, on="episode_id")
        self.assertTrue((j["t_landmark_ts"] < j["episode_outtime_ts"]).all())
        self.assertTrue((j["episode_outtime_status"] == "ok").all())


if __name__ == "__main__":
    unittest.main()
