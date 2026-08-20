# SEPSIS-MM-DYN 数据提取方案 v2.4.1

- 文档版本：v2.4.1（正式冻结审核修补版）
- 创建日期：2026-07-30（v1.0 同日创建；v2.0–v2.4 经五轮外部评审修订；v2.4.1 经第六轮外部评审后修补）
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 修订依据：《总体评价》（2026-07-30 第六轮评审，对 v2.4 结论为「通过正式冻结审核版评审；冻结生效前仍有 3 项技术阻断项需强制关闭」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**正式主分析数据提取管线冻结版（frozen）**。冻结标签：`SEPSIS-MM-DYN-data-pipeline-v2.4.1-freeze`（2026-07-30 生效）。§10 冻结清单 **31/31 项全部关闭**（关闭证据逐项登记于 §10 各条目与 `_meta/freeze_checklist.json`），五类冻结验证、Q1 自动测试（33/33）、配对回放（键匹配 1.000 / 标记 0.990 / 时间 0.977）、源事件守恒（四表 SUM(raw)=物理行数）、SOFA 时间窗完整性与 schema 一致性验证全部通过。D0 已锁定（出口 B：`suspected_infection_time`）；2020–2022 正式排除（amendment 记录于 §2.4）；eICU 三套表型经 PI 签署（§2.2 C7，外验命名 Robustness under phenotype shift）；24h 主配对队列已冻结（E-5，78,305 landmarks）。**历史状态**：v2.4.1 原为正式冻结审核修补版（patch 级，不改变主要 estimand、主标签、landmark 网格、ECG 规则、SC-common 分层、eICU 三套表型战略与 Go/No-Go 总体框架）。冻结生效后即解除原型期限制，可进入正式模型训练阶段；冻结后任何数据源、字段口径或流程变更须递增版本号并重新评审。

---

## 0. v2.4 → v2.4.1 修订总览

本节按第六轮评审《总体评价》的章节编号逐项登记修改落点。历史修订见 §12 变更日志。

### 0.1 冻结阻断项（评审 §三，3 项）

| 评审编号 | 问题 | v2.4.1 落点 |
|---|---|---|
| 阻断项 1 | A.0 未真正生成 `transfer_sequence`；`gap=0` 实质仍靠「没找到 ward/ED 就当 direct ICU-to-ICU」；final decision 用简单 `COALESCE` 会产生非法 `pending_review` 终态；clean edge 可否人工推翻未规定；`episode_id` 仅 hadm_id 内唯一但多处按 `USING (episode_id)` 连接 | §2.1 C0 与附录 A.0：①`transfer_sequence` 实际生成（有序 JSON 数组），预登记**合法路径类别**（`ICU_A→ICU_B`、`ICU_A→internal_transfer_placeholder→ICU_B`）与**异常路径类别**（经 ward/ED、缺左/右边界、边界冲突、重叠记录、未知单元）——仅合法路径方可 `zero_gap_path_status = clean` 且 `merged`，否则 `pending_review` 或预登记保守 split；②final decision 改**显式 CASE**（四分支 + `final_decision IS NULL ⇒ pipeline failure`）；③冻结裁决范围：**adjudication 仅处理 `pending_review` 边，clean merged 与规则性 split 不允许个案覆盖**——规则错误改规则版本重跑；④`episode_id` 全局唯一（`MIMIC_<hadm_id>_<episode_seq>` + `episode_mapping_version` 随行），测试 `COUNT(*) = COUNT(DISTINCT episode_id)` |
| 阻断项 2 | 源事件序列化示例仍可构造碰撞（值含 `\|`/`=`/`<NULL>` 时歧义、NULL token 冲突、`HASH`/`MD5` 并存、trim 后「exact duplicate」名不副实） | §2.2 C6a 与附录 A.4：改用**真正的 canonical JSON**（冻结 UTF-8、逐表固定字段顺序、显式类型、JSON null、规范浮点格式、标准 JSON escaping、Unicode NFC、**SHA-256**）；`source_event_id = SHA256(canonical_serialized_event)`、`source_event_id_version = eicu_source_event_sha256_v1`；拆双指纹 `raw_row_fingerprint` / `canonical_clinical_fingerprint` 与双计数 `raw_exact_duplicate_count` / `canonical_duplicate_count`，守恒规则为 `SUM(raw_exact_duplicate_count) = 物理源行数`，canonical 折叠逐表留痕；新增序列化 round-trip 自动测试（含恶意边界值） |
| 阻断项 3 | A.2 对 `episode_outtime IS NULL` 实际允许生成至 168h 全部 landmark，与正文「missing_or_open 不得外推」直接冲突 | §3.2 与附录 A.2：主分析 landmark 生成要求 **`episode_outtime_status = 'ok' AND episode_outtime_ts IS NOT NULL AND t_landmark < episode_outtime_ts`**；`missing_or_open` episode 不进主分析 landmark、进 QA、修复后重跑；敏感性分析可选 `episode_landmark_censor_time`（基于 ICU 内临床事件白名单的 `last_clinically_observed_in_icu_time`，与医院级观察终点严格区分） |

### 0.2 重要 P1（评审 §四，7 项）

| # | 问题 | v2.4.1 落点 |
|---|---|---|
| P1-1 | eICU 缺 preliminary/final episode map 与 adjudication 输出，但跨 unit stay 配对依赖 `episode_id` | §2.2 C6a 补建 **eICU episode 四表**（`eicu_episode_edges_preliminary / eicu_episode_map_preliminary / eicu_episode_merge_adjudications / eicu_episode_map_final`）与边级字段清单；路径类别区分 ICU→ICU、ICU→step-down/ward→ICU、ICU→OR/procedure→ICU、跨医院转移、重复 unit stay、offset overlap |
| P1-2 | A.3 的 `LEAST()` NULL 行为、`observation_end_source` 与选定值不一致 | §4.1 与附录 A.3：显式 NULL 分支 CASE；`observation_end_source` 按实际选定值生成；新增**临床观察源白名单**（`source_table / clinical_time_field / time_semantics / eligible_for_last_clinical_observation` 预登记表） |
| P1-3 | 恰好 `t+24h` 出院时 `full_inhospital_followup_24h` 一律 FALSE 与 §3.4 边界规则不一致 | §4.1 与附录 A.3：`non_event_observed` 或 `alive_discharge_time ≥ w_end` 时 `full_inhospital_followup_24h = TRUE` |
| P1-4 | C7 assumed-zero baseline 与 §5.4「delta 两端均完整」冲突 | §2.2 C7/§5.4：`delta_sofa` 物理拆分为 **`delta_sofa_observed_complete`（两端均观测完整、同轨同版本）与 `delta_sofa_phenotype`（qualifying 端完整；baseline 来源 ∈ observed_complete / assumed_zero_by_phenotype_rule）**；Q1 同步更新 |
| P1-5 | `administration_confirmed = structurally_unavailable` 类型语义混用（布尔 vs 可用性状态） | §2.2 C6b：拆为 **`administration_confirmation_availability ∈ {available_validated, available_unvalidated, structurally_unavailable}`** 与事件级 `administration_confirmed ∈ {TRUE, FALSE, NULL}`（结构不可用时为 NULL，`episode_has_administration_confirmed = FALSE`）；汇总报告 `administration_confirmation_structurally_available` |
| P1-6 | SOFA「口径」与「时间轨」命名层级不清 | §5.4 建立**二维命名**：`sofa_purpose ∈ {phenotype_locked, realtime_feature, completeness_qa}` × `sofa_evidence_track ∈ {strict_24h, carryforward}`（含允许组合表）；CV-SOFA≥3 亚组统一为 **`sofa_realtime_strict_24h_cv`** |
| P1-7 | 敏感性 τ_merge（30/60 min）在 A.0 主规则中无参数化实现 | §2.1 C0 与附录 A.0：`episode_merge_threshold_min` **参数化**，不同阈值生成独立版本（`main_tau0 / sensitivity_tau30 / sensitivity_tau60`），不得在同一 final map 中混用 |

### 0.3 测试与冻结清单（评审 §六/§七）

§7.1 Q1 新增 6 组测试（landmark NULL episode end、zero-gap 序列四用例、final decision 状态空间、序列化 round-trip 恶意值、eICU episode map 五条、SOFA delta 分层）；§10 保持 31 项不变，扩充 B-4、B-7、C-7、D-5 通过条件（含 eICU episode map 建立）。

---

## 1. 数据源与本地部署核验

### 1.1 数据库文件清单（实测，沿用 v1.0 核验结论）

| 库 | 文件路径 | 大小 | 引擎 | schema / 表数 | 用途 |
|---|---|---|---|---|---|
| MIMIC-IV v3.1 | `E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb` | ~53 GB | DuckDB | `main`（54）+ `mimiciv_derived`（90） | 模型开发 + 内部时间验证 |
| MIMIC-IV-ECG v1.0 | `E:\clinical_research\MIMIC_IV_3.1\ecg\`（WFDB 文件树）；索引已入库 `main.ecg_records` | — | WFDB | 800,035 份 | 12 导联 ECG 波形 |
| eICU-CRD v2.0 | `E:\clinical_research\eICUdatabase\eicu_crd.duckdb` | ~15 GB | DuckDB | `main`（53） | 临床退化模式外部验证 |

连接方式（全程**只读**，禁止对源库执行任何写操作）：

```python
import duckdb
mimic = duckdb.connect(r"E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb", read_only=True)
eicu  = duckdb.connect(r"E:\clinical_research\eICUdatabase\eicu_crd.duckdb", read_only=True)
```

### 1.2 MIMIC 库关键表（本项目涉及部分）

| 域 | 表 | 粒度 / 关键列 | 实测行数 |
|---|---|---|---|
| 患者 | `main.patients` | `subject_id`；`gender, anchor_age, anchor_year, anchor_year_group, dod` | 364,627 |
| 住院 | `main.admissions` | `hadm_id`；`admittime, dischtime, deathtime, admission_location, discharge_location, hospital_expire_flag` | 546,028 |
| ICU | `main.icustays` | `stay_id`；`first_careunit, last_careunit, intime, outtime, los`（**episode 锚定表**） | 94,458 |
| 转科 | `main.transfers` | `transfer_id`；`eventtype, careunit, intime, outtime`（episode 路径审计 + `transfer_sequence` 构建） | 2,413,581 |
| 脓毒症表型 | `mimiciv_derived.sepsis3` | 每 stay 一行；`subject_id, stay_id, antibiotic_time, culture_time, suspected_infection_time, sofa_time, sofa_score, 六组分`（**无 `sepsis_time`、无 `hadm_id`**） | 41,295 |
| 疑似感染 | `mimiciv_derived.suspicion_of_infection` | 每次抗生素-培养配对一行（**配对回放验证基准**） | 949,901 |
| SOFA（小时级） | `mimiciv_derived.sofa` | `stay_id, starttime/endtime(1h), 组分输入 + 24h 滑动组分`（表型口径） | 8,219,121 |
| 生命体征 | `mimiciv_derived.vitalsign` | `stay_id, charttime`；HR/SBP/DBP/MBP/RR/Temp/SpO2/Glucose | 13,519,533 |
| 生命体征（原始） | `main.chartevents` + `main.d_items` | `itemid, charttime, storetime, valuenum` | ~4.33 亿 |
| GCS | `mimiciv_derived.gcs` | `charttime, gcs, gcs_motor/verbal/eyes, gcs_unable` | 2,217,787 |
| 血管活性药 | `mimiciv_derived.vasoactive_agent` | `stay_id, starttime, endtime, 7 药速率列` | 839,543 |
| NEE | `mimiciv_derived.norepinephrine_equivalent_dose` | `stay_id, starttime, endtime, norepinephrine_equivalent_dose` | 783,613 |
| 单药输注 | `mimiciv_derived.{norepinephrine, epinephrine, dopamine, phenylephrine, vasopressin, dobutamine, milrinone}` | `stay_id, linkorderid, vaso_rate, vaso_amount, starttime, endtime` | — |
| 通气 | `mimiciv_derived.ventilation` | `stay_id, starttime, endtime, ventilation_status` | 144,812 |
| 检验（宽表） | `mimiciv_derived.{bg, chemistry, coagulation, complete_blood_count, ...}` | `stay_id/hadm_id + charttime + 项目列`（多无 `storetime`，仅交叉校验） | — |
| 检验（原始） | `main.labevents` + `main.d_labitems` | `itemid, charttime, storetime, valuenum, valueuom` | 158,374,764 |
| 微生物 | `main.microbiologyevents` | `charttime/chartdate, spec_type_desc, org_name, interpretation` | 3,988,224 |
| 合并症 | `mimiciv_derived.charlson` | `hadm_id`；17 组分（基于本次住院最终 ICD） | 546,028 |
| 体重/身高 | `mimiciv_derived.weight_durations`、`mimiciv_derived.height` | 时段体重；身高 | 401,850 / 43,342 |
| 尿量 | `mimiciv_derived.urine_output` | `stay_id, charttime, urineoutput` | 4,127,634 |
| ICU 汇总 | `mimiciv_derived.icustay_detail` | `stay_id`；年龄、性别、入出院时间、结局、序次 | 94,458 |
| 结局汇总 | `mimiciv_derived.patient_outcomes` | `stay_id`；死亡、SOFA/SOFA-2、通气、RRT 等 73 列 | 94,458 |
| ECG 索引 | `main.ecg_records` | `subject_id, study_id, ecg_time, path` | 800,035 |
| ECG 机测 | `main.ecg_measurements` | `study_id, ecg_time, RR/间期/电轴` | 800,035 |

> 注：`mimiciv_derived` 同时含 SOFA-2 系列表（`sofa2_*`），本项目仅用 **SOFA-1**（风险 R6）。派生表 mimic-code 版本、commit hash、SQL/R 清单与本地修改须在阶段 A 登记（冻结清单 A-4）。

### 1.3 eICU 库关键表（本项目涉及部分）

| 域 | 表 | 粒度 / 关键列 | 实测行数 |
|---|---|---|---|
| ICU 入住 | `main.patient` | `patientunitstayid`；三级 ID、年龄（VARCHAR，含 `"> 89"`）、入出院 offset 与状态、身高体重 | 200,859 |
| 医院 | `main.hospital` | `hospitalid`；床位数、教学状态、region | 208 |
| ICU 汇总 | `main.icustay_detail` | `patientunitstayid`；`hosp_mort, icu_los_hours, apache_iv, region` 等 | 200,859 |
| 生命体征（监护仪） | `main.vital_periodic` | `observationoffset`；HR/RR/SpO2/有创血压/Temp 等 | 146,671,642 |
| 生命体征（非周期） | `main.vital_aperiodic` | `observationoffset`；无创/有创血压、CO/CI/SVR | 25,075,074 |
| 生命体征（护理宽表） | `main.pivoted_vital` | `chartoffset`；HR/RR/SpO2/NIBP/IBP/Temp | 21,038,216 |
| 检验（原始） | `main.lab` | `labresultoffset, labname, labresult, labresultrevisedoffset`（语义审计见 §5.3） | 39,132,531 |
| 检验（宽表） | `main.pivoted_lab` | `chartoffset`；22 项 | 5,314,163 |
| 血气 | `main.pivoted_bg` | `chartoffset`；`fio2`（0–1 量纲）、`pao2, paco2, pH` | 1,464,012 |
| GCS | `main.pivoted_gcs` / `main.pivoted_score` | `chartoffset`；`gcs_unable, gcs_intub` | 3,451,788 / 5,709,678 |
| 输注药（原始） | `main.infusion_drug` | `infusionoffset, drugname(内嵌单位), drugrate, infusionrate, drugamount, patientweight` | 4,803,719 |
| 输注标记 | `main.pivoted_infusion` | `chartoffset`；8 药 0/1 标记（无剂量） | 1,083,074 |
| 用药医嘱 | `main.medication` | `drugorderoffset, drugstartoffset, drugstopoffset, drugname, routeadmin` | 7,301,853 |
| 微生物 | `main.micro_lab` | `culturetakenoffset, culturesite, organism` | 16,996（仅 2,923 患者） |
| 尿量 | `main.pivoted_uo` | `chartoffset, urineoutput` | 4,088,881 |
| 体重 | `main.pivoted_weight` | `chartoffset, source_table, weight_type, weight` | 501,506 |
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath`（时间语义审计见 §2.2 C7） | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring` | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部原始事件时间为**相对各 unit stay 入科的分钟偏移（offset）**；出院年份仅 2014/2015，**无绝对日期**。多数 eICU 原始表**无稳定单列事件主键**，事件标识按 §2.2 C6a 规范化规则（canonical JSON + SHA-256）生成。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C0–C5）

- **C0 连续 ICU episode 映射（两阶段 + zero-gap 序列核验 + 显式 final decision + 全局唯一 ID，评审阻断项 1/P1-7）**：

  **合并判定三规则（主值 `τ_merge = 0 min`，参数化实现）**：以 `main.icustays` 为锚，同一 `hadm_id` 按 `intime, stay_id` 排序，`gap_minutes = EPOCH(intime(j+1) − outtime(j)) / 60`。①`gap_minutes ≤ episode_merge_threshold_min`（主值 0；敏感性 30/60 经参数化生成**独立版本** `main_tau0 / sensitivity_tau30 / sensitivity_tau60`，不得在同一 final map 混用）；②`gap < 0` 不自动合并（`pending_review`）；③存在病房/ED 区间不可合并。

  **zero-gap 路径核验（进入正式 DAG，实际生成 `transfer_sequence`）**：每条 `gap = 0` 的边构建有序 transfer 路径（JSON 数组：`{transfer_id, eventtype, careunit, intime, outtime, relative_position}`），并按**预登记路径类别**判定：

  ```text
  合法路径（zero_gap_path_status = clean，允许 preliminary_decision = merged）：
    ICU_A → ICU_B
    ICU_A → internal_transfer_placeholder → ICU_B
  异常路径（pending_review 或预登记保守 split）：
    ICU_A → ward → ICU_B        ICU_A → ED → ICU_B
    missing_left_boundary       missing_right_boundary
    multiple_conflicting_boundary_events
    overlapping_transfer_records
    unknown_careunit
  ```

  仅「找不到 ward/ED」**不再**足以判 clean——必须命中明确合法模式（Q1 zero-gap 四用例测试）。

  **两阶段输出（物理分离，同 v2.4）**：`mimic_icu_episode_edges_preliminary`（边级：含 `transfer_sequence / zero_gap_path_status / left_adjacent_transfer_id / right_adjacent_transfer_id / preliminary_decision / episode_merge_exclusion_reason`）→ `mimic_icu_episode_map_preliminary`（仅 merged 延续，其余保守拆分）→ `episode_merge_adjudications`（以 edge 为单位）→ `mimic_icu_episode_map_final`。

  **final decision 显式 CASE（冻结）**：

  ```text
  preliminary pending_review + adjudicated merged  → merged
  preliminary pending_review + adjudicated split   → split
  preliminary pending_review（未裁决）              → split + unresolved_conservative_split = TRUE
  preliminary merged / split                        → 原值
  其他                                              → NULL ⇒ pipeline failure
  final_decision ∈ {merged, split}；禁止 pending_review / NULL / unknown
  ```

  **裁决范围（冻结，评审 §三.1.3 推荐方案）**：adjudication **仅处理 `pending_review` 边**；clean merged 与规则性 split **不允许个案覆盖**——发现规则错误时修改 `episode_mapping_version` 规则并重跑，不做个案例外。

  **全局唯一 episode_id（评审 §三.1.4）**：

  ```text
  episode_id = 'MIMIC_' || hadm_id || '_' || episode_seq      -- 全库唯一
  每行携带 episode_mapping_version（main_tau0 / sensitivity_*）
  测试：final map 中 COUNT(*) = COUNT(DISTINCT episode_id)；
        所有下游连接以 (episode_id, episode_mapping_version) 为键
  ```

  **final map 字段**（override 后重新聚合）：`subject_id, hadm_id, episode_id, episode_mapping_version, stay_id, stay_seq_in_episode, episode_intime_ts, episode_outtime_ts, episode_outtime_status（ok / missing_or_open）, episode_has_null_stay_outtime, gap_minutes, merge_reason, overlap_flag, intervening_careunit, transfer_evidence, episode_merge_decision（final）, episode_merge_exclusion_reason, episode_mapping_status（clean / adjudicated / unresolved_conservative_split）, unresolved_conservative_split, episode_merge_threshold_min, episode_gap_max_min, episode_transfer_path_class`。约束：每 `stay_id` 恰好一个 `episode_id`；任一组成 stay `outtime IS NULL` ⇒ `episode_outtime_status = missing_or_open`（进 QA，不外推，§3.2）。

- **C1 脓毒症相关 episode 池（基于 final map）**：`mimiciv_derived.sepsis3` 经 `main.icustays` 回填 `hadm_id`，按 final episode 归属，同一 episode 多命中 stay 先聚合为 `mimic_episode_sepsis`（`episode_id, qualifying_sepsis_count, t_sepsis_ts, t_sepsis_source_stay_id, t_sepsis_selection_rule, t_sepsis_status`；`missing` 在 eligible 阶段排除并写 QA）。

- **C2 入排初筛**：年龄 ≥18（源 stay 的 `admission_age`）；成人 ICU（episode 首 stay `first_careunit`，类别清单 QA 实测后预登记）。

- **C3 index episode 选择**：全部合格 episode 按 `subject_id` 取首次，排序键 `t_sepsis_ts NULLS LAST, admittime, episode_intime_ts, episode_id`。`first_icu_stay` 仅描述。

- **C4 探索性/敏感性标志**：外院转入、landmark 前 ECMO、近 90 天实体器官移植、landmark 前 DNR/CCO——**PPV 抽查前不作正式排除**；ICD 类标志仅用既往住院记录。

- **C5 队列事实表** `cohort_mimic_v2`（每 final episode 一行）：`subject_id, hadm_id, episode_id, episode_mapping_version, t_sepsis_source_stay_id, t_sepsis_ts（D0 锁定后生效）, episode_intime_ts, episode_outtime_ts, episode_outtime_status, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C10）

