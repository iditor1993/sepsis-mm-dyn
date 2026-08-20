# SEPSIS-MM-DYN 数据提取方案 v2.1

- 文档版本：v2.1
- 创建日期：2026-07-30（v1.0 同日创建；v2.0 同日经第一轮外部评审后修订；v2.1 经第二轮外部评审后修订）
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 修订依据：《总体评价》（2026-07-30 第二轮评审，对 v2.0 结论为「有条件通过作为主分析实施候选版，可立即进入阶段 A 和受限的阶段 B 原型提取；尚不能冻结为正式主分析提取方案」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**主分析实施候选版（第二轮评审修订版）**。本版已对两轮评审要求的全部问题建立修订方案、决策门与实施路径；其中 **D0 出口选择、`2020 - 2022` amendment、episode 合并阈值、eICU 表型时间合同、Go/No-Go 数值与 available-time 语义专项验证仍为待决项**，须在冻结前完成锁定（见 §10 冻结清单）。全部冻结清单项关闭并经小规模人工审查、主键唯一性测试与 feasibility table 审核后，本方案方可升级为正式提取管线冻结候选版。在此之前禁止正式模型训练、超参数选择与测试集评估。

---

## 0. v2.0 → v2.1 修订总览

本节按第二轮评审《总体评价》的章节编号逐项登记修改落点。v1.0 → v2.0 的修订历史见 §12 变更日志。

### 0.1 冻结前必须解决的问题（评审 §二，5 项）

| 评审编号 | 问题 | v2.1 落点 |
|---|---|---|
| P0-1 | eICU 三套表型缺少「时间原点」与「SOFA 急性增加」完整定义 | §2.2 C7 新增**表型时间合同** `phenotype_event` 表与分表型规则表（感染证据时间、SOFA 窗口、基线、ΔSOFA ≥2、`t_sepsis` 规则）；规则锁定前 `cohort_eicu_v2.t_sepsis_episode_offset` 不正式生成 |
| P0-2 | MIMIC episode 构建不应主要依赖 `transfers` 推导 | §2.1 C0 与附录 A.0 重写：**以 `main.icustays` 为锚**，按 `gap = intime(j+1) − outtime(j)` 合并；`transfers` 仅用于审计；输出 `merge_reason / gap_minutes / intervening_careunit / transfer_evidence / episode_mapping_status` |
| P0-3 | 同一 episode 内多个 `sepsis3` stay 未先聚合即做患者级排序 | §2.1 C1 新增 episode 级聚合表 `mimic_episode_sepsis`（`t_sepsis = min(t_sepsis,j)`，`t_sepsis_source_stay_id`，`t_sepsis_selection_rule`）；附录 A.1 增加 `GROUP BY episode_id` 聚合 CTE |
| P0-4 | available-time 合同未贯穿 MIMIC 生命体征、P/F、动态 SOFA | §5.2 生命体征按 itemid 分来源层（监护仪/护理录入/降级）；§5.3 P/F 双时间重建（`pf_available_time = max(pao2, fio2)`）；§5.4 SOFA 三输出（`sofa_phenotype_locked / sofa_realtime_available / sofa_realtime_completeness`） |
| P0-5 | eICU `labresultrevisedoffset` 语义「辅助」不足以实施 | §5.3 增加语义审计要求与候选公式 `available = max(labresultoffset, labresultrevisedoffset)`（验证后锁定）；阶段 A 新增专项报告 `eicu_lab_time_semantics_qa.md` |

### 0.2 明确技术错误与内部矛盾修正（评审 §三，6 项）

| # | 问题 | v2.1 落点 |
|---|---|---|
| 1 | 经典 SOFA 心血管 dopamine 3/4 分阈值重叠 | §5.4：3 分改为 `dopamine >5 且 ≤15`；并按**最大满足分值**计分（`max(MAP, dopamine, dobutamine, epinephrine, norepinephrine)` 五准则取大） |
| 2 | `label_observable` 与「提前明确存活出院判 0」语义冲突 | §4.1 拆分为 `outcome_ascertainable`（结局可判定，主分析依据）与 `full_inhospital_followup_24h`（完整院内观察，描述性） |
| 3 | 附录 A.5 仍按 `patientunitstayid` 配对，漏跨 unit stay 感染事件 | 附录 A.5 重写：抗生素与培养先经 `eicu_event_time_map` 换算至 episode 坐标，按 `episode_id` 配对 |
| 4 | ECG 主条件与 pre-admission 纳入相互矛盾 | §5.8 改为显式 OR 条件；定义 `auditable_pre_admission_encounter` 的审计来源（ED 主键、ED-入院间隔、无其他 encounter、最大允许时长） |
| 5 | 体重/身高为 landmark 动态可用，但输出表为 episode 级静态表 | §5.1/§8 拆分为 `baseline_static_v2`（每 episode 一行）与 `landmark_context_v2`（每 episode × landmark 一行） |
| 6 | 聚合后特征的单条 event/available time 语义不明 | §5.0 聚合记录输出 `bin_start/bin_end/n_source_records/min_event_time/max_event_time/max_available_time/aggregation_method/source_table_set`；断言 `max_available_time ≤ t_lm`，或聚合前先过滤 |

### 0.3 eICU 外验补充规定（评审 §四，3 项）

| # | 问题 | v2.1 落点 |
|---|---|---|
| 1 | P-clinical / P-explicit 须避免使用未来诊断记录 | §2.2 C7：所有诊断证据满足 `t_diagnosis,available ≤ t_sepsis`，或以首次诊断可用时间为候选原点；`admission_dx` 视为入院时可用并与 `diagnosis` 分开处理 |
| 2 | eICU「实际给药时间」可能实际不可得 | §2.2 C6b：输出 `antibiotic_time_source ∈ {administration_confirmed, infusion_observed, scheduled_start, order_time}`；可靠率纳入 Go/No-Go |
| 3 | Go/No-Go 数值须在看队列结果前固定 | §2.2 C7 填入全部建议数值（阶段 A PI 确认后预登记，禁止按模型效果反向调整） |

### 0.4 MIMIC 结局标签补充（评审 §五，3 项）

| # | 问题 | v2.1 落点 |
|---|---|---|
| 1 | `hospital_expire_flag = 1` 且 `deathtime` 缺失的分支归属 | §4.1：判 `unknown / death_time_missing`；反向冲突（`deathtime` 非空且 flag=0）视为状态冲突进 QA |
| 2 | `acute_transfer_time / alive_discharge_time` 的来源与互斥 | §4.1：明确由 `dischtime + discharge_location` 分类派生，二者 XOR 互斥；附录 A.3 同步 |
| 3 | eICU 出院 offset 与 episode 坐标不可直接比较 | §4.1：统一生成 `hospital_discharge_hospital_min / hospital_discharge_episode_min / death_episode_min`，标签代码只使用 `*_episode_min` |

### 0.5 状态表述、冻结清单与版本结论（评审 §六–§八）

| 评审要求 | v2.1 落点 |
|---|---|
| 状态表述不得写成「全部 P0 已完成关闭」 | 文档头部状态段改写为「建立修订方案、决策门与实施路径；待决项列入冻结清单」 |
| 新增正式 Freeze Checklist | 新增 **§10 冻结清单**（A 协议 / B 时间轴 / C 防泄漏 / D 标签 / E ECG，共 27 项） |
| 「当前可进行 / 当前不应进行」清单 | §11 实施顺序末尾固化 |

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
| ICU | `main.icustays` | `stay_id`；`first_careunit, last_careunit, intime, outtime, los`（**episode 锚定表，§2.1 C0**） | 94,458 |
| 转科 | `main.transfers` | `transfer_id`；`eventtype, careunit, intime, outtime`（**仅作 episode 审计，§2.1 C0**） | 2,413,581 |
| 脓毒症表型 | `mimiciv_derived.sepsis3` | 每 stay 一行；`suspected_infection_time, sofa_time, sofa_score, 六组分`（**无 `sepsis_time`**，见 §3.1 D0） | 41,295 |
| 疑似感染 | `mimiciv_derived.suspicion_of_infection` | 每次抗生素-培养配对一行 | 949,901 |
| SOFA（小时级） | `mimiciv_derived.sofa` | `stay_id, starttime/endtime(1h), 组分输入 + 24h 滑动组分`（表型口径；实时口径见 §5.4） | 8,219,121 |
| 生命体征 | `mimiciv_derived.vitalsign` | `stay_id, charttime`；HR/SBP/DBP/MBP/RR/Temp/SpO2/Glucose（**多无 `storetime`，§5.2 分层处理**） | 13,519,533 |
| 生命体征（原始） | `main.chartevents` + `main.d_items` | `itemid, charttime, storetime, valuenum` | ~4.33 亿 |
| GCS | `mimiciv_derived.gcs` | `charttime, gcs, gcs_motor/verbal/eyes, gcs_unable` | 2,217,787 |
| 血管活性药 | `mimiciv_derived.vasoactive_agent` | `stay_id, starttime, endtime, 7 药速率列` | 839,543 |
| NEE | `mimiciv_derived.norepinephrine_equivalent_dose` | `stay_id, starttime, endtime, norepinephrine_equivalent_dose` | 783,613 |
| 单药输注 | `mimiciv_derived.{norepinephrine, epinephrine, dopamine, phenylephrine, vasopressin, dobutamine, milrinone}` | `stay_id, linkorderid, vaso_rate, vaso_amount, starttime, endtime` | — |
| 通气 | `mimiciv_derived.ventilation` | `stay_id, starttime, endtime, ventilation_status` | 144,812 |
| 检验（宽表） | `mimiciv_derived.{bg, chemistry, coagulation, complete_blood_count, cardiac_marker, blood_differential, enzyme, inflammation}` | `stay_id/hadm_id + charttime + 项目列`（**多无 `storetime`，仅交叉校验，见 §5.3**） | — |
| 检验（原始） | `main.labevents` + `main.d_labitems` | `itemid, charttime, storetime, valuenum, valueuom` | 158,374,764 |
| 微生物 | `main.microbiologyevents` | `charttime/chartdate, spec_type_desc, org_name, interpretation` | 3,988,224 |
| 合并症 | `mimiciv_derived.charlson` | `hadm_id`；17 组分 + `charlson_comorbidity_index`（**基于本次住院最终 ICD，见 §5.1**） | 546,028 |
| 体重/身高 | `mimiciv_derived.weight_durations`、`mimiciv_derived.height` | 时段体重；身高 | 401,850 / 43,342 |
| 尿量 | `mimiciv_derived.urine_output` | `stay_id, charttime, urineoutput` | 4,127,634 |
| ICU 汇总 | `mimiciv_derived.icustay_detail` | `stay_id`；年龄、性别、入出院时间、结局、序次 | 94,458 |
| 结局汇总 | `mimiciv_derived.patient_outcomes` | `stay_id`；死亡、SOFA/SOFA-2、通气、RRT、血管活性药时长等 73 列 | 94,458 |
| ECG 索引 | `main.ecg_records` | `subject_id, study_id, ecg_time, path` | 800,035 |
| ECG 机测 | `main.ecg_measurements` | `study_id, ecg_time, RR/间期/电轴` | 800,035 |

> 注：`mimiciv_derived` 同时含 SOFA-2 系列表（`sofa2_*`）。本项目 Sepsis-3 与亚组均基于 **SOFA-1**，禁止混用（风险 R6）。本地派生表由前期 R 脚本生成；其 mimic-code 版本、commit hash、SQL/R 文件清单与本地修改须在**阶段 A** 完成登记（§11，D0 前置）。

### 1.3 eICU 库关键表（本项目涉及部分）

