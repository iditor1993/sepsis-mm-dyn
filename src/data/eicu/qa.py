"""eICU 可行性 QA：Go/No-Go 报告（v2.4.1 §2.2 C7 Go/No-Go；§7.3/§9 R22）。

输出 qa/eicu_go_nogo_v2.md：
  每 track 患者数 / 医院数 / 最大单医院占比 / 院内死亡数 /
  培养覆盖率 / 抗生素时间源覆盖率（30% 门槛）/
  主要 landmark 可估计比例（主网格 k∈[0,11] 中满足
  「阳性 ≥20 且阴性 ≥100」的 landmark 数，门槛 ≥10）。

门槛（§2.2 C7 确定建议值；PI 确认后预登记，禁止按模型效果反向调整，R22）：
  P-strict：医院数 ≥ 20 且最大单医院患者占比 ≤ 25%，患者数 ≥ 500，
            培养覆盖率 ≥ 5%
  P-clinical / P-explicit：患者数 ≥ 2,000
  全 track：院内死亡事件数 ≥ 100（月 1 样本量分析复核）
  抗生素时间源覆盖率（episode 级联合指标）≥ 30%
  主要 landmark 可估计 ≥ 10/12

P-strict 的 t_sepsis 恒 NULL（锁定选对函数 pending，R33/B-5）→
  无 landmark/标签，可估计比例为 N/A（预期行为，非 FAIL）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402

# Go/No-Go 门槛（§2.2 C7 建议值；R22 pending PI 确认）
THRESH = {
    "pstrict_hospitals_min": 20,
    "pstrict_max_share_max": 0.25,
    "pstrict_patients_min": 500,
    "other_patients_min": 2000,
    "deaths_min": 100,
    "pstrict_culture_min": 0.05,
    "abx_coverage_min": 0.30,
    "landmark_estimable_min": 10,
    "main_grid_k": 12,
    "pos_min": 20,
    "neg_min": 100,
}


def _pq(name):
    # 动态读取 OUTPUT_ROOT（测试可重定向；与 mimic/qa.py 模块级绑定不同）
    return str(config.OUTPUT_ROOT / name).replace("\\", "/")


def _md_table(df: pd.DataFrame) -> str:
    """依赖无关的 GitHub markdown 表渲染（环境未装 tabulate，
    pandas.DataFrame.to_markdown 不可用；数值列格式化到 4 位有效小数）。"""
    def _fmt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        if isinstance(v, float):
            return f"{v:.4g}"
        return str(v)
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c]) for c in cols) + " |")
    return "\n".join(lines)


def run_qa_eicu(con):
    qa_dir = config.OUTPUT_DIRS["qa"]
    qa_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    utils.log_step("QA: eicu_go_nogo_v2")

    cohort = _pq("cohorts/cohort_eicu_v2.parquet")
    culture = _pq("phenotypes/eicu_culture_events.parquet")
    labels = _pq("labels/eicu_labels_24h_v2.parquet")

    # 每 track 基础统计
    stats = con.execute(f"""
      SELECT phenotype_track,
        COUNT(*) AS n_index_episodes,
        COUNT(DISTINCT uniquepid) AS n_patients,
        COUNT(DISTINCT hospitalid) AS n_hospitals,
        SUM(CASE WHEN hospitaldischargestatus = 'Expired'
                 THEN 1 ELSE 0 END) AS n_inhospital_deaths,
        SUM(CASE WHEN episode_has_reliable_antibiotic_time
                 THEN 1 ELSE 0 END) AS n_episodes_reliable_abx_time,
        SUM(CASE WHEN selected_antibiotic_time_source IS NOT NULL
                 THEN 1 ELSE 0 END) AS n_episodes_with_abx
      FROM read_parquet('{cohort}')
      GROUP BY 1 ORDER BY 1
    """).fetchdf()

    # 最大单医院占比（每 track）
    share = con.execute(f"""
      SELECT phenotype_track,
        MAX(cnt) * 1.0 / SUM(cnt) AS max_hospital_patient_share
      FROM (
        SELECT phenotype_track, hospitalid,
               COUNT(DISTINCT uniquepid) AS cnt
        FROM read_parquet('{cohort}') GROUP BY 1, 2
      ) GROUP BY 1
    """).fetchdf()
    stats = stats.merge(share, on="phenotype_track", how="left")

    # 培养覆盖率（cohort episode 有 ≥1 培养事件）
    cult = con.execute(f"""
      SELECT c.phenotype_track,
        COUNT(DISTINCT CASE WHEN cx.episode_id IS NOT NULL
                            THEN c.episode_id END) * 1.0 / COUNT(*)
          AS culture_coverage_rate
      FROM read_parquet('{cohort}') c
      LEFT JOIN (SELECT DISTINCT episode_id FROM read_parquet('{culture}')) cx
        ON cx.episode_id = c.episode_id
      GROUP BY 1
    """).fetchdf()
    stats = stats.merge(cult, on="phenotype_track", how="left")
    stats["abx_time_source_coverage_rate"] = (
        stats["n_episodes_reliable_abx_time"]
        / stats["n_episodes_with_abx"].clip(lower=1))

    # 主要 landmark 可估计（主网格 k∈[0,11]，阳性 ≥20 且阴性 ≥100）
    lm = con.execute(f"""
      SELECT phenotype_track, k,
        SUM(CASE WHEN y_24h = 1 THEN 1 ELSE 0 END) AS n_pos,
        SUM(CASE WHEN y_24h = 0 THEN 1 ELSE 0 END) AS n_neg
      FROM read_parquet('{labels}')
      WHERE k < {THRESH["main_grid_k"]}
      GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchdf()
    lm["estimable"] = ((lm["n_pos"] >= THRESH["pos_min"])
                       & (lm["n_neg"] >= THRESH["neg_min"]))
    est = lm.groupby("phenotype_track")["estimable"].sum() \
        .reset_index(name="n_estimable_landmarks")
    stats = stats.merge(est, on="phenotype_track", how="left")

    # PASS/FAIL 逐项
    checks = []
    for _, r in stats.iterrows():
        tr = r["phenotype_track"]
        if tr == "P-strict":
            checks += [
                (tr, "医院数 ≥ 20",
                 r["n_hospitals"] >= THRESH["pstrict_hospitals_min"],
                 f"{r['n_hospitals']}"),
                (tr, "最大单医院患者占比 ≤ 25%",
                 r["max_hospital_patient_share"]
                 <= THRESH["pstrict_max_share_max"],
                 f"{r['max_hospital_patient_share']:.3f}"),
                (tr, "患者数 ≥ 500",
                 r["n_patients"] >= THRESH["pstrict_patients_min"],
                 f"{r['n_patients']}"),
                (tr, "培养覆盖率 ≥ 5%",
                 r["culture_coverage_rate"] >= THRESH["pstrict_culture_min"],
                 f"{r['culture_coverage_rate']:.4f}"),
            ]
        else:
            checks += [
                (tr, "患者数 ≥ 2,000",
                 r["n_patients"] >= THRESH["other_patients_min"],
                 f"{r['n_patients']}"),
            ]
        n_est = r["n_estimable_landmarks"]
        if pd.isna(n_est) and tr == "P-strict":
            checks.append((tr, "主要 landmark 可估计 ≥ 10/12", None,
                           "N/A（t_sepsis 锁定选对函数 pending，R33/B-5）"))
        else:
            checks.append((tr, "主要 landmark 可估计 ≥ 10/12",
                           n_est >= THRESH["landmark_estimable_min"],
                           f"{int(n_est)}/12"))
        checks.append((tr, "院内死亡事件数 ≥ 100",
                       r["n_inhospital_deaths"] >= THRESH["deaths_min"],
                       f"{r['n_inhospital_deaths']}"))
        checks.append((tr, "抗生素时间源覆盖率 ≥ 30%",
                       r["abx_time_source_coverage_rate"]
                       >= THRESH["abx_coverage_min"],
                       f"{r['abx_time_source_coverage_rate']:.4f}"))
    chk = pd.DataFrame(checks, columns=[
        "phenotype_track", "gate", "pass", "observed"])
    chk["result"] = chk["pass"].map(
        {True: "PASS", False: "FAIL"}).fillna("N/A")

    md = f"""# eicu_go_nogo_v2（eICU 可行性 Go/No-Go）

生成时间：{ts}；pipeline {config.PIPELINE_VERSION}
门槛来源：§2.2 C7 确定建议值（PI 确认后预登记，R22；禁止按模型效果反向调整）。
全部 track 为 feasibility_only（表型规则表待 PI 签署，A-5/R19；
P-strict 选对函数 pending，R33/B-5）。

## 每 track 汇总

{_md_table(stats)}

## 主要 landmark 可估计明细（主网格 k∈[0,11]）

{_md_table(lm)}

## 门槛逐项判定

{_md_table(chk)}

## 备注

- 抗生素时间源覆盖率口径：episode 级 `episode_has_reliable_antibiotic_time`
  （= 有 infusion_recorded 事件）/ 有任何抗生素事件的 episode；
  `administration_confirmation_availability = 'structurally_unavailable'`（§2.2 C6b）。
- P-strict 无 landmark/标签（t_sepsis pending）——预期行为，不作 FAIL。
- 培养覆盖率分母为 cohort index episodes（P-strict 门槛 ≥5%，§2.2 C7）。
- 出院去向清单 D-3 pending（labels.py 注释实测登记）。
"""
    (qa_dir / "eicu_go_nogo_v2.md").write_text(md, encoding="utf-8")
    n_fail = int((chk["pass"] == False).sum())
    print(f"  eicu_go_nogo_v2.md written; FAIL gates: {n_fail}")
    return chk