- **C6a 住院级统一时间坐标 + eICU episode 四表 + 规范化事件标识（评审 P1-1/阻断项 2）**：

  ```text
  t_hospital_min = -hospitaladmitoffset + eventoffset
  episode_offset_min = hospital_offset_min - episode_start_hospital_min
  ```

  **eICU episode 两阶段输出（评审 P1-1 补建）**：

  ```text
  eicu_episode_edges_preliminary            -- 边级（同一 patienthealthsystemstayid 内相邻 stay）
  - patienthealthsystemstayid
  - previous_patientunitstayid, current_patientunitstayid
  - previous_unit_end_hospital_min, current_unit_start_hospital_min
  - gap_minutes, overlap_flag
  - unitdischargelocation, unitadmitlocation, unitstaytype
  - edge_path_class            -- icu_to_icu / icu_to_stepdown_or_ward_to_icu /
                               --   icu_to_or_procedure_to_icu / cross_hospital_transfer /
                               --   duplicate_unit_stay / offset_overlap
  - preliminary_decision       -- merged / split / pending_review

  eicu_episode_map_preliminary / eicu_episode_merge_adjudications / eicu_episode_map_final
  ```

  合并主规则：同一 `patienthealthsystemstayid` 内 `gap ≤ τ_merge_eicu = 0 min` 且 `edge_path_class = icu_to_icu` 者合并；经 ward/step-down、OR/procedure、跨医院转移、重复 stay、offset 重叠者按预登记规则 split 或 pending_review（裁决范围同 MIMIC：仅 pending 边可人工裁决）。**若项目最终决定「同一 hospital stay 内全部 unit stay 无条件组成一个 hospital episode」，须在此显式写明并登记为规则版本，不得笼统表述为「同 MIMIC 机制」。** final map 约束：每 `patientunitstayid` 恰好一个 final `episode_id`；同一 final episode 不得跨 `patienthealthsystemstayid`；episode hospital-time 区间单调（Q1 eICU episode 五条测试）。

  **规范化事件标识（canonical JSON + SHA-256，评审阻断项 2）**：

  ```text
  source_event_id = SHA256(canonical_serialized_event)
  source_event_id_version = eicu_source_event_sha256_v1
  canonical serialization 冻结规范：
    编码 UTF-8；逐表固定字段顺序（schema 冻结）；显式类型；
    NULL → JSON null；布尔 → true/false；offset → 十进制整数；
    浮点 → 预登记规范十进制表示；字符串 → 标准 JSON escaping；
    Unicode → NFC；哈希 → SHA-256
  ```

  **双指纹与双计数**：`raw_row_fingerprint`（原始值，仅排除数据库加载元数据）与 `canonical_clinical_fingerprint`（允许 trim、大小写统一、单位文本规范化）；对应 `raw_exact_duplicate_count` 与 `canonical_duplicate_count`。守恒规则：**`SUM(raw_exact_duplicate_count) = 物理源行数`**；canonical 折叠规则逐表预登记并留痕。版本字段：`source_event_id_version / canonicalization_rule_version / source_snapshot_checksum`。

  输出桥接表：`eicu_unitstay_timeline`；`eicu_event_time_map`（泛化，含双指纹/双计数/版本字段）；专用桥接表 `eicu_medication_time_map / eicu_microbiology_time_map / eicu_lab_time_map / eicu_infusion_time_map`（normalized event 经 `source_event_id` 一对一回连）。

- **C6b 统一抗生素事件表 + suspected infection 重建（评审 P1-5）**：

  **administration confirmation 双字段（评审 P1-5）**：

  ```text
  administration_confirmation_availability:
    available_validated         -- 来源存在且验证规则通过
    available_unvalidated       -- 来源存在但未完成验证
    structurally_unavailable    -- eICU 无可靠 MAR 级确认来源
  事件级 administration_confirmed ∈ {TRUE, FALSE, NULL}
    -- availability = structurally_unavailable 时一律 NULL；
    -- 不得把「无确认给药事件」与「数据库无确认来源」混为一谈
  episode 级：episode_has_administration_confirmed（structurally_unavailable 时为 FALSE）
  汇总报告：administration_confirmation_structurally_available（TRUE/FALSE）
  ```

  阶段 A 明确 `administration_confirmed_source_table / _source_field / validation_rule` 后确定 availability；若为 `structurally_unavailable`，正式门槛明确为 `Pr(infusion_recorded 或 validated administration) ≥ 30%`（episode 级联合覆盖）。

  **统一抗生素事件表（五步构建，同 v2.4）**：①`infusion_drug` → `infusion_recorded`；②`medication` → `scheduled_start / order_time`；③同药相近时间去重；④四级来源互斥赋值（每事件仅最高优先级一级，总和 100%）；⑤输出 `eicu_antibiotic_events`（含双指纹字段）。

  **episode 级联合可靠覆盖（不变）**：`episode_has_administration_confirmed / episode_has_infusion_recorded / episode_has_scheduled_start_only / episode_has_order_time_only / episode_has_reliable_antibiotic_time（= 前二者 OR）/ selected_antibiotic_time_source`；汇总入 `eicu_antibiotic_time_source_summary`（track × hospital × denominator）；**Go/No-Go 正式门槛 `antibiotic_time_source_coverage_rate_episode_level ≥ 30%`**。

  抗生素事件与培养事件（`eicu_culture_events`）在 episode 坐标按 `episode_id` 配对——**仅生成候选 pair**；跨 unit stay 候选必须属于同一 **final** episode（Q1 eICU episode 测试）。

- **C7 三套可行性表型队列 + 表型时间合同（双层结构 + 冻结候选规则表）**：

  **第一层：固定合同字段**（结构冻结）：

  ```text
  phenotype_event
  - episode_id（final）
  - infection_evidence_time / infection_evidence_type
  - sofa_baseline_window_start / sofa_baseline_window_end
  - sofa_qualifying_window_start / sofa_qualifying_window_end
  - baseline_sofa_value
  - baseline_sofa_source              -- observed_complete / assumed_zero_by_phenotype_rule / unavailable
  - baseline_sofa_complete_observed
  - baseline_assumed_zero
  - qualifying_sofa
  - delta_sofa_observed_complete      -- 两端均观测完整、同轨同版本时才有值（评审 P1-4）
  - delta_sofa_phenotype              -- qualifying 端完整；baseline 来源 ∈
                                      --   {observed_complete, assumed_zero_by_phenotype_rule}
  - sofa_qualifying_time
  - t_sepsis_offset_min / t_sepsis_rule / phenotype_track
  - infection_pair_id
  - diagnosis_time / diagnosis_time_semantics / diagnosis_time_confidence
  ```

  **诊断时间语义（不变）**：`observed_record_time / assigned_admission_proxy / retrospective_only / unknown`；`admission_dx` = 入院时刻代理；`diagnosisoffset` 语义专项审计；审计关闭纳入冻结清单 A-5。

  **第二层：冻结候选规则表（PI 逐项签署后升级为正式锁定规则表）**：

  > **✅ PI 签署记录（2026-07-30，A-5）**：下列规则表经 PI 逐项确认，升级为正式锁定规则表。签署要点：①**P-clinical 为主外验表型**，`assumed_zero_by_phenotype_rule` baseline 接受（实测 98.9% 基线为 assumed-zero，作为表型假设写入论文局限性），**同时报告 `delta_sofa_observed_complete` 子集敏感性**；②**P-explicit 为第二外验**（诊断驱动稳健性对照）；③**P-strict 降为 feasibility_only**（实测 198 患者/12 医院，不达 Go/No-Go）；④回顾性表型确认的 estimand 声明接受（回顾性确认的 sepsis episode，不代表 t_I 实时识别，同步写入论文局限性）；⑤**外验命名锁定为 Robustness under phenotype shift**，禁止依据 eICU 表现反向选择表型或变量集；⑥诊断时间语义审计完成（`qa/eicu_diagnosis_time_semantics_qa.md`）：`admission_dx`=`assigned_admission_proxy`、`diagnosisoffset`=`observed_record_time`、>72h sepsis 诊断=`retrospective_only` 不进 strict 轨。

  | 参数 | P-strict | P-clinical | P-explicit |
  |---|---|---|---|
  | 定位 | 严格 Sepsis-3 复现（`sofa_phenotype_locked`） | 临床感染证据 + 器官功能障碍（回顾性临床表型） | **显式临床诊断表型**（回顾性；不与 P-strict 等价） |
  | 感染证据 | C6b 配对（经锁定函数选中的 pair） | 感染诊断证据（`admission_dx` 与 `later_dx` 分开） | 显式 sepsis / severe sepsis / septic shock 诊断字符串（清单预登记） |
  | 感染时间 t_I | 配对两事件中较早者（随锁定版 mimic-code） | 首个感染诊断时间（`admission_dx` 入院时刻代理；`later_dx` 按语义分层） | 首个显式 sepsis 诊断证据时间；`t_sepsis_rule = 'first_explicit_sepsis_dx_evidence_time'`（仅 `observed_record_time` 且经审计后方可以 `available_time` 命名） |
  | SOFA 基线窗口 | 完全复现锁定版 mimic-code（含缺失假设） | `[t_I − 48h, t_I − 24h]` 末次完整 6/6 SOFA；无先前完整 SOFA 时 `baseline_sofa_source = assumed_zero_by_phenotype_rule`、baseline = 0（**表型假设，非完整性事实**；敏感性排除） | 不适用（描述性） |
  | SOFA 合格窗口 | 同锁定代码 | `[t_I − 24h, t_I + 48h]` 内 `delta_sofa_phenotype ≥ 2`；**qualifying 端必须完整 6/6**；`delta_sofa_observed_complete` 仅 observed 子组报告，两类分层 | 不适用 |
  | ΔSOFA ≥2 | 必须 | 必须 | 不必须 |
  | t_sepsis 规则 | 同锁定代码 | `t_sepsis = t_I`，资格由合格窗口 ΔSOFA ≥2 确认；`t_sepsis_rule = 'infection_evidence_time_with_qualifying_delta_sofa'` | `t_sepsis = t_I`（同上改名规则） |

  **estimand 声明（不变）**：P-clinical 的队列成员资格由 `t_I` 后窗口**回顾性确认**，仅用于 phenotype ascertainment，不进任何 landmark 特征；外验针对「回顾性确认的 sepsis episode」，不代表 `t_I` 时实时识别。同步写入论文局限性。

  **P-clinical 前向算法（不变）**：①按 episode 时间升序排列候选证据；②逐 `t_I` 在固定窗口搜索 ΔSOFA ≥2；③首个满足者生成 `t_sepsis = t_I`；④禁止最终出院诊断反推；⑤诊断作为证据时 `t_I = diagnosis_time`。

  **候选 pair 与锁定选对函数（不变）**：附录 A.5 仅为 candidate generation template；最终选对由 `select_suspected_infection_pairs_locked_v1`（引用具体 mimic-code commit、逐条排序键、七类情形）完成；**MIMIC 回放验证**（pair/event/infection_time 三级一致率，标准预登记，冻结清单 B-5）。

  三套队列分别报告：患者数、医院数、院内死亡数、各 landmark 阳性数、SC-common 特征覆盖率、与 MIMIC 主队列基线差异。

  **Go/No-Go 门槛（确定建议值；PI 确认后预登记，禁止按模型效果反向调整）**：

  | 指标 | 阈值 | 说明 |
  |---|---|---|
  | P-strict 覆盖医院数 | ≥ 20 家，且最大单医院患者占比 ≤ 25% | 避免单中心主导 |
  | 患者数 | P-strict ≥ 500；P-clinical / P-explicit ≥ 2,000 | 外验最低规模 |
  | 院内死亡事件数 | ≥ 100 | 月 1 样本量分析复核 |
  | 主要 landmark 可估计比例 | 12 个中满足「阳性 ≥20 且阴性 ≥100」者 ≥ 10 个 | 技术文档 §5.1 |
  | 培养覆盖率 | P-strict ≥ 5% 候选 ICU episodes | 当前实测约 1.5% |
  | 给药时间可靠率（正式门槛） | **`antibiotic_time_source_coverage_rate_episode_level ≥ 30%`**（联合指标） | `structurally_unavailable` 时明确为 `Pr(infusion_recorded 或 validated administration)` |
  | SOFA 可计算率（分列） | 首个有效 landmark 处：6/6 完整总分率 ≥ 60% 且 ≥5/6 组分可计算率 ≥ 70% | 两率分别报告；strict_24h 与 carry-forward 口径再各自分列 |

  **外验命名决策（建模前锁定）**：默认预期 **Robustness under phenotype shift**；不得依据 eICU AUROC 反向选择表型。