| 域 | 表 | 粒度 / 关键列 | 实测行数 |
|---|---|---|---|
| ICU 入住 | `main.patient` | `patientunitstayid`；三级 ID、年龄（VARCHAR，含 `"> 89"`）、入出院 offset 与状态、身高体重 | 200,859 |
| 医院 | `main.hospital` | `hospitalid`；床位数、教学状态、region | 208 |
| ICU 汇总 | `main.icustay_detail` | `patientunitstayid`；`hosp_mort, icu_los_hours, apache_iv, region` 等 | 200,859 |
| 生命体征（监护仪） | `main.vital_periodic` | `observationoffset`（分钟）；HR/RR/SpO2/有创血压/Temp 等 | 146,671,642 |
| 生命体征（非周期） | `main.vital_aperiodic` | `observationoffset`；无创/有创血压、CO/CI/SVR | 25,075,074 |
| 生命体征（护理宽表） | `main.pivoted_vital` | `chartoffset`；HR/RR/SpO2/NIBP/IBP/Temp | 21,038,216 |
| 检验（原始） | `main.lab` | `labresultoffset, labname, labresult, labresultrevisedoffset`（**语义审计见 §5.3**） | 39,132,531 |
| 检验（宽表） | `main.pivoted_lab` | `chartoffset`；肌酐/胆红素/血小板/乳酸/WBC 等 22 项 | 5,314,163 |
| 血气 | `main.pivoted_bg` | `chartoffset`；`fio2`（实测 0–1 量纲）、`pao2, paco2, pH` | 1,464,012 |
| GCS | `main.pivoted_gcs` / `main.pivoted_score` | `chartoffset`；GCS 总分/分项；`gcs_unable, gcs_intub` | 3,451,788 / 5,709,678 |
| 输注药（原始） | `main.infusion_drug` | `infusionoffset, drugname(内嵌单位/浓度), drugrate, infusionrate, drugamount, volumeoffluid, patientweight` | 4,803,719 |
| 输注标记 | `main.pivoted_infusion` | `chartoffset`；8 药 0/1 标记（**无剂量**） | 1,083,074 |
| 用药医嘱 | `main.medication` | `drugorderoffset, drugstartoffset, drugstopoffset, drugname, routeadmin`（**drugstartoffset 可能仍为计划时间，§2.2 C6b**） | 7,301,853 |
| 微生物 | `main.micro_lab` | `culturetakenoffset, culturesite, organism` | 16,996（仅 2,923 患者；**见 §2.2 C7 Go/No-Go**） |
| 尿量 | `main.pivoted_uo` | `chartoffset, urineoutput` | 4,088,881 |
| 体重 | `main.pivoted_weight` | `chartoffset, source_table, weight_type, weight` | 501,506 |
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath`（**未来诊断防护见 §2.2 C7**） | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring`（含机械通气、RRT、ECMO 等路径） | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数；`respchartoffset` 呼吸记录 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部原始事件时间为**相对各 unit stay 入科的分钟偏移（offset）**，`patient.hospitaladmitoffset` 为相对住院入院的偏移（通常为负值）；出院年份仅 2014/2015（实测 95,513 / 105,346），**无绝对日期**。多 unit stay 合并前必须先换算到统一住院级时间坐标（§2.2 C6a）。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C0–C5）

- **C0 连续 ICU episode 映射（以 `icustays` 为锚，评审 P0-2 重写）**：MIMIC 中 `stay_id` 本身通常已代表一段连续 ICU 住留，ICU 单元内转科未必产生新 `stay_id`；`transfers` 的 ICU 区间与 `icustays` 边界不完全一致，直接由 `transfers` 推导 episode 会产生一对多/多对多的不稳定映射。因此：

  1. 以 `main.icustays` 为 episode 候选基础，同一 `hadm_id` 内按 `intime, stay_id` 排序；
  2. 计算相邻 stay 间隙 `gap_minutes = EPOCH(intime(j+1) − outtime(j)) / 60`；
  3. 按**预登记间隙阈值**（默认 0 分钟，即首尾相接才合并；>0 阈值作敏感性分析，阶段 A 锁定）生成 `episode_id`；
  4. `transfers` **仅用于审计**相邻 stay 之间的真实路径：直接 ICU-to-ICU / 短暂离开 ICU / 经普通病房 / 时间重叠或异常。

  输出桥接表（含审计字段）：

  ```text
  mimic_icu_episode_map
  - subject_id, hadm_id, episode_id
  - stay_id, stay_seq_in_episode
  - episode_intime, episode_outtime
  - gap_minutes               -- 与前一同住院 stay 的间隙（首个 stay 为 NULL）
  - merge_reason              -- contiguous / gap_within_threshold / first_stay
  - intervening_careunit      -- 间隙期间所在单元（transfers 审计；NULL 表示无间隙）
  - transfer_evidence         -- direct_icu_to_icu / brief_icu_exit / via_ward / overlap_or_anomaly / none
  - episode_mapping_status    -- clean / needs_review（时间倒置、区间重叠等异常）
  ```

  约束：每个 `stay_id` 恰好属于一个 `episode_id`；`episode_mapping_status = needs_review` 的记录进 QA 人工核查。下游所有 ICU 数据一律按 `stay_id → episode_id` 聚合：后续 stay 的生命体征、检验、药物、尿量全部纳入同一 episode；landmark 终止与风险集以 `episode_outtime` 为准（§3.2/§3.3）。

- **C1 脓毒症相关 episode 池（episode 级 sepsis 聚合，评审 P0-3 新增）**：`mimiciv_derived.sepsis3`（`sepsis3 = TRUE`，实测 41,295 stays / 31,910 subjects）先按 C0 映射归属 episode，**同一 episode 内多个命中 stay 先聚合为 episode 级一行**，再进行任何患者级排序：

  ```text
  mimic_episode_sepsis
  - episode_id
  - qualifying_sepsis_count      -- 本 episode 内命中 sepsis3 的 stay 数
  - t_sepsis                     -- min_j t_sepsis,j（D0 定义允许的前提下取最早）
  - t_sepsis_source_stay_id      -- 产生 t_sepsis 的 stay（按 t_sepsis, stay_id 确定性取）
  - t_sepsis_selection_rule      -- 'min_t_sepsis_within_episode'（D0 出口 A 时按锁定代码规则替换）
  ```

  再 ⨝ `mimiciv_derived.icustay_detail` ⨝ `main.icustays`（`first_careunit`）。聚合保证 `episode_id` 主键唯一、队列流图不重复计数、候选时间原点唯一。

- **C2 入排初筛**：年龄 ≥18（`t_sepsis_source_stay_id` 对应 `icustay_detail.admission_age`）；成人 ICU（episode 首个 stay 的 `first_careunit` 排除 NICU 等非成人单元，类别清单以 QA 实测为准）。

- **C3 index episode 选择**（技术文档 §4.2 层级规则）：先按 C1–C2 构造**全部合格 episode**（每 episode 恰好一行），再按 `subject_id` 选择**首次合格 episode**，排序键固定为 `t_sepsis, admittime, episode_intime, episode_id`（完全确定性）。`first_icu_stay` **仅作描述性变量，不作纳入条件**。其余合格 episode 不进入主分析。

- **C4 探索性/敏感性标志**：外院转入（`admissions.admission_location` 含 Transfer 类）；首个有效 landmark 前已存在 ECMO（`main.procedureevents` itemid QA 确认）；近 90 天实体器官移植（ICD 清单预登记）；首个有效 landmark 前 DNR/CCO（`main.poe`/code status 相关 chartevents itemid，预登记清单）。**在完成 PPV 人工抽查前，以上标志一律仅作探索性/敏感性标志，不用于正式排除。** `diagnoses_icd`/`procedures_icd` 为出院后最终编码，**不得**用来证明 landmark 前已存在某状态；凡以 ICD 为依据的标志，其「landmark 前已存在」口径仅指**既往住院**的 ICD 记录。所有标志仅使用首个有效 landmark 时点之前的记录，禁止追溯性排除。

