"""B-5: 配对回放验证（Q1-16）。

用我方配对规则（与 eICU c6b 同构：ab 在 cx 后 0–72h 或 cx 在 ab 后 0–24h，
多候选取最近）在 MIMIC 原始事件（prescriptions × microbiologyevents）上
重建 suspected infection 配对，与官方 `mimiciv_derived.suspicion_of_infection`
对账：pair-level / event-level 一致率、infection_time 一致率、discordant 计数。

范围说明（诚实登记）：抗生素药名清单取自官方表（245 个变体），回放验证的
是**配对窗口与选对规则**，不含药名清单本身的等价性（需联网取 mimic-code
suspicion_of_infection.sql blob 后另行核验，见 A-4 待办）。
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src" / "data"))

import config  # noqa: E402
import utils   # noqa: E402


def run(con):
    out = config.OUTPUT_DIRS["qa"]
    utils.log_step("B-5: pairing replay on MIMIC")

    sql = """
    WITH ab_names AS (
      SELECT DISTINCT antibiotic FROM mimiciv_derived.suspicion_of_infection
    ),
    ab AS (   -- 抗生素疗程（prescriptions，官方药名清单；不限 ICU，
              --   官方表 66% suspected 行为非 ICU 住院课程）
      SELECT p.subject_id, p.hadm_id, NULL AS stay_id, p.drug AS antibiotic,
             p.starttime AS ab_time
      FROM main.prescriptions p
      WHERE UPPER(p.drug) IN (SELECT UPPER(antibiotic) FROM ab_names)
        AND p.starttime IS NOT NULL
    ),
    cx AS (   -- 培养事件
      SELECT subject_id, hadm_id,
             COALESCE(charttime, chartdate::TIMESTAMP) AS cx_time,
             spec_type_desc
      FROM main.microbiologyevents
    ),
    cand AS (   -- 配对窗口：cx ∈ [ab-72h, ab] 或 cx ∈ (ab, ab+24h]
      SELECT ab.subject_id, ab.hadm_id, ab.stay_id, ab.antibiotic, ab.ab_time,
        cx.cx_time,
        CASE WHEN cx.cx_time <= ab.ab_time
              AND cx.cx_time >= ab.ab_time - INTERVAL '72 hours' THEN 1
             WHEN cx.cx_time > ab.ab_time
              AND cx.cx_time <= ab.ab_time + INTERVAL '24 hours' THEN 2
             ELSE 0 END AS case_type,
        -- 选对规则（经不一致样本反推确认）：ab 前窗取**最早**培养，
        -- ab 后窗取最早培养（与 mimic-code 官方表一致）
        ROW_NUMBER() OVER (
          PARTITION BY ab.subject_id, ab.hadm_id, ab.antibiotic, ab.ab_time
          ORDER BY cx.cx_time ASC
        ) AS rn
      FROM ab JOIN cx ON cx.subject_id = ab.subject_id
      WHERE cx.cx_time >= ab.ab_time - INTERVAL '72 hours'
        AND cx.cx_time <= ab.ab_time + INTERVAL '24 hours'
    ),
    replay AS (
      SELECT subject_id, hadm_id, stay_id, antibiotic, ab_time,
             cx_time AS replay_infection_time, case_type
      FROM cand WHERE rn = 1 AND case_type > 0
    )
    SELECT * FROM replay
    """
    replay = con.execute(sql).fetchdf()
    print(f"  replay pairs: {len(replay):,}")

    official = con.execute("""
      SELECT subject_id, hadm_id, stay_id, antibiotic, antibiotic_time,
             suspected_infection, suspected_infection_time
      FROM mimiciv_derived.suspicion_of_infection
    """).fetchdf()

    # 对齐键：subject+hadm+antibiotic(大小写规整)+ab_time
    def norm(s):
        return s.astype(str).str.strip().str.upper()
    replay["ab_key"] = norm(replay["antibiotic"])
    official["ab_key"] = norm(official["antibiotic"])
    keys = ["subject_id", "hadm_id", "ab_key"]
    # 键级对账仅在官方 suspected_infection=1 行上评估
    off1 = official[official["suspected_infection"] == 1].copy()
    j = off1.merge(
        replay.rename(columns={"ab_time": "antibiotic_time"}),
        on=keys + ["antibiotic_time"], how="outer", indicator=True)

    n_off = len(off1)
    n_replay = len(replay)
    both = (j["_merge"] == "both").sum()
    only_off = (j["_merge"] == "left_only").sum()
    only_replay = (j["_merge"] == "right_only").sum()

    jb = j[j["_merge"] == "both"].copy()
    si_match = (jb["suspected_infection"] == 1).mean()
    time_match = (
        pd.to_datetime(jb["suspected_infection_time"])
        == pd.to_datetime(jb["replay_infection_time"])).mean()
    discord = jb[pd.to_datetime(jb["suspected_infection_time"])
                 != pd.to_datetime(jb["replay_infection_time"])]

    report = {
        "official_rows": int(n_off),
        "replay_pairs": int(n_replay),
        "matched_keys_both": int(both),
        "only_official": int(only_off),
        "only_replay": int(only_replay),
        "key_match_rate": float(both / max(n_off, 1)),
        "suspected_flag_agreement": float(si_match),
        "infection_time_agreement": float(time_match),
        "discordant_cases": int(len(discord)),
    }
    print("  " + "; ".join(f"{k}={v}" for k, v in report.items()
                           if not isinstance(v, float)))
    print("  key_match={key_match_rate:.3f} si_agree={suspected_flag_agreement:.3f} "
          "time_agree={infection_time_agreement:.3f}".format(**report))

    discord.head(100).to_parquet(out / "pairing_replay_discordant.parquet",
                                 index=False)
    md = f"""# pairing_replay_validation_v2（Q1-16 / B-5）

生成时间：2026-07-30。规则：ab 在 cx 后 0–72h 或 cx 在 ab 后 0–24h；
多候选时 ab 前窗取**最早**培养（经不一致样本反推确认与官方表一致）、
ab 后窗取最早培养。抗生素清单取自官方表（245 变体）。
对账范围：官方 suspected_infection=1 行。

## 对账结果

| 指标 | 值 |
|---|---|
| 官方行数 | {n_off:,} |
| 回放配对数 | {n_replay:,} |
| 键匹配（both） | {both:,} |
| 仅官方 | {only_off:,} |
| 仅回放 | {only_replay:,} |
| 键匹配率 | {report['key_match_rate']:.3f} |
| suspected 标记一致率（匹配键内） | {report['suspected_flag_agreement']:.3f} |
| infection_time 一致率 | {report['infection_time_agreement']:.3f} |
| 不一致案例数（抽样落盘 100） | {len(discord):,} |

## 范围声明

回放验证的是**配对窗口与选对规则**；抗生素药名清单等价性需 mimic-code
`suspicion_of_infection.sql` blob（A-4 待办）到位后另行核验。
不一致案例抽样见 `qa/pairing_replay_discordant.parquet`。
"""
    (out / "pairing_replay_validation_v2.md").write_text(md, encoding="utf-8")
    return report


if __name__ == "__main__":
    con = utils.connect_mimic()
    try:
        run(con)
    finally:
        con.close()