- **C8 入排与 index episode**：年龄 ≥18（`"> 89"` 记 90 并打标）；同一 `uniquepid` 按 `t_sepsis_offset_min NULLS LAST, hospitaladmitoffset, episode_start_hospital_min, episode_id` 确定性排序取首次。

- **C9/C10 队列事实表** `cohort_eicu_v2`（与 C5 同构，episode 坐标分钟；只含 episode 级字段）：`episode_id, index_patientunitstayid, patienthealthsystemstayid, uniquepid, t_sepsis_offset_min（C7 锁定后生效）, episode_start_offset_min(=0), episode_end_offset_min, hospitaladmitoffset, hospital_discharge_episode_min, hospitaldischargestatus, hospitaldischargelocation, age_num, gender, unittype, hospitalid, phenotype_track, episode_has_administration_confirmed, episode_has_infusion_recorded, episode_has_scheduled_start_only, episode_has_order_time_only, episode_has_reliable_antibiotic_time, selected_antibiotic_time_source, 敏感性标志`。

### 2.3 两库队列字段同构约定与命名规范

两库队列事实表输出**同名同义列**；下游一律按「相对 t_sepsis 的小时差」对齐，禁止直接比较两库原始时间列。

**时间字段命名规范（强制执行）**：

| 语义 | MIMIC（TIMESTAMP，年份偏移） | eICU（INTEGER 分钟，episode 坐标） |
|---|---|---|
| episode 起点 | `episode_intime_ts` | `episode_start_offset_min`（恒 0） |
| episode 终点 | `episode_outtime_ts` | `episode_end_offset_min` |
| sepsis 原点 | `t_sepsis_ts` | `t_sepsis_offset_min` |
| landmark | `t_landmark_ts` | `t_landmark_offset_min` |
| 规范化相对时间 | `hours_since_sepsis`（两库同名同义） | 同左 |

历史别名一律映射到上表。所有下游连接以 `(episode_id, episode_mapping_version)` 为键（MIMIC 侧全局唯一 `episode_id`，§2.1 C0）。eICU 凡涉及结局与标签的时间一律先转换为 `*_episode_min`（§4.1）。

### 2.4 内部时间划分（技术文档 §12.2 落地）

实测 `anchor_year_group` 为 5 类，映射固定（人数为**全库 `patients` 表人数**，队列口径数字由阶段 B 产出）：

| 集合 | anchor_year_group | 全库 patients 表人数（参考） |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873 |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 须经阶段 A 正式 amendment：排除理由、**完全不查看结局与模型性能**、是否仅保留为扩展数据（风险 R2）。划分按 `subject_id`；`split_assignments_v2` 落盘冻结。**独立 calibration 集当前不单独划分**；未来启用 CP 探索需先在本文档与划分表中显式定义。

**Amendment 记录（2026-07-30，选项 A：正式排除）**：`2020 - 2022` 年组（全库 49,173 subjects；队列口径 3,341 index episodes，占 10.5%）**不进入任何 train/validation/test 索引，完全不查看结局与模型性能**。排除理由：①技术文档 v1.9 预注册按四年组（2008–2019）设计，2020–2022 无预注册角色；②该年组覆盖 COVID 时期，诊疗模式与死亡分布存在不可校正的时期效应；③测试集（2017–2019）样本量已满足时间组外验证。**不保留为扩展数据**；未来若启用（如 2020+ 时期泛化补充分析）须另行签署独立 amendment 后方可查看结局。实现状态：`split_assignments_v2` 已按 `excluded_amendment_pending` 隔离，无任何管线改动。

> 对外表述规范：称为「**基于 anchor_year_group 的时间组外验证**」，不得过度解释为精确日历年份上的时间外验证。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time —— 决策门 D0（✅ 已锁定：出口 B，2026-07-30）

**当前状态：t_sepsis 已锁定为 `suspected_infection_time`（D0 出口 B，PI 批准 2026-07-30）。** 本地 `mimiciv_derived.sepsis3` 不含技术文档 §4.1 规定的 `sepsis_time`（实有 `suspected_infection_time` 与 `sofa_time`）。

**D0 审计结论（2026-07-30，`qa/derived_provenance_v2.md`）**：锁定版 mimic-code（commit `a0af19c18a66b6d96935058ebfa830608989bd7c`，2026-07-04 master）的 MIMIC-IV `sepsis3` 概念**本身不输出 `sepsis_time` 字段**；实测 `sofa_time − suspected_infection_time` 分布 [-48h, +24h]（78.5% 感染疑似更早、19.4% SOFA 更早、6.6% SOFA 早 ≥24h）。

**Amendment 记录（出口 B）**：主时间原点正式定为 `suspected_infection_time`（mimic-code 的 suspected infection 代理时间，即 qualifying 抗生素—培养配对中的培养时间）。**敏感性轨**（技术文档 §4.1/§15.2 三口径）：① 主口径 `suspected_infection_time`；② `max(sofa_time, suspected_infection_time)`（两要件齐备时刻，SOFA≥2 晚于感染疑似的 78.5% 情形即 sofa_time）；③ `icu_admission`。三口径须以独立 run_id 分别产出，不混用。

**D0 前置审计（阶段 A）**：①定位 `sepsis3` 生成 SQL/R 脚本；②记录 mimic-code 版本、commit hash、原始 SQL、本地修改；③明确 `sofa_time` 与 `suspected_infection_time` 生成逻辑；④确认 `sepsis_time` 的应有对应（同步锁定配对函数 `select_suspected_infection_pairs_locked_v1` 的参照实现，§2.2 C7）。

**D0 两个合法出口（PI 确认后二选一，最终只能一个主口径）**：

- **出口 A**：重新生成符合预注册定义的 `sepsis_time`（技术文档不变）；
- **出口 B**：protocol amendment 将主原点正式改为 `suspected_infection_time` 或明确的合成时间。

**明确禁止**：代码层用 `suspected_infection_time` 而文档层主原点仍写 `sepsis_time`。

**D0 输出固定 schema**（`_meta/d0_decision.json`）：

```json
{
  "primary_time_origin": "...",
  "secondary_time_origins": ["..."],
  "source_table": "...",
  "source_code_commit": "...",
  "protocol_amendment_required": true,
  "pi_approval_date": "..."
}
```

**锁定前许可范围**：仅结构审计、可行性统计与原型提取；禁止正式训练、超参数选择与测试集评估。

eICU 侧：`t_sepsis_offset_min` 由 C7 合同按 `t_sepsis_rule` 合成，与 D0 一致性登记；P-clinical/P-explicit 按 `diagnosis_time_semantics` 分层解释，不作严格实时解释。敏感性分析保留三种时间原点对比（技术文档 §4.1/§15.2）。

`Δ_ICU-sepsis = episode_intime − t_sepsis`（eICU 为 `0 − t_sepsis_offset_min`），显式输入特征。

### 3.2 Landmark 生成（DAG 节点 L1；NULL episode end 处理修正，评审阻断项 3）

对每个 index episode：

1. `k0 = max(0, ceil((episode_intime − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_offset_min) / 360min))`。
2. `t_landmark(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. **终止规则（修正）**：主分析仅对 `episode_outtime_status = 'ok' AND episode_outtime_ts IS NOT NULL` 的 episode 生成 landmark，`t_landmark(k) < min(episode_outtime_ts, 死亡时间)`。**`episode_outtime_status = missing_or_open` 的 episode 不进入主分析 landmark 生成**（禁止按 `outtime IS NULL` 外推至 168h），进 QA，经人工或规则修复出院时间后重跑。敏感性分析可选 `episode_landmark_censor_time`（基于 ICU 内临床事件白名单的 `last_clinically_observed_in_icu_time`——与医院级 `last_clinically_observed_time` 严格区分，病房内后续检验不得用于证明仍在 ICU）与 `episode_landmark_censor_source`，此时 `t_landmark < episode_landmark_censor_time`（Q1 测试）。
4. ICU 转出至病房后停止生成新 landmark，已生成 landmark 的 24h 随访继续完成。主分析积分网格固定 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v2`：`episode_key, subject_key, k, t_landmark_ts / t_landmark_offset_min, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2）

landmark t 纳入：t 时刻存活且仍处于连续 ICU episode 内。排除：t 前或 t 时刻已死亡（MIMIC `deathtime ≤ t`；eICU `Expired 且 death_episode_min ≤ t_landmark_offset_min`）；t 前或 t 时刻 episode 已结束。

### 3.4 边界条件（全部转化为单元测试）

| 情形 | 判定 |
|---|---|
| landmark 时刻恰好死亡 | 不进入风险集 |
| landmark 时刻恰好 episode 结束 | 不进入风险集 |
| 死亡发生在 `(t, t+24h]` | 阳性（含恰好 `t+24h`） |
| 出院恰好发生在 `t+24h` | 按存活至窗口终点（阴性，存活出院；`full_inhospital_followup_24h = TRUE`，§4.1） |
| ECG 恰好在 landmark 时刻完成采集 | 允许使用（`ecg_available_time_assumed ≤ t_landmark`） |
| 特征恰好在 landmark 时刻可获得 | 允许使用（`available_time ≤ t_landmark`） |
| 死亡时间早于 admittime 或晚于 dischtime 且无院内死亡标志 | 时间异常，进 QA |
| `hospital_expire_flag = 1` 且 `deathtime` 缺失 | `unknown / death_time_missing`（§4.1） |
| `deathtime` 非空且 `hospital_expire_flag = 0` | `unknown / status_conflict`，待 adjudication |
| 标签脚本独立运行时遇到 `deathtime ≤ t_landmark` | `invalid_input` |
| `last_clinically_observed_time < t+24h` 且无明确存活出院 | `unknown / missing_status_left_observation`（§4.1） |
| 临床事件时间 > `dischtime` | 时间异常 QA，**不得**延长临床观察期（§4.1） |
| 组成 stay `outtime IS NULL` | `episode_outtime_status = missing_or_open`；**不生成主分析 landmark**（§3.2） |

---

## 4. 结局标签（DAG 节点 L3）

### 4.1 主结局：landmark 后 24h 院内全因死亡（状态机 + 临床观察终点 + adjudication 分离）

**标签字段（两库同构）**：

```text
y_24h            : 1 / 0 / NULL
label_status     : event / non_event / unknown
outcome_ascertainable      : TRUE / FALSE   -- 主分析纳入依据
full_inhospital_followup_24h : TRUE / FALSE -- 见下方边界定义
outcome_unknown_reason : NULL / acute_transfer / missing_status_left_observation
                         / death_time_missing / status_conflict / time_anomaly / invalid_input
label_reason     : 状态机分支标识
last_clinically_observed_time   -- 可证明患者临床在院的最晚事件/采集时间
last_database_available_time    -- 含 store/result/revision 的最晚数据库时间（仅 QA）
observation_end_source          -- discharge / clinical_event / unknown（与实际选定值一致）
```

**两类观察终点严格区分（不变）**：`last_clinically_observed_time` 只使用事件发生/采集时间；`last_database_available_time` 含 store/result/revision，仅 QA。临床事件时间 > `dischtime` 进时间异常 QA，不得延长观察期。eICU：`unitdischargeoffset` ≠ 医院出院；转病房后 24h 结局继续观察；终点优先 `hospitaldischargeoffset` 与状态。

**临床观察源白名单（新增，冻结时锁定，评审 P1-2）**：

| 来源 | 可使用时间字段 | 可证明临床在院 |
|---|---|---|
| `chartevents` | `charttime` | 是（itemid 白名单） |
| `labevents` | specimen/chart time | 是（需确认采样语义） |
| `labevents` | `storetime` | **否** |
| 输注记录 | actual start/end | 是 |
| ICD/诊断抽象 | coding time | **否** |
| 账务记录 | transaction time | **否** |
| 微生物最终结果 | final result time | **否** |
| 微生物采样 | specimen collection time | 是 |

每源登记 `source_table / clinical_time_field / time_semantics / eligible_for_last_clinical_observation`，防止「只用事件/采集时间」原则被不同脚本解释不一致。

**`full_inhospital_followup_24h` 边界定义（评审 P1-3）**：`label_state = 'non_event_observed'` **或**（`non_event_alive_discharge` 且 `alive_discharge_time ≥ w_end`）时为 **TRUE**——恰好在 `t+24h` 出院者已完成整个 24h 院内观察区间（与 §3.4 边界规则一致）；其余为 FALSE。

**状态机（按序执行，首个命中分支生效；附录 A.3 同构实现）**：

-1. **非法输入防护**：`deathtime ≤ t_landmark` → `invalid_input`；
0. **死亡状态冲突预检**：`deathtime` 非空 `AND hospital_expire_flag = 0` → `unknown / status_conflict`；`hospital_expire_flag = 1 AND deathtime IS NULL` → `unknown / death_time_missing`；
1. `(t, t+24h]` 内院内死亡 → `y_24h = 1`（event）；
2. `(t, t+24h]` 内急性转出 → `NULL`（unknown，`acute_transfer`）；
3. **`(t, t+24h]` 内明确存活出院** → `y_24h = 0`（non_event，`non_event_alive_discharge`）；
4. **`last_clinically_observed_time ≥ t+24h` 且未死亡** → `y_24h = 0`（non_event，`non_event_observed`）；
5. 其余 → `NULL`（unknown，`missing_status_left_observation`）。

> 反例固化（Q1 测试）：`t+12h` 存活出院 + `t+30h` 延迟 storetime → `non_event_alive_discharge`、`full_inhospital_followup_24h = FALSE`。

**人工 adjudication（与自动提取分离）**：冲突与缺失记录 QA 复核写入 `label_adjudications`（`label_preliminary_status / label_final_status / label_adjudication_status / label_adjudication_source`）；仅 `adjudicated` 时覆盖，覆盖比例进 QA。

**派生字段口径**：`acute_transfer_time` 与 `alive_discharge_time` 由 `dischtime + discharge_location` 分类派生，**XOR 互斥**；类别清单两库分别实测后预登记（风险 R9）。

**eICU 统一坐标**：`hospital_discharge_episode_min / death_episode_min`；**所有标签代码只使用 `*_episode_min`**。`hospitaldischargestatus` NULL 者按分支 5 以 `last_clinically_observed_time`（episode 坐标）判定。

### 4.2 次要结局：7 天竞争风险（四类事件）

```text
event_type: 0 = administrative censoring / 1 = in-hospital death /
            2 = alive discharge / 3 = transfer to another acute hospital