- **C5 队列事实表** `cohort_mimic_v2`（每 **episode** 一行）：`subject_id, hadm_id, episode_id, t_sepsis_source_stay_id, t_sepsis（D0 锁定后生效）, episode_intime, episode_outtime, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

产出规模预估：C1 后直接可得；C2–C3 后的最终队列患者数、landmark 数与事件数进入月 1 Feasibility Table（技术文档 §9.1）。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C10）

eICU 无现成 Sepsis-3 派生表，且培养覆盖极低（`micro_lab` 仅 2,923 患者），按「Robustness under phenotype shift」层级自建，**先建时间坐标，再建表型**。

- **C6a 住院级统一时间坐标**：每个 `patientunitstayid` 的 offset 以该 stay 入科为零点，多 stay 合并前统一换算为**住院级分钟坐标**：

  ```text
  t_hospital_min = -hospitaladmitoffset + eventoffset        -- 住院入院为 0 点
  episode_offset_min = hospital_offset_min - episode_start_hospital_min
  ```

  输出两张桥接表：

  ```text
  eicu_unitstay_timeline
  - patientunitstayid, patienthealthsystemstayid, uniquepid
  - unit_start_hospital_min   = -hospitaladmitoffset
  - unit_end_hospital_min     = -hospitaladmitoffset + unitdischargeoffset
  - episode_id                           -- 同一 patienthealthsystemstayid 内相邻 stay 合并
  - episode_start_hospital_min / episode_end_hospital_min

  eicu_event_time_map
  - patientunitstayid, local_offset_min  -- 原始 offset
  - hospital_offset_min                  -- 住院级坐标
  - episode_offset_min                   -- episode 级坐标（episode 起点为 0）
  ```

  episode 合并规则：同一 `patienthealthsystemstayid` 内，前一 stay 的 `unit_end_hospital_min` 与后一 stay 的 `unit_start_hospital_min` 之差 ≤ 预登记间隙阈值（默认 0 分钟，敏感性阈值阶段 A 锁定）者合并；`unitstaytype = 'readmit'` 是否纳入同一 episode 按同一规则判定并单独打标。合并后，第二个及后续 stay 的生命体征、检验、药物、尿量经 `eicu_event_time_map` 换算到 episode 坐标后全部纳入；`t_sepsis_episode_offset` 与所有 landmark 均定义在 episode 坐标上。

- **C6b suspected infection 重建（episode 坐标 + 给药时间来源分层）**：抗生素使用时点与培养采样时点（`micro_lab.culturetakenoffset`）**先分别经 `eicu_event_time_map` 换算到 episode 坐标，再按 `episode_id` 配对**（跨 unit stay 的抗生素—培养配对必须可命中，附录 A.5）。配对按**锁定版 mimic-code 的方向性规则**，**禁止**对称「±24h/±72h」：

  ```text
  培养先发生：  t_antibiotic - t_culture  ∈ [0, 72h]
  抗生素先发生：t_culture - t_antibiotic  ∈ [0, 24h]
  （具体窗口数值以阶段 A 锁定的 mimic-code 版本为准）
  ```

  **抗生素时间来源分层（评审 §四.2 新增）**：`medication.drugstartoffset` 很可能仍是医嘱计划开始时间而非 MAR 级实际给药时间。每条抗生素记录输出来源标记：

  ```text
  antibiotic_time_source:
    administration_confirmed   -- 有 MAR 级/输注记录级实际给药证据（如 infusion_drug 同时段同药记录）
    infusion_observed          -- infusion_drug 中观察到该药输注（输注即给药证据）
    scheduled_start            -- 仅 medication.drugstartoffset（计划开始）
    order_time                 -- 仅 drugorderoffset（医嘱时间）
  ```

  优先级：`administration_confirmed > infusion_observed > scheduled_start > order_time`。只能达到 `scheduled_start / order_time` 的记录显式打 `order_time_only = TRUE`；**实际给药时间可靠率（`administration_confirmed + infusion_observed` 占比）纳入 Go/No-Go 门槛**（C7），不得仅作为少量 phenotype shift 登记。抗菌药物清单（药名/HICL）预登记；规则与 MIMIC 侧 `suspicion_of_infection` 逐条对照，差异写入预登记差异表。

- **C7 三套可行性表型队列 + 表型时间合同（评审 P0-1 新增）**：

  **Sepsis-3 的核心不是绝对 SOFA ≥2，而是感染相关的急性器官功能障碍，操作化为 ΔSOFA ≥2 且 SOFA 变化与 suspected infection 处于规定时间关系内。** 因此每套表型必须先锁定「表型时间合同」，输出逐事件记录：

  ```text
  phenotype_event
  - episode_id
  - infection_evidence_time    -- 感染证据时间（episode 坐标）
  - infection_evidence_type    -- culture_antibiotic_pair / clinical_dx / explicit_sepsis_dx
  - sofa_window_start / sofa_window_end
  - baseline_sofa              -- SOFA 基线定义下的值
  - qualifying_sofa            -- 窗口内合格 SOFA 值
  - delta_sofa                 -- qualifying - baseline
  - sofa_qualifying_time       -- ΔSOFA ≥2 首次满足时点
  - t_sepsis                   -- 按 t_sepsis_rule 合成
  - t_sepsis_rule              -- 所锁定的规则标识
  - phenotype_track            -- P-strict / P-clinical / P-explicit
  ```

  分表型规则表（「预登记」项在阶段 A 由 PI 锁定，锁定前 `cohort_eicu_v2.t_sepsis_episode_offset` **不正式生成**，仅可行性统计）：

  | 表型 | 感染证据时间 | SOFA 窗口 | SOFA 基线 | t_sepsis 规则 |
  |---|---|---|---|---|
  | **P-strict** | C6b 抗生素-培养配对的 suspected infection time | **完全复现锁定版 mimic-code**（窗口长度、方向、与感染时间的关系均同锁定代码） | **完全复现锁定版 mimic-code** | 同锁定版 mimic-code |
  | **P-clinical** | landmark 前已记录且**可用**的感染诊断证据时间；必须满足 `t_diagnosis,available ≤ t_sepsis`（评审 §四.1） | 预登记：感染证据时间前后窗口内 ΔSOFA ≥2 | 预登记（如窗口前无感染证据期 SOFA 或入院基线） | 预登记：感染证据时间与 `sofa_qualifying_time` 的规则组合 |
  | **P-explicit** | 首次**可用** sepsis/septic shock 诊断记录时间 | 预登记（可选：不要求 ΔSOFA，仅描述 SOFA） | 可选 | 预登记二选一：首次可用诊断时点 / 首个 SOFA 合格时点 |

  **未来诊断防护（评审 §四.1）**：`diagnosis.diagnosisoffset` 可能晚于实际感染、甚至晚于部分 landmark——**不得**因患者最终被记录为 sepsis 就从更早时刻起算预测起点；`admission_dx` 视为入院时可用，与 `diagnosis` **分开处理**（`infection_evidence_type` 中区分 `admission_dx` 与 `later_dx`）。

  三套队列分别报告：**患者数、医院数、院内死亡数、各 landmark 阳性数、SC-common 特征覆盖率、与 MIMIC 主队列的基线差异**。

  **Go/No-Go 门槛（建议数值如下，评审 §四.3；阶段 A PI 确认后预登记，查看队列结果前冻结，禁止按模型 AUROC 或外验效果反向调整）**：

  | 指标 | 建议阈值 | 说明 |
  |---|---|---|
  | P-strict 覆盖医院数 | ≥ 20 家，且最大单医院患者占比 ≤ 25% | 避免单中心主导的选择偏倚 |
  | 患者数 | P-strict ≥ 500；P-clinical / P-explicit ≥ 2,000 | 外验评估最低规模 |
  | 院内死亡事件数 | ≥ 100 | 保证 iAUROC/校准估计精度，月 1 样本量分析复核 |
  | 主要 landmark 可估计比例 | 12 个主要 landmark 中满足「阳性 ≥20 且阴性 ≥100」者 ≥ 10 个 | 沿用技术文档 §5.1 规则 |
  | 培养覆盖率 | P-strict 要求培养记录覆盖 ≥ 5% 的候选 ICU episodes | 当前实测患者级约 1.5%（2,923/200,859），预示 P-strict 大概率不达标——门槛的意义正在于强制诚实降级 |
  | 实际给药时间可靠率 | `administration_confirmed + infusion_observed` 占抗生素记录比例 ≥ 30% | 低于此值则 P-strict 的 phenotype shift 显著升级，外验命名相应下调 |
  | SOFA 六组分可计算率 | 首个有效 landmark 处 ≥5/6 组分可计算的 episode 比例 ≥ 70% | 组分缺失模式写入 QA |

  **外验命名决策（建模前锁定）**：根据三套队列可行性结果，命名为 `Transportability validation` / `Robustness under phenotype shift` / 探索性跨库验证之一。基于当前实测，默认预期为 **Robustness under phenotype shift**，不得宣称为完全同构的 Sepsis-3 外部验证；**不得**依据 eICU AUROC 反向选择 P-strict/P-clinical/P-explicit（§11 禁止清单）。

- **C8 入排与 index episode**：年龄 ≥18（`patient.age` 数值化，`"> 89"` 记 90 并打标）；同一 `uniquepid` 按 `t_sepsis_episode_offset, hospitaladmitoffset, episode_start_hospital_min, episode_id` 确定性排序取**首次合格 episode**；多 unit stay 一律经 C6a 的 episode 坐标处理。

- **C9/C10 队列事实表** `cohort_eicu_v2`（字段与 C5 同构，时间列全部为 episode 坐标分钟）：`episode_id, index_patientunitstayid, patienthealthsystemstayid, uniquepid, t_sepsis_episode_offset（C7 合同锁定后生效）, episode_start_min(=0), episode_end_min, hospitaladmitoffset, hospital_discharge_episode_min, hospitaldischargestatus, hospitaldischargelocation, age_num, gender, unittype, hospitalid, phenotype_track, antibiotic_time_reliable_rate, 敏感性标志`。

### 2.3 两库队列字段同构约定

两库队列事实表输出**同名同义列**；时间列分两套命名：MIMIC 为 `*_ts`（TIMESTAMP，年份偏移），eICU 为 `*_offset_min`（INTEGER 分钟，episode 坐标）。所有下游节点按「相对 t_sepsis 的小时差」对齐，禁止直接比较两库原始时间列。eICU 侧凡涉及结局与标签的时间一律先转换为 `*_episode_min`（§4.1）。

### 2.4 内部时间划分（技术文档 §12.2 落地）

实测 `anchor_year_group` 为 5 类。按 v1.9 预设映射的自然延伸固定为（下表人数为**全库 `patients` 表人数，非脓毒症队列人数**；队列口径的正式数字由阶段 B 管线产出）：

| 集合 | anchor_year_group | 全库 patients 表人数（参考） |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873 |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 组为 v1.9 未预期类别：主分析不使用（与 v1.9「删除 COVID-era 分析」一致）。该处理须在**阶段 A** 形成正式 protocol amendment 或技术文档修订版，明确：①排除理由（MIMIC-IV-ECG 时间覆盖、COVID 时期诊疗模式漂移、预注册映射未覆盖）；②该组数据**完全不查看结局与模型性能**；③是否仅保留为潜在扩展/敏感性数据（风险 R2）。

划分按 `subject_id` 归入，同一患者所有 landmark 同属一个集合；患者级随机划分仅作敏感性分析。划分表 `split_assignments_v2`（`subject_id, set_name`）落盘冻结。

最终冻结时输出（技术文档 §9.1 同口径）：脓毒症患者数、ECG-available 患者数、ECG-available landmark 数、死亡患者数、主要 12 个 landmark 的阳性/阴性数。

> 对外表述规范：`anchor_year_group` 是去标识化年份组，不等于真实自然年份。论文中称为「**基于 anchor_year_group 的时间组外验证**」，不得过度解释为精确日历年份上的时间外验证。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time —— 决策门 D0（未锁定）

**当前状态：t_sepsis 未锁定。** 实测本地 `mimiciv_derived.sepsis3` 不含技术文档 §4.1 规定的 `sepsis_time` 字段（实有 `suspected_infection_time` 与 `sofa_time`）。时间原点决定 landmark 生成、风险集、ECG 时效窗、历史窗、24h 标签与 0–72h 主要 iAUROC，属于方案级 estimand 变更，不是字段别名问题。

**D0 前置审计（阶段 A 完成）**：

1. 找到本地 `mimiciv_derived.sepsis3` 的生成 SQL/R 脚本；
2. 记录 mimic-code 的：版本、commit hash、原始 SQL 文件、本地全部修改；
3. 明确 `sofa_time`、`suspected_infection_time` 各自的生成逻辑；
4. 确认技术文档所称 `sepsis_time` 实际应为：`suspected_infection_time`、`sofa_time`，或应由两者按某规则构造。

**D0 两个合法出口（PI 确认后二选一）**：

- **出口 A**：重新生成符合预注册定义的 `sepsis_time`（保留技术文档不变）；此时 `mimic_episode_sepsis.t_sepsis_selection_rule` 按锁定代码规则替换（§2.1 C1）；
- **出口 B**：通过 protocol amendment（技术文档修订版）将主时间原点正式改为 `suspected_infection_time`，并同步更新预注册记录。

**明确禁止**：技术文档写「主分析为 `sepsis_time`」而代码实际使用 `suspected_infection_time`。

**D0 锁定前的许可范围**：仅允许结构审计、可行性统计与原型提取（阶段 B 的可行性队列可在候选时间原点下并行跑通两套口径做对比）；**禁止**正式模型训练、超参数选择与测试集评估。

eICU 侧：`t_sepsis_episode_offset` 由 C7 表型时间合同按 `t_sepsis_rule` 合成，合同与 D0 结论保持一致性登记。敏感性分析（技术文档 §4.1/§15.2）保留三种时间原点对比：锁定版 sepsis_time / suspected infection time / ICU admission。

`Δ_ICU-sepsis = ICU 入科时间 − t_sepsis`（MIMIC 直接相减；eICU 为 `0 − t_sepsis_episode_offset`），作为显式输入特征（技术文档 §4.4），输出于 `landmark_context_v2`（§5.1）。

### 3.2 Landmark 生成（DAG 节点 L1）

对每个 index episode：

1. `k0 = max(0, ceil((t_ICU − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_episode_offset) / 360min))`。
2. landmark 序列 `t_lm(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. 终止规则：`t_lm(k) < min(episode_end, 死亡时间)`——**以连续 episode 结束时间为准**；ICU 转出至病房后停止生成新 landmark，但已生成 landmark 的 24h 结局随访继续完成（技术文档 §4.2）。
4. 主分析积分网格固定为 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v2`（每 landmark 一行）：`episode_key, subject_key, k, t_lm, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2）

landmark t 纳入条件：t 时刻存活且仍处于连续 ICU episode 内。排除：

- t 前或 t 时刻已死亡（MIMIC `admissions.deathtime ≤ t`；eICU `Expired 且 death_episode_min ≤ t_episode`）；
- t 前或 t 时刻连续 episode 已结束（`episode_end ≤ t`）。

### 3.4 边界条件（全部转化为单元测试）

| 情形 | 判定 |
|---|---|
| landmark 时刻恰好死亡 | 不进入风险集 |
| landmark 时刻恰好 ICU 出科（episode 结束） | 不进入风险集 |
| 死亡发生在 `(t, t+24h]` | 阳性（含恰好 `t+24h`） |
| 出院恰好发生在 `t+24h` | 按存活至窗口终点处理（阴性，存活出院） |
| ECG 恰好发生在 landmark 时刻 | 允许使用（`ecg_time ≤ t_lm`） |
| 特征恰好在 landmark 时刻可获得 | 允许使用（`available_time ≤ t_lm`） |
| 死亡时间早于 admittime 或晚于 dischtime 且无院内死亡标志 | 时间异常，打标进入 QA，不直接参与标签判定 |
| `hospital_expire_flag = 1` 且 `deathtime` 缺失 | 标签 `unknown / death_time_missing`（§4.1） |
| `deathtime` 非空且 `hospital_expire_flag = 0` | 状态冲突，进 QA（§4.1） |

---

## 4. 结局标签（DAG 节点 L3）

### 4.1 主结局：landmark 后 24h 院内全因死亡（三态标签 + 可判定性拆分）

**标签字段（两库同构，评审 §三.2 修订）**：

```text
y_24h            : 1 / 0 / NULL        -- 主分析仅使用非 NULL
label_status     : event / non_event / unknown
outcome_ascertainable      : TRUE / FALSE   -- 结局是否可判定（主分析依据）
full_inhospital_followup_24h : TRUE / FALSE -- 是否在本院持续观察至 t+24h（描述性）
outcome_unknown_reason : NULL / acute_transfer / missing_status_left_observation
                         / death_time_missing / status_conflict / time_anomaly
label_reason     : 触发分支的文本说明（审计用）
```

**两字段语义（替代 v2.0 的 `label_observable`）**：

- `outcome_ascertainable = TRUE`：可以确定院内死亡标签——**包括提前明确存活出院**（患者虽无完整 24h 院内观察，但院内死亡结局可确定为未发生）。**主分析依据此字段**（`= TRUE` 纳入）。
- `full_inhospital_followup_24h = TRUE`：患者在本院持续观察至 `t+24h`。提前存活出院者此字段为 FALSE，但不妨碍其 `y_24h = 0` 进入主分析。

**判定逻辑（按序执行，首个命中分支生效）**：

0. **死亡状态冲突预检**：`hospital_expire_flag = 1 AND deathtime IS NULL` → `NULL`（unknown，`death_time_missing`）；`deathtime` 非空 `AND hospital_expire_flag = 0` → 状态冲突打标进 QA（`status_conflict`），标签按 QA 复核结果处理；
1. `(t, t+24h]` 内院内死亡 → `y_24h = 1`（event）；
2. `(t, t+24h]` 内转至其他急性医疗机构 → `NULL`（unknown，`acute_transfer`）——不编码为阴性，从该 landmark 主分析排除；敏感性分析分别按存活离院与最坏情景编码；
3. 院内可观测期完整覆盖至 `t+24h`（仍在住院）且未死亡 → `y_24h = 0`（non_event）；
4. `(t, t+24h]` 内明确存活出院（回家/康复机构等非急性转出类别）→ `y_24h = 0`（non_event，技术文档 §2.1；`full_inhospital_followup_24h = FALSE` 但 `outcome_ascertainable = TRUE`）；
5. 结局状态缺失且患者在预测窗前已离开可观测范围 → `NULL`（unknown，`missing_status_left_observation`）；
6. 出院状态缺失但患者在本院持续被观察至 `t+24h` → `y_24h = 0`（non_event），不必一律排除。

**派生字段口径（评审 §五.2 明确）**：`acute_transfer_time` 与 `alive_discharge_time` **由 `dischtime + discharge_location` 分类派生**（MIMIC 中无独立事件时间字段），二者**互斥（XOR）**：同一 `hadm_id` 不得同时非空；同时命中时按急性转出优先并打 QA 标记。两库的转出/存活出院类别字符串不同，清单在 QA 步骤实测后预登记（风险 R9）。

**MIMIC 实现要点**：`admissions.deathtime` 仅记录院内死亡（`hospital_expire_flag = 1` 互查，冲突规则见分支 0）；`patients.dod` 为日期精度，仅用于 1 年死亡等辅助分析，不进主结局。

**eICU 实现要点（评审 §五.3：统一坐标）**：原始 `hospitaldischargeoffset` 相对 unit stay，**不得**直接与 episode offset 比较。C9/C10 统一生成：

```text
hospital_discharge_hospital_min   -- -hospitaladmitoffset + hospitaldischargeoffset
hospital_discharge_episode_min    -- 转换至 episode 坐标
death_episode_min                 -- Expired 时同上转换
```

**所有标签代码只能使用转换后的 `*_episode_min`**：`death_episode_min ∈ (t_episode, t_episode + 1440]` → 1。实测 `hospitaldischargestatus`：Alive 181,104 / Expired 18,004 / NULL 1,751；NULL 者按分支 5/6 依据完整 24h 院内可观测性判定（可观测依据：unit 出院 offset、后续护理/检验记录存在性等，规则预登记）。

### 4.2 次要结局：7 天竞争风险（四类事件）

同一 landmark 输出事件类型与事件时间（eICU 一律用 `*_episode_min`），供 DeepHit CIF 使用：

```text
event_type:
  0 = administrative censoring        -- t_lm + 168h 行政截尾
  1 = in-hospital death
  2 = alive discharge
  3 = transfer to another acute hospital
```

**同时刻多状态优先级（预登记）**：死亡 > 急性转出 > 存活出院 > 删失。若急性转出事件数不足，按技术文档 §15.2 降级为状态未知删失并明确报告。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（组分总分增加 ≥2，基于 §5.4 `sofa_realtime_available`）、新启用血管活性药（§5.5 NEE 流由 0 转 >0）。

---

## 5. 特征提取模块

### 5.0 数据可用时间契约（含聚合记录时间字段，评审 §三.6 修订）

**每条原始特征记录必须携带三个时间字段**：

```text
event_time       -- 临床事件发生/测量时刻
available_time   -- 临床实际可见（可获得）时刻
source_time_type -- 可用时间的来源类型（见下表）
```

**主分析断言：`available_time ≤ t_lm`**。event_time 与 available_time 的差异分布进入 QA 报告（§7.2）。

| 数据域 | 主时间语义（available_time 口径） | source_time_type 取值 |
|---|---|---|
| 床旁连续生命体征（监护仪自动导入） | 测量/观察时间 | `measured` |
| 生命体征（护理人工录入） | 优先 `storetime`；无法确认时降级打标 | `entry_verified` / `charttime_fallback` |
| 检验 | **结果可用时间优先**（MIMIC `storetime`；eICU 经 §5.3 语义审计后锁定）；无法获得时显式降级 | `result_available` / `charttime_fallback` |
| 药物输注 | 实际 start/end time，非 order time | `infusion_actual` / `order_time_only` |
| 微生物 | 初步/最终结果分别使用各自可用时间 | `preliminary` / `final` |
| ECG | 采集开始时间（技术文档 §7.2） | `acquired` |
| 诊断 | 仅使用 landmark 前明确可见的诊断记录 | `recorded_pre_landmark` |
| 治疗限制（DNR/CCO） | 实际记录/生效时间 | `order_effective` |

**聚合记录时间字段（新增）**：按 1h 网格聚合（同小时取中位数）后，单条 `event_time/available_time` 语义不再明确，聚合表改为输出：

```text
bin_start, bin_end        -- 聚合窗口（1h）
n_source_records          -- 参与聚合的原始记录数
min_event_time            -- 原始记录最早事件时间
max_event_time            -- 原始记录最晚事件时间
max_available_time        -- 原始记录最晚可用时间（防泄漏字段）
aggregation_method        -- median 等
source_table_set          -- 参与聚合的来源表集合
```

**防泄漏执行方式（双重）**：①聚合**前先过滤** `available_time > t_lm` 的原始记录（推荐主路径）；②聚合后断言 `max_available_time ≤ t_lm`（Q1 自动测试）。

统一时间语义（技术文档 §15.3，不变）：生命体征与检验映射至 **landmark 前 24h 的 1h 时间网格**，同小时多条取**中位数**，无记录保留缺失并生成观测 mask 与距上次观测间隔（GRU-D 输入三元组）。t=0 landmark 允许使用 sepsis onset 前数据。

### 5.1 静态特征（DAG 节点 F1；拆分为基线静态表 + landmark 上下文表，评审 §三.5）

**输出拆分**：体重/身高遵守 `t_weight ≤ t_lm` 后已非「每 episode 一行」的纯静态变量，拆为两张表：

```text
baseline_static_v2        -- 每 episode 一行：年龄、性别、入院类型/来源、ICU 类型、
                            Charlson(charlson_prior)、anchor_year_group、敏感性标志等固定量
landmark_context_v2       -- 每 episode × landmark 一行：最近可用体重（t_weight ≤ t_lm）、
                            最近可用身高、Δ_ICU-sepsis、当前支持状态（通气/血管活性药/尿量不足等）
```

| 特征 | MIMIC 来源 | eICU 来源 | 归属表 / 备注 |
|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` 数值化（`"> 89"`→90 并打标） | baseline_static |
| 性别 | `patients.gender` | `patient.gender` | baseline_static；类别对齐 M/F/Other |
| 体重 | `weight_durations` | `pivoted_weight` + `admissionweight` | **landmark_context**：每 landmark 取 `t_weight ≤ t_lm` 的最近值并记录其 `available_time`；landmark 前尚无记录者保留缺失 |
| 身高 | `height` / `omr` | `admissionheight` | landmark_context；同体重规则 |
| 入院类型/来源 | `admissions.admission_type, admission_location` | `hospitaladmitsource, unitadmitsource` | baseline_static；C 层低同构（§6） |
| ICU 类型 | `icustays.first_careunit`（episode 首 stay） | `patient.unittype` | baseline_static；C 层低同构（§6） |
| Δ_ICU-sepsis | 计算列 | 计算列 | landmark_context（同 episode 内恒定，随表输出便于对齐） |
| Charlson 合并症 | **重建 `charlson_prior`：仅用 index 入院前已完成的既往住院 ICD**；本次住院最终 ICD 版 `charlson_discharge_coded` 仅敏感性 | `past_history` 路径映射自建近似（不同构，差异预登记） | baseline_static；**移出 SC-common**（§6） |

**体重固定口径的替代方案（敏感性分析）**：若将体重固定为入院初始值，则只取入院初始测量、不随 landmark 更新、对入院初期尚不可用的 landmark 保留缺失，并在数据字典中显式声明该口径。**禁止**为早期 landmark 使用住院后较晚测得的体重。

### 5.2 生命体征时序（DAG 节点 F2；来源分层 + 去重）

| 变量 | MIMIC 来源 | eICU 来源（主来源 → 缺失补充） | 目标单位 |
|---|---|---|---|
| HR | 分层后的 `vitalsign`/`chartevents` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | 同上有创/无创分层，有创优先 | `pivoted_vital.ibp_*` 优先，次 `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 仅在主来源缺失时补充 | mmHg |
| RR | 同上 | `pivoted_vital.RespiratoryRate` → `vital_periodic.respiration` | 次/分 |
| SpO2 | 同上 | `pivoted_vital.spo2` → `vital_periodic.sao2` | % |
| 体温 | 同上（派生表已转 °C，QA 验证分布） | `pivoted_vital.temperature`（量纲 QA 验证，°F→°C 转换规则预登记） | °C |

**MIMIC 生命体征按 itemid 分来源层（评审 P0-4.1 新增）**：`derived.vitalsign` 主要保留 `charttime`，不保证保留人工录入的 `storetime`，不能自动认定 `available_time = charttime`。按 `d_items` itemid 分三层：

1. **自动监护仪导入**：available_time 用测量时间（`measured`）；
2. **护理人工录入**：优先 `storetime`（`entry_verified`）——派生宽表无法保留该信息时**从 `main.chartevents` 重建**；
3. **来源不明**：打 `charttime_fallback`，并将该变量在数据字典中显式降级为「回顾性测量时间口径」。

itemid 分层清单在阶段 A 经 `d_items` 类别审计后预登记；各层占比与录入延迟分布写入 QA 报告。

**eICU 三来源去重规则（沿 v2.0）**：①每变量明确主来源；②其他来源仅在主来源缺失时补充，不得与主来源记录一起取中位数；③记录级去重（同患者、同时刻、同值判重）；④每条记录输出 `source_table`；⑤抽查同一时间同一变量的跨表重复率写入 QA。

### 5.3 检验（DAG 节点 F3；原始重建 + P/F 双时间 + eICU 语义审计）

SOFA 及 SC-common 所需项目：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- **MIMIC**：关键项目**从 `main.labevents` 重建**（itemid 清单预登记），同时保留 `charttime`（event_time）与 `storetime`（available_time 主口径）；派生宽表仅交叉校验。
- **eICU**：`pivoted_lab`（22 项宽表）+ `pivoted_bg`；原始 `lab` 表补充未入宽表项目（`labname` 字符串匹配）。
- **eICU lab 时间语义审计（评审 P0-5 新增，阶段 A 专项）**：`labresultrevisedoffset` 仅写「辅助」不足以实施。须先回答：①`labresultoffset` 表示采样、结果还是记录时间；②`labresultrevisedoffset` 是否仅表示修订时间；③当前行保存的 `labresult` 是原始值还是最终修订值；④修订时间晚于 landmark 时是否会把最终修订值提前用于预测；⑤`labresultrevisedoffset` 缺失、负值或小于 `labresultoffset` 时如何处理。**候选规则（须经数据语义与样本审计后锁定）**：若当前行为最终修订值，则 `available_time = max(labresultoffset, labresultrevisedoffset)`。阶段 A 输出专项报告 `qa/eicu_lab_time_semantics_qa.md`，至少含：两 offset 非空率、差值分布、修订值比例、时间倒置比例、典型检验分项差异、人工抽查结果。**报告完成前 eICU 检验一律按 `chartoffset` 口径并打 `charttime_fallback`。**
- **PaO₂/FiO₂（评审 P0-4.2 重写）**：FiO₂ 通常来自 `chartevents` 或呼吸机设置而非 `labevents`，两项须**分别确定可用时间**。主分析 P/F 逐对输出：

  ```text
  pao2_value, pao2_event_time, pao2_available_time
  fio2_value, fio2_event_time, fio2_available_time
  pf_available_time = max(pao2_available_time, fio2_available_time)   -- 防泄漏字段
  pf_pairing_gap_min                                                  -- 两项事件时间差
  fio2_source ∈ {measured, ventilator_setting, device_based_estimated, flow_only_estimated}
  ```

  断言 `pf_available_time ≤ t_lm`（配对间隙上限预登记，超出者不配对）。**`derived.bg.pao2fio2ratio` 仅作交叉校验，不直接作为严格实时主特征**。FiO₂ 使用规则不变：主分析仅默认使用明确记录的 FiO₂；氧流量换算降级为敏感性分析且必须联合设备类型（`pivoted_o2.o2_device`）；无设备类型时不得仅按流量换算。

### 5.4 SOFA 组分（DAG 节点 F4；经典心血管规则修正 + 三套口径分离）

**三套口径分离（评审 P0-4.3 新增）**：

```text
sofa_phenotype_locked        -- 表型 SOFA：锁定版 mimic-code 的回顾性 Sepsis-3 定义口径
                                （用于 sepsis 判定与表型描述）
sofa_realtime_available      -- 模型输入 SOFA：仅使用 landmark 前 available_time ≤ t_lm
                                的输入重建，满足 §5.0 主合同
sofa_realtime_completeness   -- 实时 SOFA 各组分的可计算完整性（QA 与缺失模式描述）
```

- **MIMIC**：表型口径直接使用 `mimiciv_derived.sofa`（1h 粒度，24h 滑动最差）。**`derived.sofa` 可能按检验 `charttime` 而非 `storetime` 计算，即使窗口语义验证通过也不自动满足 available-time 主合同**——`sofa_realtime_available` 须以 §5.3 重建的可用时间口径输入重算；若阶段 C 不重建实时 SOFA，则 **`derived.sofa` 总分不得作为严格实时模型特征**，仅保留各原始可用组分变量，并在论文中声明口径限制。
- **亚组 SOFA 口径**：CV-SOFA≥3 固定亚组（首个有效 landmark、患者级，技术文档 §15.1）**应使用 `sofa_realtime_available` 的心血管组分**；若实时 SOFA 重建未通过 QA，则该亚组分析整体标注为回顾性口径并列入局限性。
- **窗口语义验证（沿 v2.0）**：QA 人工抽查 20–50 个 stay，核对 `starttime/endtime` 区间、`*_24hours` 是否只用 `endtime` 前数据、小时边界取值（§7.5）。
- **eICU**：自建六组分（24h 滑动最差，与 MIMIC 口径对齐）——呼吸（`pivoted_bg` P/F + 通气标志）、凝血（`platelets`）、肝脏（`bilirubin`）、心血管（经典规则，见下）、神经（`pivoted_gcs`，镇静口径差异预登记）、肾脏（`creatinine` + `pivoted_uo` 24h 尿量）。
- **心血管组分经典规则（评审 §三.1 修正：阈值重叠 + 最大分值计分）**：

  | 分值 | 标准 |
  |---|---|
  | 0 | MAP ≥ 70 mmHg，且无相关血管活性药 |
  | 1 | MAP < 70 mmHg |
  | 2 | dopamine ≤ 5 μg/kg/min，或任意剂量 dobutamine |
  | 3 | dopamine **> 5 且 ≤ 15** μg/kg/min，或 epinephrine ≤ 0.1 μg/kg/min，或 norepinephrine ≤ 0.1 μg/kg/min |
  | 4 | dopamine > 15 μg/kg/min，或 epinephrine > 0.1 μg/kg/min，或 norepinephrine > 0.1 μg/kg/min |

  **计分规则**：对所有满足条件的准则分别计分后**取最大值**：

  ```text
  SOFA_CV = max(MAP criterion, dopamine criterion, dobutamine criterion,
                epinephrine criterion, norepinephrine criterion)
  ```

  （否则「dobutamine + 高剂量 norepinephrine 联用」会被错误计为 2 分。）

  **三者严格分离（沿 v2.0）**：①`sofa_cv_original`＝上述经典评分（表型判定、CV-SOFA≥3 亚组、跨库可比性的唯一口径）；②`nee_current`＝模型输入特征与论文 2 标签基础（§5.5）；③`vasopressor_burden`＝探索性扩展变量。Vasopressin、phenylephrine **不进入**经典 SOFA 心血管计分；**禁止**用 NEE 直接生成主分析 SOFA 心血管组分（风险 R15）。
- `sepsis3` 表内静态 SOFA 组分仅用于表型判定，**禁止**作为 landmark 级特征（风险 R11）。

### 5.5 血管活性药与 NEE（DAG 节点 F5；论文 1 特征 + 论文 2 标签基础）

- **MIMIC**：`mimiciv_derived.vasoactive_agent` → 按技术文档 §6.2 公式合成 NEE。**NEE 双实现核验**：逐时点保存并比较 `nee_project_formula / nee_mimic_derived / nee_difference / nee_source_drug_components`；核验一致率、绝对误差分布、误差来源、vasopressin 单位、dopamine/phenylephrine 换算、重叠输注处理；不一致时以附件 B 公式为准并记录原因。体重按 §6.2 优先级且遵守 §5.1 landmark 截断。
- **eICU**：`infusion_drug` 解析管线：①药名正则归类（清单 QA 枚举后预登记）；②`drugrate`/`infusionrate` 文本数值化；③单位→μg/kg/min（VAS 保持 U/min）；④体重优先级：`infusion_drug.patientweight` → `pivoted_weight` → `admissionweight` → 理想体重；⑤按 NEE 公式求和。`pivoted_infusion` 仅作存在性交叉校验（风险 R5）。
- 输注 episode 规则：短间隙 <30min 合并；重叠记录按 order ID/通路判重（MIMIC `linkorderid`；eICU 无 order ID，按药名+时间连续性）。
- **论文 2 标签人工审核拆分**：月 1 人工审核按 7 环节分别审核并各自报告一致性：①药物归类；②单位解析；③速率标准化；④episode 合并；⑤`t_stop`；⑥`t_0`；⑦48h 复用事件。eICU 论文 2 标签在 MIMIC 侧双实现核验通过前暂缓实施。

### 5.6 机械通气与氧合支持（DAG 节点 F6）

- MIMIC：`derived.ventilation`（`ventilation_status` 时段）；`oxygen_delivery` 补充 HFNC。
- eICU：`respiratory_care.airwaytype/venttype`、`treatment` 通气路径、`pivoted_o2` 设备与流量。

### 5.7 尿量与液体平衡（DAG 节点 F7）

- MIMIC：`derived.urine_output`（必要时 `outputevents` 补充非尿出量）。
- eICU：`pivoted_uo.urineoutput`；`intake_output` 计算 24h 液体平衡。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC；显式 OR 归属条件）

1. **就诊归属条件（评审 §三.4 重写为显式 OR）**：

   ```text
   eligible ECG =
       [ admittime ≤ t_ecg ≤ min(t_lm, dischtime) ]                -- same_hospitalization
     ∨ [ auditable_pre_admission_encounter ∧ t_ecg ≤ t_lm ]        -- 入院前可审计就诊
   ```

   每份候选 ECG 输出四态归属：

   ```text
   ecg_encounter_status:
     same_hospitalization                  -- 主分析纳入
     auditable_pre_admission_encounter     -- 主分析纳入（打 pre_admission_ecg = TRUE）
     uncertain                             -- 仅敏感性分析
     outside_index_encounter               -- 排除
   ```

   **`auditable_pre_admission_encounter` 的审计来源（预登记）**：①ED stay 主键关联（若本地库含 MIMIC-ED `edstays`，按 subject/hadm 键链）；②ED 离开时刻至住院入院时刻的间隔 ≤ 预登记阈值；③ECG 与入院之间不存在其他 encounter；④入院前最大允许时长（小时数预登记）。四条件全部满足方可判为该态，否则判 `uncertain`。
2. **ECG availability 五层级**：`ecg_found_raw → ecg_same_encounter → ecg_structurally_valid → ecg_pass_frozen_qc → ecg_selected_for_model`，逐级输出，区分「没做 / 不属本次就诊 / 文件损坏 / 质量差 / 被时间窗淘汰」。
3. **两层 QC**：固定结构性 QC（全部集合统一：文件可读、时长、采样率与增益可解析、导联数、非全平线、无损坏）；数据驱动 QC（SNR、基线漂移、饱和比例、极端振幅、导联相关性——阈值**仅训练集确定**，验证/测试用冻结阈值）。
4. **时效与选片**：landmark 级 availability＝`∃ eligible ecg_time ≤ t_lm 且 t_lm − ecg_time ≤ 24h`（主分析），48h/72h 敏感性；多份取时效窗内最近一份通过 QC 者；序列编码作敏感性。**主配对队列定义在 QC 完成后、查看测试集结果前冻结**：`ECG available = eligible encounter ∩ within 24h ∩ structurally valid ∩ pass frozen QC`。
5. 患者级 ECG 描述队列：`t_sepsis ± 24h` 内 ≥1 份 ECG（仅描述/可行性，不作纳入条件）。
6. 波形定位：`E:\clinical_research\MIMIC_IV_3.1\ecg\` + `ecg_records.path` + `.hea/.dat`。预处理按技术文档 §20（500 Hz、10 s、12 导联标准顺序、训练集拟合归一化参数）。
7. `ecg_measurements` 作廉价试金石特征与 QC 辅助；`ecg_waveform_features` 为 100 行试点表，不进管线。
8. 防泄漏：`ecg_time` 为采集开始时间；QC 阈值仅训练集确定。

---

## 6. SC-common 跨库变量分层映射总表

按跨库同构程度分四层；每变量须补齐技术文档 §8 数据字典字段（时间戳类型、单位转换、异常值范围、聚合规则、缺失定义、数据可用时刻、泄漏风险等级）。**core/extended 的升级决策依据阶段 D 同构性核验结果，在建模前锁定，禁止依据模型 AUROC 或外验效果反向调整。**

### A 层：高同构变量 → `SC-common-core`（主外验模型候选）

| 临床概念 | MIMIC 来源 | eICU 来源 | 单位 | 泄漏风险 |
|---|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` | 岁 | 低 |
| 性别 | `patients.gender` | `patient.gender` | — | 低 |
| HR | §5.2 分层来源 | `pivoted_vital`/`vital_periodic` | bpm | 低 |
| MAP（有创/无创） | §5.2 分层来源 | `ibp_mean`/`nibp_mean`/`systemicmean` | mmHg | 低 |
| RR | 同上 | `RespiratoryRate`/`respiration` | /min | 低 |
| SpO2 | 同上 | `spo2`/`sao2` | % | 低 |
| 体温 | 同上 | `temperature` | °C（eICU 量纲 QA） | 低 |
| 肌酐 | `labevents` 重建 | `pivoted_lab.creatinine` | mg/dL | 低 |
| 胆红素 | 同上 | `pivoted_lab.bilirubin` | mg/dL | 低 |
| 血小板 | 同上 | `pivoted_lab.platelets` | K/μL | 低 |
| 乳酸 | 同上 | `pivoted_lab.lactate` | mmol/L | 低 |
| WBC | 同上 | `pivoted_lab.wbc` | K/μL | 低 |

### B 层：中等同构变量 → 与 A 层合并构成 `SC-common-extended`

| 临床概念 | MIMIC 来源 | eICU 来源 | 主要差异 | 泄漏风险 |
|---|---|---|---|---|
| GCS | `derived.gcs` | `pivoted_gcs`/`pivoted_score` | 镇静口径（`gcs_unable` 规则） | 中 |
| PaO2/FiO2 | §5.3 双时间重建 | `pivoted_bg` | 拼接规则、`fio2_source` 层级 | 中 |
| 尿量（24h） | `derived.urine_output` | `pivoted_uo` | 记录完整性 | 低 |
| 机械通气 | `derived.ventilation` | `respiratory_care`/`treatment` | 状态判定路径 | 中 |
| 血管活性药使用（0/1） | `vasoactive_agent` | `infusion_drug`/`pivoted_infusion` | 仅使用标记，不涉及剂量 | 中 |

### C 层：低同构变量 → **不进入主外验模型**（仅敏感性分析或 SC-MIMIC）

| 临床概念 | 主要不等价来源 | 处置 |
|---|---|---|
| NEE 精确剂量 | eICU 文本单位解析、体重缺失、给药协议差异 | C 层；剂量协议差异预注册；`nee_current` 可作 MIMIC 侧模型特征与论文 2 标签 |
| Charlson | eICU `past_history` 与 MIMIC 既往住院 ICD 不同构；派生表含本次住院最终 ICD | **移出 SC-common**；`charlson_prior` 作 SC-MIMIC 特征；或仅保留少数两库可靠映射的既往病史二元变量 |
| ICU 类型 | 科室命名体系不同 | C 层；类别映射表预登记后仅描述 |
| 入院来源 | 类别体系不同 | C 层；同上 |
| SOFA 总分及部分组分 | eICU 自建与 `derived.sofa` 的输入完备性差异 | C 层；SOFA 用于表型判定与亚组分层，不作为 SC-common-all 的输入特征 |

### 固定定义

```text
SC-common-core     = A 层
SC-common-extended = A + B 层
SC-MIMIC           = 全量 MIMIC 特征（含 C 层与 ECG 相关描述变量；内部探索，不纳入唯一主要比较）
```

首版外验模型默认使用 `SC-common-core`。感染源（`microbiologyevents`/`micro_lab`）稀疏且结果滞后，不进主模型，仅事后描述与亚组分析（技术文档 §15.3，泄漏风险高）。

---

## 7. 防泄漏与质量控制

### 7.1 防泄漏断言（管线自动测试 Q1）

每次提取后自动运行并输出 `qa/leakage_report.md`：

1. `ecg_time ≤ t_lm` 且满足 §5.8 显式 OR 归属条件；
2. **全部特征 `available_time ≤ t_lm`（主断言）**；聚合记录断言 `max_available_time ≤ t_lm`；P/F 断言 `pf_available_time ≤ t_lm`；同时报告 `event_time ≤ t_lm` 与差异分布；
3. 结局窗起点 > t_lm；
4. 同一患者不跨 train/val/test；
5. 同一患者 landmark 不跨 calibration/test；
6–8. 标准化/异常值阈值/插补器仅训练集拟合；
9. 特征筛选仅训练集；
10. ECG 数据驱动 QC 阈值仅训练集确定。

附加断言：landmark 单调递增且间隔 6h；`k0 ≥ 0`；结局三态判定与 `deathtime/hospitaldischargestatus` 双向一致分层抽查（§7.4）；eICU offset→小时换算零误差；**§3.4 全部边界条件单元测试**；`ecg_encounter_status` 四态分布与非空校验；`sofa_cv_original` 与 `nee_current` 字段共存且来源列不同；**episode 主键唯一性**（每 stay 恰好一个 episode、`mimic_episode_sepsis` 每 episode 恰好一行）；`acute_transfer_time` XOR `alive_discharge_time`；eICU 标签仅使用 `*_episode_min`（静态代码检查）。

### 7.2 时间逻辑 QA

- `admittime ≤ icu_intime < icu_outtime ≤ dischtime` 成立比例；
- `t_sepsis` 相对 ICU 入科分布；`k0` 分布；t=0 不在 ICU 的患者比例；
- 每个 k 的风险集人数；landmark 后仍在 ICU（episode 内）验证；
- 每变量 `event_time` 与 `available_time` 差异分布；
- eICU 多 unit stay 时间映射连续性（`eicu_event_time_map` 单调性、episode 间隙分布）；
- MIMIC episode 审计：`gap_minutes` 分布、`transfer_evidence` 构成、`episode_mapping_status = needs_review` 比例；
- MIMIC 生命体征 itemid 三层（监护仪/护理录入/不明）占比与录入延迟分布。

### 7.3 队列表型 QA

- **MIMIC 随机抽查**：suspected infection 配对、SOFA ≥2、index episode、连续 episode 合并（含 `transfers` 审计一致性）、`t_sepsis`；
- **eICU 分层抽查**：抗生素识别阳性/阴性、培养识别、配对方向、**跨 unit stay 配对命中**、SOFA 六组分、sepsis time、多 unit stay 时间映射、`antibiotic_time_source` 四层构成。

### 7.4 结局 QA（分层抽查）

分层抽样并分别报告一致性：24h 死亡阳性、24h 明确阴性、24h 内存活出院、急性转出、eICU 出院状态缺失、ICU 转出后院内死亡、死亡恰好在 `t+24h` 边界附近、**死亡状态冲突（`death_time_missing` / `status_conflict`）**。

### 7.5 派生表来源验证（D0 前置）

本地 `mimiciv_derived` 由前期 R 脚本生成，须补齐：SQL/R 文件 checksum、mimic-code commit、DuckDB 版本、生成日期、源表版本、随机样本与原始表回溯验证、行数与主键唯一性、与官方参考输出的统计分布比较（含 §5.4 SOFA 窗口语义 20–50 stay 人工核对）。

### 7.6 ECG 配对 QA

至少抽查：同一住院内 ECG、入院前 ECG（审计四条件逐项核验）、出院后 ECG（应判 `outside_index_encounter`）、多份 ECG 取最近者、landmark 恰好等于 ECG 时间、文件路径与 study_id 一致、header 导联顺序与单位。

### 7.7 专项与常规 QA 输出（Q2/Q3）

- `qa/eicu_lab_time_semantics_qa.md`（§5.3，阶段 A）：两 offset 非空率、差值分布、修订值比例、时间倒置比例、典型检验分项差异、人工抽查；
- `qa/sofa_realtime_completeness_v2.md`（§5.4）：实时 SOFA 各组分可计算率与缺失模式；
- 队列流程图计数（每 DAG 节点纳入/排除人数，两库分别；eICU 三套表型分列）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；当前已知原始基线：MIMIC sepsis3 stays 41,295 / subjects 31,910，ECG 总覆盖 161,352 subjects，eICU 全库 200,859 stays / 院内死亡 18,004——队列过滤后的正式数字由管线产出）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集统计）；
- eICU Go/No-Go 门槛检查表（§2.2 C7，逐项对照预登记阈值）。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v2.parquet, cohort_eicu_v2.parquet   # 每 episode 一行
  episodes/  mimic_icu_episode_map.parquet                     # icustays 锚定 + 审计字段（C0）
             mimic_episode_sepsis.parquet                      # episode 级 sepsis 聚合（C1）
             eicu_unitstay_timeline.parquet                    # C6a
             eicu_event_time_map.parquet                       # C6a
  phenotypes/ eicu_phenotype_tracks_v2.parquet                 # 三套队列汇总（C7）
             eicu_phenotype_event_v2.parquet                   # 表型时间合同逐事件记录（C7）
  splits/    split_assignments_v2.parquet
  landmarks/ landmarks_v2.parquet
  labels/    labels_24h_v2.parquet           # y_24h, label_status, outcome_ascertainable,
                                             # full_inhospital_followup_24h,
                                             # outcome_unknown_reason, label_reason
             labels_competing_7d_v2.parquet  # event_type 0/1/2/3 + event_time（eICU 用 *_episode_min）
  features/  baseline_static_v2.parquet      # 每 episode 一行（固定量，含 charlson_prior）
             landmark_context_v2.parquet     # 每 episode × landmark 一行（最近可用体重/身高、
                                             #   Δ_ICU-sepsis、当前支持状态）
             vitals_hourly_v2.parquet        # 长表 + mask + Δt + bin 聚合字段（§5.0）
                                             #   + source_table + source_time_type
             labs_hourly_v2.parquet          # 同上；P/F 含双时间字段与 fio2_source
             sofa_hourly_v2.parquet          # sofa_phenotype_locked / sofa_realtime_available /
                                             #   sofa_realtime_completeness / sofa_cv_original
             nee_stream_v2.parquet           # episode × 时间（5min 网格）；
                                             #   nee_project_formula / nee_mimic_derived /
                                             #   nee_difference / nee_source_drug_components
  ecg_index/ ecg_landmark_index_v2.parquet   # landmark × 最近合格 ECG（study_id, ecg_time, path,
                                             #   时效, ecg_encounter_status, pre_admission_ecg,
                                             #   五层级 availability 标志）
  qa/        cohort_flow_v2.md, feasibility_table_v2.md, leakage_report_v2.md,
             time_logic_qa_v2.md, phenotype_qa_v2.md, outcome_stratified_qa_v2.md,
             ecg_pairing_qa_v2.md, derived_provenance_v2.md, eicu_go_nogo_v2.md,
             eicu_lab_time_semantics_qa.md, sofa_realtime_completeness_v2.md
  _meta/     code_version.json               # mimic-code 版本/commit、SQL/R checksum、本地修改、
                                             # DuckDB 版本、提取时间
             d0_decision.json                # D0 出口、依据、日期、PI 确认
             freeze_checklist.json           # §10 各项关闭状态
```

规范：①统一 Parquet；②所有表携带 `subject_key / episode_key / landmark_k` 三级键，eICU 侧键为 `uniquepid / episode_id / k`，原始 `stay_id / patientunitstayid` 保留溯源；③患者级 ID 管理与划分表冻结后不得重算；④每个 DAG 节点独立脚本、I/O schema 校验、中间产物持久化（技术文档 §19.1）；⑤`code_version.json` 阶段 A 补齐；⑥D0 结论与冻结清单关闭状态分别写入 `_meta/d0_decision.json` 与 `_meta/freeze_checklist.json`。

---

## 9. 已识别风险与待决事项（R1–R22）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| R1 | 本地 `sepsis3` 表无 `sepsis_time` 字段 | 主分析时间原点/estimand | **决策门 D0（§3.1）**：阶段 A 审计后 PI 锁定出口 A/B；冻结清单 A-1 |
| R2 | `anchor_year_group` 含 `2020 - 2022`，v1.9 未规定 | 时间划分 | 主分析不用；阶段 A 正式 amendment（§2.4）；冻结清单 A-3 |
| R3 | eICU 无 Sepsis-3 派生表，`micro_lab` 仅 2,923 患者有培养 | 外验队列表型 | C6b 方向性配对 + C7 三套队列 + Go/No-Go；默认命名 Robustness under phenotype shift |
| R4 | eICU SOFA 需自建，GCS 镇静口径差异 | SOFA 可比性 | F4 口径对齐；差异预登记；窗口语义人工核对 |
| R5 | eICU 输注速率文本内嵌单位，`pivoted_infusion` 无剂量 | NEE/论文 2 标签 | F5 解析管线；人工审核 7 环节拆分 |
| R6 | 库内并存 SOFA-1 与 SOFA-2（`sofa2_*`） | 误用风险 | 仅用 SOFA-1；命名检查进 Q1 |
| R7 | 遗留/试点表（`test_*`、`tmp_*`、`crab_modeling_cohort`、`ecg_waveform_features` 等） | 误用风险 | 白名单制 |
| R8 | eICU 无 ECG，availability 与库来源共线 | 门控外推 | eICU 仅走 SC-common-all 独立路径 |
| R9 | 转急性医疗机构类别字符串两库不一致 | 结局 unknown 标记 | QA 实测类别清单后预登记；冻结清单 D-3 |
| R10 | 体重缺失/极端值（<40 / >150 kg） | NEE 与论文 2 标签 | 技术文档 §6.2 规则；landmark 截断（§5.1） |
| R11 | `sepsis3` 静态 SOFA 组分被误用作 landmark 特征 | 泄漏/口径错误 | 禁用；landmark SOFA 取小时表/实时重建（§5.4） |
| R12 | 检验 `charttime` 早于结果可用时间导致实时泄漏 | 主分析时间语义 | §5.0 契约；关键检验原始重建保留 `storetime`；不可用时声明「按测量时间的回顾性预测」 |
| R13 | ECG 仅按 subject/time 配对可能跨住院 | ECG-EHR 配对正确性 | §5.8 显式 OR 归属 + 四态；`uncertain` 仅敏感性 |
| R14 | eICU 多 unit stay offset 坐标不一致 | 连续 episode 时间正确性 | C6a 统一坐标 + 桥接表；标签仅用 `*_episode_min` |
| R15 | 使用 NEE 替代经典 SOFA 心血管评分 | 表型/亚组可比性 | §5.4 修正阈值 + 最大分值计分；三变量分离；Q1 共存检查 |
| R16 | Charlson 派生表包含本次住院最终 ICD | 静态特征泄漏 | 重建 `charlson_prior`；移出 SC-common |
| R17 | 标签 SQL 将未知结局误编码为阴性 | 标签正确性 | §4.1 三态 + `outcome_ascertainable`；eICU 按 24h 可观测性判定 |
| R18 | eICU 培养覆盖过低导致严重 phenotype selection | 外验有效性 | C7 三套队列对比 + Go/No-Go；命名建模前锁定 |
| **R19** | **eICU 三套表型时间合同（感染时间/SOFA 窗口/基线/ΔSOFA/t_sepsis）未锁定** | 外验时间原点 | C7 `phenotype_event` 合同；锁定前 `t_sepsis_episode_offset` 不正式生成；冻结清单 A-5 |
| **R20** | **eICU lab offset 语义未审计（修订值可能提前使用）** | eICU 检验泄漏 | 阶段 A 专项报告 `eicu_lab_time_semantics_qa.md`；候选 max 公式验证后锁定；报告前按 `charttime_fallback`（§5.3）；冻结清单 C-2 |
| **R21** | **available-time 合同在 MIMIC 生命体征/PF/动态 SOFA 落实不完整** | 主分析防泄漏 | §5.2 itemid 三层、§5.3 P/F 双时间、§5.4 SOFA 三口径；不满足时降级声明；冻结清单 C-3/4/5 |
| **R22** | **Go/No-Go 数值与给药时间可靠率未最终锁定** | 外验可行性决策 | §2.2 C7 建议值；阶段 A PI 确认预登记，禁止按模型效果反向调整；冻结清单 A-6 |

---

## 10. 冻结清单（Freeze Checklist）

正式冻结为主分析提取方案前，以下各项须全部关闭；状态实时记录于 `_meta/freeze_checklist.json`。

### A. 协议冻结

- [ ] A-1 D0 出口 A/B 已确定；
- [ ] A-2 `_meta/d0_decision.json` 已生成；
- [ ] A-3 `2020–2022` amendment 已签署；
- [ ] A-4 mimic-code commit 与本地修改已锁定（含 SQL/R checksum）；
- [ ] A-5 eICU 三套表型时间合同已锁定（§2.2 C7 规则表全部「预登记」项填定）；
- [ ] A-6 Go/No-Go 数值已预登记（PI 确认，未据模型效果调整）。

### B. 时间轴冻结

- [ ] B-1 MIMIC episode 以 `icustays` 为锚构建；
- [ ] B-2 episode 映射一对多关系符合预期（`episode_mapping_status` 审计通过）；
- [ ] B-3 每个 stay 仅属于一个 episode（主键唯一性测试通过）；
- [ ] B-4 eICU 所有事件均转换到 hospital/episode 坐标；
- [ ] B-5 跨 unit stay 的抗生素—培养配对测试通过；
- [ ] B-6 标签只使用统一坐标（eICU 仅 `*_episode_min`）。

### C. 防泄漏冻结

- [ ] C-1 MIMIC 关键检验使用 `storetime`（`labevents` 重建完成）；
- [ ] C-2 eICU lab revised time 语义已验证（专项报告关闭）；
- [ ] C-3 MIMIC 人工记录生命体征的录入延迟已处理（itemid 分层落地）；
- [ ] C-4 P/F 使用两部分中较晚 available time（`pf_available_time` 断言通过）；
- [ ] C-5 动态 SOFA 的 available-time 口径已确定（`sofa_realtime_available` 或降级声明）；
- [ ] C-6 聚合记录的 `max_available_time` 已定义并接入 Q1。

### D. 标签冻结

- [ ] D-1 `outcome_ascertainable` 与 `full_inhospital_followup_24h` 已拆分；
- [ ] D-2 死亡状态冲突规则已固定（`death_time_missing` / `status_conflict`）；
- [ ] D-3 急性转出清单已冻结（两库分别，XOR 互斥验证通过）；
- [ ] D-4 eICU 出院 offset 已转换到 episode 坐标；
- [ ] D-5 全部边界单元测试通过（§3.4）。

### E. ECG 冻结

- [ ] E-1 pre-admission ECG 的 OR 条件已修正（§5.8）；
- [ ] E-2 ED-to-admission 审计规则已固定（四条件参数预登记）；
- [ ] E-3 结构性 QC 已固定；
- [ ] E-4 数据驱动 QC 只在训练集拟合；
- [ ] E-5 24h 主配对队列定义已冻结（查看测试集结果前）。

---

## 11. 实施顺序（阶段 A–D）

**阶段 A：协议与来源锁定（本阶段结束前不查看验证/测试集性能差异）**

1. 核对 mimic-code commit、本地派生 SQL/R 脚本与 checksum（§7.5）；
2. 完成 D0 审计并由 PI 锁定出口 A/B（§3.1）；
3. `2020 - 2022` 正式 amendment（§2.4）；
4. 锁定连续 ICU episode 定义与间隙阈值（C0/C6a）；
5. 锁定数据可用时间语义（§5.0）；**eICU lab 时间语义专项报告**（§5.3）；**MIMIC 生命体征 itemid 来源分层审计**（§5.2）；
6. 锁定经典 SOFA 与 NEE 独立定义（§5.4/§5.5）；**锁定 eICU 三套表型时间合同**（§2.2 C7）；**PI 确认 Go/No-Go 数值并预登记**；
7. 关闭冻结清单 A 组与 C-2/C-3。

**阶段 B：仅做 MIMIC 可行性队列（可在 D0 候选口径下并行两套，但不冻结）**

1. episode 映射（C0，icustays 锚定 + transfers 审计）；2. episode 级 sepsis 聚合与 index episode（C1–C3）；3. landmark（L1）；4. 三态标签（L3，含冲突规则）；5. ECG 就诊归属与 availability 五层级（F8）；6. 输出主要 12 个 landmark 的患者数、阳性数、ECG 覆盖率——决定 ECG 主要比较是否有足够事件数（技术文档 §9.1 Go 条件）。

**阶段 C：MIMIC 特征与论文 2 标签**

1. 按 available_time 提取特征（F1–F7，关键检验原始重建、P/F 双时间、itemid 分层）；2. 重建 `charlson_prior`；3. ECG 两层 QC 与阈值冻结；4. NEE 双实现核验；5. 实时 SOFA 重建与完整性评估（或降级声明）；6. 论文 2 人工标签验证（7 环节拆分，PPV >80% 为 Go 条件）。

**阶段 D：eICU 外验可行性**

1. 统一住院时间坐标（C6a）；2. 按已锁定的表型时间合同构建三套 phenotype（C7）；3. 严格复现经典 SOFA（F4）；4. 评估各医院数据覆盖与 Go/No-Go 逐项核对；5. 锁定 `SC-common-core`（或 extended）终稿；6. 决定外验命名与层级。

**当前可进行（评审 §八固化）**：源表与派生表 provenance 审计；D0 双口径比较；episode 映射原型；MIMIC 队列规模估算；三态标签原型；ECG 覆盖率与就诊归属统计；eICU 时间轴与三套 phenotype 的可行性统计；SC-common 缺失率与单位映射审计。

**当前不应进行（冻结清单关闭前禁止）**：正式训练最终模型；选择超参数；查看测试集性能；依据 eICU AUROC 选择 P-strict/P-clinical/P-explicit；依据模型效果决定 core/extended；正式开展跨库性能结论；把 eICU 称为完全同构 Sepsis-3 外部验证。

---

## 12. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：基于两库 DuckDB 实测结构核验的可实施提取方案（DAG、输出规范、风险 R1–R11、SQL 模板） |
| v2.0 | 2026-07-30 | 第一轮评审修订：D0 决策门；eICU 方向性配对与三套表型 + Go/No-Go；两库 episode 桥接表；数据可用时间契约；经典 SOFA 心血管规则；ECG 就诊归属与两层 QC；三态标签；竞争风险急性转出；SC-common 分层；QA 强化；风险 R12–R18；阶段 A–D |
| v2.1 | 2026-07-30 | **第二轮评审修订**——①eICU 表型时间合同 `phenotype_event`（感染时间/SOFA 窗口/基线/ΔSOFA/t_sepsis 规则表，锁定前不生成正式 t_sepsis）；②MIMIC episode 改以 `icustays` 为锚、`transfers` 仅审计，输出 5 个审计字段；③新增 `mimic_episode_sepsis` episode 级聚合，患者级排序在聚合后进行；④available-time 贯穿：MIMIC 生命体征 itemid 三层、P/F 双时间重建（`pf_available_time`）、SOFA 三口径（phenotype_locked/realtime_available/realtime_completeness）；⑤eICU lab 时间语义专项审计与候选 max 公式；⑥SOFA 心血管 3/4 分阈值重叠修正 + 最大分值计分；⑦`label_observable` 拆分为 `outcome_ascertainable`/`full_inhospital_followup_24h`，死亡状态冲突规则，转出/存活出院 XOR，eICU 标签统一 `*_episode_min`；⑧附录 A.5 改 episode 坐标配对；⑨ECG 归属改显式 OR + 审计四条件；⑩静态表拆 `baseline_static_v2`/`landmark_context_v2`；⑪聚合记录 bin 时间字段与 `max_available_time` 断言；⑫P-clinical/P-explicit 未来诊断防护、`antibiotic_time_source` 四态、Go/No-Go 建议数值；⑬新增 §10 冻结清单（A–E 共 27 项）与「可进行/不应进行」清单；⑭风险 R19–R22；⑮状态表述修正为「待决项列入冻结清单」。 |

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

> **说明**：附录均为**概念性模板**，用于固定逻辑与边界语义，不构成 C0–C10 的完整实现；正式实施以各 DAG 节点脚本及 I/O schema 校验为准。

### A.0 MIMIC 连续 ICU episode 映射（C0；icustays 锚定，transfers 仅审计）

```sql
-- 以 main.icustays 为 episode 候选基础（stay_id 本身通常已代表连续 ICU 住留）
WITH s AS (
  SELECT subject_id, hadm_id, stay_id, intime, outtime,
         LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_outtime
  FROM main.icustays
),
g AS (
  SELECT *,
         EPOCH(intime - prev_outtime) / 60.0 AS gap_minutes,
         CASE WHEN prev_outtime IS NULL THEN 1
              WHEN EPOCH(intime - prev_outtime) / 60.0 > 0 THEN 1   -- 预登记阈值，默认 0 分钟
              ELSE 0 END AS new_episode_flag
  FROM s
),
e AS (
  SELECT *,
         SUM(new_episode_flag) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS episode_seq
  FROM g
)
SELECT subject_id, hadm_id,
       hadm_id::VARCHAR || '_EP' || episode_seq::VARCHAR AS episode_id,
       stay_id,
       ROW_NUMBER() OVER (PARTITION BY hadm_id, episode_seq ORDER BY intime, stay_id)
         AS stay_seq_in_episode,
       MIN(intime)  OVER (PARTITION BY hadm_id, episode_seq) AS episode_intime,
       MAX(outtime) OVER (PARTITION BY hadm_id, episode_seq) AS episode_outtime,
       gap_minutes
FROM e;
-- 审计层（另表输出，概念性）：相邻 stay 间查询 main.transfers，判定
--   transfer_evidence ∈ {direct_icu_to_icu, brief_icu_exit, via_ward, overlap_or_anomaly, none}
--   intervening_careunit；时间倒置/区间重叠 → episode_mapping_status = 'needs_review'
-- 约束：每 stay_id 恰好一个 episode_id（主键唯一性测试，冻结清单 B-3）
```

### A.1 MIMIC 队列骨架（C1–C3；episode 先聚合，再患者级排序）

```sql
WITH sepsis AS (
  SELECT subject_id, stay_id, suspected_infection_time AS t_sepsis   -- D0 锁定后替换
  FROM mimiciv_derived.sepsis3
  WHERE sepsis3
),
-- 同一 episode 内多个 sepsis3 stay 先聚合（P0-3）
ep_sepsis AS (
  SELECT e.episode_id,
         COUNT(*)                                          AS qualifying_sepsis_count,
         MIN(s.t_sepsis)                                   AS t_sepsis,
         (ARRAY_AGG(s.stay_id ORDER BY s.t_sepsis, s.stay_id))[1]
                                                           AS t_sepsis_source_stay_id,
         'min_t_sepsis_within_episode'                     AS t_sepsis_selection_rule
  FROM sepsis s
  JOIN mimic_icu_episode_map e USING (subject_id, hadm_id, stay_id)
  GROUP BY e.episode_id
),
eligible AS (
  SELECT es.episode_id, es.t_sepsis, es.qualifying_sepsis_count,
         es.t_sepsis_source_stay_id,
         em.subject_id, em.hadm_id, em.episode_intime, em.episode_outtime,
         a.admittime, d.admission_age
  FROM ep_sepsis es
  JOIN (SELECT DISTINCT episode_id, subject_id, hadm_id,
                        episode_intime, episode_outtime
        FROM mimic_icu_episode_map) em USING (episode_id)
  JOIN main.admissions a USING (hadm_id)
  JOIN mimiciv_derived.icustay_detail d
    ON d.stay_id = es.t_sepsis_source_stay_id
  WHERE d.admission_age >= 18
    -- 成人 ICU 类别清单（QA 实测后预登记）
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY subject_id
           ORDER BY t_sepsis, admittime, episode_intime, episode_id   -- 完全确定性
         ) AS rn
  FROM eligible
)
SELECT * FROM ranked WHERE rn = 1;   -- 首次合格 sepsis-associated episode（每 episode 一行）
-- first_icu_stay 仅描述，不作过滤条件
```

### A.2 Landmark 网格与风险集（MIMIC；稳定 interval 写法）

```sql
SELECT c.episode_id, k,
       c.t_sepsis + (6 * k) * INTERVAL '1 hour' AS t_lm
