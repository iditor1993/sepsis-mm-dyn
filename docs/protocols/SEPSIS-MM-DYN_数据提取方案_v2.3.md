# SEPSIS-MM-DYN 数据提取方案 v2.3

- 文档版本：v2.3
- 创建日期：2026-07-30（v1.0 同日创建；v2.0–v2.2 经三轮外部评审修订；v2.3 经第四轮外部评审修订）
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 修订依据：《总体评价》（2026-07-30 第四轮评审，对 v2.2 结论为「通过作为正式提取管线冻结候选版，但不建议当前直接冻结生效」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**正式冻结审核版**。核心规则结构及候选确定值已补全；**所有依赖专项语义审计、类别清单和 PI 决策的参数均已进入冻结清单，关闭后生效**（含：普通病房/ED/成人 ICU 清单、显式 sepsis 诊断字符串清单、抗生素 pattern 清单、急性转出清单、ECG ED-to-admission 阈值、diagnosis offset 语义、eICU lab revised time 语义、D0 主原点、Go/No-Go PI 签署）。**冻结生效条件**：§10 冻结清单（31 项，含扩充通过条件）全部关闭，且五类冻结验证（小规模人工抽查、主键唯一性测试、标签边界单元测试、available-time 泄漏测试、feasibility table 审核）与新增的一对一桥接/SOFA 完整性测试全部通过。满足后经审核升级为**正式主分析数据提取管线冻结版**。在此之前维持现行限制：允许来源审计、原型提取与可行性分析，禁止正式模型训练、超参数选择与测试集评估。

---

## 0. v2.2 → v2.3 修订总览

本节按第四轮评审《总体评价》的章节编号逐项登记修改落点。历史修订见 §12 变更日志。

### 0.1 冻结前强制关闭的 P0（评审 §二/§八，5 项）

| 评审编号 | 问题 | v2.3 落点 |
|---|---|---|
| P0-1 | A.0 的 `pending_review` 在 episode 编号逻辑中仍被隐式并入前一 episode（正文—SQL 不一致） | §2.1 C0 与附录 A.0 修正编号规则：**preliminary 映射中仅 `merged` 延续前 episode，`split` 与 `pending_review` 一律保守拆分**；新增独立裁决表 `episode_merge_adjudications`，最终映射 = preliminary + adjudication override，与标签 adjudication 同构，不改写自动结果 |
| P0-2 | A.5 声称按源主键回连，实际仍只按 `patientunitstayid` 连接（笛卡尔放大风险）；eICU 表未必有稳定原生主键 | §2.2 C6a 定义**稳定事件标识规则** `source_event_id = HASH(source_table, patientunitstayid, local_offset_min, canonicalized_fields)`；完全重复记录输出 `exact_duplicate_count` 并在建桥时裁决；新增 `eicu_antibiotic_events` 统一事件事实表，附录 A.5 直接使用事件表，不再反连原始 `medication`；桥接一对一守恒测试进 Q1 |
| P0-3 | 「≥5/6 组分即可计算总分」产生不标准的 5 组分 SOFA，与完整 SOFA 不可比并制造伪 ΔSOFA；乳酸误列入 SOFA 组分回溯清单 | §5.4 改为：`sofa_total_complete` **仅 6/6 可计算时生成**；5/6 仅输出 `sofa_partial_sum`（`sofa_total_status = partial_5_of_6`），**不得用于 ΔSOFA≥2 表型、完整 SOFA 亚组与标准阈值比较**；Go/No-Go 分列 6/6 完整率与 ≥5/6 可计算率；**乳酸从 SOFA 组分回溯清单删除**（乳酸仅作独立模型变量） |
| P0-4 | eICU `admission_dx`/`diagnosisoffset` 被直接称为真实 available time，缺乏数据库语义支持 | §2.2 C7 新增 `diagnosis_time / diagnosis_time_semantics / diagnosis_time_confidence` 三字段，语义取值 `observed_record_time / assigned_admission_proxy / retrospective_only / unknown`；`admission_dx` 明确定位为**入院时刻代理**；P-clinical/P-explicit 全文标注为「使用数据库诊断字段构建的回顾性临床表型」；审计关闭进冻结清单 A-5 |
| P0-5 | A.3 将 `dischtime IS NULL` 自动判为完整阴性（回顾性库中 NULL ≠ 仍在院） | §4.1 引入 `last_observable_hospital_time` 与 `observation_end_source`：仅 `last_observable ≥ t+24h` 方可判 `non_event_observed`，否则 `unknown / missing_status_left_observation`；附录 A.3 同步重写 |

### 0.2 重要 P1（评审 §三，10 项）

| # | 问题 | v2.3 落点 |
|---|---|---|
| 1 | transfers 区间匹配漏跨边界区间 | A.0 改区间**相交**条件 `t.intime < g.intime AND COALESCE(t.outtime, g.intime) > g.prev_outtime`；`gap = 0` 边界检查相邻 transfer 序列（前 stay 末端相邻 + 当前 stay 起点相邻 + 完整序列） |
| 2 | A.0 未输出正文承诺的全部字段（`merge_reason / episode_gap_max_min / episode_transfer_path_class / episode_mapping_version`） | A.0 最终 SELECT 补齐全部字段；§7.1 新增「正文定义与 Parquet 实际列一致」schema 测试 |
| 3 | 抗生素—培养候选配对缺去重与选对规则 | §2.2 C7/A.5 新增 `candidate_pair_rank / pair_selection_status / pair_selection_rule / suspected_infection_event_id`；优先级完全复现锁定版 mimic-code；`infection_pair_id` 仅为候选 pair ID |
| 4 | A.5 缺 `infusion_recorded` 路径 | 新增 `eicu_infusion_time_map`；`eicu_antibiotic_events` 五步构建（infusion_drug 识别 → medication 识别 → 同药相近时间去重 → 四级来源赋值 → 再配对） |
| 5 | 三个给药时间率的分母未定义 | §2.2 C6b：定义**事件级率**（分母＝全部最终选中抗生素事件）与 **episode 级覆盖率**（分母＝含抗生素证据 episode）；Go/No-Go 正式门槛预登记采用 **episode 级覆盖率**，事件级率作描述 |
| 6 | `charlson_prior = 0` 会被误读为无合并症 | §5.1：改为 `charlson_prior = NULL` + `charlson_prior_available = FALSE` + `prior_hospital_count = 0`；数值填充仅由训练集拟合的缺失处理器完成并保留缺失指示 |
| 7 | ECG 防泄漏应用采集开始时间 | §5.8：`ecg_available_time_assumed = ecg_acquisition_time + N_samples/f_s`（WFDB header 可得；不可得时预登记固定假设并留痕）；Q1 断言与选片规则统一改用 `ecg_available_time_assumed ≤ t_landmark` |
| 8 | A.1 未排除 `t_sepsis IS NULL` 的 episode | A.1 增加 `t_sepsis IS NOT NULL` 过滤（缺失 episode 打 `t_sepsis_status = missing` 进 QA）；患者级排序显式 `NULLS LAST` |
| 9 | SOFA「最差值」时间逻辑不明 | §5.4：`SOFA_d(t) = max SOFA_d(u), u ∈ (t−24h, t]` 且所有输入 `available(u) ≤ t`；血管活性药取 24h 最大剂量、MAP 取 24h 最小值；「当前生理状态分数」另命名 `sofa_current_state`，不与标准 24h SOFA 混用 |
| 10 | P-strict「复现锁定代码」与「缺失不计 0」可能冲突 | §5.4：`sofa_phenotype_locked` 逐字复现锁定代码**含其缺失假设**（写入局限性）；`sofa_realtime_available` 严格 available-time 缺失不计 0；**两者不共享缺失处理函数**；P-strict 明确使用 `sofa_phenotype_locked` |

### 0.3 表述微调（评审 §四，3 项）

①文档头部状态改为「核心规则结构及候选确定值已补全；依赖专项审计/清单/PI 决策的参数均已进入冻结清单，关闭后生效」（替代「全部可文档化规则已补全」）；②C7「正式规则表」更名「**冻结候选规则表**」，PI 签署后升级为正式锁定规则表；③C6b 明确**四级来源在每个最终抗生素事件上互斥赋值（仅取最高优先级一级），总和 100%**。

### 0.4 新增自动测试与冻结清单扩充（评审 §五/§六）

§7.1 新增 6 类测试（episode preliminary/final 映射、eICU 桥接一对一、SOFA 可比性、标签可观察性、ECG 可用性、表型证据时间）；§10 在**不增加总项数**的前提下扩充 B-7、B-4、C-7、A-5、D-5 的通过条件。

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
| 转科 | `main.transfers` | `transfer_id`；`eventtype, careunit, intime, outtime`（仅作 episode 路径审计） | 2,413,581 |
| 脓毒症表型 | `mimiciv_derived.sepsis3` | 每 stay 一行；`subject_id, stay_id, antibiotic_time, culture_time, suspected_infection_time, sofa_time, sofa_score, 六组分`（**无 `sepsis_time`、无 `hadm_id`**） | 41,295 |
| 疑似感染 | `mimiciv_derived.suspicion_of_infection` | 每次抗生素-培养配对一行 | 949,901 |
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
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath`（**时间语义审计见 §2.2 C7**） | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring` | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部原始事件时间为**相对各 unit stay 入科的分钟偏移（offset）**；出院年份仅 2014/2015，**无绝对日期**。多数 eICU 原始表**无稳定单列事件主键**，事件标识按 §2.2 C6a 规则生成。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C0–C5）