```

同时刻优先级：死亡 > 急性转出 > 存活出院 > 删失。急性转出事件不足时按技术文档 §15.2 降级。eICU 一律 `*_episode_min`。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（`sofa_realtime_strict_24h` 完整总分增加 ≥2）、新启用血管活性药（NEE 流由 0 转 >0）。

---

## 5. 特征提取模块

### 5.0 数据可用时间契约（双轨报告）

每条原始记录携带 `event_time / available_time / source_time_type`。**主分析断言：`available_time ≤ t_landmark`**。

| 数据域 | available_time 口径 | source_time_type 取值 |
|---|---|---|
| 床旁连续生命体征（监护仪自动导入） | 测量/观察时间 | `measured` |
| 生命体征（护理人工录入） | 优先 `storetime`；无法确认时降级 | `entry_verified` / `charttime_fallback` |
| 检验 | 结果可用时间优先 | `result_available` / `charttime_fallback` |
| 药物输注 | 实际 start/end time | `infusion_actual` / `order_time_only` |
| 微生物 | 初步/最终结果各自可用时间 | `preliminary` / `final` |
| ECG | **采集完成时间**（`ecg_available_time_assumed`，§5.8） | `acquired` |
| 诊断 | 按 `diagnosis_time_semantics` 分层（§2.2 C7） | `observed_record_time` / `assigned_admission_proxy` / `retrospective_only` |
| 治疗限制 | 实际记录/生效时间 | `order_effective` |

**双轨结果报告**：`strict_available_time`（主分析）与 `chart_or_event_time`（回顾性敏感性）分别报告；无法获得真实可用时间的域不得并入严格实时主模型，论文明确为 **retrospective chart-time prediction**。`diagnosis_time_semantics = retrospective_only` 的证据不得进入 strict 轨道（Q1 测试）。

**聚合记录时间字段**：`bin_start, bin_end, n_source_records, min_event_time, max_event_time, max_available_time, aggregation_method, source_table_set`。防泄漏双保险：聚合前先过滤 `available_time > t_landmark`；聚合后断言 `max_available_time ≤ t_landmark`。

统一时间语义（不变）：landmark 前 24h 的 1h 网格、同小时中位数、缺失保留 + mask + Δt；t=0 landmark 允许使用 sepsis onset 前数据。

### 5.1 静态特征（DAG 节点 F1；baseline_static + landmark_context）

```text
baseline_static_v2        -- 每 episode 一行
landmark_context_v2       -- 每 episode × landmark 一行（最近可用体重/身高、Δ_ICU-sepsis、支持状态）
```

| 特征 | MIMIC 来源 | eICU 来源 | 归属表 / 备注 |
|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` 数值化 | baseline_static |
| 性别 | `patients.gender` | `patient.gender` | baseline_static |
| 体重 | `weight_durations` | `pivoted_weight` + `admissionweight` | landmark_context；`t ≤ t_landmark` 最近值 |
| 身高 | `height` / `omr` | `admissionheight` | landmark_context；同上 |
| 入院类型/来源 | `admissions.*` | `hospitaladmitsource, unitadmitsource` | baseline_static；C 层 |
| ICU 类型 | `icustays.first_careunit` | `patient.unittype` | baseline_static；C 层 |
| Δ_ICU-sepsis | 计算列 | 计算列 | landmark_context |
| Charlson | **`charlson_prior`（NULL 口径）** | `past_history` 自建近似 | baseline_static；移出 SC-common |

**`charlson_prior` 固定口径**：窗口 `t_diagnosis < t_index_admission`；无既往住院：**`charlson_prior = NULL`**、`charlson_prior_available = FALSE`、`prior_hospital_count = 0`；数值填充仅由训练集拟合的缺失处理器完成并保留缺失指示；报告 `prior_icd_observation_window`；本次入院早期病史不并入。

体重固定口径替代方案（敏感性）：只取入院初始测量、不随 landmark 更新、初期不可用者保留缺失。**禁止**为早期 landmark 使用住院后较晚测得的体重。

### 5.2 生命体征时序（DAG 节点 F2；多信号分层 + 双轨）

| 变量 | MIMIC 来源 | eICU 来源（主来源 → 缺失补充） | 目标单位 |
|---|---|---|---|
| HR | 分层后 `vitalsign`/`chartevents` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | 同上，有创优先 | `pivoted_vital.ibp_*` → `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 仅缺失补充 | mmHg |
| RR | 同上 | `pivoted_vital.RespiratoryRate` → `vital_periodic.respiration` | /min |
| SpO2 | 同上 | `pivoted_vital.spo2` → `vital_periodic.sao2` | % |
| 体温 | 同上 | `pivoted_vital.temperature`（量纲 QA） | °C |

**MIMIC itemid 分层审计（多信号）**：`itemid`、`d_items.category`、`storetime − charttime` 分布、同时刻重复模式、记录间隔特征、与监护仪专用表/派生表对应关系；清单阶段 A 预登记。

**双轨输出**：`vitals_realtime_strict`（仅确认自动导入/可用时间可靠）/ `vitals_charttime_retro`（全部按 charttime）。

**eICU 三来源去重**：主来源明确；补充仅补缺；记录级去重；输出 `source_table`；抽查跨表重复率。

### 5.3 检验（DAG 节点 F3；原始重建 + P/F 双时间 + eICU 语义审计）

项目清单：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- **MIMIC**：关键项目从 `main.labevents` 重建，保留 `charttime` 与 `storetime`；派生宽表仅交叉校验。
- **eICU**：`pivoted_lab` + `pivoted_bg`；原始 `lab` 补充（经 `eicu_lab_time_map` 换算坐标）。
- **eICU lab 时间语义审计（阶段 A 专项，冻结清单 C-2）**：`labresultoffset` 语义、`labresultrevisedoffset` 是否仅修订时间、当前值是否最终修订值、修订晚于 landmark 的提前使用风险、缺失/负值/倒置处理；候选规则（审计后锁定）：最终修订值行 `available_time = max(labresultoffset, labresultrevisedoffset)`。报告 `qa/eicu_lab_time_semantics_qa.md`。**报告完成前 eICU 检验一律 `charttime_fallback`。**
- **PaO₂/FiO₂ 双时间**：`pao2_value/pao2_event_time/pao2_available_time`、`fio2_value/fio2_event_time/fio2_available_time`、`pf_available_time = max(两者)`、`pf_pairing_gap_min`、`fio2_source ∈ {measured, ventilator_setting, device_based_estimated, flow_only_estimated}`。断言 `pf_available_time ≤ t_landmark`；`derived.bg.pao2fio2ratio` 仅交叉校验；FiO₂ 主分析仅用明确记录值，流量换算仅敏感性且须联合设备类型。

### 5.4 SOFA 组分（DAG 节点 F4；purpose × evidence-track 二维命名，评审 P1-6）

**二维命名结构（评审 P1-6，冻结）**：

```text
sofa_purpose:
  phenotype_locked      -- 表型 SOFA：逐字复现锁定代码（含其缺失假设，写入局限性）
  realtime_feature      -- 模型输入 SOFA：严格 available-time，缺失不计 0
  completeness_qa       -- 完整性 QA
sofa_evidence_track:
  strict_24h            -- 仅原始观测 event_time ∈ (t−24h, t]
  carryforward          -- 项目特定近似：按组分上限 carry-forward（胆红素/肌酐/血小板 ≤48h，
                        --   GCS/PF ≤24h），不得标记为 strict_24h evidence
```

允许组合（冻结）：

| purpose | strict_24h | carryforward |
|---|---|---|
| phenotype_locked | 按锁定代码决定，不强行套 realtime 轨 | 按锁定代码决定 |
| realtime_feature | **主分析** | 敏感性/备选 |
| completeness_qa | 是 | 是 |

carry-forward 轨逐组分额外输出：`component_age_at_landmark / component_carried_forward_from / component_carryforward_limit`。完整性率分列「直接观测完整率」与「含 carry-forward 完整率」。P-strict 表型判定用 `phenotype_locked`；**CV-SOFA≥3 固定亚组统一使用 `sofa_realtime_strict_24h_cv`**（明确字段名，防止误用 carry-forward 版）。「当前生理状态分数」另命名 `sofa_current_state`，不与标准 24h SOFA 混用。SOFA 组分为经典六组分（**乳酸不是 SOFA 组分，仅独立模型变量**）。

`SOFA_d(t) = max SOFA_d(u)，u ∈ (t−24h, t]`（strict 轨）且所有输入 `available(u) ≤ t`；血管活性药取 24h 最大剂量、MAP 取 24h 最小值。

**逐组分输出（两轨同构）**：`component_value / component_observed / component_available / component_window_start / component_window_end / component_missing_reason / component_imputation_flag`。

**完整性规则**：①缺失组分不得默认计 0；②**标准实时 SOFA 总分仅 6/6 可计算时生成**（`sofa_total_complete`，两轨各自）；③5/6 仅 `sofa_partial_sum`（`sofa_total_status = partial_5_of_6`），不得用于 ΔSOFA≥2、完整 SOFA 亚组、标准阈值比较；④**delta 双分层（评审 P1-4）**：

```text
delta_sofa_observed_complete   -- 仅 baseline 与 qualifying 两端均观测完整 6/6、
                                  同轨同规则版本时有值
delta_sofa_phenotype           -- qualifying 端完整 6/6；baseline 来源 ∈
                                  {observed_complete, assumed_zero_by_phenotype_rule}
```

缺失掩码变化不得单独制造 delta（Q1 测试）；⑤输出 `sofa_component_count / sofa_missing_component_mask / sofa_total_status`；⑥GCS unable/镇静/插管按锁定 `gcs_unable` 口径；镇静期优先镇静前 24h 内最近值；⑦尿量缺失 vs 无尿区分（§5.7）；⑧规则版本留痕。

**心血管经典规则（修正阈值 + 最大分值计分）**：

| 分值 | 标准 |
|---|---|
| 0 | MAP ≥ 70 mmHg，且无相关血管活性药 |
| 1 | MAP < 70 mmHg |
| 2 | dopamine ≤ 5 μg/kg/min，或任意剂量 dobutamine |
| 3 | dopamine > 5 且 ≤ 15 μg/kg/min，或 epinephrine ≤ 0.1，或 norepinephrine ≤ 0.1 μg/kg/min |
| 4 | dopamine > 15 μg/kg/min，或 epinephrine > 0.1，或 norepinephrine > 0.1 μg/kg/min |

`SOFA_CV = max(MAP, dopamine, dobutamine, epinephrine, norepinephrine 各准则分值)`。三变量严格分离：`sofa_cv_original` / `nee_current` / `vasopressor_burden`；vasopressin、phenylephrine 不进经典计分（风险 R15）。

**MIMIC `derived.sofa` 总分不得直接作为严格实时模型特征**；窗口语义 20–50 stay 人工核对（§7.5）。`sepsis3` 静态组分禁用作 landmark 特征（风险 R11）。

### 5.5 血管活性药与 NEE（DAG 节点 F5）

- **MIMIC**：`vasoactive_agent` → 技术文档 §6.2 公式合成 NEE；双实现核验四字段。体重按优先级且遵守 landmark 截断。
- **eICU**：`infusion_drug` 解析管线（药名正则 → 数值化 → 单位换算 → 体重优先级 → NEE 求和）；`pivoted_infusion` 仅存在性交叉校验。
- 输注 episode：短间隙 <30min 合并；重叠判重沿技术文档 §6.2。
- **论文 2 人工审核 7 环节**：药物归类、单位解析、速率标准化、episode 合并、`t_stop`、`t_0`、48h 复用事件；eICU 标签在 MIMIC 双实现核验通过前暂缓。

### 5.6 机械通气与氧合支持（DAG 节点 F6）

- MIMIC：`derived.ventilation` + `oxygen_delivery` 补充 HFNC。
- eICU：`respiratory_care`、`treatment` 通气路径、`pivoted_o2`。

### 5.7 尿量与液体平衡（DAG 节点 F7）

- MIMIC：`derived.urine_output`（必要时 `outputevents` 补充）。
- eICU：`pivoted_uo`；`intake_output` 计算 24h 平衡。**尿量缺失（无记录）与真正无尿（有记录且低于阈值）严格区分**，不得互相充填。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC）

1. **就诊归属（显式 OR）**：

   ```text
   eligible ECG =
       [ admittime ≤ t_ecg ≤ min(t_landmark, dischtime) ]
     ∨ [ auditable_pre_admission_encounter ∧ t_ecg ≤ t_landmark ]
   ```

   四态 `ecg_encounter_status`；主分析纳入前两类，后者打 `pre_admission_ecg = TRUE`。审计四条件（预登记）：ED stay 主键关联、ED 离开至入院间隔 ≤ 阈值、期间无其他 encounter、入院前最大允许时长。
2. **ECG 时间语义（防泄漏统一采集完成时间）**：

   ```text
   ecg_acquisition_time          -- 采集开始时间
   ecg_available_time_assumed    -- = ecg_acquisition_time + recording_duration
                                    recording_duration = N_samples / f_s（WFDB header 解析）；
                                    header 不可得时按预登记固定假设并留痕
   ecg_processing_time / ecg_selection_time
   ```

   **声明**：采集完成即可用是部署假设，非数据库事实。**防泄漏断言、时效窗与选片统一使用 `ecg_available_time_assumed ≤ t_landmark`**。
3. **五层级 availability**：`ecg_found_raw → ecg_same_encounter → ecg_structurally_valid → ecg_pass_frozen_qc → ecg_selected_for_model`。
4. **两层 QC**：固定结构性 QC（全集统一）；数据驱动 QC（阈值仅训练集确定并冻结）。
5. **时效与选片**：多份取时效窗内最近一份通过 QC 者；**主配对队列定义在 QC 后、查看测试集结果前冻结**。
6. 患者级 ECG 描述队列：`t_sepsis ± 24h` ≥1 份（仅描述）。
7. 波形定位与预处理按技术文档 §20；`ecg_measurements` 作试金石与 QC 辅助；试点表不进管线。

---

## 6. SC-common 跨库变量分层映射总表（含变量级等价性合同）

按跨库同构程度分四层。core/extended 锁定前，A/B 层每个变量先完成变量级**等价性合同**：

```text
sc_common_variable_contract_v2
- concept_name
- source_table / source_column
- unit / conversion_rule
- priority_rule
- event_time_rule / available_time_rule
- missing_rule
- physiologic_range
- cross_database_equivalence_grade   -- A / B / C
```

**已知须逐条核验的实现差异**：MAP（有创/无创/周期/非周期优先级）；体温（eICU 华氏/摄氏混合）；SpO₂（eICU `sao2` 可能为动脉血气 SaO₂）；乳酸（独立模型变量，非 SOFA 组分）；血小板（单位与异常值）；WBC（计数与分类映射）。**合同完成并逐变量评级前，不得锁定 `SC-common-core` 为主外验输入。**

> **✅ C2 审计完成记录（2026-08-01）**：按提取方案 §11 完成 SC-common 跨库合同审计（`qa/sc_common_contract_v2.md`）。结果：17 core 变量中 **16 评 A、1 评 B（bilirubin，中位差 30%，队列构成差异登记）**；语义专项：eICU 体温全为 °C、SpO₂ 为脉搏血氧（分布一致 [86,97,100]）、MAP ibp 25%/nibp 75%；**附带发现并修复 MIMIC 体温 °F/°C 混入 bug**（itemid 223761 华氏占 84%，F2 已加 °F→°C 转换并重建下游 P2/P7/P9/P10）。合同表 `sc_common_variable_contract_v2.parquet` 状态升级为 `audited_locked_2026-08-01`，**SC-common-core 锁定为主外验输入的许可生效**。

### A 层：高同构变量 → `SC-common-core`（主外验模型候选）

| 临床概念 | MIMIC 来源 | eICU 来源 | 单位 | 泄漏风险 |
|---|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` | 岁 | 低 |
| 性别 | `patients.gender` | `patient.gender` | — | 低 |
| HR | §5.2 分层来源 | `pivoted_vital`/`vital_periodic` | bpm | 低 |
| MAP（有创/无创） | §5.2 分层来源 | `ibp_mean`/`nibp_mean`/`systemicmean` | mmHg | 低 |
| RR | 同上 | `RespiratoryRate`/`respiration` | /min | 低 |
| SpO2 | 同上 | `spo2`/`sao2`（须区分脉搏/动脉血氧） | % | 低 |
| 体温 | 同上 | `temperature`（量纲 QA） | °C | 低 |
| 肌酐 | `labevents` 重建 | `pivoted_lab.creatinine` | mg/dL | 低 |
| 胆红素 | 同上 | `pivoted_lab.bilirubin` | mg/dL | 低 |
| 血小板 | 同上 | `pivoted_lab.platelets` | K/μL | 低 |
| 乳酸 | 同上 | `pivoted_lab.lactate` | mmol/L | 低 |
| WBC | 同上 | `pivoted_lab.wbc` | K/μL | 低 |