FROM cohort_mimic_v2 c
JOIN main.admissions a USING (hadm_id)
CROSS JOIN generate_series(
  CAST(GREATEST(0, CEIL(EPOCH(c.episode_intime - c.t_sepsis) / 21600)) AS INTEGER),
  27) AS t(k)
WHERE c.t_sepsis + (6 * k) * INTERVAL '1 hour'
      < LEAST(
          COALESCE(c.episode_outtime, TIMESTAMP '9999-01-01 00:00:00'),
          COALESCE(a.deathtime,       TIMESTAMP '9999-01-01 00:00:00')
        );
-- 显式处理：episode_outtime IS NULL、deathtime IS NULL、
-- 时间倒置（outtime < intime）、出院后死亡时间异常 → 打标进 QA（§7.2）
```

### A.3 三态 24h 标签（MIMIC；含冲突预检与派生处置表）

```sql
-- 派生处置（概念性）：由 dischtime + discharge_location 分类生成，二者 XOR 互斥
WITH disp AS (
  SELECT hadm_id,
         CASE WHEN discharge_location IN (/* 急性转出清单，QA 实测后预登记 */)
              THEN dischtime END AS acute_transfer_time,
         CASE WHEN discharge_location IN (/* 存活出院清单（回家/康复等） */)
              THEN dischtime END AS alive_discharge_time
  FROM main.admissions
)
SELECT l.episode_id, l.k, l.t_lm,
  CASE
    WHEN a.hospital_expire_flag = 1 AND a.deathtime IS NULL
      THEN NULL                                        -- unknown: death_time_missing
    WHEN a.deathtime >  l.t_lm
     AND a.deathtime <= l.t_lm + INTERVAL '24 hours'
      THEN 1                                           -- event
    WHEN d.acute_transfer_time >  l.t_lm
     AND d.acute_transfer_time <= l.t_lm + INTERVAL '24 hours'
      THEN NULL                                        -- unknown: acute_transfer
    WHEN COALESCE(a.dischtime, TIMESTAMP '9999-01-01 00:00:00')
         >= l.t_lm + INTERVAL '24 hours'
      THEN 0                                           -- 观察期完整覆盖窗口
    WHEN d.alive_discharge_time >  l.t_lm
     AND d.alive_discharge_time <= l.t_lm + INTERVAL '24 hours'
      THEN 0                                           -- non_event: 存活出院（可判定）
    ELSE NULL                                          -- unknown
  END AS y_24h
  -- 同步输出：label_status / outcome_ascertainable (= y_24h IS NOT NULL 的语义化判定) /
  --   full_inhospital_followup_24h / outcome_unknown_reason / label_reason（§4.1）
  -- deathtime 非空 AND hospital_expire_flag = 0 → status_conflict，打标进 QA