- **C0 连续 ICU episode 映射（`icustays` 锚定 + 三规则 + preliminary/adjudication 分离，评审 P0-1）**：以 `main.icustays` 为候选基础，同一 `hadm_id` 内按 `intime, stay_id` 排序，`gap_minutes = EPOCH(intime(j+1) − outtime(j)) / 60`。三条独立规则：

  1. **主合并阈值**：`gap_minutes ≤ τ_merge`，`τ_merge = 0 min`（30/60 min 仅敏感性，阶段 A 锁定主值）；
  2. **重叠处理**：`gap_minutes < 0` **不自动合并**，`overlap_flag = TRUE`、`episode_mapping_status = needs_review`，进入裁决流程；
  3. **路径佐证**：两 stay 间经 `transfers` 审计存在普通病房或 ED 区间时 `episode_merge_eligible = FALSE`（区间相交条件，附录 A.0）；`gap = 0` 边界还须核对相邻 transfer 序列（前 stay 末端相邻 transfer + 当前 stay 起点相邻 transfer + 完整序列）。

  **preliminary 映射编号规则（修正点）**：自动提取阶段**仅 `episode_merge_decision = 'merged'` 的 stay 延续前一 episode；`split` 与 `pending_review` 一律保守生成新 episode**——语义与 SQL 一致，重叠记录绝不被隐式合并。

  **合并裁决独立表（与标签 adjudication 同构）**：

  ```text
  episode_merge_adjudications
  - hadm_id, previous_stay_id, current_stay_id
  - preliminary_decision            -- 自动提取结果（不改写）
  - final_decision                  -- merged / split（人工复核）
  - adjudication_status             -- pending / adjudicated / rejected
  - adjudication_reason / adjudicator / adjudication_datetime
  ```

  **最终 episode 映射 = preliminary decision + adjudication override**；任何人工裁决不改写 preliminary 字段（Q1 测试）。

  输出桥接表（全部字段与附录 A.0 对齐，schema 一致性测试见 §7.1）：

  ```text
  mimic_icu_episode_map
  - subject_id, hadm_id, episode_id
  - stay_id, stay_seq_in_episode
  - episode_intime_ts, episode_outtime_ts
  - gap_minutes, merge_reason                      -- first_stay / contiguous / overlap / gap
  - overlap_flag
  - intervening_careunit, transfer_evidence        -- direct_icu_to_icu / brief_icu_exit / via_ward /
                                                   --   via_ed / overlap_or_anomaly / none
  - episode_merge_eligible, episode_merge_decision, episode_merge_exclusion_reason
  - episode_mapping_status                         -- clean / needs_review / adjudicated
  - episode_merge_threshold_min                    -- 实际 τ_merge 留痕
  - episode_gap_max_min                            -- episode 内最大间隙（汇总）
  - episode_transfer_path_class                    -- 全 episode 路径分类汇总
  - episode_mapping_version                        -- 映射规则版本
  ```

  约束：每个 `stay_id` 恰好一个 `episode_id`（裁决后）；`needs_review` 进 QA。

- **C1 脓毒症相关 episode 池（episode 级 sepsis 聚合）**：`mimiciv_derived.sepsis3` 先经 `main.icustays` 回填 `hadm_id`，再按 C0 归属 episode，同一 episode 多命中 stay 先聚合：

  ```text
  mimic_episode_sepsis
  - episode_id
  - qualifying_sepsis_count
  - t_sepsis_ts                    -- min_j t_sepsis,j（D0 出口 A 时按锁定代码规则替换）
  - t_sepsis_source_stay_id        -- 按 t_sepsis NULLS LAST, stay_id 确定性取
  - t_sepsis_selection_rule        -- 'min_t_sepsis_within_episode'
  - t_sepsis_status                -- ok / missing（全 NULL 时）
  ```

  **`t_sepsis_status = missing` 的 episode 在 eligible 阶段排除并写 QA**（评审 §三.8）。

- **C2 入排初筛**：年龄 ≥18（源 stay 的 `admission_age`）；成人 ICU（episode 首 stay `first_careunit`，类别清单 QA 实测后预登记）。

- **C3 index episode 选择**：全部合格 episode（每 episode 一行）按 `subject_id` 取首次，排序键 `t_sepsis_ts NULLS LAST, admittime, episode_intime_ts, episode_id`。`first_icu_stay` 仅描述。

- **C4 探索性/敏感性标志**：外院转入、landmark 前 ECMO、近 90 天实体器官移植、landmark 前 DNR/CCO——**PPV 抽查前不作正式排除**；ICD 类标志仅用既往住院记录。