### B 层：中等同构变量 → 与 A 层合并构成 `SC-common-extended`

| 临床概念 | MIMIC 来源 | eICU 来源 | 主要差异 | 泄漏风险 |
|---|---|---|---|---|
| GCS | `derived.gcs` | `pivoted_gcs`/`pivoted_score` | 镇静口径 | 中 |
| PaO2/FiO2 | §5.3 双时间重建 | `pivoted_bg` | 拼接规则、`fio2_source` | 中 |
| 尿量（24h） | `derived.urine_output` | `pivoted_uo` | 记录完整性 | 低 |
| 机械通气 | `derived.ventilation` | `respiratory_care`/`treatment` | 状态判定路径 | 中 |
| 血管活性药使用（0/1） | `vasoactive_agent` | `infusion_drug`/`pivoted_infusion` | 仅使用标记 | 中 |

### C 层：低同构变量 → **不进入主外验模型**

| 临床概念 | 主要不等价来源 | 处置 |
|---|---|---|
| NEE 精确剂量 | eICU 文本单位解析、体重缺失、给药协议差异 | C 层；`nee_current` 限 MIMIC 侧特征与论文 2 |
| Charlson | eICU `past_history` 与既往住院 ICD 不同构 | `charlson_prior`（NULL 口径）作 SC-MIMIC 特征 |
| ICU 类型 | 科室命名体系不同 | C 层；仅描述 |
| 入院来源 | 类别体系不同 | C 层；仅描述 |
| SOFA 总分及部分组分 | 输入完备性与完整性规则差异 | 用于表型判定与亚组分层，不作 core/extended 输入 |

### 固定定义（命名统一）

```text
SC-common-core     = A 层
SC-common-extended = A + B 层
SC-MIMIC           = 全量 MIMIC 特征（内部探索，不纳入唯一主要比较）
```

> 技术文档 `SC-common-all`（全体 landmarks 训练的 common 变量模型）在本方案中以「`SC-common-core`（或锁定后的 extended）× 全体 landmarks」实现；eICU 外验与无 ECG 部署分支均指此模型。

首版外验模型默认 `SC-common-core`；core/extended 依据阶段 C2 合同与阶段 D 同构核验在建模前锁定，禁止按模型效果反向调整。感染源不进主模型。

---

## 7. 防泄漏与质量控制

### 7.1 防泄漏断言（管线自动测试 Q1）

**时间断言**：

1. `ecg_available_time_assumed ≤ t_landmark` 且满足 §5.8 显式 OR 归属（含选片：`ecg_selected_for_model = TRUE ⇒ ecg_available_time_assumed ≤ t_landmark`）；
2. **全部特征 `available_time ≤ t_landmark`（主断言）**；聚合记录 `max_available_time ≤ t_landmark`；P/F `pf_available_time ≤ t_landmark`；
3. 结局窗起点 > t_landmark；
4. 同一患者不跨 train/validation/test（当前无独立 calibration 集；未来启用须先在 §2.4 定义且同一患者 landmark 不跨 calibration/test）；
5. 标准化/异常值阈值/插补器仅训练集拟合；
6. 特征筛选仅训练集；
7. ECG 数据驱动 QC 阈值仅训练集确定。

**episode final map 测试（Q1-8）**：preliminary/final 物理分离；preliminary `pending_review` 必须保守 split；clean `merged` 可直接 final merged；`pending_review` 仅 adjudicated override 后方可 final merged；未裁决 `pending_review` 保持 conservative split（`unresolved_conservative_split = TRUE`）；final `episode_id` 在应用全部 override 后重新生成且**全局唯一**（`COUNT(*) = COUNT(DISTINCT episode_id)`）；主分析 final map 不得含未定义 final decision；人工裁决不得改写 preliminary 字段；**`final_decision ∈ {merged, split}`（禁止 pending_review/NULL/unknown）；preliminary pending + 无裁决 ⇒ final split + `unresolved_conservative_split = TRUE`**；**裁决范围仅 pending 边**（clean/规则性决策无个案覆盖）。

**zero-gap transfer sequence 测试（Q1-9，评审 §六.2）**：用例①`ICU_A outtime = ICU_B intime` 且有序路径 `ICU_A → ICU_B` ⇒ clean/merged；②同边界但边界 transfer 含 ward/ED ⇒ 不得 merged；③多 transfer 同时命中边界且顺序冲突 ⇒ `pending_review`；④缺左/右边界证据 ⇒ 按预登记规则 `pending_review` 或 conservative split；`transfer_sequence` 字段实际生成且有序。

**eICU episode map 测试（Q1-10，评审 §六.5）**：每 `patientunitstayid` 恰属于一个 final episode；同一 final episode 不得跨 `patienthealthsystemstayid`；episode hospital-time 区间单调；跨 unit 候选 pair 必须属于同一 final episode；eICU preliminary/final/adjudication 物理分离。

**eICU 源事件守恒与序列化测试（Q1-11，评审 §六.4 扩充）**：`SUM(raw_exact_duplicate_count) = 物理源行数`；normalized `source_event_id` 唯一；normalized event ↔ time-map 双向一对一；canonical fingerprint 碰撞数 = 0；同一 fingerprint 下 canonicalized fields 完全一致；`source_event_id_version / canonicalization_rule_version / source_snapshot_checksum` 非空；**canonical serialized event 可无歧义反序列化（round-trip），恶意/边界值（`|`、`=`、`<NULL>`、空字符串、真正 NULL、前后空格、Unicode、换行符、引号、科学计数法浮点、NaN/Inf）不得产生碰撞；不同 typed field structure 不得生成相同序列化**。

**SOFA 完整性与 delta 分层测试（Q1-12）**：`sofa_total_complete` 非空 ⇒ `component_count = 6`；`component_count = 5` ⇒ `sofa_total_complete IS NULL`；缺失掩码变化不得单独制造 delta；**`sofa_realtime_strict_24h` 全部组分证据 `event_time ∈ (t−24h, t]`，carry-forward 证据不得标记为 strict_24h**；**`delta_sofa_observed_complete` 非空 ⇒ baseline 与 qualifying 均 observed complete 6/6 且同轨同版本；`delta_sofa_phenotype` 非空且 `baseline_assumed_zero = TRUE` ⇒ `baseline_sofa_source = assumed_zero_by_phenotype_rule` 且 `baseline_sofa_complete_observed = FALSE` 且 qualifying 端完整 6/6**。

**landmark 与 NULL episode end 测试（Q1-13，评审 §六.1）**：`episode_outtime_status = missing_or_open` ⇒ 不得按 `outtime IS NULL` 生成至 168h 全部 landmark（主分析零 landmark + QA 记录）；若启用敏感性截尾：`t_landmark < episode_landmark_censor_time` 且其来源属于预登记 ICU 内临床事件白名单。

**标签与表型测试（Q1-14/15）**：

14. 标签可观察性：`last_clinically_observed_time < w_end` 且无明确存活出院 ⇒ `outcome_ascertainable = FALSE`；反例：`t+12h` 存活出院 + `t+30h` 延迟 storetime ⇒ `non_event_alive_discharge` 且 `full_inhospital_followup_24h = FALSE`；**恰好 `w_end` 出院 ⇒ `full_inhospital_followup_24h = TRUE`**；临床事件时间 > `dischtime` ⇒ 时间异常 QA；**`outcome_unknown_reason` 必须属于预登记枚举**（含 `missing_status_left_observation`）；**`observation_end_source` 与实际选定 observation endpoint 一致**；临床观察源白名单外来源不得进入 `last_clinically_observed_time`；
15. 表型证据时间：`diagnosis_time_semantics = retrospective_only` ⇒ 不得进入 strict_available_time 特征轨道。

**配对回放验证（Q1-16，冻结清单 B-5 联动）**：MIMIC 同输入字段运行 `select_suspected_infection_pairs_locked_v1`，与锁定版 `suspicion_of_infection` 比较 `pair-level / event-level exact agreement / infection_time agreement / discordant case count`，达预登记标准。

**其余结构断言（Q1-17/18）**：

17. `mimic_episode_sepsis` 每 episode 一行；`acute_transfer_time` XOR `alive_discharge_time`；eICU 标签仅用 `*_episode_min`（静态检查）；final `episode_merge_decision` 与 `transfer_evidence` 一致性；结局三态分层抽查（§7.4）；
18. **schema 一致性测试**：正文定义输出表字段与 Parquet 实际列完全一致（含 `mimic_icu_episode_edges_preliminary` 的 `transfer_sequence`、`mimic_icu_episode_map_final` 全部审计字段、`cohort_eicu_v2` 不含队列级 rate）；landmark 单调递增且间隔 6h；`k0 ≥ 0`；§3.4 全部边界单元测试；`ecg_encounter_status` 四态校验；`sofa_cv_original` 与 `nee_current` 来源列不同；SOFA 缺失组分未计 0。

### 7.2 时间逻辑 QA

- `admittime ≤ icu_intime < icu_outtime ≤ dischtime` 成立比例；NULL stay outtime 比例（`episode_outtime_status` 分布）；
- `t_sepsis` 相对 ICU 入科分布；`k0` 分布；t=0 不在 ICU 比例；`t_sepsis_status = missing` episode 数；
- 各 k 风险集人数；landmark 后仍在 episode 内验证；
- 每变量 `event_time` 与 `available_time` 差异分布；strict/chart 双轨差异；
- eICU 时间映射连续性与间隙分布；MIMIC `gap_minutes` 分布、`transfer_evidence` 构成、`zero_gap_path_status` 分布、adjudication 量级、`unresolved_conservative_split` 命中数；eICU `edge_path_class` 构成；
- MIMIC 生命体征三层占比与录入延迟分布；`last_database_available_time − last_clinically_observed_time` 延迟分布。

### 7.3 队列表型 QA

- **MIMIC 随机抽查**：suspected infection 配对（含回放 discordant case 复核）、SOFA ≥2、index episode、episode 合并（含边表与 zero-gap 序列审计）、`t_sepsis`；
- **eICU 分层抽查**：抗生素识别、培养识别、配对方向、跨 unit stay 配对命中（同一 final episode）、锁定函数选对执行、SOFA 六组分（两轨）、sepsis time、多 stay 时间映射、`antibiotic_time_source` 四级构成与互斥、`administration_confirmation_availability` 判定、`diagnosis_time_semantics` 分布。

### 7.4 结局 QA（分层抽查）

分层：24h 死亡阳性、明确阴性（覆盖型）、存活出院（含延迟录入反例与恰好 `w_end` 出院）、急性转出、eICU 状态缺失、ICU 转出后院内死亡、`t+24h` 边界、`death_time_missing`、`status_conflict`、可观察期不足、adjudication 覆盖样本复核。

### 7.5 派生表来源验证（D0 前置）

SQL/R checksum、mimic-code commit、DuckDB 版本、生成日期、源表版本、回溯验证、行数与主键唯一性、与官方参考分布比较（含 SOFA 窗口语义 20–50 stay 人工核对）。

### 7.6 ECG 配对 QA

同一住院内 ECG、入院前 ECG（审计四条件）、出院后 ECG、多份取最近、landmark 恰等于 `ecg_available_time_assumed`、路径与 study_id 一致、header 导联/采样率/样本数解析（recording_duration 计算抽查）。

### 7.7 专项与常规 QA 输出

- `qa/eicu_lab_time_semantics_qa.md`、`qa/eicu_diagnosis_time_semantics_qa.md`（阶段 A）；
- `qa/sofa_realtime_completeness_v2.md`（6/6 与 ≥5/6 分列；两轨分列）；
- `qa/pairing_replay_validation_v2.md`（配对回放一致性，Q1-16）；
- `qa/sc_common_contract_v2.md`（阶段 C2）；`qa/vitals_dual_track_v2.md`；
- 队列流程图（两库分别，eICU 三套表型分列）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；原始基线：MIMIC sepsis3 41,295 stays / 31,910 subjects，ECG 覆盖 161,352 subjects，eICU 200,859 stays / 院内死亡 18,004）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集）；eICU Go/No-Go 检查表（含 `eicu_antibiotic_time_source_summary`）。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v2.parquet, cohort_eicu_v2.parquet   # episode 级字段（无队列级 rate）
  episodes/  mimic_icu_episode_edges_preliminary.parquet       # 边级表（含 transfer_sequence）
             mimic_icu_episode_map_preliminary.parquet
             episode_merge_adjudications.parquet
             mimic_icu_episode_map_final.parquet               # 全局唯一 episode_id + mapping_version
             mimic_episode_sepsis.parquet
             eicu_episode_edges_preliminary.parquet            # 新增（P1-1）
             eicu_episode_map_preliminary.parquet
             eicu_episode_merge_adjudications.parquet
             eicu_episode_map_final.parquet
             eicu_unitstay_timeline.parquet
             eicu_event_time_map.parquet                       # 双指纹/双计数/版本字段
             eicu_medication_time_map.parquet
             eicu_microbiology_time_map.parquet
             eicu_lab_time_map.parquet
             eicu_infusion_time_map.parquet
  phenotypes/ eicu_antibiotic_events.parquet
             eicu_culture_events.parquet
             eicu_infection_pairs.parquet                      # 候选 pair
             eicu_suspected_infection_events.parquet           # 锁定函数输出
             eicu_antibiotic_time_source_summary.parquet
             eicu_phenotype_tracks_v2.parquet
             eicu_phenotype_event_v2.parquet                   # baseline 分层 + delta 双分层 + diagnosis 三字段
  splits/    split_assignments_v2.parquet
  landmarks/ landmarks_v2.parquet
  labels/    labels_24h_v2.parquet           # 状态机字段 + 双观察终点 + observation_end_source
             label_adjudications.parquet
             labels_competing_7d_v2.parquet
  features/  baseline_static_v2.parquet      # charlson_prior NULL 口径
             landmark_context_v2.parquet
             vitals_hourly_v2.parquet        # bin 聚合字段 + source_table + source_time_type
             vitals_realtime_strict_v2.parquet / vitals_charttime_retro_v2.parquet
             labs_hourly_v2.parquet          # P/F 双时间字段 + fio2_source
             sofa_hourly_v2.parquet          # purpose × evidence_track 二维 +
                                             #   逐组分字段 + delta 双分层 + 掩码/规则版本
             nee_stream_v2.parquet
  contracts/ sc_common_variable_contract_v2.parquet
             clinical_observation_whitelist_v2.parquet         # 临床观察源白名单（§4.1）
  ecg_index/ ecg_landmark_index_v2.parquet   # 四时间字段 + recording_duration + 归属 + 五层级标志
  qa/        cohort_flow_v2.md, feasibility_table_v2.md, leakage_report_v2.md,
             time_logic_qa_v2.md, phenotype_qa_v2.md, outcome_stratified_qa_v2.md,
             ecg_pairing_qa_v2.md, derived_provenance_v2.md, eicu_go_nogo_v2.md,
             eicu_lab_time_semantics_qa.md, eicu_diagnosis_time_semantics_qa.md,
             sofa_realtime_completeness_v2.md, pairing_replay_validation_v2.md,
             sc_common_contract_v2.md, vitals_dual_track_v2.md
  _meta/     code_version.json
             d0_decision.json
             freeze_checklist.json