FROM landmarks_v2 l
JOIN cohort_mimic_v2 c USING (episode_id)
JOIN main.admissions a USING (hadm_id)
LEFT JOIN disp d      USING (hadm_id);
```

### A.4 eICU 住院级时间坐标换算（C6a）

```sql
SELECT patientunitstayid, patienthealthsystemstayid, uniquepid,
       -hospitaladmitoffset                       AS unit_start_hospital_min,
       -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min
FROM main.patient;
-- 任意事件：hospital_offset_min = -hospitaladmitoffset + event_offset_min
-- episode 合并：同一 patienthealthsystemstayid 内按 unit_start_hospital_min 排序，
--   相邻间隙 ≤ 预登记阈值者并入同一 episode_id；
--   episode_offset_min = hospital_offset_min - episode_start_hospital_min
-- 结局同步转换：hospital_discharge_episode_min / death_episode_min（§4.1）
```

### A.5 eICU 方向性 suspected infection 配对（C6b；episode 坐标配对，评审 §三.3 修正）

```sql
-- 抗生素与培养先各自经 eicu_event_time_map 换算到 episode 坐标，再按 episode_id 配对
WITH ab AS (
  SELECT etm.episode_id, etm.episode_offset_min AS ab_time,
         m.patientunitstayid AS source_stay
  FROM main.medication m
  JOIN eicu_event_time_map etm
    ON etm.patientunitstayid = m.patientunitstayid
   AND etm.local_offset_min   = m.drugstartoffset   -- 优先级见 C6b antibiotic_time_source
  WHERE m.drugname ILIKE ANY (SELECT pattern FROM preregistered_antibiotics)
),
cx AS (
  SELECT etm.episode_id, etm.episode_offset_min AS cx_time,
         ml.patientunitstayid AS source_stay
  FROM main.micro_lab ml
  JOIN eicu_event_time_map etm
    ON etm.patientunitstayid = ml.patientunitstayid
   AND etm.local_offset_min   = ml.culturetakenoffset
)
SELECT ab.episode_id, ab.ab_time, cx.cx_time,
       ab.source_stay AS ab_stay, cx.source_stay AS cx_stay   -- 跨 stay 配对可审计
