"""MIMIC QA: Q1 自动断言子集 + 报告（v2.4.1 §7）。

报告：cohort_flow_v2.md / leakage_report_v2.md / time_logic_qa_v2.md /
feasibility_table_v2.md
"""
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

OUT = config.OUTPUT_ROOT


def _pq(name):
    return str(OUT / name).replace("\\", "/")


def run_q1_assertions(con):
    """Q1 自动测试子集（结果写入 leakage_report）。"""
    utils.log_step("QA: Q1 assertions")
    results = []

    def check(name, ok, detail=""):
        results.append({"assertion": name, "pass": bool(ok),
                        "detail": str(detail)})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # Q1-8 episode final map
    for v in config.EPISODE_MERGE_THRESHOLDS:
        df = con.execute(f"""
          SELECT COUNT(*) n_rows, COUNT(DISTINCT episode_id) n_ep,
                 COUNT(DISTINCT stay_id) n_stays
          FROM read_parquet('{_pq("episodes/mimic_icu_episode_map_final.parquet")}')
          WHERE episode_mapping_version = '{v}'
        """).fetchone()
        check(f"Q1-8 {v} stay->episode 一对一",
              df[0] == df[2], f"rows={df[0]}, stays={df[2]}")
        bad = con.execute(f"""
          SELECT COUNT(*) FROM read_parquet(
            '{_pq("episodes/mimic_icu_episode_map_final.parquet")}')
          WHERE episode_mapping_version = '{v}'
            AND episode_merge_decision NOT IN
              ('episode_start', 'merged', 'split')
        """).fetchone()[0]
        check(f"Q1-8 {v} final_decision 状态空间", bad == 0,
              f"illegal={bad}")

    # landmark 单调 6h / k0>=0 / 主网格
    lm = con.execute(f"""
      SELECT COUNT(*) FROM (
        SELECT episode_id, k, t_landmark_ts,
          LAG(t_landmark_ts) OVER (PARTITION BY episode_id ORDER BY k) AS prev
        FROM read_parquet('{_pq("landmarks/landmarks_v2.parquet")}'))
      WHERE prev IS NOT NULL
        AND t_landmark_ts - prev <> INTERVAL '6 hours'
    """).fetchone()[0]
    check("Q1-18 landmark 间隔恒为 6h", lm == 0, f"violations={lm}")
    k0 = con.execute(f"""
      SELECT COUNT(*) FROM read_parquet('{_pq("landmarks/landmarks_v2.parquet")}')
      WHERE k < k0 OR k0 < 0
    """).fetchone()[0]
    check("Q1-18 k >= k0 >= 0", k0 == 0, f"violations={k0}")

    # 标签断言
    lab = f"{_pq('labels/labels_24h_v2.parquet')}"
    xor = con.execute(f"""
      SELECT COUNT(*) FROM read_parquet('{lab}')
      WHERE acute_transfer_time IS NOT NULL
        AND alive_discharge_time IS NOT NULL
    """).fetchone()[0]
    check("Q1-17 acute_transfer XOR alive_discharge", xor == 0,
          f"violations={xor}")
    enum = con.execute(f"""
      SELECT DISTINCT outcome_unknown_reason FROM read_parquet('{lab}')
      WHERE outcome_unknown_reason IS NOT NULL
    """).fetchdf()["outcome_unknown_reason"].tolist()
    allowed = {"acute_transfer", "missing_status_left_observation",
               "death_time_missing", "status_conflict", "time_anomaly",
               "invalid_input"}
    check("Q1-14 outcome_unknown_reason 枚举",
          set(enum) <= allowed, f"observed={sorted(enum)}")
    leak_label = con.execute(f"""
      SELECT COUNT(*) FROM read_parquet('{lab}')
      WHERE label_reason = 'event' AND deathtime <= t_landmark_ts
    """).fetchone()[0]
    check("防泄漏 #3 结局窗起点 > landmark", leak_label == 0,
          f"violations={leak_label}")
    # 恰好 w_end 出院 ⇒ full followup TRUE（P1-3 反例固化）
    edge = con.execute(f"""
      SELECT COUNT(*) FROM read_parquet('{lab}')
      WHERE label_reason = 'non_event_alive_discharge'
        AND alive_discharge_time >= w_end
        AND full_inhospital_followup_24h <> TRUE
    """).fetchone()[0]
    check("P1-3 恰好 t+24h 出院 full_followup=TRUE", edge == 0,
          f"violations={edge}")

    # 特征防泄漏：max_available_time <= t_landmark
    for f, tcol in [("features/vitals_realtime_strict_v2.parquet",
                     "max_available_time"),
                    ("features/labs_hourly_v2.parquet", "max_available_time")]:
        fp = _pq(f)
        if not Path(fp).exists():
            check(f"防泄漏 #2 {f}", None, "file missing")
            continue
        track_filter = ("AND time_track = 'strict_available_time'"
                        if "labs" in f else "")
        bad = con.execute(f"""
          SELECT COUNT(*) FROM read_parquet('{fp}')
          WHERE {tcol} > t_landmark_ts {track_filter}
        """).fetchone()[0]
        check(f"防泄漏 #2 {f} max_available<=landmark", bad == 0,
              f"violations={bad}")
        # bin 边界一致性：事件时间必须落在自身 bin 区间（bin0=最近约定）
        if Path(fp).exists():
            bin_bad = con.execute(f"""
              SELECT COUNT(*) FROM read_parquet('{fp}')
              WHERE min_event_time < bin_start
                 OR max_event_time > bin_end
                 {track_filter}
            """).fetchone()[0]
            check(f"Q1-18 {f} 事件落在 bin 区间", bin_bad == 0,
                  f"violations={bin_bad}")

    # ECG 防泄漏
    ecg_fp = _pq("ecg_index/ecg_landmark_index_v2.parquet")
    if Path(ecg_fp).exists():
        bad = con.execute(f"""
          SELECT COUNT(*) FROM read_parquet('{ecg_fp}')
          WHERE ecg_selected_for_model
            AND ecg_available_time_assumed > t_landmark_ts
        """).fetchone()[0]
        check("防泄漏 #1 ECG available<=landmark", bad == 0,
              f"violations={bad}")
        enc = con.execute(f"""
          SELECT DISTINCT ecg_encounter_status FROM read_parquet('{ecg_fp}')
        """).fetchdf()["ecg_encounter_status"].tolist()
        allowed_enc = {"same_hospitalization",
                       "auditable_pre_admission_encounter"}
        check("Q1-18 ECG 归属仅前两类入选",
              set(enc) <= allowed_enc, f"observed={sorted(enc)}")

    # SOFA 完整性
    sofa_fp = _pq("features/sofa_hourly_v2.parquet")
    if Path(sofa_fp).exists():
        bad = con.execute(f"""
          SELECT COUNT(*) FROM read_parquet('{sofa_fp}')
          WHERE (sofa_total_complete IS NOT NULL AND sofa_component_count <> 6)
             OR (sofa_component_count = 5 AND sofa_total_complete IS NOT NULL)
        """).fetchone()[0]
        check("Q1-12 SOFA 完整总分仅 6/6", bad == 0, f"violations={bad}")

    # schema 一致性（Q1-18）：关键表列非空
    for name in ["episodes/mimic_icu_episode_edges_preliminary.parquet",
                 "episodes/mimic_icu_episode_map_final.parquet",
                 "cohorts/cohort_mimic_v2.parquet",
                 "landmarks/landmarks_v2.parquet",
                 "labels/labels_24h_v2.parquet",
                 "features/baseline_static_v2.parquet",
                 "features/vitals_hourly_v2.parquet",
                 "features/labs_hourly_v2.parquet",
                 "features/sofa_hourly_v2.parquet",
                 "features/nee_stream_v2.parquet",
                 "features/ventilation_v2.parquet",
                 "features/urine_output_v2.parquet",
                 "ecg_index/ecg_landmark_index_v2.parquet",
                 "splits/split_assignments_v2.parquet"]:
        fp = _pq(name)
        if not Path(fp).exists():
            check(f"产物存在 {name}", False, "missing")
            continue
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{fp}')").fetchone()[0]
        adj = "adjudication" in name
        check(f"产物非空 {name}", n > 0 or adj, f"rows={n:,}")

    rep = pd.DataFrame(results)
    return rep