```

规范：①统一 Parquet；②三级键 `subject_key / episode_key / landmark_k`，下游连接以 `(episode_id, episode_mapping_version)` 为键（MIMIC 全局唯一），原始 stay 标识与 `source_event_id` 保留溯源；③患者级 ID 与划分表冻结后不得重算；④每 DAG 节点独立脚本、I/O schema 校验、中间产物持久化；⑤时间字段命名执行 §2.3 规范；⑥D0 与冻结清单状态落 `_meta/`；⑦自动结果（episode preliminary map、标签）与人工裁决（adjudication 表）物理分离；⑧队列级汇总指标只入汇总表与 QA；⑨不同 `episode_merge_threshold_min` 的映射版本物理分目录或分版本字段，不得混用。

---

## 9. 已识别风险与待决事项（R1–R37）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| R1 | 本地 `sepsis3` 无 `sepsis_time` | 主时间原点 | D0 决策门（§3.1 固定 JSON）；A-1 |
| R2 | `2020 - 2022` v1.9 未规定 | 时间划分 | 阶段 A amendment；A-3 |
| R3 | eICU 无 Sepsis-3 派生表、培养覆盖极低 | 外验表型 | C7 双层合同 + 三套队列 + Go/No-Go |
| R4 | eICU SOFA 自建、GCS 镇静口径差异 | SOFA 可比性 | F4 口径对齐 + 完整性规则；差异预登记 |
| R5 | eICU 输注速率文本内嵌单位 | NEE/论文 2 | F5 解析管线；7 环节人工审核 |
| R6 | SOFA-1 与 SOFA-2 并存 | 误用 | 仅用 SOFA-1；Q1 命名检查 |
| R7 | 遗留/试点表 | 误用 | 白名单制 |
| R8 | eICU 无 ECG，availability 与库来源共线 | 门控外推 | eICU 仅走 SC-common-core（或 extended）× 全体 landmarks 独立路径 |
| R9 | 急性转出类别两库不一致 | unknown 标记 | QA 实测清单预登记；D-3 |
| R10 | 体重缺失/极端值 | NEE/论文 2 | 技术文档 §6.2 规则；landmark 截断 |
| R11 | `sepsis3` 静态组分误用 | 泄漏 | 禁用；landmark SOFA 取实时口径 |
| R12 | 检验 charttime 早于结果可用 | 实时泄漏 | 原始重建 + 双轨；不可用时声明 retrospective chart-time prediction |
| R13 | ECG 跨住院配对 | 配对正确性 | 显式 OR 归属四态 |
| R14 | eICU 多 stay offset 坐标不一致 | 时间正确性 | C6a 统一坐标 + eICU episode 四表；标签仅用 `*_episode_min` |
| R15 | NEE 替代经典 SOFA 心血管 | 可比性 | 修正阈值 + 最大分值；三变量分离 |
| R16 | Charlson 派生表含本次住院 ICD | 泄漏 | `charlson_prior` NULL 口径；移出 SC-common |
| R17 | 未知结局误编码阴性 | 标签正确性 | 状态机 + 临床观察终点 + adjudication 分离 |
| R18 | eICU 培养覆盖低的表型选择 | 外验有效性 | 三套队列 + Go/No-Go；命名建模前锁定 |
| R19 | eICU 表型规则未 PI 签署 | 外验时间原点 | C7 冻结候选规则表；A-5 |
| R20 | eICU lab offset 语义未审计 | eICU 检验泄漏 | 专项报告；候选 max 公式验证后锁定；C-2 |
| R21 | available-time 落实不完整 | 防泄漏 | 分层重建 + 双轨；C-3/4/5 |
| R22 | Go/No-Go 数值未 PI 确认 | 可行性决策 | §2.2 C7 确定建议值；A-6 |
| R23 | episode 合并误判病房/ED 区间、重叠被掩盖 | 队列时间轴 | C0 三规则 + 两阶段分离 + zero-gap 序列核验；B-7 |
| R24 | MIMIC 生命体征来源识别不稳定 | 实时口径可信度 | 多信号分层 + 双轨输出 |
| R25 | 标签冲突被隐式处理 | 标签完整性 | 冲突先 unknown；`label_adjudications` 独立覆盖 |
| R26 | eICU 事件按 offset 反连重复匹配 | 配对/特征正确性 | 规范化 `source_event_id` + 专用桥接 + Q1-11 守恒测试 |
| R27 | preliminary episode 隐式合并 pending_review | 队列时间轴 | preliminary/final 物理分离 + override 后重建 final ID；B-7 |
| R28 | 5 组分 partial SOFA 与完整不可比 | SOFA 完整性 | `sofa_total_complete` 仅 6/6；partial 限定用途；C-7 |
| R29 | eICU 诊断时间被当作真实 available time | 外验时间语义 | `diagnosis_time_semantics` 分层 + 专项审计；A-5 |
| R30 | `dischtime IS NULL` 被误判为持续在院 | 标签正确性 | `last_clinically_observed_time` 覆盖方可判阴性；D-5 |
| R31 | 延迟录入/结果时间被当作在院证据 | 标签可观察性 | 双观察终点 + 白名单；存活出院分支优先；D-5 |
| R32 | 源事件哈希实现不规范 | 事件标识可靠性 | canonical JSON + SHA-256 + 双指纹双计数 + 版本留痕（阻断项 2）；B-4 |
| R33 | 选对算法未证明等价 mimic-code | 表型复现 | A.5 仅候选生成；锁定函数 + MIMIC 回放验证；B-5 |
| R34 | 48h carry-forward 混入「标准 24h SOFA」 | SOFA 口径 | purpose × evidence_track 二维分离 + 完整率分列；C-7 |
| **R35** | **`episode_outtime IS NULL` 被外推生成全部 landmark** | landmark 正确性 | 主分析仅 `status = ok` 生成；missing_or_open 进 QA；敏感性可选 ICU 内截尾时间（阻断项 3）；Q1-13 |
| **R36** | **zero-gap 边仅靠「无 ward/ED 证据」判 clean，序列未真正核验** | episode 合并正确性 | `transfer_sequence` 实际生成 + 合法/异常路径类别 + Q1-9 四用例（阻断项 1）；B-7 |
| **R37** | **`episode_id` 非全局唯一导致跨住院错误连接** | 下游连接完整性 | `MIMIC_<hadm_id>_<episode_seq>` 全局唯一 + `(episode_id, episode_mapping_version)` 连接键 + 唯一性测试（阻断项 1）；B-7 |

---

## 10. 冻结清单（Freeze Checklist，共 31 项：A6 + B7 + C7 + D6 + E5；通过条件含第六轮扩充）

正式冻结前全部关闭；状态实时记录于 `_meta/freeze_checklist.json`。

### A. 协议冻结（6 项）

- [x] A-1 D0 出口 A/B 已确定；（✅ 2026-07-30 关闭：**D0 出口 B——主时间原点定为 `suspected_infection_time`**（PI 批准 2026-07-30）；依据：锁定版 mimic-code（a0af19c）MIMIC-IV sepsis3 不输出 `sepsis_time`（`qa/derived_provenance_v2.md` §2）；实测 sofa_time 与 suspected_infection_time 差值分布 [-48h, +24h]，78.5% 感染疑似更早；敏感性轨保留 `max(sofa_time, suspected_infection_time)` 与 `icu_admission` 两口径）
- [x] A-2 `_meta/d0_decision.json` 已按 §3.1 固定 schema 生成；（✅ 2026-07-30 关闭：`status=decided`、`primary_time_origin=suspected_infection_time`、`secondary_time_origins=[max(sofa_time,si_time), icu_admission]`、`source_table=mimiciv_derived.sepsis3`、`source_code_commit=a0af19c`、`protocol_amendment_required=true`、`pi_approval_date=2026-07-30`）
- [x] A-3 `2020–2022` amendment 已签署；（✅ 2026-07-30 关闭（选项 A：正式排除）——49,173 subjects / 3,341 index episodes（占队列 10.5%）不进入任何 train/validation/test 索引，完全不查看结局与模型性能；理由：与 v1.9 四年组预注册一致 + COVID 时期分布漂移不可校正；`split_assignments_v2` 已按 `excluded_amendment_pending` 隔离；不保留为扩展数据，未来启用须独立 amendment）
- [x] A-4 mimic-code commit 与本地修改已锁定（含 SQL/R checksum 与配对函数参照实现）；（✅ 2026-07-30 关闭：commit `a0af19c18a66b6d96935058ebfa830608989bd7c`（2026-07-04 master）锁定；两 SQL 文件 SHA-256 登记于 `_meta/mimic_code_reference/manifest.json`（经 gh-proxy 镜像取得）；DB 构建来源经 PI 确认为自建自最近 master；schema 级一致性验证通过（sepsis3 14 列全同、`antibiotic` 表 949,901 行吻合）；配对函数参照实现已落地并经逐行移植回放验证）
- [x] A-5 eICU 三套表型冻结候选规则表已由 PI 逐项签字确认；且 `admission_dx` 与 `diagnosisoffset` 时间语义已审计，代理时间与真实 available time 已区分；（✅ 2026-07-30 关闭：P-clinical 主外验（assumed-zero 接受+局限性声明+observed-complete 子集敏感性）、P-explicit 第二外验、P-strict 降 feasibility_only、命名锁定 Robustness under phenotype shift；诊断时间语义三层已区分（`qa/eicu_diagnosis_time_semantics_qa.md`）；签署记录见 §2.2 C7）
- [x] A-6 Go/No-Go 数值已预登记（PI 确认，未据模型效果调整）。（✅ 2026-07-30 关闭：§2.2 C7 门槛数值原样预登记——P-strict 因患者数/医院数/单院占比/死亡数/抗生素覆盖率多项不达，正式退出外验（feasibility_only）；P-clinical 与 P-explicit 因感染证据为诊断驱动、不依赖抗生素配对，其余门槛全过，按 Robustness under phenotype shift 继续；30% 抗生素覆盖率门槛保留原文，约束力限于 P-strict；eICU 首个 landmark SOFA 可计算率补测数据作为附件登记，不调整阈值）

### B. 时间轴冻结（7 项）

- [x] B-1 MIMIC episode 以 `icustays` 为锚构建；（✅ 2026-07-30 关闭：`mimic/c0_episodes.py` 以 `main.icustays` 为锚（LAG 按 hadm_id 排序生成 9,216 条边），final map 覆盖全部 94,458 stays；Q1-8 断言通过）
- [x] B-2 episode 映射一对多关系符合预期；（✅ 2026-07-30 关闭：实测 main_tau0 无合并（v3.1 最小间隙 0.1min），tau30/60 合并 791/792 边形成多 stay episode（93,667/93,666 episodes）；`qa/time_logic_qa_v2.md` 登记）
- [x] B-3 每个 stay 仅属于一个 episode（final map 主键唯一性测试通过）；（✅ 2026-07-30 关闭：Q1-8 断言三版本均通过——每 stay_id 恰好一个 episode_id、(episode_id, stay_id) 无重复、episode_id 不跨 hadm）
- [x] B-4 eICU 所有事件均转换到 hospital/episode 坐标；**canonical serialization 使用固定 UTF-8、固定字段顺序、显式类型与 JSON null，分隔符及保留 token 无歧义（round-trip 测试通过）；哈希算法固定 SHA-256；raw exact duplicate 与 canonical duplicate 已区分；`SUM(raw_exact_duplicate_count)` 与物理源行数守恒；normalized event 与 time-map 一对一；哈希碰撞测试通过**。（✅ 2026-07-30 关闭：`eicu/c6a_episodes.py` 落地，四张 time-map 守恒断言全过；2026-07-30 补做 round-trip/恶意值测试与 micro 全量唯一性/一致性验证，全部通过）
- [x] B-5 跨 unit stay 的抗生素—培养配对测试通过；候选配对生成、锁定版选对函数与 `suspected_infection_event_id` 生成通过；在 MIMIC `suspicion_of_infection` 上的配对回放验证达到预登记一致性标准；（✅ 2026-07-30 关闭：**终版（锁定 SQL `suspicion_of_infection.sql` @ a0af19c 逐行移植）**：行数对账 949,901=949,901、**键匹配 1.000、suspected 标记一致 0.990、suspected_infection_time 一致 0.977、culture_time 一致 0.979**；`qa/pairing_replay_validation_v2.md`。注：同日早些时候的简化版回放（0.999/1.000/0.890）已被本终版取代；选对规则「最早培养 + 不限 ICU」经官方 SQL 源码确认）
- [x] B-6 标签只使用统一坐标（eICU 仅 `*_episode_min`）；**eICU preliminary/final episode map 与 adjudication 输出已建立，每个 `patientunitstayid` 恰属于一个 final episode**。（✅ 2026-07-30 关闭：episode 四表已建立（200,859 stays → 190,627 episodes，261 条 offset_overlap 进裁决）；约束断言通过；`eicu/labels.py` 全用 `*_episode_min`）
- [x] B-7 episode 合并三规则已锁定且边表字段落地；**`gap = 0` 的 `transfer_sequence` 已实际生成并通过合法路径单元测试；final decision 仅允许 merged/split（NULL ⇒ pipeline failure）；preliminary/final 物理分离；final episode ID 在 override 后重新生成且全局唯一；未裁决 pending_review 保守拆分；主分析 final map 无未定义裁决状态**。（✅ 2026-07-30 关闭：`mimic/c0_episodes.py` 全部落地；实测发现 v3.1 无 gap=0 边（最小 0.1min），占位路径规则（≤30min internal_transfer_placeholder）使 tau30/60 捕获 791/792 条真实合并；**待评审关注**：PLACEHOLDER_MAX_MIN=30 为候选参数、`τ=0` 主值在本数据版本下无合并的实际语义，见 `qa/time_logic_qa_v2.md`）

### C. 防泄漏冻结（7 项）

- [x] C-1 MIMIC 关键检验使用 `storetime`（`labevents` 重建完成）；（✅ 2026-07-30 关闭：`mimic/f3_labs.py` 从 labevents 重建 14 项，strict 轨 available_time=storetime，labs_hourly_v2 双轨 17,188,578 行，防泄漏断言 0 违规）
- [x] C-2 eICU lab revised time 语义已验证（专项报告关闭）；（✅ 2026-07-30 关闭：`qa/eicu_lab_time_semantics_qa.md`——revised 100% 覆盖、5.0% 倒置、中位滞后 28min、`max(result,revised)` 规则验证通过、多版本残留 55,023 组去重规则登记；当前产物维持 `charttime_fallback`）
- [x] C-3 MIMIC 人工记录生命体征录入延迟已处理（itemid 分层 + 双轨落地）；（✅ 2026-07-30 关闭：`qa/vitals_dual_track_v2.md`——16 个 itemid storetime 100% 可用，延迟中位 15–26min，strict 轨采用 storetime，双轨已落地）
- [x] C-4 P/F 使用两部分中较晚 available time（`pf_available_time` 断言通过）；（✅ 2026-07-30 关闭：`pf_available_time = max(pao2, fio2 available)`，全量断言 0 违规；`pf_strict_eligible` 已接入 SOFA 呼吸组分）
- [x] C-5 动态 SOFA available-time 口径已确定；（✅ 2026-07-30 关闭：labs/vitals=storetime（缺失降级打标）、vaso/vent=实际输注时间、GCS/尿量=charttime_fallback 登记；`qa/sofa_available_time_semantics_v2.md`）
- [x] C-6 聚合记录 `max_available_time` 已定义并接入 Q1；（✅ 2026-07-30 关闭：vitals/labs 聚合记录必备字段，Q1 断言 vitals 53.6M 行 + labs 17.2M 行 `max_available_time ≤ t_landmark` 0 违规，`qa/leakage_report_v2.md`）
- [x] C-7 实时 SOFA 缺失组分规则已锁定；标准总分仅 6/6 生成，5/6 仅 partial 且不用于 ΔSOFA；严格 24h 与 carry-forward 已分离；**`delta_sofa_observed_complete` 与 `delta_sofa_phenotype` 已分离，assumed-zero baseline 不得标记为观测完整**。（✅ 2026-07-30 关闭：完整率 strict 6/6=7.8%、carryforward 6/6=10.6%；Q1-12 断言通过；delta 双分层互斥校验通过；`qa/sofa_realtime_completeness_v2.md`）

### D. 标签冻结（6 项）

- [x] D-1 `outcome_ascertainable` 与 `full_inhospital_followup_24h` 已拆分；（✅ 2026-07-30 关闭：两字段独立生成，P1-3 边界断言通过）
- [x] D-2 死亡状态冲突规则已固定（`death_time_missing` / `status_conflict` 先 unknown）；（✅ 2026-07-30 关闭：状态机分支落地，实测 status_conflict=0、death_time_missing=78 全部进裁决表，不隐式编码）
- [x] D-3 急性转出清单已冻结（两库分别，XOR 互斥验证通过）；（✅ 2026-07-30 关闭：实测冻结——MIMIC acute=`('ACUTE HOSPITAL')`、alive=11 类；eICU acute=`('Other Hospital','Telemetry','Step-Down Unit')`、alive=6 类（'Long Term Care Hospital'/'Assisted Living' 未实测保留）；XOR 互斥两库 0 违规；'Other External'/'Other' 含义不明保守入分支 5；实测分布见 `config.py` 与 `eicu/labels.py` docstring）
- [x] D-4 eICU 出院 offset 已转换到 episode 坐标；（✅ 2026-07-30 关闭：`death_episode_min = hospitaldischargeoffset − episode_start_hospital_min`（Expired），出院/急性转出/存活出院均落 `*_episode_min`）
- [x] D-5 全部边界单元测试通过（§3.4）；存活出院分支优先于 observation-coverage 阴性；延迟录入/结果时间不得延长临床观察时间；`outcome_unknown_reason` 枚举与 schema 一致；**恰好 `t+24h` 存活出院时 `full_inhospital_followup_24h` 的定义已固定；`observation_end_source` 与实际选定 observation endpoint 一致；临床观察源白名单已锁定**。（✅ 2026-07-30 关闭：`src/data/tests/test_label_boundaries.py` 13/13 通过，含 P1-3 边界与延迟 storetime 反例固化；白名单冻结于 `contracts/clinical_observation_whitelist_v2.parquet`）
- [x] D-6 `label_adjudications` 表与 preliminary/final 分离机制已建立。（✅ 2026-07-30 关闭：MIMIC 78 条 pending + eICU 独立裁决表，仅 adjudicated 可覆盖）

### E. ECG 冻结（5 项）

- [x] E-1 pre-admission ECG 的 OR 条件已修正；（✅ 2026-07-30 关闭：`mimic/f8_ecg.py` 显式 OR 落地（same_hospitalization ∨ auditable_pre_admission_encounter，后者 ≤30d 且无其他住院覆盖、打 `pre_admission_ecg=TRUE`）；实测归属构成 171,520/39,840；ED 审计四条件参数候选见 E-2）
- [x] E-2 ED-to-admission 审计规则已固定（四条件参数预登记）；（✅ 2026-07-30 关闭：四条件落地 `f8_ecg.py`——①`edregtime` 非空且 ECG≥edregtime；②ECG≤max(admittime, edouttime)（实测 edouttime 常晚于 admittime，中位 −1.5h）；③|admittime−edouttime|≤24h（实测 p99=3.09h）；④无其他 encounter + 入院前 ≤30d；重跑后 pre-admission 39,840→37,988）
- [x] E-3 结构性 QC 已固定；（✅ 2026-07-30 关闭：可读 + 时长 ≥9s + 导联数 ≥8 + 非全平线，参数登记于 `preprocess/configs/preprocess_v1.yaml`；36,648 份全部过 QC，36,639 合格（99.98%），见 `p5_ecg_cache/ecg_qc_flags.parquet`）
- [x] E-4 数据驱动 QC 只在训练集拟合；（✅ 2026-07-30 关闭：5 指标（SNR/基线漂移/饱和/极端振幅/导联相关）阈值仅由 22,662 份训练集 study 拟合（零膨胀指标 p99 上界、其余 median±3MAD），`fitted_on=train` 登记 `p7_fitted/ecg_quality_thresholds.json` 与 registry；应用后 25,599/36,648（69.9%）通过）
- [x] E-5 24h 主配对队列定义已冻结（查看测试集结果前），防泄漏与选片统一使用 `ecg_available_time_assumed`。（✅ 2026-07-30 关闭：两层 QC（结构性 + 训练集数据驱动）后 `ecg_selected_for_model_frozen` = 78,305 landmarks；经标签可判定过滤后 `idx_paired_ecg` = 77,385（15,498 episodes，1,797 阳性 landmark）；选片/防泄漏统一 `ecg_available_time_assumed ≤ t_landmark`（断言通过）；P4/P9/P10 已按冻结选片重跑，paired 两包（train 49,180 / val 18,861 / test 9,344）索引哈希一致）

---

## 11. 实施顺序（阶段 A → B → C1 → C2 → D）

**阶段 A：协议与来源锁定（结束前不查看验证/测试集性能差异）**

1. mimic-code commit、派生 SQL/R 与 checksum 核对（§7.5）；配对函数参照实现锁定；
2. D0 审计与 PI 锁定（§3.1）；3. `2020 - 2022` amendment（§2.4）；
4. episode 定义、合并三规则、zero-gap 路径类别与两阶段输出机制锁定（C0/C6a）；eICU episode 四表建立；eICU 事件桥接表与 canonical JSON/SHA-256 规范化规则（含版本留痕）冻结；
5. 数据可用时间语义（§5.0）；eICU lab 专项报告（§5.3）；eICU 诊断时间语义专项报告（§2.2 C7）；MIMIC 生命体征 itemid 多信号分层审计（§5.2）；`administration_confirmation_availability` 来源评估（§2.2 C6b）；临床观察源白名单锁定（§4.1）；
6. 经典 SOFA 与 NEE 独立定义（§5.4/§5.5）；实时 SOFA 完整性、两轨与 delta 双分层锁定；**eICU 冻结候选规则表 PI 逐项签署**；Go/No-Go 数值预登记；
7. 关闭冻结清单 A 组、B-4/B-6/B-7、C-2/C-3/C-7。

**阶段 B：仅做 MIMIC 可行性队列（D0 候选口径可并行，不冻结）**

episode 两阶段映射（边表 + zero-gap 序列 → preliminary → adjudication 演练 → final，全局唯一 ID）→ episode 级 sepsis 聚合与 index episode → landmark（`status = ok` 门槛）→ 三态标签状态机（含临床观察终点与白名单）→ ECG 归属与五层级 availability → 主要 12 个 landmark 患者数/阳性数/ECG 覆盖率核对；**配对函数 MIMIC 回放验证**（Q1-16）。

**阶段 C1：MIMIC 特征工程**

available-time 特征（F1–F7）；`charlson_prior`（NULL 口径）；ECG 两层 QC 冻结；NEE 双实现核验；实时 SOFA 两轨重建与完整性评估；标签与 episode adjudication 机制运行；论文 2 人工标签验证（7 环节，PPV >80% 为 Go）。

**阶段 C2：SC-common 跨库合同（先于正式 MIMIC 模型训练）**

变量级单位映射、异常值范围、缺失定义、聚合规则、available-time、MIMIC/eICU 交叉库等价性评级——完成 `sc_common_variable_contract_v2`。**未完成 C2 不得开始正式 MIMIC 模型训练。**

**阶段 D：eICU 表型与可行性**

统一时间坐标与规范化事件标识 → eICU episode 四表 → `eicu_antibiotic_events` 与候选配对 → 锁定函数选对与回放核验 → 按锁定规则构建三套 phenotype → 经典 SOFA 复现 → 医院覆盖与 Go/No-Go 逐项核对 → `SC-common-core`（或 extended）终稿锁定 → 外验命名与层级确定。

**当前可进行**：来源审计；D0 双口径比较；episode 原型（含两阶段、zero-gap 序列与 adjudication 演练）；MIMIC 队列规模估算；三态标签原型；ECG 覆盖与归属统计；eICU 时间轴、episode 与表型可行性统计；SC-common 覆盖率与合同草拟；配对回放验证；序列化 round-trip 测试开发。

**当前不应进行（冻结生效前禁止）**：正式训练最终模型；选择超参数；查看测试集性能；依据 eICU AUROC 选择表型；依据模型效果决定 core/extended；正式跨库性能结论；把 eICU 称为完全同构 Sepsis-3 外部验证。

---

## 12. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：两库实测核验的可实施提取方案（DAG、输出规范、R1–R11、SQL 模板） |
| v2.0 | 2026-07-30 | 第一轮评审修订：D0 决策门；eICU 方向性配对与三套表型；episode 桥接表；available-time 契约；经典 SOFA CV；ECG 归属；三态标签；SC-common 分层；R12–R18；阶段 A–D |
| v2.1 | 2026-07-30 | 第二轮评审修订：表型时间合同；icustays 锚定；episode 级聚合；available-time 贯穿；SOFA 阈值修正；标签可判定性拆分；ECG OR 条件；冻结清单 28 项；R19–R22 |
| v2.2 | 2026-07-30 | 第三轮评审修订：episode 合并三规则；表型确定值规则表；P-clinical 前向算法；A.1 回填 hadm_id；A.3 标签状态机；SOFA 缺失规则；生命体征双轨；事件源主键；ECG 四时间字段；变量等价合同；冻结清单 31 项；阶段 C1/C2；R23–R26 |
| v2.3 | 2026-07-30 | 第四轮评审修订：preliminary 保守拆分与 episode adjudication；稳定 source_event_id；eicu_antibiotic_events；SOFA 6/6 完整性与去乳酸；诊断时间语义；last_observable 标签；配对选对字段；给药三率；charlson NULL；ECG 采集完成时间；Q1 新增 6 类测试；R27–R30 |
| v2.4 | 2026-07-30 | 第五轮评审修订：episode 两阶段输出（edges→preliminary→adjudications→final）；标签双观察终点与存活出院优先；source_event_id 规范化初版；A.5 降候选生成 + 锁定函数 + 回放验证；SOFA strict/carryforward 分离；baseline 分层；administration_confirmed 来源定义；episode 联合覆盖率；队列级 rate 移出；P-explicit 改名；gap=0 审计入边表；missing_or_open；estimand 声明；R31–R34 |
| v2.4.1 | 2026-07-30 | **第六轮评审修补（patch）**——①A.0 实际生成 `transfer_sequence` 并按预登记合法/异常路径类别判定 zero-gap 边（仅合法路径可 clean/merged）；②final decision 改显式 CASE（`final_decision ∈ {merged, split}`，NULL ⇒ pipeline failure）；③裁决范围冻结为仅 pending 边（clean/规则性决策不个案覆盖）；④`episode_id` 全局唯一（`MIMIC_<hadm_id>_<episode_seq>` + `(episode_id, episode_mapping_version)` 连接键）；⑤源事件改真正 canonical JSON（UTF-8/固定顺序/显式类型/JSON null/规范浮点/NFC/SHA-256），`source_event_id_version = eicu_source_event_sha256_v1`，拆 `raw_row_fingerprint`/`canonical_clinical_fingerprint` 与 `raw_exact_duplicate_count`/`canonical_duplicate_count`；⑥A.2 禁止 `episode_outtime IS NULL` 外推 landmark（主分析仅 `status = ok`，敏感性可选 ICU 内截尾时间）；⑦补建 eICU episode 四表（edges/preliminary/adjudications/final）与边级路径类别；⑧A.3 显式 NULL 分支、`observation_end_source` 与选定值一致、恰好 `t+24h` 出院 `full_inhospital_followup_24h = TRUE`、临床观察源白名单；⑨`delta_sofa_observed_complete` 与 `delta_sofa_phenotype` 物理分离；⑩`administration_confirmation_availability`（三态）与事件级 `administration_confirmed`（TRUE/FALSE/NULL）拆分；⑪SOFA purpose × evidence_track 二维命名，亚组统一 `sofa_realtime_strict_24h_cv`；⑫`episode_merge_threshold_min` 参数化（`main_tau0 / sensitivity_tau30 / sensitivity_tau60` 独立版本）；⑬Q1 新增六组测试（NULL episode end、zero-gap 四用例、final decision 状态空间、序列化 round-trip、eICU episode 五条、SOFA delta 分层）；⑭冻结清单 B-4/B-6/B-7/C-7/D-5 扩充；⑮风险 R35–R37。 |
| v2.4.1-sup | 2026-07-30 | **冻结推进记录（同日，31/31 项全部关闭，冻结标签生效）**——①**D0 锁定（A-1/A-2）**：出口 B，主原点 `suspected_infection_time`（依据：锁定版 mimic-code `a0af19c` 的 sepsis3 不输出 `sepsis_time`）；②**2020-2022 正式排除（A-3）**；③**eICU 表型签署（A-5）**：P-clinical 主外验、P-explicit 第二外验、P-strict 降 feasibility_only、命名 Robustness under phenotype shift；④**Go/No-Go 阈值原样预登记（A-6）**；⑤**A-4 关闭**：commit `a0af19c` 锁定，两 SQL 文件 SHA-256 登记（`_meta/mimic_code_reference/`），DB 构建来源经 PI 确认；⑥B 组全关闭：**B-5 终版回放（锁定 SQL 逐行移植）：行数 949,901=949,901、键匹配 1.000、标记 0.990、infection_time 0.977、culture_time 0.979**；⑦C 组全关闭：C-2 eICU lab revised 语义（5% 倒置、max 规则验证）、C-3 vitals 录入延迟分层（中位 15-26min）、C-5 SOFA 口径、C-7 完整性报告；⑧D 组全关闭：13 项边界单测、出院清单两库冻结；⑨E 组全关闭：E-2 ED 四条件（pre-admission 39,840→37,988）、E-4 训练集 QC 阈值（69.9% 通过）、E-5 冻结配对队列 78,305 landmarks（paired 包 49,180/18,861/9,344）；⑩新增 QA 产物：`sofa_realtime_completeness_v2.md`、`sofa_available_time_semantics_v2.md`、`vitals_dual_track_v2.md`、`eicu_lab_time_semantics_qa.md`、`eicu_diagnosis_time_semantics_qa.md`、`pairing_replay_validation_v2.md`、`derived_provenance_v2.md`、`eicu_sofa_computability_first_landmark.md`、`cv_subgroup_completeness.md`、`src/data/tests/test_label_boundaries.py`；⑪实测留痕：MIMIC 无 gap=0 边（main_tau0 无合并）、`internal_transfer_placeholder` 30min 候选参数、eICU 首个 landmark SOFA 可计算率 12.8%/45.7%（写论文局限性）、CV 亚组组分缺失 22.1%。 |

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

> **说明**：附录均为**概念性模板**，用于固定逻辑与边界语义，不构成完整实现；正式实施以各 DAG 节点脚本及 I/O schema 校验为准。附录模板配套 SQL 单元测试。正文输出表字段与 Parquet 实际列的一致性由 Q1-18 schema 测试保证。

### A.0 MIMIC 连续 ICU episode 映射（C0；transfer_sequence + 显式 final decision + 全局唯一 ID）

```sql
-- 阶段①：边级表 mimic_icu_episode_edges_preliminary（每对相邻 stay 一行）
WITH s AS (
  SELECT subject_id, hadm_id, stay_id, intime, outtime,
         LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_outtime,
         LAG(stay_id)  OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_stay_id
  FROM main.icustays
),
g AS (SELECT *, EPOCH(intime - prev_outtime) / 60.0 AS gap_minutes FROM s),
seq AS (   -- 实际生成 transfer_sequence（有序 JSON 数组；含 relative_position）
  SELECT g.*,
         (SELECT LIST({
            'transfer_id': t.transfer_id, 'eventtype': t.eventtype,
            'careunit': t.careunit, 'intime': t.intime, 'outtime': t.outtime,
            'relative_position': ROW_NUMBER() OVER (ORDER BY t.intime, t.transfer_id)
          } ORDER BY t.intime, t.transfer_id)
          FROM main.transfers t
          WHERE t.hadm_id = g.hadm_id
            AND t.intime < g.intime
            AND COALESCE(t.outtime, g.intime) > g.prev_outtime)
                                                        AS transfer_sequence
  FROM g
),
ev AS (
  SELECT s2.*,
         -- zero_gap_path_status：仅命中预登记合法路径类别方可 clean
         CASE
           WHEN gap_minutes <> 0 THEN 'not_applicable'
           WHEN transfer_sequence IS NULL THEN 'missing_boundary'
           WHEN <路径匹配 'ICU_A → ICU_B' 或 'ICU_A → internal_transfer_placeholder → ICU_B'>
             THEN 'clean'
           WHEN <路径含 ward/ED/重叠/未知单元/边界冲突>
             THEN 'anomaly'
           ELSE 'anomaly'
         END                                             AS zero_gap_path_status,
         <由 transfer_sequence 提取的 ward/ED/ICU 证据>     AS transfer_evidence
  FROM seq s2
)
SELECT hadm_id, prev_stay_id AS previous_stay_id, stay_id AS current_stay_id,
       gap_minutes,
       CASE WHEN gap_minutes < 0 THEN TRUE ELSE FALSE END   AS overlap_flag,
       transfer_sequence, zero_gap_path_status, transfer_evidence,
       <intervening_careunit 取自 transfer_sequence 中非 ICU 单元>,
       CASE WHEN prev_outtime IS NULL THEN 'split'
            WHEN gap_minutes < 0 THEN 'pending_review'
            WHEN transfer_evidence IN ('via_ward','via_ed') THEN 'split'
            WHEN gap_minutes <= <episode_merge_threshold_min>     -- 参数化：主值 0
             AND zero_gap_path_status IN ('clean','not_applicable') THEN 'merged'
            WHEN gap_minutes = 0 AND zero_gap_path_status = 'anomaly' THEN 'pending_review'
            ELSE 'split' END                                AS preliminary_decision,
       <episode_merge_exclusion_reason 同前规则>