FROM ab
JOIN cx USING (episode_id)        -- 允许 ab_stay <> cx_stay（同一连续 episode 内）
WHERE (ab.ab_time - cx.cx_time) BETWEEN 0 AND 4320   -- 培养先：72h 内首剂抗生素
   OR (cx.cx_time - ab.ab_time) BETWEEN 0 AND 1440   -- 抗生素先：24h 内培养
-- 窗口数值以阶段 A 锁定版 mimic-code 为准；配对结果进入 C7 表型时间合同
```

### A.6 eICU 去甲肾上腺素速率解析（片段）

```sql
SELECT patientunitstayid, infusionoffset,
       TRY_CAST(drugrate AS DOUBLE) AS rate_value,
       REGEXP_EXTRACT(drugname, '\(([^)]*)\)', 1) AS unit_hint   -- 如 mcg/min、mg/hr
FROM main.infusion_drug
WHERE drugname ILIKE 'Norepinephrine%';
-- 后续：unit_hint → μg/kg/min 换算 × 体重优先级（F5）；双实现核验字段见 §5.5
```

---

*本方案 v2.1 基于 2026-07-30 对两库的只读结构核验与两轮外部评审《总体评价》生成；与技术文档 v1.9 冲突之处以技术文档为准，需变更技术文档的事项（D0 出口 B、`2020-2022` 处理）须经 protocol amendment 正式登记。§10 冻结清单全部关闭前，本方案不得作为正式主分析提取管线使用。*