def _md_table(df) -> str:
    """Local markdown table renderer (no tabulate dependency)."""
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def run_qa_reports(con, assertion_df: pd.DataFrame):
    qa_dir = config.OUTPUT_DIRS["qa"]
    qa_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # cohort flow
    ep = con.execute(f"""
      SELECT episode_mapping_version, COUNT(*) stays,
             COUNT(DISTINCT episode_id) episodes
      FROM read_parquet('{_pq("episodes/mimic_icu_episode_map_final.parquet")}')
      GROUP BY 1 ORDER BY 1""").fetchdf()
    es = con.execute(f"""
      SELECT COUNT(*) FROM read_parquet(
        '{_pq("episodes/mimic_episode_sepsis.parquet")}')""").fetchone()[0]
    co = con.execute(f"""
      SELECT COUNT(*), COUNT(DISTINCT subject_id)
      FROM read_parquet('{_pq("cohorts/cohort_mimic_v2.parquet")}')"""
      ).fetchone()
    lm = con.execute(f"""
      SELECT COUNT(*), COUNT(DISTINCT episode_id)
      FROM read_parquet('{_pq("landmarks/landmarks_v2.parquet")}')"""
      ).fetchone()
    lab = con.execute(f"""
      SELECT label_reason, COUNT(*) c
      FROM read_parquet('{_pq("labels/labels_24h_v2.parquet")}')
      GROUP BY 1 ORDER BY 2 DESC""").fetchdf()
    flow_md = f"""# cohort_flow_v2（MIMIC）

生成时间：{ts}；pipeline {config.PIPELINE_VERSION}；merge version 主分析 = {config.DEFAULT_MERGE_VERSION}

## C0 episode 映射

{_md_table(ep)}

## C1 sepsis episode 池

- mimic_episode_sepsis：{es:,} episodes

## C2-C5 队列

- cohort_mimic_v2：{co[0]:,} index episodes / {co[1]:,} subjects

## L1/L2 landmark

- landmarks_v2：{lm[0]:,} rows / {lm[1]:,} episodes

## L3 标签分布

{_md_table(lab)}
"""
    (qa_dir / "cohort_flow_v2.md").write_text(flow_md, encoding="utf-8")

    # leakage report
    leak_md = f"""# leakage_report_v2（Q1 自动断言子集）

生成时间：{ts}

{_md_table(assertion_df)}

> 失败项需修复后重跑；全部通过是冻结清单关闭的前置之一。
"""
    (qa_dir / "leakage_report_v2.md").write_text(leak_md, encoding="utf-8")

    # time logic QA
    k0d = con.execute(f"""
      SELECT k0, COUNT(*) c FROM read_parquet(
        '{_pq("landmarks/landmarks_v2.parquet")}')
      GROUP BY 1 ORDER BY 1""").fetchdf()
    gap = con.execute(f"""
      SELECT episode_mapping_version, preliminary_decision, COUNT(*) c
      FROM read_parquet(
        '{_pq("episodes/mimic_icu_episode_edges_preliminary.parquet")}')
      GROUP BY 1,2 ORDER BY 1,2""").fetchdf()
    tl_md = f"""# time_logic_qa_v2

生成时间：{ts}

## k0 分布（首个有效 landmark）

{_md_table(k0d.head(20))}

## episode 边判定分布（按版本）

{_md_table(gap)}

## 实测说明

- MIMIC-IV v3.1 实测最小 stay 间隙 0.1 min（无 gap=0 边）；main_tau0 下无合并，
  每 stay 独立 episode；sensitivity_tau30/60 捕获占位路径合并。
- 全部白名单临床事件时间与 storetime 分布见 observation_endpoints_v2。
"""
    (qa_dir / "time_logic_qa_v2.md").write_text(tl_md, encoding="utf-8")

    # feasibility table（技术文档 §9.1）
    pos = con.execute(f"""
      SELECT SUM(CASE WHEN y_24h = 1 THEN 1 ELSE 0 END) pos,
             SUM(CASE WHEN y_24h = 0 THEN 1 ELSE 0 END) neg,
             COUNT(*) total
      FROM read_parquet('{_pq("labels/labels_24h_v2.parquet")}')
    """).fetchone()
    test_pos = con.execute(f"""
      SELECT COUNT(DISTINCT l.episode_id)
      FROM read_parquet('{_pq("labels/labels_24h_v2.parquet")}') l
      JOIN read_parquet('{_pq("cohorts/cohort_mimic_v2.parquet")}') c
        ON l.episode_id = c.episode_id
      JOIN read_parquet('{_pq("splits/split_assignments_v2.parquet")}') s
        ON c.subject_id = s.subject_id
      WHERE s.set_name = 'test' AND l.y_24h = 1
    """).fetchone()[0]
    ecg_cov = None
    ecg_fp = _pq("ecg_index/ecg_landmark_index_v2.parquet")
    if Path(ecg_fp).exists():
        ecg_cov = con.execute(f"""
          SELECT AVG(CASE WHEN ecg_available THEN 1.0 ELSE 0.0 END)
          FROM read_parquet('{ecg_fp}')
        """).fetchone()[0]
    feas_md = f"""# feasibility_table_v2（技术文档 §9.1）

生成时间：{ts}

| 指标 | 统计量 |
|---|---|
| MIMIC-IV 脓毒症 index episode 数 | {co[0]:,} |
| landmark 总数 / 阳性 landmark / 阳性率 | {pos[2]:,} / {pos[0]:,} / {pos[0]/max(pos[2],1):.4f} |
| 24h 阴性 landmark 数 | {pos[1]:,} |
| 测试集有 ≥1 阳性 landmark 的 episode 数 | {test_pos:,} |
| landmark 级 24h ECG 覆盖率 | {ecg_cov if ecg_cov is not None else 'pending'} |

> 月 1 Go/No-Go 需 PI 确认阈值（冻结清单 A-6）。
"""
    (qa_dir / "feasibility_table_v2.md").write_text(feas_md,
                                                    encoding="utf-8")
    print("  QA reports written to qa/")


def run_qa_pipeline(con):
    rep = run_q1_assertions(con)
    run_qa_reports(con, rep)
    n_fail = int((rep["pass"] == False).sum())
    print(f"QA done: {n_fail} failed assertions")
    return rep