FROM ev;

-- 阶段②：mimic_icu_episode_map_preliminary（仅 merged 延续，其余保守拆分）

-- 阶段③：episode_merge_adjudications（仅 pending_review 边进入人工裁决）

-- 阶段④：final decision（显式 CASE；禁止 COALESCE 产生 pending_review 终态）
SELECT e.*,
  CASE
    WHEN e.preliminary_decision = 'pending_review'
     AND a.adjudication_status = 'adjudicated'
     AND a.final_decision = 'merged' THEN 'merged'
    WHEN e.preliminary_decision = 'pending_review'
     AND a.adjudication_status = 'adjudicated'
     AND a.final_decision = 'split'  THEN 'split'
    WHEN e.preliminary_decision = 'pending_review'
      THEN 'split'                                    -- 未裁决：保守拆分
    WHEN e.preliminary_decision IN ('merged','split')
      THEN e.preliminary_decision
    ELSE NULL                                         -- ⇒ pipeline failure（Q1-8）
  END AS final_decision,
  CASE WHEN e.preliminary_decision = 'pending_review'
        AND (a.adjudication_status IS NULL OR a.adjudication_status <> 'adjudicated')
       THEN TRUE ELSE FALSE END AS unresolved_conservative_split
FROM mimic_icu_episode_map_preliminary_edges e
LEFT JOIN episode_merge_adjudications a
  ON a.hadm_id = e.hadm_id
 AND a.previous_stay_id = e.previous_stay_id
 AND a.current_stay_id = e.current_stay_id;