- **C5 队列事实表** `cohort_mimic_v2`（每 episode 一行）：`subject_id, hadm_id, episode_id, t_sepsis_source_stay_id, t_sepsis_ts（D0 锁定后生效）, episode_intime_ts, episode_outtime_ts, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C10）

- **C6a 住院级统一时间坐标 + 稳定事件标识（评审 P0-2）**：

  ```text
  t_hospital_min = -hospitaladmitoffset + eventoffset
  episode_offset_min = hospital_offset_min - episode_start_hospital_min
  ```

  **稳定事件标识规则**：多数 eICU 原始表无原生单列主键，统一生成：

  ```text
  source_event_id = HASH(source_table, patientunitstayid, local_offset_min, canonicalized_event_fields)
  -- canonicalized_event_fields 示例（medication）：lower(trim(drugname)) | routeadmin |
  --   drugorderoffset | drugstartoffset；各表规则逐表预登记
  ```

  完全重复记录（哈希相同）：输出 `exact_duplicate_count`，建桥时裁决保留一行并计数。**禁止**仅依赖无稳定排序保障的 `ROW_NUMBER()` 生成永久主键（除非排序完全确定且在冻结快照中持久化）。

  输出桥接表：

  ```text
  eicu_unitstay_timeline
  - patientunitstayid, patienthealthsystemstayid, uniquepid
  - unit_start_hospital_min / unit_end_hospital_min
  - episode_id, episode_start_hospital_min / episode_end_hospital_min

  eicu_event_time_map                      -- 泛化映射（含源标识）
  - event_type, source_table, source_event_id
  - patientunitstayid, local_offset_min
  - hospital_offset_min, episode_offset_min
  - exact_duplicate_count
  ```

  **专用桥接表**（与源表经 `source_event_id` 一对一回连，Q1 双向守恒测试）：

  ```text
  eicu_medication_time_map      (source_table = medication)
  eicu_microbiology_time_map    (source_table = micro_lab)
  eicu_lab_time_map             (source_table = lab)
  eicu_infusion_time_map        (source_table = infusion_drug)   -- 新增（评审 §三.4）
  ```

  episode 合并规则：同一 `patienthealthsystemstayid` 内相邻间隙 ≤ `τ_merge_eicu = 0 min`（敏感性阈值阶段 A 锁定）者合并；存在非 ICU 区间者不合并并打标；`readmit` 按同一规则判定并单独打标。pending 情形参照 MIMIC C0 的 preliminary/adjudication 分离机制处理。

- **C6b 统一抗生素事件表 + suspected infection 重建（评审 P0-2/§三.4/§三.5）**：先构建统一抗生素事件事实表（五步）：

  ```text
  eicu_antibiotic_events 构建步骤：
  ① infusion_drug 识别抗菌药（药名正则清单预登记）→ infusion_recorded 候选
  ② medication 识别抗菌药 → scheduled_start / order_time 候选
  ③ 同药、相近时间记录去重（episode 坐标，间隙阈值预登记）
  ④ 按四级来源赋值：administration_confirmed > infusion_recorded >
     scheduled_start > order_time —— 每个最终事件仅赋予一个最高优先级来源，
     四类互斥、总和 100%（评审 §四.3）
  ⑤ 输出事件表：
     antibiotic_event_id / source_table / source_event_id / patientunitstayid /
     episode_id / antibiotic_time_episode / antibiotic_time_raw /
     drug_name_normalized / antibiotic_time_source / antibiotic_time_confidence
  ```

  抗生素事件与培养事件（`eicu_culture_events`，同构构建）**在 episode 坐标上按 `episode_id` 配对**，方向性规则（窗口随锁定版 mimic-code）：

  ```text
  培养先发生：  t_antibiotic - t_culture  ∈ [0, 72h]
  抗生素先发生：t_culture - t_antibiotic  ∈ [0, 24h]
  ```

  **给药时间三率（分母明确，评审 §三.5）**：

  ```text
  source_rate_event      = 该来源最终选中抗生素事件数 / 全部最终选中抗生素事件数
                           （administration_confirmed_rate / infusion_recorded_rate /
                             non_administration_time_rate 三率分列，描述性）
  source_coverage_episode = 含 ≥1 个该来源事件的 episode 数 / 含抗生素证据 episode 数
                           （Go/No-Go 正式门槛采用 episode 级覆盖率）
  ```

- **C7 三套可行性表型队列 + 表型时间合同（双层结构 + 冻结候选规则表）**：

  **第一层：固定合同字段**（结构冻结）：

  ```text
  phenotype_event
  - episode_id
  - infection_evidence_time           -- t_I（episode 坐标）
  - infection_evidence_type           -- culture_antibiotic_pair / admission_dx / later_dx / explicit_sepsis_dx
  - sofa_baseline_window_start / sofa_baseline_window_end
  - sofa_qualifying_window_start / sofa_qualifying_window_end
  - baseline_sofa / qualifying_sofa / delta_sofa / sofa_qualifying_time
  - t_sepsis_offset_min / t_sepsis_rule / phenotype_track
  - infection_pair_id                 -- 候选 pair 溯源
  - diagnosis_time / diagnosis_time_semantics / diagnosis_time_confidence   -- 评审 P0-4
  ```

  **诊断时间语义（评审 P0-4）**：

  ```text
  diagnosis_time_semantics:
    observed_record_time       -- 经审计确认的记录时间（可作严格 available time）
    assigned_admission_proxy   -- admission_dx 指定为入院时刻的代理时间（非真实可用时间）
    retrospective_only         -- 仅回顾性用途，不得进入 strict_available_time 轨道
    unknown                    -- 语义未核验（核验前一律按 retrospective_only 处理）
  ```

  `admission_dx` 明确定位为**入院时刻代理**（`assigned_admission_proxy`），不代表数据库证明该条目在入院时刻已电子可用；`diagnosis.diagnosisoffset` 的语义（首记/更新/生效/抽象相对时间）须专项审计。**P-clinical 与 P-explicit 整体定位为：使用数据库诊断字段构建的回顾性临床表型**——用于外部稳健性分析，但其 `t_sepsis` 不作严格临床实时解释，不与 MIMIC Sepsis-3 时间原点声称等价。审计关闭纳入冻结清单 A-5。

  **第二层：冻结候选规则表（PI 逐项签署后升级为正式锁定规则表）**：

  | 参数 | P-strict | P-clinical | P-explicit |
  |---|---|---|---|
  | 定位 | 严格 Sepsis-3 复现（用 `sofa_phenotype_locked`） | 临床感染证据 + 器官功能障碍（回顾性临床表型） | **显式临床诊断表型**（回顾性；不暗示与 P-strict 等价） |
  | 感染证据 | C6b 抗生素-培养配对（选中 pair） | 感染诊断证据（`admission_dx` 与 `later_dx` 分开） | 显式 sepsis / severe sepsis / septic shock 诊断字符串（清单预登记） |
  | 感染时间 t_I | 配对两事件中较早者（随锁定版 mimic-code） | 首个感染诊断时间：`admission_dx` = 入院时刻代理；`later_dx` = `diagnosis_time`（按语义分层） | 首个显式 sepsis 诊断时间（同左规则） |
  | SOFA 基线窗口 | 完全复现锁定版 mimic-code（含其缺失假设） | `[t_I − 48h, t_I − 24h]` 末次**完整** SOFA；无先前完整 SOFA 时 baseline = 0 并打 `baseline_assumed_zero = TRUE`（敏感性排除） | 不适用（描述性报告） |
  | SOFA 合格窗口 | 同锁定代码 | `[t_I − 24h, t_I + 48h]` 内 ΔSOFA ≥ 2（两端均须完整 6/6 且同规则版本） | 不适用 |
  | ΔSOFA ≥2 | 必须 | 必须 | 不必须 |
  | t_sepsis 规则 | 同锁定代码 | `t_sepsis = t_I`，资格由合格窗口 ΔSOFA ≥2 确认；`t_sepsis_rule = 'infection_evidence_time_with_qualifying_delta_sofa'` | `t_sepsis = t_I`；`t_sepsis_rule = 'first_explicit_sepsis_dx_available_time'` |

  **P-clinical 前向算法（防循环定义）**：①按 episode 时间升序排列候选感染证据；②逐 `t_I` 在固定窗口搜索 ΔSOFA ≥2；③首个满足者生成 `t_sepsis = t_I` 候选；④仅 `t_sepsis` 前可用的诊断记录作描述/验证变量，禁止最终出院诊断反推；⑤诊断作为证据时 `t_I = diagnosis_time`。

  **候选配对去重与选对规则（评审 §三.3）**：配对阶段允许一事件产生多个候选 pair；每个候选输出 `infection_pair_id`（候选 ID，非最终事件 ID）、`candidate_pair_rank`、`pair_selection_status`（selected / candidate / rejected）、`pair_selection_rule`（完全复现锁定版 mimic-code 的配对优先级：同一培养命中多个抗生素、同一抗生素命中多个培养、完全同时间排序、多 infection event 的 index 选择均随锁定代码）；最终生成 `suspected_infection_event_id` 供 C7 使用。

  三套队列分别报告：患者数、医院数、院内死亡数、各 landmark 阳性数、SC-common 特征覆盖率、与 MIMIC 主队列基线差异。

  **Go/No-Go 门槛（确定建议值；PI 确认后预登记，禁止按模型效果反向调整）**：

  | 指标 | 阈值 | 说明 |
  |---|---|---|
  | P-strict 覆盖医院数 | ≥ 20 家，且最大单医院患者占比 ≤ 25% | 避免单中心主导 |
  | 患者数 | P-strict ≥ 500；P-clinical / P-explicit ≥ 2,000 | 外验最低规模 |
  | 院内死亡事件数 | ≥ 100 | 月 1 样本量分析复核 |
  | 主要 landmark 可估计比例 | 12 个中满足「阳性 ≥20 且阴性 ≥100」者 ≥ 10 个 | 技术文档 §5.1 |
  | 培养覆盖率 | P-strict ≥ 5% 候选 ICU episodes | 当前实测约 1.5% |
  | 给药时间可靠率（正式门槛） | **episode 级覆盖率**（administration_confirmed 或 infusion_recorded）≥ 30% | 事件级三率分列作描述；四级来源互斥总和 100% |
  | SOFA 可计算率（分列） | 首个有效 landmark 处：**6/6 完整总分率** ≥ 60%**且** ≥5/6 组分可计算率 ≥ 70% | 两率分别报告（评审 P0-3） |

  **外验命名决策（建模前锁定）**：默认预期 **Robustness under phenotype shift**；不得依据 eICU AUROC 反向选择表型。

- **C8 入排与 index episode**：年龄 ≥18（`"> 89"` 记 90 并打标）；同一 `uniquepid` 按 `t_sepsis_offset_min NULLS LAST, hospitaladmitoffset, episode_start_hospital_min, episode_id` 确定性排序取首次。

- **C9/C10 队列事实表** `cohort_eicu_v2`（与 C5 同构，episode 坐标分钟）：`episode_id, index_patientunitstayid, patienthealthsystemstayid, uniquepid, t_sepsis_offset_min（C7 锁定后生效）, episode_start_offset_min(=0), episode_end_offset_min, hospitaladmitoffset, hospital_discharge_episode_min, hospitaldischargestatus, hospitaldischargelocation, age_num, gender, unittype, hospitalid, phenotype_track, administration_confirmed_rate, infusion_recorded_rate, non_administration_time_rate, source_coverage_episode, 敏感性标志`。

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

历史别名一律映射到上表。eICU 凡涉及结局与标签的时间一律先转换为 `*_episode_min`（§4.1）。

### 2.4 内部时间划分（技术文档 §12.2 落地）

实测 `anchor_year_group` 为 5 类，映射固定（人数为**全库 `patients` 表人数**，队列口径数字由阶段 B 产出）：

| 集合 | anchor_year_group | 全库 patients 表人数（参考） |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873 |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 须经阶段 A 正式 amendment：排除理由、**完全不查看结局与模型性能**、是否仅保留为扩展数据（风险 R2）。划分按 `subject_id`；`split_assignments_v2` 落盘冻结。**独立 calibration 集当前不单独划分**；未来启用 CP 探索需先在本文档与划分表中显式定义。

> 对外表述规范：称为「**基于 anchor_year_group 的时间组外验证**」，不得过度解释为精确日历年份上的时间外验证。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time —— 决策门 D0（未锁定）

**当前状态：t_sepsis 未锁定。** 本地 `mimiciv_derived.sepsis3` 不含技术文档 §4.1 规定的 `sepsis_time`（实有 `suspected_infection_time` 与 `sofa_time`）。时间原点属 estimand 级决策。

**D0 前置审计（阶段 A）**：①定位 `sepsis3` 生成 SQL/R 脚本；②记录 mimic-code 版本、commit hash、原始 SQL、本地修改；③明确 `sofa_time` 与 `suspected_infection_time` 生成逻辑；④确认 `sepsis_time` 的应有对应。

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

eICU 侧：`t_sepsis_offset_min` 由 C7 合同按 `t_sepsis_rule` 合成，与 D0 一致性登记；P-clinical/P-explicit 的 `t_sepsis` 按 `diagnosis_time_semantics` 分层解释（§2.2 C7），不作严格实时解释。敏感性分析保留三种时间原点对比（技术文档 §4.1/§15.2）。

`Δ_ICU-sepsis = episode_intime − t_sepsis`（eICU 为 `0 − t_sepsis_offset_min`），显式输入特征。

### 3.2 Landmark 生成（DAG 节点 L1）

对每个 index episode：

1. `k0 = max(0, ceil((episode_intime − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_offset_min) / 360min))`。
2. `t_landmark(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. 终止规则：`t_landmark(k) < min(episode 终点, 死亡时间)`；ICU 转出至病房后停止生成新 landmark，已生成 landmark 的 24h 随访继续完成。
4. 主分析积分网格固定 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v2`：`episode_key, subject_key, k, t_landmark_ts / t_landmark_offset_min, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2）

landmark t 纳入：t 时刻存活且仍处于连续 ICU episode 内。排除：t 前或 t 时刻已死亡（MIMIC `deathtime ≤ t`；eICU `Expired 且 death_episode_min ≤ t_landmark_offset_min`）；t 前或 t 时刻 episode 已结束。

### 3.4 边界条件（全部转化为单元测试）

| 情形 | 判定 |
|---|---|
| landmark 时刻恰好死亡 | 不进入风险集 |
| landmark 时刻恰好 episode 结束 | 不进入风险集 |
| 死亡发生在 `(t, t+24h]` | 阳性（含恰好 `t+24h`） |
| 出院恰好发生在 `t+24h` | 按存活至窗口终点（阴性，存活出院） |
| ECG 恰好在 landmark 时刻完成采集 | 允许使用（`ecg_available_time_assumed ≤ t_landmark`，§5.8） |
| 特征恰好在 landmark 时刻可获得 | 允许使用（`available_time ≤ t_landmark`） |
| 死亡时间早于 admittime 或晚于 dischtime 且无院内死亡标志 | 时间异常，进 QA |
| `hospital_expire_flag = 1` 且 `deathtime` 缺失 | `unknown / death_time_missing`（§4.1） |
| `deathtime` 非空且 `hospital_expire_flag = 0` | `unknown / status_conflict`，待 adjudication |
| 标签脚本独立运行时遇到 `deathtime ≤ t_landmark` | `invalid_input` |
| `dischtime IS NULL` 且 `last_observable_hospital_time < t+24h` | `unknown`，`outcome_ascertainable = FALSE`（§4.1） |

---

## 4. 结局标签（DAG 节点 L3）

### 4.1 主结局：landmark 后 24h 院内全因死亡（状态机 + 可观察性 + adjudication 分离）

**标签字段（两库同构）**：

```text
y_24h            : 1 / 0 / NULL
label_status     : event / non_event / unknown
outcome_ascertainable      : TRUE / FALSE   -- 主分析纳入依据
full_inhospital_followup_24h : TRUE / FALSE -- 描述性
outcome_unknown_reason : NULL / acute_transfer / missing_status_left_observation
                         / death_time_missing / status_conflict / time_anomaly / invalid_input
label_reason     : 状态机分支标识
last_observable_hospital_time  -- 可证明患者在院的最晚时间（评审 P0-5）
observation_end_source         -- 该时间的来源（dischtime / 最晚活动记录 / 推断规则）
```

**最后可观测时间（评审 P0-5）**：`last_observable_hospital_time` = 可证明患者在院内被观察的最晚时间，取 `dischtime` 与可证明院内活动的最晚记录时间（护理/检验/输注等，来源规则预登记）之较大者；`observation_end_source` 记录取值来源。**仅当 `last_observable_hospital_time ≥ t+24h` 时方可判完整阴性**；`dischtime IS NULL` 在回顾性已发布数据库中**不得**解释为「仍在院」（也可能是出院时间缺失、记录不完整、连接失败、异常住院或数据截断）。

**状态机（按序执行，首个命中分支生效；附录 A.3 同构实现）**：

-1. **非法输入防护**：`deathtime ≤ t_landmark` → `invalid_input`；
0. **死亡状态冲突预检**：`deathtime` 非空 `AND hospital_expire_flag = 0` → `unknown / status_conflict`；`hospital_expire_flag = 1 AND deathtime IS NULL` → `unknown / death_time_missing`（均 `outcome_ascertainable = FALSE`，先 unknown）；
1. `(t, t+24h]` 内院内死亡 → `y_24h = 1`（event）；
2. `(t, t+24h]` 内急性转出 → `NULL`（unknown，`acute_transfer`）；
3. **`last_observable_hospital_time ≥ t+24h` 且未死亡** → `y_24h = 0`（non_event，`non_event_observed`）；
4. `(t, t+24h]` 内明确存活出院 → `y_24h = 0`（non_event，`non_event_alive_discharge`；`full_inhospital_followup_24h = FALSE` 但 `outcome_ascertainable = TRUE`）；
5. 其余（含 `dischtime IS NULL` 且可观察期不足、结局状态缺失且已离开可观测范围）→ `NULL`（unknown，`missing_status_left_observation`）。

**人工 adjudication（与自动提取分离）**：冲突与缺失记录不在原始标签 SQL 中隐式处理，QA 复核写入独立表：

```text
label_adjudications
- episode_key, landmark_k
- label_preliminary_status / label_final_status
- label_adjudication_status   -- pending / adjudicated / rejected
- label_adjudication_source
```

下游默认用 `label_preliminary_status`；仅 `adjudicated` 时以 `label_final_status` 覆盖，覆盖比例进 QA 报告。

**派生字段口径**：`acute_transfer_time` 与 `alive_discharge_time` 由 `dischtime + discharge_location` 分类派生，**XOR 互斥**；同时命中按急性转出优先并打 QA 标记；类别清单两库分别实测后预登记（风险 R9）。

**eICU 统一坐标**：`hospital_discharge_episode_min / death_episode_min`；**所有标签代码只使用 `*_episode_min`**。eICU 的可观察期依据：unit 出院 offset、后续护理/检验记录存在性（规则预登记）。

### 4.2 次要结局：7 天竞争风险（四类事件）

```text
event_type: 0 = administrative censoring / 1 = in-hospital death /
            2 = alive discharge / 3 = transfer to another acute hospital
```

同时刻优先级：死亡 > 急性转出 > 存活出院 > 删失。急性转出事件不足时按技术文档 §15.2 降级。eICU 一律 `*_episode_min`。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（`sofa_realtime_available` 完整总分增加 ≥2）、新启用血管活性药（NEE 流由 0 转 >0）。

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

**双轨结果报告**：`strict_available_time`（主分析）与 `chart_or_event_time`（回顾性敏感性）分别报告；无法获得真实可用时间的域不得并入严格实时主模型，论文明确为 **retrospective chart-time prediction**。`diagnosis_time_semantics = retrospective_only` 的证据**不得进入 strict 轨道**（Q1 测试）。

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
| Charlson | **`charlson_prior`（NULL 口径，见下）** | `past_history` 自建近似 | baseline_static；移出 SC-common |

**`charlson_prior` 固定口径（评审 §三.6 修正）**：

- 窗口固定 `t_diagnosis < t_index_admission`（仅 index 入院前已完成住院的 ICD）；
- 无既往住院（含首次住院）：**`charlson_prior = NULL`**、`charlson_prior_available = FALSE`、`prior_hospital_count = 0`——**「数据库未观察到既往住院」不等于「Charlson 确为 0」**；数值填充仅由训练集拟合的缺失处理器完成并保留缺失指示变量；
- 既往住院时间范围不设上限，报告 `prior_icd_observation_window`；
- 本次入院早期已记录的既往病史不并入 `charlson_prior`，可作独立二元变量另行评估；
- 输出：`prior_hospital_count`、`prior_icd_observation_window`、`charlson_prior_available`。

体重固定口径替代方案（敏感性）：只取入院初始测量、不随 landmark 更新、初期不可用者保留缺失。**禁止**为早期 landmark 使用住院后较晚测得的体重。

### 5.2 生命体征时序（DAG 节点 F2；多信号分层 + 双轨）

| 变量 | MIMIC 来源 | eICU 来源（主来源 → 缺失补充） | 目标单位 |
|---|---|---|---|
| HR | 分层后 `vitalsign`/`chartevents` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | 同上，有创优先 | `pivoted_vital.ibp_*` → `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 仅缺失补充 | mmHg |
| RR | 同上 | `pivoted_vital.RespiratoryRate` → `vital_periodic.respiration` | /min |
| SpO2 | 同上 | `pivoted_vital.spo2` → `vital_periodic.sao2` | % |
| 体温 | 同上 | `pivoted_vital.temperature`（量纲 QA） | °C |

**MIMIC itemid 分层审计（多信号，不得只靠 `d_items.label`）**：综合 `itemid`、`d_items.category`、`storetime − charttime` 分布、同时刻重复记录模式、记录间隔特征、与监护仪专用表/派生表对应关系；清单阶段 A 预登记。

**双轨输出（来源识别不稳定时）**：

```text
vitals_realtime_strict     -- 仅确认自动导入/可用时间可靠
vitals_charttime_retro     -- 全部记录按 charttime（回顾性）
```

**eICU 三来源去重**：主来源明确；补充仅补缺；记录级去重；输出 `source_table`；抽查跨表重复率。

### 5.3 检验（DAG 节点 F3；原始重建 + P/F 双时间 + eICU 语义审计）

项目清单：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- **MIMIC**：关键项目从 `main.labevents` 重建，保留 `charttime` 与 `storetime`；派生宽表仅交叉校验。
- **eICU**：`pivoted_lab` + `pivoted_bg`；原始 `lab` 补充（经 `eicu_lab_time_map` 换算坐标）。
- **eICU lab 时间语义审计（阶段 A 专项，冻结清单 C-2）**：`labresultoffset` 语义、`labresultrevisedoffset` 是否仅修订时间、当前值是否最终修订值、修订晚于 landmark 的提前使用风险、缺失/负值/倒置处理；候选规则（审计后锁定）：最终修订值行 `available_time = max(labresultoffset, labresultrevisedoffset)`。报告 `qa/eicu_lab_time_semantics_qa.md`。**报告完成前 eICU 检验一律 `charttime_fallback`。**
- **PaO₂/FiO₂ 双时间**：`pao2_value/pao2_event_time/pao2_available_time`、`fio2_value/fio2_event_time/fio2_available_time`、`pf_available_time = max(两者)`、`pf_pairing_gap_min`、`fio2_source ∈ {measured, ventilator_setting, device_based_estimated, flow_only_estimated}`。断言 `pf_available_time ≤ t_landmark`；`derived.bg.pao2fio2ratio` 仅交叉校验；FiO₂ 主分析仅用明确记录值，流量换算仅敏感性且须联合设备类型。

### 5.4 SOFA 组分（DAG 节点 F4；三口径 + 6/6 完整性 + 缺失处理分离）

**三套口径（评审 §三.10 明确边界）**：

```text
sofa_phenotype_locked        -- 表型 SOFA：逐字复现锁定版 mimic-code，
                                包括其缺失假设（如 COALESCE(...,0)，假设写入局限性）
sofa_realtime_available      -- 模型输入 SOFA：严格 available-time，缺失不计 0
sofa_realtime_completeness   -- 完整性 QA
```

**两者不共享缺失处理函数**；P-strict 表型判定明确使用 `sofa_phenotype_locked`（其 eICU 同构复现沿用同一缺失假设并登记差异）。

**标准 24h 滑动最差定义（评审 §三.9）**：

```text
SOFA_d(t) = max SOFA_d(u)，u ∈ (t−24h, t]，且所有输入满足 available(u) ≤ t
```

血管活性药取 24h 内**最大剂量**、MAP 取 24h 内**最小值**；若另需「当前生理状态分数」，命名 `sofa_current_state`，**不得**与标准 24h SOFA 混用。

**逐组分输出**：

```text
component_value / component_observed / component_available
component_window_start / component_window_end
component_missing_reason        -- not_measured / not_yet_available / out_of_range / source_conflict
component_imputation_flag       -- none / carried_forward / cohort_rule
```

**完整性规则（评审 P0-3 修正）**：

1. 缺失组分**不得默认计 0**；
2. **标准实时 SOFA 总分仅 6/6 组分可计算时生成**：`sofa_total_complete = Σ SOFA_d`；
3. 5/6 时仅输出 `sofa_partial_sum`，`sofa_total_status = partial_5_of_6`；`sofa_partial_sum` **不得用于**：ΔSOFA ≥2 表型、CV-SOFA 以外的完整 SOFA 亚组、需与标准 SOFA 阈值比较的分析；
4. `delta_sofa` 仅在两端均为完整 6/6 且同一规则版本时计算；**缺失掩码变化不得单独制造 delta_sofa**（Q1 测试）；
5. 同步输出 `sofa_component_count`、`sofa_missing_component_mask`（6 位）、`sofa_total_status`（complete / partial_5_of_6 / incomplete）；
6. 各组分最大回溯（超过视为缺失）：胆红素/肌酐/血小板 48h，GCS 24h，P/F 24h，MAP/血管活性药按 24h 滑动最差，尿量 24h 累计窗（**乳酸不是经典 SOFA 组分，已从本清单删除；乳酸仅作独立模型变量**）；
7. GCS unable/镇静/插管按锁定 mimic-code `gcs_unable` 口径（eICU 差异预登记）；镇静期优先取镇静前 24h 内最近值，否则缺失；
8. 尿量缺失 vs 真正无尿区分（§5.7）；
9. 规则版本留痕：`sofa_realtime_rule_version`、`sofa_baseline_definition`、`sofa_window_definition`。

**心血管经典规则（修正阈值 + 最大分值计分）**：

| 分值 | 标准 |
|---|---|
| 0 | MAP ≥ 70 mmHg，且无相关血管活性药 |
| 1 | MAP < 70 mmHg |
| 2 | dopamine ≤ 5 μg/kg/min，或任意剂量 dobutamine |
| 3 | dopamine > 5 且 ≤ 15 μg/kg/min，或 epinephrine ≤ 0.1，或 norepinephrine ≤ 0.1 μg/kg/min |
| 4 | dopamine > 15 μg/kg/min，或 epinephrine > 0.1，或 norepinephrine > 0.1 μg/kg/min |

`SOFA_CV = max(MAP, dopamine, dobutamine, epinephrine, norepinephrine 各准则分值)`。三变量严格分离：`sofa_cv_original` / `nee_current` / `vasopressor_burden`；vasopressin、phenylephrine 不进经典计分（风险 R15）。

**亚组口径**：CV-SOFA≥3 固定亚组用 `sofa_realtime_available` 心血管组分（完整可计算时）；实时 SOFA 未通过 QA 时亚组整体标注回顾性口径。**MIMIC `derived.sofa` 总分不得直接作为严格实时模型特征**；窗口语义 20–50 stay 人工核对（§7.5）。`sepsis3` 静态组分禁用作 landmark 特征（风险 R11）。

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
- eICU：`pivoted_uo`；`intake_output` 计算 24h 平衡。**尿量缺失（无记录，`not_measured`）与真正无尿（有记录且累计低于阈值）严格区分**，不得互相充填。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC）

1. **就诊归属（显式 OR）**：

   ```text
   eligible ECG =
       [ admittime ≤ t_ecg ≤ min(t_landmark, dischtime) ]
     ∨ [ auditable_pre_admission_encounter ∧ t_ecg ≤ t_landmark ]
   ```

   四态 `ecg_encounter_status`；主分析纳入前两类，后者打 `pre_admission_ecg = TRUE`。审计四条件（预登记）：ED stay 主键关联、ED 离开至入院间隔 ≤ 阈值、期间无其他 encounter、入院前最大允许时长。
2. **ECG 时间语义（评审 §三.7：以防泄漏口径统一为采集完成时间）**：

   ```text
   ecg_acquisition_time          -- 采集开始时间
   ecg_available_time_assumed    -- = ecg_acquisition_time + recording_duration
                                    recording_duration = N_samples / f_s（WFDB header 解析）；
                                    header 不可得时按预登记固定假设并留痕
   ecg_processing_time           -- 波形预处理完成时间（管线留痕）
   ecg_selection_time            -- 被选为 landmark 输入的时间
   ```

   **声明**：本研究假设 ECG 在采集完成时即可作为模型输入（部署假设，非数据库事实）。**防泄漏断言与选片规则统一使用 `ecg_available_time_assumed ≤ t_landmark`**（Q1 测试）；时效窗判断（24h/48h/72h）亦以该时间为准。
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

**已知须逐条核验的实现差异**：MAP（有创/无创/周期/非周期优先级）；体温（eICU 华氏/摄氏混合）；SpO₂（eICU `sao2` 可能为动脉血气 SaO₂ 而非脉搏血氧）；乳酸（表型项目与结果时间语义——乳酸为独立模型变量，非 SOFA 组分）；血小板（单位与异常值）；WBC（计数与分类映射）。**合同完成并逐变量评级前，不得锁定 `SC-common-core` 为主外验输入。**

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

1. `ecg_available_time_assumed ≤ t_landmark` 且满足 §5.8 显式 OR 归属（**含选片：`ecg_selected_for_model = TRUE ⇒ ecg_available_time_assumed ≤ t_landmark`**）；
2. **全部特征 `available_time ≤ t_landmark`（主断言）**；聚合记录 `max_available_time ≤ t_landmark`；P/F `pf_available_time ≤ t_landmark`；
3. 结局窗起点 > t_landmark；
4. 同一患者不跨 train/validation/test（当前无独立 calibration 集；未来启用须先在 §2.4 定义且同一患者 landmark 不跨 calibration/test）；
5. 标准化/异常值阈值/插补器仅训练集拟合；
6. 特征筛选仅训练集；
7. ECG 数据驱动 QC 阈值仅训练集确定。

**结构与一致性断言（含评审 §五新增 6 类测试）**：

8. landmark 单调递增且间隔 6h；`k0 ≥ 0`；§3.4 全部边界单元测试；
9. **episode preliminary/final 映射测试**：`pending_review` 在 preliminary 映射中必须 split；final `merged` 只能来自 adjudicated override；任何人工裁决不得改写 preliminary 字段；
10. **eICU 桥接一对一测试**：每个 `source_event_id` 恰有一个 time-map 行；每个 time-map 行恰回溯一个 source event；桥接前后 unique `source_event_id` 数一致（检查对象为源事件桥接，非抗生素—培养配对行数——配对允许一事件多候选）；
11. **SOFA 可比性测试**：`sofa_total_complete` 非空 ⇒ `component_count = 6`；`component_count = 5` ⇒ `sofa_total_complete IS NULL`；`delta_sofa` 非空 ⇒ 两端均完整且同一规则版本；缺失掩码变化不得单独制造 `delta_sofa`；
12. **标签可观察性测试**：`dischtime IS NULL` 且 `last_observable_hospital_time < w_end` ⇒ `outcome_ascertainable = FALSE`；
13. **表型证据时间测试**：`diagnosis_time_semantics = retrospective_only` ⇒ 不得进入 strict_available_time 特征轨道；
14. episode 主键唯一性（每 stay 一个 episode、`mimic_episode_sepsis` 每 episode 一行）；`acute_transfer_time` XOR `alive_discharge_time`；eICU 标签仅用 `*_episode_min`（静态检查）；`episode_merge_decision` 与 `transfer_evidence` 一致性（via_ward/via_ed 不得 merged）；SOFA 缺失组分未计 0；结局三态分层抽查（§7.4）；
15. **schema 一致性测试**：正文定义的输出表字段与 Parquet 实际列完全一致（含 `mimic_icu_episode_map` 全部审计字段）。

### 7.2 时间逻辑 QA

- `admittime ≤ icu_intime < icu_outtime ≤ dischtime` 成立比例；
- `t_sepsis` 相对 ICU 入科分布；`k0` 分布；t=0 不在 ICU 比例；`t_sepsis_status = missing` episode 数；
- 各 k 风险集人数；landmark 后仍在 episode 内验证；
- 每变量 `event_time` 与 `available_time` 差异分布；strict/chart 双轨差异报告；
- eICU 时间映射连续性与间隙分布；MIMIC `gap_minutes` 分布、`transfer_evidence` 构成、`needs_review` 与 `overlap_flag` 命中数、`episode_merge_adjudications` 量级；
- MIMIC 生命体征三层占比与录入延迟分布（含双轨差异）。

### 7.3 队列表型 QA

- **MIMIC 随机抽查**：suspected infection 配对、SOFA ≥2、index episode、episode 合并（含 transfers 审计与病房/ED 排除）、`t_sepsis`；
- **eICU 分层抽查**：抗生素识别、培养识别、配对方向、跨 unit stay 配对命中、候选 pair 选对规则执行、SOFA 六组分、sepsis time、多 stay 时间映射、`antibiotic_time_source` 四级构成与互斥性、`diagnosis_time_semantics` 分布。

### 7.4 结局 QA（分层抽查）

分层：24h 死亡阳性、明确阴性（含 `last_observable` 覆盖型）、存活出院、急性转出、eICU 状态缺失、ICU 转出后院内死亡、`t+24h` 边界、`death_time_missing`、`status_conflict`、`dischtime NULL` 可观察性不足、adjudication 覆盖样本复核。

### 7.5 派生表来源验证（D0 前置）

SQL/R checksum、mimic-code commit、DuckDB 版本、生成日期、源表版本、回溯验证、行数与主键唯一性、与官方参考分布比较（含 SOFA 窗口语义 20–50 stay 人工核对）。

### 7.6 ECG 配对 QA

同一住院内 ECG、入院前 ECG（审计四条件）、出院后 ECG、多份取最近、landmark 恰等于 `ecg_available_time_assumed`、路径与 study_id 一致、header 导联/采样率/样本数解析（recording_duration 计算抽查）。

### 7.7 专项与常规 QA 输出

- `qa/eicu_lab_time_semantics_qa.md`（阶段 A）；`qa/eicu_diagnosis_time_semantics_qa.md`（阶段 A，评审 P0-4）；
- `qa/sofa_realtime_completeness_v2.md`（6/6 完整率与 ≥5/6 可计算率分列）；
- `qa/sc_common_contract_v2.md`（阶段 C2）；`qa/vitals_dual_track_v2.md`；
- 队列流程图（两库分别，eICU 三套表型分列）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；原始基线：MIMIC sepsis3 41,295 stays / 31,910 subjects，ECG 覆盖 161,352 subjects，eICU 200,859 stays / 院内死亡 18,004）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集）；eICU Go/No-Go 检查表。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v2.parquet, cohort_eicu_v2.parquet
  episodes/  mimic_icu_episode_map.parquet                     # 全部审计字段（C0，schema 测试对齐）
             episode_merge_adjudications.parquet               # 合并裁决独立表（P0-1）
             mimic_episode_sepsis.parquet                      # 含 t_sepsis_status
             eicu_unitstay_timeline.parquet
             eicu_event_time_map.parquet                       # 含 source_event_id / exact_duplicate_count
             eicu_medication_time_map.parquet                  # 专用桥接（source_event_id 一对一）
             eicu_microbiology_time_map.parquet
             eicu_lab_time_map.parquet
             eicu_infusion_time_map.parquet                    # 新增（infusion_recorded 路径）
  phenotypes/ eicu_antibiotic_events.parquet                   # 统一抗生素事件表（五步构建）
             eicu_culture_events.parquet
             eicu_infection_pairs.parquet                      # 候选 pair + 选对字段
             eicu_phenotype_tracks_v2.parquet
             eicu_phenotype_event_v2.parquet                   # 含 diagnosis_time 三字段
  splits/    split_assignments_v2.parquet
  landmarks/ landmarks_v2.parquet
  labels/    labels_24h_v2.parquet           # 状态机字段 + last_observable_hospital_time /
                                             #   observation_end_source
             label_adjudications.parquet
             labels_competing_7d_v2.parquet
  features/  baseline_static_v2.parquet      # charlson_prior NULL 口径 + 三报告字段
             landmark_context_v2.parquet
             vitals_hourly_v2.parquet        # bin 聚合字段 + source_table + source_time_type
             vitals_realtime_strict_v2.parquet / vitals_charttime_retro_v2.parquet
             labs_hourly_v2.parquet          # P/F 双时间字段 + fio2_source
             sofa_hourly_v2.parquet          # 三口径 + 逐组分 7 字段 + sofa_total_complete /
                                             #   sofa_partial_sum / 掩码 / 规则版本
             nee_stream_v2.parquet           # 双实现核验四字段
  contracts/ sc_common_variable_contract_v2.parquet
  ecg_index/ ecg_landmark_index_v2.parquet   # ecg_acquisition_time / ecg_available_time_assumed /
                                             # recording_duration / ecg_processing_time /
                                             # ecg_selection_time / path / 时效 /
                                             # ecg_encounter_status / pre_admission_ecg / 五层级标志
  qa/        cohort_flow_v2.md, feasibility_table_v2.md, leakage_report_v2.md,
             time_logic_qa_v2.md, phenotype_qa_v2.md, outcome_stratified_qa_v2.md,
             ecg_pairing_qa_v2.md, derived_provenance_v2.md, eicu_go_nogo_v2.md,
             eicu_lab_time_semantics_qa.md, eicu_diagnosis_time_semantics_qa.md,
             sofa_realtime_completeness_v2.md, sc_common_contract_v2.md, vitals_dual_track_v2.md
  _meta/     code_version.json
             d0_decision.json                # §3.1 固定 schema
             freeze_checklist.json           # §10 各项关闭状态
```

规范：①统一 Parquet；②三级键 `subject_key / episode_key / landmark_k`，原始 stay 标识与 `source_event_id` 保留溯源；③患者级 ID 与划分表冻结后不得重算；④每 DAG 节点独立脚本、I/O schema 校验、中间产物持久化；⑤时间字段命名执行 §2.3 规范；⑥D0 与冻结清单状态落 `_meta/`；⑦自动结果（episode 映射、标签）与人工裁决（adjudication 表）物理分离。

---

## 9. 已识别风险与待决事项（R1–R30）

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
| R14 | eICU 多 stay offset 坐标不一致 | 时间正确性 | C6a 统一坐标；标签仅用 `*_episode_min` |
| R15 | NEE 替代经典 SOFA 心血管 | 可比性 | 修正阈值 + 最大分值；三变量分离 |
| R16 | Charlson 派生表含本次住院 ICD | 泄漏 | `charlson_prior` NULL 口径；移出 SC-common |
| R17 | 未知结局误编码阴性 | 标签正确性 | 状态机 + 可观察性 + adjudication 分离 |
| R18 | eICU 培养覆盖低的表型选择 | 外验有效性 | 三套队列 + Go/No-Go；命名建模前锁定 |
| R19 | eICU 表型规则未 PI 签署 | 外验时间原点 | C7 冻结候选规则表；A-5 |
| R20 | eICU lab offset 语义未审计 | eICU 检验泄漏 | 专项报告；候选 max 公式验证后锁定；C-2 |
| R21 | available-time 落实不完整 | 防泄漏 | 分层重建 + 双轨；C-3/4/5 |
| R22 | Go/No-Go 数值未 PI 确认 | 可行性决策 | §2.2 C7 确定建议值；A-6 |
| R23 | episode 合并误判病房/ED 区间、重叠被掩盖 | 队列时间轴 | C0 三规则 + preliminary/adjudication 分离；B-7 |
| R24 | MIMIC 生命体征来源识别不稳定 | 实时口径可信度 | 多信号分层 + 双轨输出 |
| R25 | 标签冲突被隐式处理 | 标签完整性 | 冲突先 unknown；`label_adjudications` 独立覆盖 |
| R26 | eICU 事件按 offset 反连重复匹配 | 配对/特征正确性 | `source_event_id` 哈希规则 + 专用桥接 + Q1 一对一测试 |
| **R27** | **preliminary episode 映射隐式合并 pending_review** | 队列时间轴 | preliminary 仅 merged 延续，其余保守拆分；`episode_merge_adjudications`（P0-1）；B-7 |
| **R28** | **5 组分 partial SOFA 与完整 SOFA 不可比、制造伪 ΔSOFA** | SOFA 完整性 | `sofa_total_complete` 仅 6/6；partial 不用于 ΔSOFA/亚组/阈值比较（P0-3）；C-7 |
| **R29** | **eICU 诊断时间被当作真实 available time** | 外验时间语义 | `diagnosis_time_semantics` 分层；代理/回顾性定位；专项审计（P0-4）；A-5 |
| **R30** | **`dischtime IS NULL` 被误判为持续在院** | 标签正确性 | `last_observable_hospital_time ≥ t+24h` 方可判完整阴性（P0-5）；D-5 |

---

## 10. 冻结清单（Freeze Checklist，共 31 项：A6 + B7 + C7 + D6 + E5；通过条件已按第四轮评审扩充）

正式冻结前全部关闭；状态实时记录于 `_meta/freeze_checklist.json`。

### A. 协议冻结（6 项）

- [ ] A-1 D0 出口 A/B 已确定；
- [ ] A-2 `_meta/d0_decision.json` 已按 §3.1 固定 schema 生成；
- [ ] A-3 `2020–2022` amendment 已签署；
- [ ] A-4 mimic-code commit 与本地修改已锁定（含 SQL/R checksum）；
- [ ] A-5 eICU 三套表型冻结候选规则表已由 PI 逐项签字确认（升级为正式锁定规则表）；**且 `admission_dx` 与 `diagnosisoffset` 时间语义已审计，代理时间与真实 available time 已区分**；
- [ ] A-6 Go/No-Go 数值已预登记（PI 确认，未据模型效果调整）。

### B. 时间轴冻结（7 项）

- [ ] B-1 MIMIC episode 以 `icustays` 为锚构建；
- [ ] B-2 episode 映射一对多关系符合预期；
- [ ] B-3 每个 stay 仅属于一个 episode（主键唯一性测试通过）；
- [ ] B-4 eICU 所有事件均转换到 hospital/episode 坐标；**源事件—专用时间桥接表一对一回连测试通过，`source_event_id` 生成规则稳定且冻结**；
- [ ] B-5 跨 unit stay 的抗生素—培养配对测试通过（含选对规则与 `suspected_infection_event_id` 生成）；
- [ ] B-6 标签只使用统一坐标（eICU 仅 `*_episode_min`）；
- [ ] B-7 episode 合并三规则已锁定且 `episode_merge_*` 字段落地；**`pending_review` 在 preliminary 映射中保守拆分；episode merge adjudication 与自动结果分离**。

### C. 防泄漏冻结（7 项）

- [ ] C-1 MIMIC 关键检验使用 `storetime`（`labevents` 重建完成）；
- [ ] C-2 eICU lab revised time 语义已验证（专项报告关闭）；
- [ ] C-3 MIMIC 人工记录生命体征录入延迟已处理（itemid 分层 + 双轨落地）；
- [ ] C-4 P/F 使用两部分中较晚 available time（`pf_available_time` 断言通过）；
- [ ] C-5 动态 SOFA available-time 口径已确定；
- [ ] C-6 聚合记录 `max_available_time` 已定义并接入 Q1；
- [ ] C-7 实时 SOFA 缺失组分规则已锁定；**标准 SOFA 总分仅在 6/6 组分可计算时生成；5/6 仅输出 partial score 且不用于 ΔSOFA**。

### D. 标签冻结（6 项）

- [ ] D-1 `outcome_ascertainable` 与 `full_inhospital_followup_24h` 已拆分；
- [ ] D-2 死亡状态冲突规则已固定（`death_time_missing` / `status_conflict` 先 unknown）；
- [ ] D-3 急性转出清单已冻结（两库分别，XOR 互斥验证通过）；
- [ ] D-4 eICU 出院 offset 已转换到 episode 坐标；
- [ ] D-5 全部边界单元测试通过（§3.4）；**`dischtime` NULL 不得自动视为持续在院，必须有 `last_observable_hospital_time` 覆盖预测窗**；
- [ ] D-6 `label_adjudications` 表与 preliminary/final 分离机制已建立。

### E. ECG 冻结（5 项）

- [ ] E-1 pre-admission ECG 的 OR 条件已修正；
- [ ] E-2 ED-to-admission 审计规则已固定（四条件参数预登记）；
- [ ] E-3 结构性 QC 已固定；
- [ ] E-4 数据驱动 QC 只在训练集拟合；
- [ ] E-5 24h 主配对队列定义已冻结（查看测试集结果前），**防泄漏与选片统一使用 `ecg_available_time_assumed`**。

---

## 11. 实施顺序（阶段 A → B → C1 → C2 → D）

**阶段 A：协议与来源锁定（结束前不查看验证/测试集性能差异）**

1. mimic-code commit、派生 SQL/R 与 checksum 核对（§7.5）；
2. D0 审计与 PI 锁定（§3.1）；3. `2020 - 2022` amendment（§2.4）；
4. episode 定义与合并三规则锁定（C0/C6a）；eICU 事件桥接表与 `source_event_id` 规则建立；
5. 数据可用时间语义（§5.0）；eICU lab 专项报告（§5.3）；**eICU 诊断时间语义专项报告**（§2.2 C7）；MIMIC 生命体征 itemid 多信号分层审计（§5.2）；
6. 经典 SOFA 与 NEE 独立定义（§5.4/§5.5）；实时 SOFA 完整性与缺失规则锁定；**eICU 冻结候选规则表 PI 逐项签署**；Go/No-Go 数值预登记；
7. 关闭冻结清单 A 组、B-4/B-7、C-2/C-3/C-7。

**阶段 B：仅做 MIMIC 可行性队列（D0 候选口径可并行，不冻结）**

episode 映射（C0 三规则 + preliminary/adjudication）→ episode 级 sepsis 聚合与 index episode → landmark → 三态标签状态机（含可观察性）→ ECG 归属与五层级 availability → 主要 12 个 landmark 患者数/阳性数/ECG 覆盖率核对。

**阶段 C1：MIMIC 特征工程**

available-time 特征（F1–F7）；`charlson_prior`（NULL 口径）；ECG 两层 QC 冻结；NEE 双实现核验；实时 SOFA 重建与完整性评估（6/6 规则）；标签与 episode adjudication 机制运行；论文 2 人工标签验证（7 环节，PPV >80% 为 Go）。

**阶段 C2：SC-common 跨库合同（先于正式 MIMIC 模型训练）**

变量级单位映射、异常值范围、缺失定义、聚合规则、available-time、MIMIC/eICU 交叉库等价性评级——完成 `sc_common_variable_contract_v2`。**未完成 C2 不得开始正式 MIMIC 模型训练。**

**阶段 D：eICU 表型与可行性**

统一时间坐标 → `eicu_antibiotic_events` 与配对选对 → 按锁定规则构建三套 phenotype → 经典 SOFA 复现 → 医院覆盖与 Go/No-Go 逐项核对 → `SC-common-core`（或 extended）终稿锁定 → 外验命名与层级确定。

**当前可进行**：来源审计；D0 双口径比较；episode 原型（含 adjudication 流程演练）；MIMIC 队列规模估算；三态标签原型；ECG 覆盖与归属统计；eICU 时间轴与表型可行性统计；SC-common 覆盖率与合同草拟。

**当前不应进行（冻结生效前禁止）**：正式训练最终模型；选择超参数；查看测试集性能；依据 eICU AUROC 选择表型；依据模型效果决定 core/extended；正式跨库性能结论；把 eICU 称为完全同构 Sepsis-3 外部验证。

---

## 12. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：两库实测核验的可实施提取方案（DAG、输出规范、R1–R11、SQL 模板） |
| v2.0 | 2026-07-30 | 第一轮评审修订：D0 决策门；eICU 方向性配对与三套表型；episode 桥接表；available-time 契约；经典 SOFA CV；ECG 归属；三态标签；SC-common 分层；R12–R18；阶段 A–D |
| v2.1 | 2026-07-30 | 第二轮评审修订：表型时间合同；icustays 锚定；episode 级聚合；available-time 贯穿；SOFA 阈值修正；标签可判定性拆分；ECG OR 条件；冻结清单 28 项；R19–R22 |
| v2.2 | 2026-07-30 | 第三轮评审修订：episode 合并三规则；表型确定值规则表；P-clinical 前向算法；A.1 回填 hadm_id；A.3 标签状态机；SOFA 缺失规则；生命体征双轨；事件源主键；ECG 四时间字段；变量等价合同；冻结清单 31 项；阶段 C1/C2；R23–R26 |
| v2.3 | 2026-07-30 | **第四轮评审修订**——①preliminary episode 映射仅 `merged` 延续、`pending_review` 保守拆分，新增 `episode_merge_adjudications` 独立裁决表；②稳定 `source_event_id` 哈希规则（含 `exact_duplicate_count`），新增 `eicu_antibiotic_events` 统一事件表与 `eicu_infusion_time_map`，A.5 改为事件表直接配对；③SOFA 完整性：`sofa_total_complete` 仅 6/6，5/6 仅 `sofa_partial_sum` 且不用于 ΔSOFA/亚组/阈值比较，乳酸移出 SOFA 组分清单，24h 滑动最差定义与 `sofa_current_state` 分离，phenotype_locked 与 realtime 缺失处理分离；④`diagnosis_time_semantics` 三字段与回顾性表型定位，新增诊断时间语义专项审计；⑤`last_observable_hospital_time` 取代 `dischtime IS NULL` 自动阴性；⑥候选配对选对规则（`candidate_pair_rank / pair_selection_status / pair_selection_rule / suspected_infection_event_id`）；⑦给药三率分母定义（事件级率 + episode 级覆盖率，正式门槛为 episode 级）；⑧`charlson_prior` 改 NULL 口径；⑨ECG 防泄漏统一 `ecg_available_time_assumed = 采集开始 + N_samples/f_s`；⑩transfers 区间相交条件与相邻序列审计；⑪A.0 字段与正文 schema 对齐、A.1 排除 NULL t_sepsis；⑫四级来源互斥赋值；⑬Q1 新增 6 类测试（episode preliminary/final、桥接一对一、SOFA 可比性、标签可观察性、ECG 可用性、表型证据时间）与 schema 一致性测试；⑭冻结清单 31 项通过条件扩充（A-5/B-4/B-7/C-7/D-5/E-5）；⑮风险 R27–R30；⑯状态表述微调（核心规则结构及候选确定值已补全，审计/清单/PI 决策项入冻结清单）。 |

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

> **说明**：附录均为**概念性模板**，用于固定逻辑与边界语义，不构成完整实现；正式实施以各 DAG 节点脚本及 I/O schema 校验为准。附录模板配套 SQL 单元测试（含 `NULLS LAST` 排序、空集、同值并列、类型推断用例）。正文输出表字段与 Parquet 实际列的一致性由 Q1-15 schema 测试保证。

### A.0 MIMIC 连续 ICU episode 映射（C0；preliminary 保守拆分 + 区间相交审计 + 全字段）

```sql
WITH s AS (
  SELECT subject_id, hadm_id, stay_id, intime, outtime,
         LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_outtime,
         LAG(stay_id)  OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_stay_id
  FROM main.icustays
),
g AS (
  SELECT *, EPOCH(intime - prev_outtime) / 60.0 AS gap_minutes FROM s
),
ev AS (   -- 路径审计：区间相交条件（覆盖跨边界区间与 outtime NULL）
  SELECT g.*,
         (SELECT t.careunit FROM main.transfers t
          WHERE t.hadm_id = g.hadm_id
            AND t.intime < g.intime
            AND COALESCE(t.outtime, g.intime) > g.prev_outtime
            AND t.careunit NOT IN (/* ICU 清单 */)
          ORDER BY t.intime LIMIT 1)                        AS intervening_careunit,
         CASE
           WHEN g.prev_outtime IS NULL THEN 'none'
           WHEN g.gap_minutes < 0 THEN 'overlap_or_anomaly'
           WHEN EXISTS (SELECT 1 FROM main.transfers t
                        WHERE t.hadm_id = g.hadm_id
                          AND t.intime < g.intime
                          AND COALESCE(t.outtime, g.intime) > g.prev_outtime
                          AND t.careunit IN (/* 普通病房清单 */)) THEN 'via_ward'
           WHEN EXISTS (SELECT 1 FROM main.transfers t
                        WHERE t.hadm_id = g.hadm_id
                          AND t.intime < g.intime
                          AND COALESCE(t.outtime, g.intime) > g.prev_outtime
                          AND t.careunit IN (/* ED 清单 */)) THEN 'via_ed'
           WHEN g.gap_minutes = 0 THEN 'direct_icu_to_icu'
           ELSE 'brief_icu_exit'
         END                                                 AS transfer_evidence
  FROM g
),
d AS (
  SELECT *,
         CASE WHEN prev_outtime IS NULL THEN FALSE
              WHEN gap_minutes < 0 THEN FALSE                              -- 规则②
              WHEN transfer_evidence IN ('via_ward','via_ed') THEN FALSE   -- 规则③
              WHEN gap_minutes <= 0 THEN TRUE                              -- 规则①：τ_merge = 0
              ELSE FALSE END                                AS episode_merge_eligible,
         CASE WHEN prev_outtime IS NULL THEN 'split'
              WHEN gap_minutes < 0 THEN 'pending_review'
              WHEN transfer_evidence IN ('via_ward','via_ed') THEN 'split'
              WHEN gap_minutes <= 0 THEN 'merged'
              ELSE 'split' END                              AS episode_merge_decision,
         CASE WHEN prev_outtime IS NULL THEN 'none'
              WHEN gap_minutes < 0 THEN 'overlap'
              WHEN transfer_evidence = 'via_ward' THEN 'ward_interval'
              WHEN transfer_evidence = 'via_ed' THEN 'ed_interval'
              WHEN gap_minutes > 0 THEN 'gap_exceeds_threshold'
              ELSE 'none' END                               AS episode_merge_exclusion_reason,
         CASE WHEN gap_minutes < 0 THEN TRUE ELSE FALSE END AS overlap_flag,
         CASE WHEN prev_outtime IS NULL THEN 'first_stay'
              WHEN gap_minutes < 0 THEN 'overlap'
              WHEN gap_minutes = 0 THEN 'contiguous'
              ELSE 'gap' END                                AS merge_reason
  FROM ev
),
e AS (   -- preliminary 编号：仅 merged 延续前一 episode；split/pending_review 保守拆分
  SELECT *,
         SUM(CASE WHEN prev_outtime IS NULL THEN 1
                  WHEN episode_merge_decision = 'merged' THEN 0
                  ELSE 1 END)
           OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS episode_seq
  FROM d
)
SELECT subject_id, hadm_id,
       hadm_id::VARCHAR || '_EP' || episode_seq::VARCHAR AS episode_id,
       stay_id,
       ROW_NUMBER() OVER (PARTITION BY hadm_id, episode_seq ORDER BY intime, stay_id)
         AS stay_seq_in_episode,
       MIN(intime)  OVER (PARTITION BY hadm_id, episode_seq) AS episode_intime_ts,
       MAX(outtime) OVER (PARTITION BY hadm_id, episode_seq) AS episode_outtime_ts,
       gap_minutes, merge_reason, overlap_flag,
       intervening_careunit, transfer_evidence,
       episode_merge_eligible, episode_merge_decision, episode_merge_exclusion_reason,
       CASE WHEN episode_merge_decision = 'pending_review' THEN 'needs_review' ELSE 'clean' END
         AS episode_mapping_status,
       0 AS episode_merge_threshold_min,
       MAX(gap_minutes) OVER (PARTITION BY hadm_id, episode_seq) AS episode_gap_max_min,
       CASE WHEN BOOL_OR(transfer_evidence IN ('via_ward','via_ed','overlap_or_anomaly'))
                  OVER (PARTITION BY hadm_id, episode_seq) THEN 'interrupted'
            ELSE 'continuous' END                          AS episode_transfer_path_class,
       'v2.3' AS episode_mapping_version
FROM e;
-- pending_review 个案进入 episode_merge_adjudications（hadm_id, previous_stay_id,
-- current_stay_id, preliminary_decision, final_decision, adjudication_status, ...）；
-- final merged 仅由 adjudicated override 产生，不改写本 preliminary 输出（Q1-9）
-- 注：gap = 0 边界另需核对相邻 transfer 序列（前 stay 末端相邻 + 当前 stay 起点相邻），
-- 序列异常者 transfer_evidence 改判并 needs_review（实现层规则，QA 抽查）
```

### A.1 MIMIC 队列骨架（C1–C3；回填 hadm_id + NULL t_sepsis 防护）

```sql
WITH sepsis AS (
  SELECT s.subject_id, i.hadm_id, s.stay_id,          -- 本地 sepsis3 无 hadm_id：经 icustays 回填
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
  JOIN mimic_icu_episode_map e USING (subject_id, hadm_id, stay_id)
),
ep_sepsis AS (   -- mimic_episode_sepsis：每 episode 恰好一行；NULL 防护
  SELECT episode_id, qualifying_sepsis_count,
         t_sepsis AS t_sepsis_ts,
         stay_id AS t_sepsis_source_stay_id,
         'min_t_sepsis_within_episode' AS t_sepsis_selection_rule,
         CASE WHEN t_sepsis IS NULL THEN 'missing' ELSE 'ok' END AS t_sepsis_status
  FROM ep_ranked WHERE rn = 1
),
eligible AS (
  SELECT es.episode_id, es.t_sepsis_ts, es.qualifying_sepsis_count,
         es.t_sepsis_source_stay_id,
         em.subject_id, em.hadm_id, em.episode_intime_ts, em.episode_outtime_ts,
         a.admittime, d.admission_age
  FROM ep_sepsis es
  JOIN (SELECT DISTINCT episode_id, subject_id, hadm_id,
                        episode_intime_ts, episode_outtime_ts
        FROM mimic_icu_episode_map) em USING (episode_id)
  JOIN main.admissions a USING (hadm_id)
  JOIN mimiciv_derived.icustay_detail d
    ON d.stay_id = es.t_sepsis_source_stay_id
  WHERE d.admission_age >= 18
    AND es.t_sepsis_status = 'ok'        -- t_sepsis 缺失 episode 排除并写 QA
    -- 成人 ICU 类别清单（QA 实测后预登记）
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

### A.2 Landmark 网格与风险集（MIMIC；显式 NULL 逻辑）

```sql
SELECT c.episode_id, k,
       c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' AS t_landmark_ts
FROM cohort_mimic_v2 c
JOIN main.admissions a USING (hadm_id)
CROSS JOIN generate_series(
  CAST(GREATEST(0, CEIL(EPOCH(c.episode_intime_ts - c.t_sepsis_ts) / 21600)) AS INTEGER),
  27) AS t(k)
WHERE (c.episode_outtime_ts IS NULL
       OR c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' < c.episode_outtime_ts)
  AND (a.deathtime IS NULL
       OR c.t_sepsis_ts + (6 * k) * INTERVAL '1 hour' < a.deathtime);
```

### A.3 三态 24h 标签状态机（MIMIC；last_observable 可观察性 + 冲突先 unknown）

```sql
WITH disp AS (
  SELECT hadm_id,
         CASE WHEN discharge_location IN (/* 急性转出清单 */) THEN dischtime END AS acute_transfer_time,
         CASE WHEN discharge_location IN (/* 存活出院清单 */) THEN dischtime END AS alive_discharge_time
  FROM main.admissions
),
obs AS (   -- 最后可观测院内时间（规则预登记；来源留痕）
  SELECT c.episode_id, c.hadm_id,
         GREATEST(a.dischtime, x.last_activity_ts) AS last_observable_hospital_time,
         CASE WHEN a.dischtime >= COALESCE(x.last_activity_ts, a.dischtime)
              THEN 'dischtime' ELSE 'last_activity' END AS observation_end_source
  FROM cohort_mimic_v2 c
  JOIN main.admissions a USING (hadm_id)
  LEFT JOIN last_inhospital_activity x USING (hadm_id)   -- 最晚护理/检验/输注记录（概念性）
),
base AS (
  SELECT l.episode_id, l.k, l.t_landmark_ts,
         l.t_landmark_ts + INTERVAL '24 hours' AS w_end,
         a.hospital_expire_flag, a.deathtime, a.dischtime,
         d.acute_transfer_time, d.alive_discharge_time,
         o.last_observable_hospital_time, o.observation_end_source
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
      WHEN last_observable_hospital_time >= w_end
        THEN 'non_event_observed'                     -- 覆盖型完整阴性（含 dischtime ≥ w_end）
      WHEN alive_discharge_time > t_landmark_ts AND alive_discharge_time <= w_end
        THEN 'non_event_alive_discharge'
      ELSE 'unascertainable'                          -- 含 dischtime IS NULL 且可观察期不足
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
  (label_state = 'non_event_observed')                            AS full_inhospital_followup_24h,
  CASE WHEN label_state IN ('acute_transfer','death_time_missing','status_conflict',
                            'unascertainable','invalid_input')
        THEN label_state END                                      AS outcome_unknown_reason,
  label_state                                                     AS label_reason,
  last_observable_hospital_time, observation_end_source
FROM state;
-- 冲突与 unascertainable：QA 复核写入 label_adjudications，不改写本自动提取结果
-- Q1-12：dischtime IS NULL 且 last_observable_hospital_time < w_end ⇒ outcome_ascertainable = FALSE
```

### A.4 eICU 住院级时间坐标与稳定事件标识（C6a）

```sql
SELECT patientunitstayid, patienthealthsystemstayid, uniquepid,
       -hospitaladmitoffset                       AS unit_start_hospital_min,
       -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min
FROM main.patient;
-- 稳定事件标识（各源表规则逐表预登记；示例 medication）：
--   source_event_id = MD5('medication' || patientunitstayid || drugstartoffset ||
--                         lower(trim(drugname)) || routeadmin || drugorderoffset)
--   完全重复记录：exact_duplicate_count 计数，建桥时裁决保留一行
-- 专用桥接表（eicu_medication_time_map 等）经 source_event_id 与源表一对一回连；
-- Q1-10：source_event_id → time-map 一对一、time-map → source event 一对一、
--        桥接前后 unique source_event_id 数一致
-- 结局同步转换：hospital_discharge_episode_min / death_episode_min（§4.1）
```

### A.5 eICU suspected infection 配对（C6b；统一事件表 + 候选选对）

```sql
-- 统一抗生素事件表（五步构建，§2.2 C6b）：eicu_antibiotic_events(
--   antibiotic_event_id, source_table, source_event_id, patientunitstayid, episode_id,
--   antibiotic_time_episode, antibiotic_time_raw, drug_name_normalized,
--   antibiotic_time_source, antibiotic_time_confidence)
-- 培养事件表同构：eicu_culture_events(culture_event_id, ..., culture_time_episode)
WITH pairs AS (
  SELECT ab.episode_id,
         ab.antibiotic_event_id, cx.culture_event_id,
         ab.antibiotic_time_episode AS ab_time,
         cx.culture_time_episode    AS cx_time
  FROM eicu_antibiotic_events ab
  JOIN eicu_culture_events cx USING (episode_id)      -- 允许跨 unit stay（同一 episode）
  WHERE (ab.antibiotic_time_episode - cx.culture_time_episode) BETWEEN 0 AND 4320
     OR (cx.culture_time_episode - ab.antibiotic_time_episode) BETWEEN 0 AND 1440
),
ranked AS (
  SELECT *,
         CONCAT(antibiotic_event_id::VARCHAR, '__', culture_event_id::VARCHAR)
           AS infection_pair_id,                      -- 候选 pair ID（非最终事件 ID）
         ROW_NUMBER() OVER (PARTITION BY culture_event_id
                            ORDER BY ab_time, antibiotic_event_id) AS rank_per_culture,
         ROW_NUMBER() OVER (PARTITION BY antibiotic_event_id
                            ORDER BY cx_time, culture_event_id)    AS rank_per_antibiotic
  FROM pairs
)
SELECT *,
       rank_per_culture   AS candidate_pair_rank_culture,
       rank_per_antibiotic AS candidate_pair_rank_antibiotic,
       CASE WHEN rank_per_culture = 1 AND rank_per_antibiotic = 1
            THEN 'selected' ELSE 'candidate' END       AS pair_selection_status,
       'locked_mimicode_priority'                      AS pair_selection_rule
       -- 最终 suspected_infection_event_id 由 selected pair 按锁定 mimic-code 规则生成（C7）
FROM ranked;
-- 说明：pair 阶段允许一事件多候选；选对优先级完全复现锁定版 mimic-code
--       （同培养多抗生素、同抗生素多培养、同时间排序、多 event 的 index 选择）
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

*本方案 v2.3 基于 2026-07-30 对两库的只读结构核验与四轮外部评审《总体评价》生成；与技术文档 v1.9 冲突之处以技术文档为准，需变更技术文档的事项（D0 出口 B、`2020-2022` 处理）须经 protocol amendment 正式登记。§10 冻结清单（31 项）全部关闭且五类冻结验证与新增一对一桥接/SOFA 完整性测试通过前，本方案不得作为正式主分析提取管线使用。*