-- 阶段④续：按 final_decision 重新计算 episode 指派并生成全局唯一 ID：
--   episode_id = 'MIMIC_' || hadm_id::VARCHAR || '_' || episode_seq::VARCHAR
--   episode_mapping_version ∈ {main_tau0, sensitivity_tau30, sensitivity_tau60}
--   测试：COUNT(*) = COUNT(DISTINCT episode_id)
```

### A.1 MIMIC 队列骨架（C1–C3；基于 final map，连接含 mapping_version）

```sql
WITH sepsis AS (
  SELECT s.subject_id, i.hadm_id, s.stay_id,
         s.suspected_infection_time AS t_sepsis       -- D0 锁定后替换
  FROM mimiciv_derived.sepsis3 s
  JOIN main.icustays i USING (stay_id)
  WHERE s.sepsis3
),
ep_ranked AS (
  SELECT e.episode_id, s.stay_id, s.t_sepsis,
         COUNT(*) OVER (PARTITION BY e.episode_id) AS qualifying_sepsis_count,
         ROW_NUMBER() OVER (
           PARTITION BY e.episode_id
           ORDER BY s.t_sepsis NULLS LAST, s.stay_id
         ) AS rn
  FROM sepsis s
  JOIN mimic_icu_episode_map_final e
    ON e.subject_id = s.subject_id AND e.hadm_id = s.hadm_id AND e.stay_id = s.stay_id
   AND e.episode_mapping_version = 'main_tau0'        -- 主分析版本固定；敏感性版本独立运行
),
ep_sepsis AS (
  SELECT episode_id, qualifying_sepsis_count,
         t_sepsis AS t_sepsis_ts,
         stay_id AS t_sepsis_source_stay_id,
         'min_t_sepsis_within_episode' AS t_sepsis_selection_rule,
         CASE WHEN t_sepsis IS NULL THEN 'missing' ELSE 'ok' END AS t_sepsis_status
  FROM ep_ranked WHERE rn = 1
),
eligible AS (
  SELECT es.*, em.subject_id, em.hadm_id, em.episode_intime_ts, em.episode_outtime_ts,
         a.admittime, d.admission_age
  FROM ep_sepsis es
  JOIN (SELECT DISTINCT episode_id, subject_id, hadm_id,
                        episode_intime_ts, episode_outtime_ts
        FROM mimic_icu_episode_map_final
        WHERE episode_mapping_version = 'main_tau0') em USING (episode_id)
  JOIN main.admissions a USING (hadm_id)
  JOIN mimiciv_derived.icustay_detail d
    ON d.stay_id = es.t_sepsis_source_stay_id
  WHERE d.admission_age >= 18
    AND es.t_sepsis_status = 'ok'
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY subject_id
           ORDER BY t_sepsis_ts NULLS LAST, admittime, episode_intime_ts, episode_id
         ) AS rn
  FROM eligible
)
SELECT * FROM ranked WHERE rn = 1;
```

### A.2 Landmark 网格与风险集（MIMIC；status = ok 门槛，评审阻断项 3）

```sql
SELECT c.episode_id, k,
       c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' AS t_landmark_ts
FROM cohort_mimic_v2 c
JOIN main.admissions a USING (hadm_id)
CROSS JOIN generate_series(
  CAST(GREATEST(0, CEIL(EPOCH(c.episode_intime_ts - c.t_sepsis_ts) / 21600)) AS INTEGER),
  27) AS t(k)
WHERE c.episode_outtime_status = 'ok'                      -- 主分析硬门槛
  AND c.episode_outtime_ts IS NOT NULL
  AND c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' < c.episode_outtime_ts
  AND (a.deathtime IS NULL
       OR c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' < a.deathtime);
-- episode_outtime_status = 'missing_or_open'：主分析零 landmark，进 QA，修复后重跑
-- 敏感性截尾（可选，另版本）：AND t_landmark_ts < episode_landmark_censor_time
--   （episode_landmark_censor_time 来自 ICU 内临床事件白名单
--     last_clinically_observed_in_icu_time；Q1-13 测试）
```

### A.3 三态 24h 标签状态机（MIMIC；显式 NULL + 来源一致 + t+24h 边界）

```sql
WITH disp AS (
  SELECT hadm_id,
         CASE WHEN discharge_location IN (/* 急性转出清单 */) THEN dischtime END AS acute_transfer_time,
         CASE WHEN discharge_location IN (/* 存活出院清单 */) THEN dischtime END AS alive_discharge_time
  FROM main.admissions
),
obs AS (   -- 双观察终点（白名单见 §4.1 contracts/clinical_observation_whitelist_v2）
  SELECT c.episode_id, c.hadm_id,
         CASE
           WHEN a.dischtime IS NOT NULL AND x.last_clinical_event_ts IS NOT NULL
             THEN LEAST(a.dischtime, x.last_clinical_event_ts)   -- 临床事件不得晚于 dischtime；
           WHEN a.dischtime IS NOT NULL THEN a.dischtime         --   超出者进时间异常 QA（Q1-14）
           ELSE x.last_clinical_event_ts
         END AS last_clinically_observed_time,
         x.last_database_available_ts AS last_database_available_time,
         CASE                                                       -- 来源与实际选定值一致
           WHEN a.dischtime IS NOT NULL
            AND (x.last_clinical_event_ts IS NULL
                 OR a.dischtime <= x.last_clinical_event_ts)
             THEN 'discharge'
           WHEN x.last_clinical_event_ts IS NOT NULL THEN 'clinical_event'
           ELSE 'unknown'
         END AS observation_end_source
  FROM cohort_mimic_v2 c
  JOIN main.admissions a USING (hadm_id)
  LEFT JOIN last_inhospital_activity x USING (hadm_id)   -- 仅白名单事件/采集时间（概念性）
),
base AS (
  SELECT l.episode_id, l.k, l.t_landmark_ts,
         l.t_landmark_ts + INTERVAL '24 hours' AS w_end,
         a.hospital_expire_flag, a.deathtime, a.dischtime,
         d.acute_transfer_time, d.alive_discharge_time,
         o.last_clinically_observed_time, o.last_database_available_time,
         o.observation_end_source
  FROM landmarks_v2 l
  JOIN cohort_mimic_v2 c USING (episode_id)
  JOIN main.admissions a USING (hadm_id)
  LEFT JOIN disp d       USING (hadm_id)
  LEFT JOIN obs o        USING (episode_id, hadm_id)
),
state AS (
  SELECT *,
    CASE
      WHEN deathtime IS NOT NULL AND deathtime <= t_landmark_ts
        THEN 'invalid_input'
      WHEN deathtime IS NOT NULL AND hospital_expire_flag = 0
        THEN 'status_conflict'
      WHEN hospital_expire_flag = 1 AND deathtime IS NULL
        THEN 'death_time_missing'
      WHEN deathtime > t_landmark_ts AND deathtime <= w_end
        THEN 'event'
      WHEN acute_transfer_time > t_landmark_ts AND acute_transfer_time <= w_end
        THEN 'acute_transfer'
      WHEN alive_discharge_time > t_landmark_ts AND alive_discharge_time <= w_end
        THEN 'non_event_alive_discharge'              -- 存活出院优先于 coverage
      WHEN last_clinically_observed_time >= w_end
        THEN 'non_event_observed'
      ELSE 'missing_status_left_observation'
    END AS label_state
  FROM base
)
SELECT episode_id, k, t_landmark_ts,
  CASE WHEN label_state = 'event' THEN 1
       WHEN label_state IN ('non_event_observed','non_event_alive_discharge') THEN 0
       ELSE NULL END                                              AS y_24h,
  CASE WHEN label_state = 'event' THEN 'event'
       WHEN label_state IN ('non_event_observed','non_event_alive_discharge') THEN 'non_event'
       ELSE 'unknown' END                                         AS label_status,
  (label_state IN ('event','non_event_observed','non_event_alive_discharge'))
                                                                  AS outcome_ascertainable,
  CASE WHEN label_state = 'non_event_observed' THEN TRUE          -- t+24h 边界（P1-3）
       WHEN label_state = 'non_event_alive_discharge'
        AND alive_discharge_time >= w_end THEN TRUE
       ELSE FALSE END                                             AS full_inhospital_followup_24h,
  CASE WHEN label_state IN ('acute_transfer','death_time_missing','status_conflict',
                            'missing_status_left_observation','invalid_input')
        THEN label_state END                                      AS outcome_unknown_reason,
  label_state                                                     AS label_reason,
  last_clinically_observed_time, last_database_available_time, observation_end_source
FROM state;
-- 冲突与 missing：QA 复核写入 label_adjudications，不改写本自动提取结果
```

### A.4 eICU canonical 事件标识与时间坐标（C6a；canonical JSON + SHA-256）

```sql
-- canonical JSON 规范（冻结；字段顺序逐表 schema 冻结；显式类型；JSON null）：
-- {
--   "source_table": "medication",
--   "patientunitstayid": 123,
--   "drugorderoffset": 10,
--   "drugstartoffset": null,
--   "drugstopoffset": 50,
--   "drugname": "norepinephrine",
--   "routeadmin": "iv",
--   "dosage": null,
--   "frequency": null
-- }
-- 规范细节：UTF-8；浮点用预登记规范十进制表示；offset 用十进制整数；
--   字符串标准 JSON escaping；Unicode NFC；布尔 true/false
-- 双指纹：
--   raw_row_fingerprint          -- 原始值（仅排除加载元数据）
--   canonical_clinical_fingerprint -- trim / 大小写统一 / 单位文本规范化
-- ID 与版本：
--   source_event_id = SHA256(canonical_clinical_serialization)
--   source_event_id_version = 'eicu_source_event_sha256_v1'
-- 双计数与守恒：
--   raw_exact_duplicate_count      -- SUM(...) = 物理源行数（Q1-11）
--   canonical_duplicate_count      -- 折叠规则逐表预登记并留痕
-- round-trip 测试（Q1-11）：可无歧义反序列化；恶意值（'|'、'='、'<NULL>'、空串、
--   真正 NULL、空格、Unicode、换行、引号、科学计数法、NaN/Inf）不碰撞
SELECT patientunitstayid, patienthealthsystemstayid, uniquepid,
       -hospitaladmitoffset                       AS unit_start_hospital_min,
       -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min
FROM main.patient;
-- hospital_offset_min = -hospitaladmitoffset + local_offset_min
-- episode_offset_min  = hospital_offset_min - episode_start_hospital_min
-- eICU episode 两阶段输出：eicu_episode_edges_preliminary（edge_path_class 六类）→
--   eicu_episode_map_preliminary → eicu_episode_merge_adjudications → eicu_episode_map_final
-- 结局同步转换：hospital_discharge_episode_min / death_episode_min（§4.1）
```

### A.5 eICU suspected infection 候选生成（C6b；candidate generation template）

```sql
-- 本模板仅生成候选 pair；最终选对由 select_suspected_infection_pairs_locked_v1 完成
-- （引用具体 mimic-code commit，阶段 A 锁定；覆盖七类情形；MIMIC 回放验证见 Q1-16）
SELECT ab.episode_id,
       ab.antibiotic_event_id, cx.culture_event_id,
       ab.antibiotic_time_episode AS ab_time,
       cx.culture_time_episode    AS cx_time,
       CONCAT(ab.antibiotic_event_id::VARCHAR, '__', cx.culture_event_id::VARCHAR)
         AS infection_pair_id          -- 候选 pair ID（非最终 suspected_infection_event_id）
FROM eicu_antibiotic_events ab
JOIN eicu_culture_events cx USING (episode_id)        -- 同一 final episode（Q1-10）
WHERE (ab.antibiotic_time_episode - cx.culture_time_episode) BETWEEN 0 AND 4320
   OR (cx.culture_time_episode - ab.antibiotic_time_episode) BETWEEN 0 AND 1440;
-- select_suspected_infection_pairs_locked_v1 输出：
--   candidate_pair_rank / pair_selection_status / pair_selection_rule /
--   pair_selection_rule_version / suspected_infection_event_id
```

### A.6 eICU 去甲肾上腺素速率解析（片段）

```sql
SELECT patientunitstayid, infusionoffset,
       TRY_CAST(drugrate AS DOUBLE) AS rate_value,
       REGEXP_EXTRACT(drugname, '\(([^)]*)\)', 1) AS unit_hint
FROM main.infusion_drug
WHERE drugname ILIKE 'Norepinephrine%';
-- 后续：unit_hint → μg/kg/min 换算 × 体重优先级（F5）；双实现核验字段见 §5.5
```

---

*本方案 v2.4.1 基于 2026-07-30 对两库的只读结构核验与六轮外部评审《总体评价》生成；与技术文档 v1.9 冲突之处以技术文档为准，需变更技术文档的事项（D0 出口 B、`2020-2022` 处理）须经 protocol amendment 正式登记。§10 冻结清单（31 项）全部关闭且五类冻结验证、Q1 自动测试、配对回放、源事件守恒、SOFA 时间窗完整性与 schema 一致性验证通过前，本方案不得作为正式主分析提取管线使用；通过后打标签 `SEPSIS-MM-DYN-data-pipeline-v2.4.1-freeze`。*
