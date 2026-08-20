# SEPSIS-MM-DYN 数据提取方案 v2.2

- 文档版本：v2.2
- 创建日期：2026-07-30（v1.0 同日创建；v2.0、v2.1 经两轮外部评审修订；v2.2 经第三轮外部评审修订）
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 修订依据：《总体评价》（2026-07-30 第三轮评审，对 v2.1 结论为「有条件通过作为冻结前实施候选版」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**正式提取管线冻结候选版**。本版已按第三轮评审补全全部可文档化规则为可冻结形态（eICU 表型规则已无「预登记/可选/二选一」占位）。**冻结生效条件**：§10 冻结清单（31 项）全部关闭，且小规模人工抽查、主键唯一性测试、标签边界单元测试、available-time 泄漏测试与 feasibility table 审核全部通过。冻结生效前禁止正式模型训练、超参数选择与测试集评估。仍属外部决策的待决项（D0 出口、`2020 - 2022` amendment、Go/No-Go 数值 PI 确认、各专项语义审计）全部列入冻结清单，未关闭不视为已解决。

---

## 0. v2.1 → v2.2 修订总览

本节按第三轮评审《总体评价》的章节编号逐项登记修改落点。历史修订见 §12 变更日志。

### 0.1 最后一轮 P0（评审 §二，5 项）

| 评审编号 | 问题 | v2.2 落点 |
|---|---|---|
| P0-1 | episode 合并「默认 0 分钟」含混；重叠记录取 `MAX(outtime)` 掩盖时间异常；病房/ED 区间是否合并未定义 | §2.1 C0 拆为**三条独立规则**：①主合并阈值明确为 `τ_merge = 0 min`（30/60 min 仅敏感性）；②`gap < 0` 重叠单独打 `overlap_flag = TRUE` + `needs_review`，不自动合并；③合并须 transfers 路径佐证，存在病房/ED 区间一律 `episode_merge_eligible = FALSE`。新增 `episode_merge_eligible / episode_merge_decision / episode_merge_exclusion_reason` 三字段 |
| P0-2 | eICU 三套表型仍含「预登记/可选/二选一」占位 | §2.2 C7 改为**双层结构**：第一层固定合同字段；第二层规则表**全部填为确定值**（本版拟定，阶段 A PI 逐项签字确认，确认后仅可经 protocol amendment 变更）。P-explicit 明确定位为「显式临床诊断表型」，不暗示与 P-strict 等价 |
| P0-3 | P-clinical「诊断时间 ≤ t_sepsis」存在循环定义风险 | §2.2 C7 改为**前向算法**：按 episode 时间排序感染证据 → 逐 `t_I` 搜索 SOFA 窗口 → 首个 ΔSOFA ≥2 生成候选 `t_sepsis`；`t_sepsis` 之后可用的诊断不得参与证据选择；诊断作为证据时 `t_I = t_diagnosis,available` |
| P0-4 | 附录 A.1 的 `sepsis` CTE 缺 `hadm_id`，SQL 不可执行 | 附录 A.1 修正：经 `main.icustays(stay_id → hadm_id)` 回填（本地 `sepsis3` 实无 `hadm_id`） |
| P0-5 | A.3 未实现正文三态状态机；冲突处理与 adjudication 机制缺失；未防护 `deathtime ≤ t_lm` | 附录 A.3 重写为**完整标签状态机**：单一 CASE 输出 `y_24h / label_status / outcome_ascertainable / full_inhospital_followup_24h / outcome_unknown_reason / label_reason` 六字段；冲突状态一律先 `unknown + outcome_ascertainable = FALSE`，QA 复核经独立 `label_adjudications` 表覆盖（不改写自动提取结果）；显式 `deathtime ≤ t_lm` → `invalid_input` |

### 0.2 P1 明确化（评审 §三，7 项）

| # | 问题 | v2.2 落点 |
|---|---|---|
| 1 | 实时 SOFA 缺失组分处理未定义 | §5.4：逐组分输出 7 字段；总分仅在预登记最少组分数（≥5/6）满足时计算，**缺失组分不得默认计 0**；明确各组分最大回溯、GCS unable/镇静/插管、尿量缺失 vs 无尿规则 |
| 2 | MIMIC 生命体征 itemid 分层不能只靠 `d_items.label` | §5.2：分层审计综合 `itemid / d_items.category / storetime−charttime 分布 / 重复记录模式 / 记录间隔特征 / 监护仪表对应关系`；无法稳定识别时保留双轨 `vitals_realtime_strict / vitals_charttime_retro` 并报告差异 |
| 3 | `infusion_observed` 过度等同于实际给药确认 | §2.2 C6b 更名并降级为 `infusion_recorded`；Go/No-Go 改为分别报告 `administration_confirmed_rate / infusion_recorded_rate / non_administration_time_rate` 三个率 |
| 4 | `eicu_event_time_map` 按 offset 连接有重复匹配风险 | §2.2 C6a：映射表增加 `event_type / source_table / source_row_id`；并按事件类生成专用桥接表 `eicu_medication_time_map / eicu_microbiology_time_map / eicu_lab_time_map`；附录 A.5 改按源主键回连 |
| 5 | ECG 时间语义未区分采集/处理/选片 | §5.8：索引同时保留 `ecg_acquisition_time / ecg_available_time_assumed / ecg_processing_time / ecg_selection_time`，并声明「采集即可用」是部署假设而非数据库事实 |
| 6 | `charlson_prior` 可用性定义不严 | §5.1：固定窗口 `t_diagnosis < t_index_admission`；输出 `prior_hospital_count / prior_icd_observation_window / charlson_prior_available`；首次住院与入院早期病史的处理规则明确 |
| 7 | SC-common-core 动态输入同构性未逐项核验 | §6：新增变量级**等价性合同** `sc_common_variable_contract_v2`（11 字段），列明 MAP/体温/SpO₂/乳酸/血小板/WBC 的具体风险；合同完成前不得锁定 core 为主外验输入 |

### 0.3 内部不一致修正（评审 §四，4 项）

| # | 问题 | v2.2 落点 |
|---|---|---|
| 1 | 冻结清单「27 项」与实际 28 项不符 | §10 清单新增条目后**实为 31 项（A6+B7+C7+D6+E5）**，§0.5 与 §10 同步写明并改为按组计数 |
| 2 | §7.1 的 `calibration/test` 与 §2.4 划分不一致 | §7.1 改为 `train/validation/test`；独立 calibration 集仅在未来启用 CP 探索时经 §2.4 显式定义后方可引用 |
| 3 | `SC-common-all` 未在正文定义 | 统一为 `SC-common-core / SC-common-extended`；R8 与 §6 同步修正（注明与技术文档模型名的对应关系） |
| 4 | `episode_* / t_ICU / t_sepsis / *_offset_min` 命名不统一 | §2.3 新增**命名规范表**：MIMIC `*_ts`、eICU `*_offset_min`、规范化相对时间 `hours_since_sepsis`；全文关键表定义同步更名 |

### 0.4 附录 SQL 修正（评审 §五，6 项）

A.0 显式输出全部审计字段及来源键；A.1 回填 `hadm_id` 并以 `ROW_NUMBER ... NULLS LAST` 替代 `ARRAY_AGG`（附 SQL 单元测试要求）；A.2 弃用 `TIMESTAMP '9999-01-01'` 哨兵，改显式 NULL 逻辑；A.3 状态机 + 冲突优先判 unknown；A.5 源主键回连；A.6 不变。

### 0.5 新增字段（评审 §六，4 组）

Episode 级 5 字段（§2.1 C0）；SOFA 6 字段（§5.4）；标签 adjudication 4 字段（§4.1）；eICU 抗生素 7 字段（§2.2 C6b）。

### 0.6 冻结建议与阶段调整（评审 §七、§八）

| 评审要求 | v2.2 落点 |
|---|---|
| D0 输出固定 JSON schema | §3.1：`primary_time_origin / secondary_time_origins / source_table / source_code_commit / protocol_amendment_required / pi_approval_date` |
| available-time 双轨报告 | §5.0：`strict_available_time` 与 `chart_or_event_time` 分别报告；无法获得真实可用时间的域不得默默并入严格实时主模型，论文须明确 retrospective chart-time prediction |
| 阶段 C 拆分提前跨库合同 | §11：阶段 C1（MIMIC 特征工程）→ **C2（SC-common 跨库合同，先于正式 MIMIC 训练）** → D（eICU 表型与可行性），杜绝「按模型效果选择跨库变量」 |

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
| 转科 | `main.transfers` | `transfer_id`；`eventtype, careunit, intime, outtime`（**仅作 episode 路径审计**） | 2,413,581 |
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
| 合并症 | `mimiciv_derived.charlson` | `hadm_id`；17 组分 + `charlson_comorbidity_index`（基于本次住院最终 ICD） | 546,028 |
| 体重/身高 | `mimiciv_derived.weight_durations`、`mimiciv_derived.height` | 时段体重；身高 | 401,850 / 43,342 |
| 尿量 | `mimiciv_derived.urine_output` | `stay_id, charttime, urineoutput` | 4,127,634 |
| ICU 汇总 | `mimiciv_derived.icustay_detail` | `stay_id`；年龄、性别、入出院时间、结局、序次 | 94,458 |
| 结局汇总 | `mimiciv_derived.patient_outcomes` | `stay_id`；死亡、SOFA/SOFA-2、通气、RRT 等 73 列 | 94,458 |
| ECG 索引 | `main.ecg_records` | `subject_id, study_id, ecg_time, path` | 800,035 |
| ECG 机测 | `main.ecg_measurements` | `study_id, ecg_time, RR/间期/电轴` | 800,035 |

> 注：`mimiciv_derived` 同时含 SOFA-2 系列表（`sofa2_*`），本项目仅用 **SOFA-1**（风险 R6）。本地派生表的 mimic-code 版本、commit hash、SQL/R 清单与本地修改须在阶段 A 完成登记（冻结清单 A-4）。

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
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath` | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring` | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部原始事件时间为**相对各 unit stay 入科的分钟偏移（offset）**；出院年份仅 2014/2015，**无绝对日期**。多 unit stay 合并前必须先换算到统一住院级时间坐标（§2.2 C6a）。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C0–C5）

- **C0 连续 ICU episode 映射（`icustays` 锚定 + 三条独立合并规则，评审 P0-1）**：MIMIC 中 `stay_id` 本身通常已代表一段连续 ICU 住留；`transfers` 区间与 `icustays` 边界不完全一致，仅作审计。以 `main.icustays` 为候选基础，同一 `hadm_id` 内按 `intime, stay_id` 排序，计算 `gap_minutes = EPOCH(intime(j+1) − outtime(j)) / 60`，按以下**三条独立规则**判定：

  1. **主合并阈值（确定值）**：`gap_minutes ≤ τ_merge` 且 `τ_merge = 0 min`（首尾相接或无间隙才进入合并候选；`τ_merge = 30 / 60 min` 仅作敏感性分析，阶段 A 锁定主值后不再变更）；
  2. **重叠处理（确定值）**：`gap_minutes < 0`（区间重叠）**不自动合并**，单独打 `overlap_flag = TRUE`、`episode_mapping_status = needs_review`，经 QA 人工复核后写入合并裁决（避免 `MAX(outtime)` 掩盖时间异常）；
  3. **路径佐证（确定值）**：两 stay 之间经 `transfers` 审计存在**普通病房或 ED 区间**时，即使间隙很短也 `episode_merge_eligible = FALSE`（规则上相接不等于临床连续 ICU 观察）；仅 `direct_icu_to_icu` 或无反向证据（`none`）且满足规则 1–2 者方可合并。

  输出桥接表（含全部审计字段）：

  ```text
  mimic_icu_episode_map
  - subject_id, hadm_id, episode_id
  - stay_id, stay_seq_in_episode
  - episode_intime_ts, episode_outtime_ts           -- 命名规范见 §2.3
  - gap_minutes                                     -- 与前一同住院 stay 的间隙（首个 stay 为 NULL）
  - merge_reason                                    -- first_stay / contiguous / overlap / gap
  - overlap_flag                                    -- gap < 0
  - intervening_careunit                            -- 间隙期间所在单元（transfers 审计）
  - transfer_evidence                               -- direct_icu_to_icu / brief_icu_exit / via_ward /
                                                    --   via_ed / overlap_or_anomaly / none
  - episode_merge_eligible                          -- 规则 1–3 综合
  - episode_merge_decision                          -- merged / split / pending_review
  - episode_merge_exclusion_reason                  -- ward_interval / ed_interval / overlap /
                                                    --   gap_exceeds_threshold / none
  - episode_mapping_status                          -- clean / needs_review / adjudicated
  - episode_merge_threshold_min                     -- 实际使用的 τ_merge（留痕）
  - episode_gap_max_min                             -- episode 内最大间隙（描述）
  - episode_transfer_path_class                     -- 全 episode 路径分类汇总
  - episode_mapping_version                         -- 映射规则版本
  ```

  约束：每个 `stay_id` 恰好属于一个 `episode_id`（合并裁决后）；`needs_review` 记录进 QA。下游所有 ICU 数据一律按 `stay_id → episode_id` 聚合；landmark 终止与风险集以 `episode_outtime_ts` 为准。

- **C1 脓毒症相关 episode 池（episode 级 sepsis 聚合）**：`mimiciv_derived.sepsis3`（`sepsis3 = TRUE`，41,295 stays / 31,910 subjects）先经 `main.icustays` 回填 `hadm_id`（本地 `sepsis3` 实无该列），再按 C0 映射归属 episode，**同一 episode 内多个命中 stay 先聚合为 episode 级一行**：

  ```text
  mimic_episode_sepsis
  - episode_id
  - qualifying_sepsis_count
  - t_sepsis_ts                    -- min_j t_sepsis,j（D0 出口 A 时按锁定代码规则替换）
  - t_sepsis_source_stay_id        -- 按 t_sepsis NULLS LAST, stay_id 确定性取
  - t_sepsis_selection_rule        -- 'min_t_sepsis_within_episode'
  ```

  再 ⨝ `mimiciv_derived.icustay_detail` ⨝ `main.icustays`（`first_careunit`）。

- **C2 入排初筛**：年龄 ≥18（`t_sepsis_source_stay_id` 对应 `icustay_detail.admission_age`）；成人 ICU（episode 首个 stay 的 `first_careunit` 排除 NICU 等非成人单元，类别清单 QA 实测为准）。

- **C3 index episode 选择**：先构造全部合格 episode（每 episode 恰好一行），再按 `subject_id` 取**首次合格 episode**，排序键固定为 `t_sepsis_ts, admittime, episode_intime_ts, episode_id`。`first_icu_stay` 仅描述，不作纳入条件。

- **C4 探索性/敏感性标志**：外院转入、首个有效 landmark 前 ECMO、近 90 天实体器官移植、首个有效 landmark 前 DNR/CCO——**完成 PPV 人工抽查前一律仅作探索性/敏感性标志，不用于正式排除**。ICD 为出院后最终编码，不得证明 landmark 前状态；以 ICD 为依据的标志仅用**既往住院**记录。

- **C5 队列事实表** `cohort_mimic_v2`（每 episode 一行）：`subject_id, hadm_id, episode_id, t_sepsis_source_stay_id, t_sepsis_ts（D0 锁定后生效）, episode_intime_ts, episode_outtime_ts, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C10）

- **C6a 住院级统一时间坐标（事件映射含源主键，评审 §三.4）**：

  ```text
  t_hospital_min = -hospitaladmitoffset + eventoffset
  episode_offset_min = hospital_offset_min - episode_start_hospital_min
  ```

  输出桥接表：

  ```text
  eicu_unitstay_timeline
  - patientunitstayid, patienthealthsystemstayid, uniquepid
  - unit_start_hospital_min / unit_end_hospital_min
  - episode_id
  - episode_start_hospital_min / episode_end_hospital_min

  eicu_event_time_map                      -- 泛化事件映射（含源标识，禁按 offset 反连）
  - event_type                             -- medication / microbiology / lab / vital / infusion / ...
  - source_table                           -- 源表名
  - source_row_id                          -- 源记录主键（或稳定行标识）
  - patientunitstayid, local_offset_min
  - hospital_offset_min, episode_offset_min
  ```

  同一 unit stay 同一分钟可有多个事件，**禁止**仅按 `patientunitstayid + local_offset_min` 回连。按事件类生成**专用桥接表**（与源表主键一一对应）：

  ```text
  eicu_medication_time_map      (source_table = medication)
  eicu_microbiology_time_map    (source_table = micro_lab)
  eicu_lab_time_map             (source_table = lab)
  ```

  episode 合并规则：同一 `patienthealthsystemstayid` 内相邻间隙 ≤ `τ_merge_eicu = 0 min`（确定主值；敏感性阈值阶段 A 锁定）者合并；存在非 ICU 区间（`unitstaytype`/住院内转科证据）者不合并并打标；`readmit` 按同一规则判定并单独打标。

- **C6b suspected infection 重建（episode 坐标 + 给药时间四级来源）**：抗生素与培养**先经专用桥接表换算到 episode 坐标，再按 `episode_id` 配对**（跨 unit stay 可命中，附录 A.5）。方向性规则（确定值，窗口随锁定版 mimic-code）：

  ```text
  培养先发生：  t_antibiotic - t_culture  ∈ [0, 72h]
  抗生素先发生：t_culture - t_antibiotic  ∈ [0, 24h]
  ```

  **抗生素时间四级来源（评审 §三.3 修正，`infusion_observed` 更名降级）**：

  ```text
  antibiotic_time_source:
    administration_confirmed   -- 仅明确 MAR/给药完成证据（默认 eICU 不可得，逐例标注依据）
    infusion_recorded          -- infusion_drug 存在该药输注记录（不保证实际完成）
    scheduled_start            -- 仅 medication.drugstartoffset（计划开始）
    order_time                 -- 仅 drugorderoffset（医嘱时间）
  ```

  优先级 `administration_confirmed > infusion_recorded > scheduled_start > order_time`。Go/No-Go **分别报告三个率**：`administration_confirmed_rate / infusion_recorded_rate / non_administration_time_rate`——不得把所有 `infusion_drug` 记录直接计入「实际给药时间可靠率」。

  每条配对输出（评审 §六.4 新增字段）：

  ```text
  antibiotic_time_raw / antibiotic_time_episode
  antibiotic_source_table / antibiotic_source_row_id
  antibiotic_time_confidence     -- high / medium / low（由来源级映射）
  culture_time_episode
  infection_pair_id              -- 配对唯一标识（供 C7 与 QA 回溯）
  ```

- **C7 三套可行性表型队列 + 表型时间合同（双层结构 + 确定规则，评审 P0-2/P0-3）**：

  **第一层：固定合同字段**（结构冻结，不再变更）：

  ```text
  phenotype_event
  - episode_id
  - infection_evidence_time           -- t_I（episode 坐标）
  - infection_evidence_type           -- culture_antibiotic_pair / admission_dx / later_dx / explicit_sepsis_dx
  - sofa_baseline_window_start / sofa_baseline_window_end
  - sofa_qualifying_window_start / sofa_qualifying_window_end
  - baseline_sofa
  - qualifying_sofa
  - delta_sofa
  - sofa_qualifying_time
  - t_sepsis_offset_min
  - t_sepsis_rule
  - phenotype_track
  - infection_pair_id                 -- P-strict 溯源
  ```

  **第二层：正式规则表（本版拟定为确定值；阶段 A PI 逐项签字确认后预登记，之后仅可经 protocol amendment 变更）**：

  | 参数 | P-strict | P-clinical | P-explicit |
  |---|---|---|---|
  | 定位 | 严格 Sepsis-3 复现 | 临床感染证据 + 器官功能障碍 | **显式临床诊断表型**（不暗示与 P-strict 等价） |
  | 感染证据 | C6b 抗生素-培养配对 | 感染诊断证据（`admission_dx` 与 `later_dx` 分开） | 显式 sepsis / severe sepsis / septic shock 诊断字符串（清单已预登记） |
  | 感染时间 t_I | 配对两事件中较早者（随锁定版 mimic-code） | 首个可用感染诊断的 available time：`admission_dx` = 住院入院时刻；`later_dx` = 诊断记录可用时间 | 首个显式 sepsis 诊断的 available time（同上规则） |
  | SOFA 基线窗口 | 完全复现锁定版 mimic-code | `[t_I − 48h, t_I − 24h]`，取窗口内末次可计算 SOFA；episode 内无先前可计算 SOFA 时 baseline = 0 并打 `baseline_assumed_zero = TRUE`（敏感性分析排除） | 不适用（不强制；描述性报告） |
  | SOFA 合格窗口 | 完全复现锁定版 mimic-code | `[t_I − 24h, t_I + 48h]` 内 ΔSOFA ≥ 2 | 不适用（不强制） |
  | ΔSOFA ≥2 | 必须 | 必须 | 不必须 |
  | t_sepsis 规则 | 同锁定版 mimic-code | `t_sepsis = t_I`，资格由合格窗口内 ΔSOFA ≥2 确认；`t_sepsis_rule = 'infection_evidence_time_with_qualifying_delta_sofa'` | `t_sepsis = t_I`；`t_sepsis_rule = 'first_explicit_sepsis_dx_available_time'` |

  **P-clinical 前向算法（评审 P0-3，消除循环定义）**：

  1. 按 episode 时间升序排列全部候选感染证据；
  2. 对每个 `t_I`，在上述固定窗口内搜索 ΔSOFA ≥2；
  3. **首个**满足者生成 `t_sepsis = t_I` 的候选 phenotype_event；
  4. 仅 `t_sepsis` 之前可用的诊断记录可作为描述/验证变量；**禁止**用最终出院诊断反推更早的 `t_sepsis`；
  5. 诊断作为感染证据本身时，`t_I = t_diagnosis,available`（而不是先生成 `t_sepsis` 再筛诊断）。

  三套队列分别报告：患者数、医院数、院内死亡数、各 landmark 阳性数、SC-common 特征覆盖率、与 MIMIC 主队列基线差异。

  **Go/No-Go 门槛（确定建议值；阶段 A PI 确认后预登记，禁止按模型效果反向调整）**：

  | 指标 | 阈值 | 说明 |
  |---|---|---|
  | P-strict 覆盖医院数 | ≥ 20 家，且最大单医院患者占比 ≤ 25% | 避免单中心主导 |
  | 患者数 | P-strict ≥ 500；P-clinical / P-explicit ≥ 2,000 | 外验最低规模 |
  | 院内死亡事件数 | ≥ 100 | 月 1 样本量分析复核 |
  | 主要 landmark 可估计比例 | 12 个中满足「阳性 ≥20 且阴性 ≥100」者 ≥ 10 个 | 技术文档 §5.1 规则 |
  | 培养覆盖率 | P-strict ≥ 5% 候选 ICU episodes | 当前实测约 1.5%，预示大概率不达标 |
  | 给药时间可靠率 | `administration_confirmed_rate` 与 `infusion_recorded_rate` 分别报告；二者合计 ≥ 30%，且 `non_administration_time_rate` ≤ 70% | 三率分列，不得混算 |
  | SOFA 六组分可计算率 | 首个有效 landmark 处 ≥5/6 组分可计算的 episode 比例 ≥ 70% | 缺失模式写 QA |

  **外验命名决策（建模前锁定）**：`Transportability validation` / `Robustness under phenotype shift` / 探索性跨库验证之一；默认预期 **Robustness under phenotype shift**；不得依据 eICU AUROC 反向选择表型。

- **C8 入排与 index episode**：年龄 ≥18（`"> 89"` 记 90 并打标）；同一 `uniquepid` 按 `t_sepsis_offset_min, hospitaladmitoffset, episode_start_hospital_min, episode_id` 确定性排序取首次合格 episode。

- **C9/C10 队列事实表** `cohort_eicu_v2`（与 C5 同构，episode 坐标分钟）：`episode_id, index_patientunitstayid, patienthealthsystemstayid, uniquepid, t_sepsis_offset_min（C7 锁定后生效）, episode_start_offset_min(=0), episode_end_offset_min, hospitaladmitoffset, hospital_discharge_episode_min, hospitaldischargestatus, hospitaldischargelocation, age_num, gender, unittype, hospitalid, phenotype_track, administration_confirmed_rate, infusion_recorded_rate, non_administration_time_rate, 敏感性标志`。

### 2.3 两库队列字段同构约定与命名规范

两库队列事实表输出**同名同义列**；下游一律按「相对 t_sepsis 的小时差」对齐，禁止直接比较两库原始时间列。

**时间字段命名规范（评审 §四.4，本版起强制执行）**：

| 语义 | MIMIC（TIMESTAMP，年份偏移） | eICU（INTEGER 分钟，episode 坐标） |
|---|---|---|
| episode 起点 | `episode_intime_ts` | `episode_start_offset_min`（恒 0） |
| episode 终点 | `episode_outtime_ts` | `episode_end_offset_min` |
| sepsis 原点 | `t_sepsis_ts` | `t_sepsis_offset_min` |
| landmark | `t_landmark_ts` | `t_landmark_offset_min` |
| 规范化相对时间 | `hours_since_sepsis`（两库同名同义，浮点小时） | 同左 |

历史别名（`episode_intime / t_ICU / ICU 入科时间 / episode_start_min` 等）一律映射到上表，新表与新代码不得再使用旧名。eICU 凡涉及结局与标签的时间一律先转换为 `*_episode_min`（§4.1）。

### 2.4 内部时间划分（技术文档 §12.2 落地）

实测 `anchor_year_group` 为 5 类，映射固定为（人数为**全库 `patients` 表人数**，队列口径数字由阶段 B 产出）：

| 集合 | anchor_year_group | 全库 patients 表人数（参考） |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873 |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 须经阶段 A 正式 amendment：①排除理由；②**完全不查看结局与模型性能**；③是否仅保留为潜在扩展数据（风险 R2）。划分按 `subject_id` 归入；`split_assignments_v2`（`subject_id, set_name`）落盘冻结。**独立 calibration 集当前不单独划分**（校准参数仅验证集拟合，技术文档 §14.1）；若未来启用 CP 探索需独立 calibration 集（技术文档 §16.2），须先在本节与划分表中显式定义。

> 对外表述规范：称为「**基于 anchor_year_group 的时间组外验证**」，不得过度解释为精确日历年份上的时间外验证。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time —— 决策门 D0（未锁定）

**当前状态：t_sepsis 未锁定。** 本地 `mimiciv_derived.sepsis3` 不含技术文档 §4.1 规定的 `sepsis_time`（实有 `suspected_infection_time` 与 `sofa_time`）。时间原点决定 landmark 生成、风险集、ECG 时效窗、历史窗、24h 标签与 0–72h 主要 iAUROC，属 estimand 级决策。

**D0 前置审计（阶段 A）**：①定位本地 `sepsis3` 生成 SQL/R 脚本；②记录 mimic-code 版本、commit hash、原始 SQL、本地修改；③明确 `sofa_time` 与 `suspected_infection_time` 生成逻辑；④确认技术文档所称 `sepsis_time` 的应有对应。

**D0 两个合法出口（PI 确认后二选一，评审 §七.1：最终只能一个主口径）**：

- **出口 A**：重新生成符合预注册定义的 `sepsis_time`（技术文档不变）；
- **出口 B**：protocol amendment 将主原点正式改为 `suspected_infection_time` 或明确的合成时间。

**明确禁止**：代码层用 `suspected_infection_time` 而文档层主原点仍写 `sepsis_time`。

**D0 输出固定 schema**（写入 `_meta/d0_decision.json`，评审 §七.1）：

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

**锁定前许可范围**：仅结构审计、可行性统计与原型提取（阶段 B 可在两套候选口径下并行对比）；禁止正式训练、超参数选择与测试集评估。

eICU 侧：`t_sepsis_offset_min` 由 C7 表型时间合同按 `t_sepsis_rule` 合成，与 D0 结论一致性登记。敏感性分析保留三种时间原点对比（技术文档 §4.1/§15.2）。

`Δ_ICU-sepsis = episode_intime − t_sepsis`（eICU 为 `0 − t_sepsis_offset_min`），显式输入特征，输出于 `landmark_context_v2`。

### 3.2 Landmark 生成（DAG 节点 L1）

对每个 index episode：

1. `k0 = max(0, ceil((episode_intime − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_offset_min) / 360min))`。
2. `t_landmark(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. 终止规则：`t_landmark(k) < min(episode 终点, 死亡时间)`——以连续 episode 结束时间为准；ICU 转出至病房后停止生成新 landmark，已生成 landmark 的 24h 随访继续完成。
4. 主分析积分网格固定 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v2`：`episode_key, subject_key, k, t_landmark_ts / t_landmark_offset_min, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2）

landmark t 纳入：t 时刻存活且仍处于连续 ICU episode 内。排除：

- t 前或 t 时刻已死亡（MIMIC `deathtime ≤ t`；eICU `Expired 且 death_episode_min ≤ t_landmark_offset_min`）；
- t 前或 t 时刻 episode 已结束（`episode 终点 ≤ t`）。

### 3.4 边界条件（全部转化为单元测试）

| 情形 | 判定 |
|---|---|
| landmark 时刻恰好死亡 | 不进入风险集 |
| landmark 时刻恰好 episode 结束 | 不进入风险集 |
| 死亡发生在 `(t, t+24h]` | 阳性（含恰好 `t+24h`） |
| 出院恰好发生在 `t+24h` | 按存活至窗口终点（阴性，存活出院） |
| ECG 恰好发生在 landmark 时刻 | 允许使用（`ecg_time ≤ t_landmark`） |
| 特征恰好在 landmark 时刻可获得 | 允许使用（`available_time ≤ t_landmark`） |
| 死亡时间早于 admittime 或晚于 dischtime 且无院内死亡标志 | 时间异常，进 QA |
| `hospital_expire_flag = 1` 且 `deathtime` 缺失 | `unknown / death_time_missing`（§4.1） |
| `deathtime` 非空且 `hospital_expire_flag = 0` | `unknown / status_conflict`，待 adjudication（§4.1） |
| 标签脚本独立运行时遇到 `deathtime ≤ t_landmark` | `invalid_input`（不进任何分支，附录 A.3） |

---

## 4. 结局标签（DAG 节点 L3）

### 4.1 主结局：landmark 后 24h 院内全因死亡（状态机 + adjudication 分离）

**标签字段（两库同构）**：

```text
y_24h            : 1 / 0 / NULL
label_status     : event / non_event / unknown
outcome_ascertainable      : TRUE / FALSE   -- 主分析纳入依据
full_inhospital_followup_24h : TRUE / FALSE -- 描述性
outcome_unknown_reason : NULL / acute_transfer / missing_status_left_observation
                         / death_time_missing / status_conflict / time_anomaly / invalid_input
label_reason     : 状态机分支标识（审计用）
```

**状态机（按序执行，首个命中分支生效；附录 A.3 同构实现）**：

-1. **非法输入防护**：`deathtime ≤ t_landmark` → `invalid_input`（正常流程已被风险集排除；独立运行标签脚本时不得落入其他任何分支）；
0. **死亡状态冲突预检**：`deathtime` 非空 `AND hospital_expire_flag = 0` → `unknown / status_conflict`；`hospital_expire_flag = 1 AND deathtime IS NULL` → `unknown / death_time_missing`。两者均 `outcome_ascertainable = FALSE`，**主提取阶段一律先判 unknown**；
1. `(t, t+24h]` 内院内死亡 → `y_24h = 1`（event）；
2. `(t, t+24h]` 内急性转出 → `NULL`（unknown，`acute_transfer`）；
3. 院内观察完整覆盖至 `t+24h` 且未死亡 → `y_24h = 0`（non_event）；
4. `(t, t+24h]` 内明确存活出院 → `y_24h = 0`（non_event；`full_inhospital_followup_24h = FALSE` 但 `outcome_ascertainable = TRUE`）；
5. 结局状态缺失且预测窗前已离开可观测范围 → `NULL`（`missing_status_left_observation`）；
6. 出院状态缺失但在本院持续被观察至 `t+24h` → `y_24h = 0`（non_event）。

**人工 adjudication 机制（评审 P0-5.2 / §六.3，与自动提取分离）**：冲突与缺失记录**不得**在原始标签 SQL 中隐式处理，QA 复核后写入独立表：

```text
label_adjudications
- episode_key, landmark_k
- label_preliminary_status    -- 自动提取的原始状态（不改写）
- label_final_status          -- 人工复核后的最终状态
- label_adjudication_status   -- pending / adjudicated / rejected
- label_adjudication_source   -- 复核人 / 依据 / 日期
```

下游分析默认使用 `label_preliminary_status`；仅当 `label_adjudication_status = adjudicated` 时以 `label_final_status` 覆盖，并在 QA 报告中给出覆盖比例。

**派生字段口径**：`acute_transfer_time` 与 `alive_discharge_time` 由 `dischtime + discharge_location` 分类派生，二者 **XOR 互斥**；同时命中按急性转出优先并打 QA 标记。类别清单两库分别实测后预登记（风险 R9）。

**eICU 统一坐标**：C9/C10 统一生成 `hospital_discharge_hospital_min / hospital_discharge_episode_min / death_episode_min`；**所有标签代码只使用 `*_episode_min`**。`hospitaldischargestatus` NULL 者按分支 5/6 以完整 24h 可观测性判定。

### 4.2 次要结局：7 天竞争风险（四类事件）

```text
event_type:
  0 = administrative censoring        -- t_lm + 168h 行政截尾
  1 = in-hospital death
  2 = alive discharge
  3 = transfer to another acute hospital
```

同时刻多状态优先级（预登记）：死亡 > 急性转出 > 存活出院 > 删失。急性转出事件数不足时按技术文档 §15.2 降级为状态未知删失并明确报告。eICU 一律用 `*_episode_min`。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（`sofa_realtime_available` 总分增加 ≥2）、新启用血管活性药（NEE 流由 0 转 >0）。

---

## 5. 特征提取模块

### 5.0 数据可用时间契约（双轨报告，评审 §七.3）

**每条原始特征记录携带三个时间字段**：`event_time / available_time / source_time_type`。**主分析断言：`available_time ≤ t_landmark`**。

| 数据域 | available_time 口径 | source_time_type 取值 |
|---|---|---|
| 床旁连续生命体征（监护仪自动导入） | 测量/观察时间 | `measured` |
| 生命体征（护理人工录入） | 优先 `storetime`；无法确认时降级 | `entry_verified` / `charttime_fallback` |
| 检验 | 结果可用时间优先（MIMIC `storetime`；eICU 经 §5.3 审计锁定） | `result_available` / `charttime_fallback` |
| 药物输注 | 实际 start/end time | `infusion_actual` / `order_time_only` |
| 微生物 | 初步/最终结果各自可用时间 | `preliminary` / `final` |
| ECG | 采集开始时间（部署假设，§5.8 声明） | `acquired` |
| 诊断 | landmark 前明确可见的记录 | `recorded_pre_landmark` |
| 治疗限制 | 实际记录/生效时间 | `order_effective` |

**双轨结果报告（评审 §七.3）**：所有依赖时间口径的结果分别报告两套：

```text
strict_available_time     -- 严格可用时间口径（主分析）
chart_or_event_time       -- chart/event 时间口径（回顾性敏感性）
```

某数据域无法获得真实结果可用时间时：①不得默默并入严格实时主模型；②仅作回顾性时间口径敏感性分析；③论文中明确为 **retrospective chart-time prediction**。

**聚合记录时间字段**：`bin_start, bin_end, n_source_records, min_event_time, max_event_time, max_available_time, aggregation_method, source_table_set`。防泄漏双保险：聚合前先过滤 `available_time > t_landmark` 的记录；聚合后断言 `max_available_time ≤ t_landmark`（Q1）。

统一时间语义（不变）：landmark 前 24h 的 1h 网格、同小时中位数、缺失保留 + mask + Δt；t=0 landmark 允许使用 sepsis onset 前数据。

### 5.1 静态特征（DAG 节点 F1；baseline_static + landmark_context）

**输出拆分**：

```text
baseline_static_v2        -- 每 episode 一行：年龄、性别、入院来源、ICU 类型、charlson_prior 等固定量
landmark_context_v2       -- 每 episode × landmark 一行：最近可用体重/身高（t ≤ t_landmark）、
                            Δ_ICU-sepsis、当前支持状态
```

| 特征 | MIMIC 来源 | eICU 来源 | 归属表 / 备注 |
|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` 数值化 | baseline_static |
| 性别 | `patients.gender` | `patient.gender` | baseline_static |
| 体重 | `weight_durations` | `pivoted_weight` + `admissionweight` | landmark_context；记录其 `available_time`；landmark 前无记录保留缺失 |
| 身高 | `height` / `omr` | `admissionheight` | landmark_context；同上 |
| 入院类型/来源 | `admissions.*` | `hospitaladmitsource, unitadmitsource` | baseline_static；C 层 |
| ICU 类型 | `icustays.first_careunit` | `patient.unittype` | baseline_static；C 层 |
| Δ_ICU-sepsis | 计算列 | 计算列 | landmark_context |
| Charlson | **`charlson_prior`（固定窗口，见下）**；`charlson_discharge_coded` 仅敏感性 | `past_history` 自建近似（差异预登记） | baseline_static；移出 SC-common |

**`charlson_prior` 固定口径（评审 §三.6）**：

- 窗口固定为 `t_diagnosis < t_index_admission`（仅 index 入院前已完成住院的 ICD）；
- 患者首次住院（无既往住院）：`charlson_prior = 0` 并打 `charlson_prior_available = FALSE`，不得用本次住院 ICD 填补；
- 既往住院时间范围不设上限（全库历史），但须报告观察窗口；
- 本次入院早期已记录的既往病史（如 MIMIC 入院记录、eICU `past_history`）**不并入** `charlson_prior`，可作为独立二元变量另行评估；
- 逐 episode 输出：`prior_hospital_count`、`prior_icd_observation_window`（最早既往住院距 index 入院的时间）、`charlson_prior_available`。

体重固定口径替代方案（敏感性）：只取入院初始测量、不随 landmark 更新、初期不可用者保留缺失。**禁止**为早期 landmark 使用住院后较晚测得的体重。

### 5.2 生命体征时序（DAG 节点 F2；多信号分层 + 双轨）

| 变量 | MIMIC 来源 | eICU 来源（主来源 → 缺失补充） | 目标单位 |
|---|---|---|---|
| HR | 分层后 `vitalsign`/`chartevents` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | 同上，有创优先 | `pivoted_vital.ibp_*` → `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 仅缺失补充 | mmHg |
| RR | 同上 | `pivoted_vital.RespiratoryRate` → `vital_periodic.respiration` | /min |
| SpO2 | 同上 | `pivoted_vital.spo2` → `vital_periodic.sao2` | % |
| 体温 | 同上 | `pivoted_vital.temperature`（量纲 QA） | °C |

**MIMIC itemid 分层审计（评审 §三.2：不得只靠 `d_items.label`）**：综合以下信号判定来源层——`itemid`、`d_items.category`、`storetime − charttime` 分布、同时刻重复记录模式、记录时间间隔特征、与监护仪专用表/派生表的对应关系。分层清单阶段 A 预登记；各层占比与录入延迟分布写入 QA。

**无法稳定识别自动记录来源时的双轨输出（评审 §三.2）**：

```text
vitals_realtime_strict     -- 仅确认自动导入/可用时间可靠的记录
vitals_charttime_retro     -- 全部记录按 charttime（回顾性口径）
```

两轨分别进入对应分析轨道，差异分布写入 QA；不得强行声称 `available_time = measured_time`。

**eICU 三来源去重规则（沿前版）**：主来源明确；补充来源仅补缺；记录级去重；输出 `source_table`；抽查跨表重复率。

### 5.3 检验（DAG 节点 F3；原始重建 + P/F 双时间 + eICU 语义审计）

项目清单：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- **MIMIC**：关键项目从 `main.labevents` 重建，保留 `charttime` 与 `storetime`；派生宽表仅交叉校验。
- **eICU**：`pivoted_lab` + `pivoted_bg`；原始 `lab` 补充（经 `eicu_lab_time_map` 换算坐标）。
- **eICU lab 时间语义审计（阶段 A 专项，冻结清单 C-2）**：回答 `labresultoffset` 语义、`labresultrevisedoffset` 是否仅修订时间、当前值是否最终修订值、修订晚于 landmark 的提前使用风险、缺失/负值/倒置处理。候选规则（审计后锁定）：最终修订值行 `available_time = max(labresultoffset, labresultrevisedoffset)`。报告 `qa/eicu_lab_time_semantics_qa.md`：两 offset 非空率、差值分布、修订值比例、倒置比例、分项差异、人工抽查。**报告完成前 eICU 检验一律 `charttime_fallback`。**
- **PaO₂/FiO₂ 双时间（沿 v2.1）**：逐对输出 `pao2_value/pao2_event_time/pao2_available_time`、`fio2_value/fio2_event_time/fio2_available_time`、`pf_available_time = max(两者)`、`pf_pairing_gap_min`、`fio2_source ∈ {measured, ventilator_setting, device_based_estimated, flow_only_estimated}`。断言 `pf_available_time ≤ t_landmark`；`derived.bg.pao2fio2ratio` 仅交叉校验。FiO₂ 主分析仅用明确记录值；流量换算仅敏感性且必须联合设备类型。

### 5.4 SOFA 组分（DAG 节点 F4；三口径 + 缺失规则确定值）

**三套口径**：`sofa_phenotype_locked`（表型，锁定 mimic-code 回顾性口径）、`sofa_realtime_available`（模型输入，仅 `available_time ≤ t_landmark` 输入重建）、`sofa_realtime_completeness`（完整性 QA）。

**逐组分输出（评审 §三.1）**：

```text
component_value / component_observed / component_available
component_window_start / component_window_end
component_missing_reason        -- not_measured / not_yet_available / out_of_range / source_conflict
component_imputation_flag       -- none / carried_forward / cohort_rule
```

**缺失规则（确定值，评审 P0-4）**：

1. **缺失组分不得默认计 0**；
2. `SOFA_total = Σ SOFA_d` 仅在可计算组分数 ≥ 5/6 时计算，否则 `sofa_total_status = incomplete` 且总分为 NULL；同步输出 `sofa_component_count`、`sofa_missing_component_mask`（6 位掩码）、`sofa_total_status`；
3. 各组分最大回溯时间（超过则视为缺失）：胆红素/肌酐/血小板 48h，乳酸 24h，GCS 24h，P/F 24h，MAP/血管活性药实时（用当前窗口值），尿量按 24h 累计窗；
4. `GCS unable` / 镇静 / 插管：按锁定 mimic-code 的 `gcs_unable` 口径处理（eICU 差异预登记）；镇静期间 GCS 不得用插管后镇静评分充填，优先取镇静前最近值（24h 内），否则记缺失；
5. 尿量缺失与真正无尿区分：无任何尿量记录 = 缺失（`not_measured`）；有记录且 24h 累计 <阈值 = 实测低尿量，不得互相充填；
6. 规则版本留痕：`sofa_realtime_rule_version`、`sofa_baseline_definition`、`sofa_window_definition`。

**心血管经典规则（阈值修正 + 最大分值计分）**：

| 分值 | 标准 |
|---|---|
| 0 | MAP ≥ 70 mmHg，且无相关血管活性药 |
| 1 | MAP < 70 mmHg |
| 2 | dopamine ≤ 5 μg/kg/min，或任意剂量 dobutamine |
| 3 | dopamine > 5 且 ≤ 15 μg/kg/min，或 epinephrine ≤ 0.1，或 norepinephrine ≤ 0.1 μg/kg/min |
| 4 | dopamine > 15 μg/kg/min，或 epinephrine > 0.1，或 norepinephrine > 0.1 μg/kg/min |

`SOFA_CV = max(MAP, dopamine, dobutamine, epinephrine, norepinephrine 各准则分值)`。三变量严格分离：`sofa_cv_original`（表型/亚组唯一口径）/ `nee_current`（模型输入、论文 2）/ `vasopressor_burden`（探索）。vasopressin、phenylephrine 不进经典计分；禁止 NEE 生成主分析 SOFA 组分（风险 R15）。

**亚组口径**：CV-SOFA≥3 固定亚组用 `sofa_realtime_available` 心血管组分；实时 SOFA 未通过 QA 时亚组整体标注回顾性口径并列入局限性。**MIMIC `derived.sofa` 总分不得直接作为严格实时模型特征**（其检验输入可能按 charttime）；窗口语义 20–50 stay 人工核对（§7.5）。`sepsis3` 静态组分禁用作 landmark 特征（风险 R11）。

### 5.5 血管活性药与 NEE（DAG 节点 F5）

- **MIMIC**：`vasoactive_agent` → 技术文档 §6.2 公式合成 NEE；双实现核验 `nee_project_formula / nee_mimic_derived / nee_difference / nee_source_drug_components`。体重按优先级且遵守 landmark 截断。
- **eICU**：`infusion_drug` 解析管线（药名正则 → 文本数值化 → 单位换算 → 体重优先级 → NEE 求和）；`pivoted_infusion` 仅存在性交叉校验。
- 输注 episode：短间隙 <30min 合并；重叠记录判重规则沿技术文档 §6.2。
- **论文 2 人工审核 7 环节**：药物归类、单位解析、速率标准化、episode 合并、`t_stop`、`t_0`、48h 复用事件；eICU 标签在 MIMIC 双实现核验通过前暂缓。

### 5.6 机械通气与氧合支持（DAG 节点 F6）

- MIMIC：`derived.ventilation` + `oxygen_delivery` 补充 HFNC。
- eICU：`respiratory_care`、`treatment` 通气路径、`pivoted_o2`。

### 5.7 尿量与液体平衡（DAG 节点 F7）

- MIMIC：`derived.urine_output`（必要时 `outputevents` 补充）。
- eICU：`pivoted_uo`；`intake_output` 计算 24h 平衡。尿量缺失 vs 无尿区分规则见 §5.4-5。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC）

1. **就诊归属（显式 OR）**：

   ```text
   eligible ECG =
       [ admittime ≤ t_ecg ≤ min(t_landmark, dischtime) ]
     ∨ [ auditable_pre_admission_encounter ∧ t_ecg ≤ t_landmark ]
   ```

   四态 `ecg_encounter_status`（`same_hospitalization` / `auditable_pre_admission_encounter` / `uncertain` / `outside_index_encounter`）；主分析纳入前两类，后者打 `pre_admission_ecg = TRUE`。审计四条件（预登记）：ED stay 主键关联、ED 离开至入院间隔 ≤ 阈值、期间无其他 encounter、入院前最大允许时长。
2. **ECG 时间语义字段（评审 §三.5 新增）**：索引同时保留——

   ```text
   ecg_acquisition_time          -- 采集开始时间（配对与防泄漏用）
   ecg_available_time_assumed    -- 假定可用时间（默认 = 采集完成）
   ecg_processing_time           -- 波形预处理完成时间（管线留痕）
   ecg_selection_time            -- 被选为 landmark 输入的时间
   ```

   **声明**：本研究假设 ECG 在采集完成时即可作为模型输入；该假设是**部署假设**，不代表数据库提供了真实临床报告时间或波形处理完成时间。若未来获得真实报告时间，须作为 `available_time` 口径的敏感性分析。
3. **五层级 availability**：`ecg_found_raw → ecg_same_encounter → ecg_structurally_valid → ecg_pass_frozen_qc → ecg_selected_for_model`。
4. **两层 QC**：固定结构性 QC（全集统一）；数据驱动 QC（阈值仅训练集确定并冻结）。
5. **时效与选片**：`∃ eligible ecg ≤ t_landmark 且间隔 ≤ 24h`（主分析），48h/72h 敏感性；多份取最近一份通过 QC 者。**主配对队列定义在 QC 后、查看测试集结果前冻结**。
6. 患者级 ECG 描述队列：`t_sepsis ± 24h` ≥1 份（仅描述）。
7. 波形定位与预处理按技术文档 §20；`ecg_measurements` 作试金石与 QC 辅助；试点表 `ecg_waveform_features` 不进管线。

---

## 6. SC-common 跨库变量分层映射总表（含变量级等价性合同）

按跨库同构程度分四层。**新增前置要求（评审 §三.7）**：core/extended 锁定前，A/B 层每个变量必须先完成变量级**等价性合同**，逐行填写：

```text
sc_common_variable_contract_v2
- concept_name            -- 如 MAP
- source_table / source_column
- unit / conversion_rule
- priority_rule           -- 有创/无创、多来源优先级
- event_time_rule / available_time_rule
- missing_rule
- physiologic_range
- cross_database_equivalence_grade   -- A / B / C（对应下述三层）
```

**已知须逐条核验的实现差异**：MAP（有创/无创/周期监护/非周期记录优先级两库不同）；体温（eICU 华氏/摄氏来源混合）；SpO₂（eICU `sao2` 可能是动脉血气 SaO₂ 而非脉搏血氧，须区分）；乳酸（不同表型项目与结果时间语义）；血小板（单位与异常值处理）；WBC（白细胞计数与分类计数表间映射）。**合同完成并逐变量评级前，不得锁定 `SC-common-core` 为主外验输入。**

### A 层：高同构变量 → `SC-common-core`（主外验模型候选）

| 临床概念 | MIMIC 来源 | eICU 来源 | 单位 | 泄漏风险 |
|---|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` | 岁 | 低 |
| 性别 | `patients.gender` | `patient.gender` | — | 低 |
| HR | §5.2 分层来源 | `pivoted_vital`/`vital_periodic` | bpm | 低 |
| MAP（有创/无创） | §5.2 分层来源 | `ibp_mean`/`nibp_mean`/`systemicmean` | mmHg | 低 |
| RR | 同上 | `RespiratoryRate`/`respiration` | /min | 低 |
| SpO2 | 同上 | `spo2`/`sao2`（须区分脉搏/动脉血氧，见合同） | % | 低 |
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
| Charlson | eICU `past_history` 与既往住院 ICD 不同构 | `charlson_prior` 作 SC-MIMIC 特征 |
| ICU 类型 | 科室命名体系不同 | C 层；仅描述 |
| 入院来源 | 类别体系不同 | C 层；仅描述 |
| SOFA 总分及部分组分 | 输入完备性差异 | 用于表型判定与亚组分层，不作 SC-common-core/extended 输入特征 |

### 固定定义（命名统一，评审 §四.3）

```text
SC-common-core     = A 层
SC-common-extended = A + B 层
SC-MIMIC           = 全量 MIMIC 特征（内部探索，不纳入唯一主要比较）
```

> 与技术文档模型名的对应：技术文档 `SC-common-all`（全体 landmarks 训练的 common 变量模型）在本方案中以「`SC-common-core`（或锁定后的 extended）× 全体 landmarks」实现；eICU 外验与该部署分支均指此模型，不再使用 `SC-common-all` 一词。

首版外验模型默认 `SC-common-core`；core/extended 升级依据阶段 C2 合同与阶段 D 同构核验，建模前锁定，禁止按模型效果反向调整。感染源不进主模型（稀疏且滞后，泄漏风险高）。

---

## 7. 防泄漏与质量控制

### 7.1 防泄漏断言（管线自动测试 Q1）

1. `ecg_time ≤ t_landmark` 且满足 §5.8 显式 OR 归属；
2. **全部特征 `available_time ≤ t_landmark`（主断言）**；聚合记录 `max_available_time ≤ t_landmark`；P/F `pf_available_time ≤ t_landmark`；
3. 结局窗起点 > t_landmark；
4. **同一患者不跨 train/validation/test**（评审 §四.2：与 §2.4 划分一致；当前无独立 calibration 集——若未来启用，须先在 §2.4 定义，且同一患者 landmark 不得跨 calibration/test）；
5. 标准化/异常值阈值/插补器仅训练集拟合；
6. 特征筛选仅训练集；
7. ECG 数据驱动 QC 阈值仅训练集确定。

附加断言：landmark 单调递增且间隔 6h；`k0 ≥ 0`；结局三态分层抽查（§7.4）；eICU offset 换算零误差；§3.4 全部边界单元测试；`ecg_encounter_status` 四态校验；`sofa_cv_original` 与 `nee_current` 来源列不同；**episode 主键唯一性**（每 stay 一个 episode、`mimic_episode_sepsis` 每 episode 一行）；`acute_transfer_time` XOR `alive_discharge_time`；eICU 标签仅用 `*_episode_min`（静态检查）；**eICU 事件桥接表按源主键连接、无 offset 重复匹配**（连接前后行数守恒校验）；**`episode_merge_decision` 与 `transfer_evidence` 一致性**（via_ward/via_ed 不得 merged）；**SOFA 缺失组分未计 0**（`component_imputation_flag` 与总分 NULL 规则校验）。

### 7.2 时间逻辑 QA

- `admittime ≤ icu_intime < icu_outtime ≤ dischtime` 成立比例；
- `t_sepsis` 相对 ICU 入科分布；`k0` 分布；t=0 不在 ICU 比例；
- 各 k 风险集人数；landmark 后仍在 episode 内验证；
- 每变量 `event_time` 与 `available_time` 差异分布；**strict/chart 双轨差异报告**（§5.0）；
- eICU 时间映射连续性与间隙分布；MIMIC `gap_minutes` 分布、`transfer_evidence` 构成、`needs_review` 比例、`overlap_flag` 命中数；
- MIMIC 生命体征三层占比与录入延迟分布（含双轨差异）。

### 7.3 队列表型 QA

- **MIMIC 随机抽查**：suspected infection 配对、SOFA ≥2、index episode、episode 合并（含 transfers 审计一致性、病房/ED 区间排除）、`t_sepsis`；
- **eICU 分层抽查**：抗生素识别、培养识别、配对方向、跨 unit stay 配对命中、SOFA 六组分、sepsis time、多 stay 时间映射、`antibiotic_time_source` 四级构成。

### 7.4 结局 QA（分层抽查）

分层：24h 死亡阳性、明确阴性、存活出院、急性转出、eICU 状态缺失、ICU 转出后院内死亡、`t+24h` 边界、`death_time_missing`、`status_conflict`、**adjudication 覆盖样本复核**。

### 7.5 派生表来源验证（D0 前置）

SQL/R checksum、mimic-code commit、DuckDB 版本、生成日期、源表版本、回溯验证、行数与主键唯一性、与官方参考分布比较（含 SOFA 窗口语义 20–50 stay 人工核对）。

### 7.6 ECG 配对 QA

同一住院内 ECG、入院前 ECG（审计四条件）、出院后 ECG、多份取最近、landmark 等于 ECG 时间、路径与 study_id 一致、header 导联与单位。

### 7.7 专项与常规 QA 输出

- `qa/eicu_lab_time_semantics_qa.md`（阶段 A）；`qa/sofa_realtime_completeness_v2.md`；
- `qa/sc_common_contract_v2.md`：等价性合同逐变量评级与差异（阶段 C2）；
- `qa/vitals_dual_track_v2.md`：strict/retro 双轨差异；
- 队列流程图（两库分别，eICU 三套表型分列）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；原始基线：MIMIC sepsis3 41,295 stays / 31,910 subjects，ECG 覆盖 161,352 subjects，eICU 200,859 stays / 院内死亡 18,004）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集）；eICU Go/No-Go 检查表。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v2.parquet, cohort_eicu_v2.parquet
  episodes/  mimic_icu_episode_map.parquet                     # 含全部审计字段（C0）
             mimic_episode_sepsis.parquet                      # episode 级聚合（C1）
             eicu_unitstay_timeline.parquet                    # C6a
             eicu_event_time_map.parquet                       # 含 event_type/source_table/source_row_id
             eicu_medication_time_map.parquet                  # 专用桥接（源主键对应）
             eicu_microbiology_time_map.parquet
             eicu_lab_time_map.parquet
  phenotypes/ eicu_phenotype_tracks_v2.parquet
             eicu_phenotype_event_v2.parquet                   # 表型时间合同（第一层字段冻结）
  splits/    split_assignments_v2.parquet
  landmarks/ landmarks_v2.parquet
  labels/    labels_24h_v2.parquet           # 状态机六字段（§4.1）
             label_adjudications.parquet     # 人工复核覆盖表（与自动结果分离）
             labels_competing_7d_v2.parquet  # event_type 0/1/2/3（eICU 用 *_episode_min）
  features/  baseline_static_v2.parquet      # 含 charlson_prior 三报告字段
             landmark_context_v2.parquet     # 最近可用体重/身高、Δ_ICU-sepsis、支持状态
             vitals_hourly_v2.parquet        # bin 聚合字段 + source_table + source_time_type
             vitals_realtime_strict_v2.parquet / vitals_charttime_retro_v2.parquet  # 双轨
             labs_hourly_v2.parquet          # P/F 双时间字段 + fio2_source
             sofa_hourly_v2.parquet          # 三口径 + 逐组分 7 字段 + 缺失掩码 + 规则版本
             nee_stream_v2.parquet           # 双实现核验四字段
  contracts/ sc_common_variable_contract_v2.parquet            # 变量级等价性合同（§6）
  ecg_index/ ecg_landmark_index_v2.parquet   # study_id, ecg_acquisition_time,
                                             # ecg_available_time_assumed, ecg_processing_time,
                                             # ecg_selection_time, path, 时效,
                                             # ecg_encounter_status, pre_admission_ecg, 五层级标志
  qa/        cohort_flow_v2.md, feasibility_table_v2.md, leakage_report_v2.md,
             time_logic_qa_v2.md, phenotype_qa_v2.md, outcome_stratified_qa_v2.md,
             ecg_pairing_qa_v2.md, derived_provenance_v2.md, eicu_go_nogo_v2.md,
             eicu_lab_time_semantics_qa.md, sofa_realtime_completeness_v2.md,
             sc_common_contract_v2.md, vitals_dual_track_v2.md
  _meta/     code_version.json
             d0_decision.json                # §3.1 固定 schema
             freeze_checklist.json           # §10 各项关闭状态
```

规范：①统一 Parquet；②三级键 `subject_key / episode_key / landmark_k`，原始 stay 标识保留溯源；③患者级 ID 与划分表冻结后不得重算；④每 DAG 节点独立脚本、I/O schema 校验、中间产物持久化；⑤时间字段命名执行 §2.3 规范；⑥D0 与冻结清单状态落 `_meta/`。

---

## 9. 已识别风险与待决事项（R1–R26）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| R1 | 本地 `sepsis3` 无 `sepsis_time` | 主时间原点 | D0 决策门（§3.1，固定 JSON 输出）；冻结清单 A-1 |
| R2 | `2020 - 2022` v1.9 未规定 | 时间划分 | 主分析不用；阶段 A amendment（§2.4）；A-3 |
| R3 | eICU 无 Sepsis-3 派生表、培养覆盖极低 | 外验表型 | C7 双层合同 + 三套队列 + Go/No-Go；默认 Robustness under phenotype shift |
| R4 | eICU SOFA 自建、GCS 镇静口径差异 | SOFA 可比性 | F4 口径对齐 + 缺失规则；差异预登记 |
| R5 | eICU 输注速率文本内嵌单位 | NEE/论文 2 | F5 解析管线；7 环节人工审核 |
| R6 | SOFA-1 与 SOFA-2 并存 | 误用 | 仅用 SOFA-1；Q1 命名检查 |
| R7 | 遗留/试点表 | 误用 | 白名单制 |
| R8 | eICU 无 ECG，availability 与库来源共线 | 门控外推 | eICU 仅走 **SC-common-core（或锁定后的 extended）× 全体 landmarks** 独立路径（对应技术文档 SC-common-all，§6 命名统一） |
| R9 | 急性转出类别的两库字符串不一致 | unknown 标记 | QA 实测清单预登记；D-3 |
| R10 | 体重缺失/极端值 | NEE/论文 2 | 技术文档 §6.2 规则；landmark 截断 |
| R11 | `sepsis3` 静态组分误用作 landmark 特征 | 泄漏 | 禁用；landmark SOFA 取实时口径 |
| R12 | 检验 charttime 早于结果可用 | 实时泄漏 | 原始重建 + 双轨报告；不可用时声明 retrospective chart-time prediction |
| R13 | ECG 跨住院配对 | 配对正确性 | 显式 OR 归属四态；`uncertain` 仅敏感性 |
| R14 | eICU 多 stay offset 坐标不一致 | 时间正确性 | C6a 统一坐标；标签仅用 `*_episode_min` |
| R15 | NEE 替代经典 SOFA 心血管 | 可比性 | 修正阈值 + 最大分值；三变量分离；Q1 检查 |
| R16 | Charlson 派生表含本次住院 ICD | 泄漏 | `charlson_prior` 固定窗口（§5.1）；移出 SC-common |
| R17 | 未知结局误编码阴性 | 标签正确性 | 状态机 + `outcome_ascertainable` + adjudication 分离 |
| R18 | eICU 培养覆盖低的表型选择 | 外验有效性 | 三套队列 + Go/No-Go；命名建模前锁定 |
| R19 | eICU 表型规则未锁定 | 外验时间原点 | C7 确定值规则表；A-5 |
| R20 | eICU lab offset 语义未审计 | eICU 检验泄漏 | 专项报告；候选 max 公式验证后锁定；C-2 |
| R21 | available-time 落实不完整 | 防泄漏 | §5.2/§5.3/§5.4 分层重建 + 双轨；C-3/4/5 |
| R22 | Go/No-Go 数值未 PI 确认 | 可行性决策 | §2.2 C7 确定建议值；A-6 |
| **R23** | **episode 合并把病房/ED 区间误判为连续 ICU；重叠记录被 MAX 掩盖** | 队列时间轴 | C0 三条独立规则 + `episode_merge_*` 字段 + `overlap_flag`；B-7 |
| **R24** | **MIMIC 生命体征自动/人工来源无法稳定识别** | 实时口径可信度 | 多信号分层审计；双轨 `vitals_realtime_strict / vitals_charttime_retro`（§5.2） |
| **R25** | **标签冲突在 SQL 中被隐式处理，破坏可复现性** | 标签完整性 | 冲突先 unknown；`label_adjudications` 独立覆盖（§4.1）；D-6 |
| **R26** | **eICU 事件按 offset 反连产生重复匹配** | 配对/特征正确性 | 事件映射含 `event_type/source_table/source_row_id` + 专用桥接表（C6a/A.5）；Q1 行数守恒校验 |

---

## 10. 冻结清单（Freeze Checklist，共 31 项：A6 + B7 + C7 + D6 + E5）

正式冻结前全部关闭；状态实时记录于 `_meta/freeze_checklist.json`。

### A. 协议冻结（6 项）

- [ ] A-1 D0 出口 A/B 已确定；
- [ ] A-2 `_meta/d0_decision.json` 已按 §3.1 固定 schema 生成；
- [ ] A-3 `2020–2022` amendment 已签署；
- [ ] A-4 mimic-code commit 与本地修改已锁定（含 SQL/R checksum）；
- [ ] A-5 eICU 三套表型规则表已按 §2.2 C7 确定值由 PI 逐项签字确认；
- [ ] A-6 Go/No-Go 数值已预登记（PI 确认，未据模型效果调整）。

### B. 时间轴冻结（7 项）

- [ ] B-1 MIMIC episode 以 `icustays` 为锚构建；
- [ ] B-2 episode 映射一对多关系符合预期（`episode_mapping_status` 审计通过）；
- [ ] B-3 每个 stay 仅属于一个 episode（主键唯一性测试通过）；
- [ ] B-4 eICU 所有事件均转换到 hospital/episode 坐标（含源主键桥接表）；
- [ ] B-5 跨 unit stay 的抗生素—培养配对测试通过；
- [ ] B-6 标签只使用统一坐标（eICU 仅 `*_episode_min`）；
- [ ] B-7 episode 合并三规则（τ_merge、重叠处理、病房/ED 排除）已锁定且 `episode_merge_*` 字段落地。

### C. 防泄漏冻结（7 项）

- [ ] C-1 MIMIC 关键检验使用 `storetime`（`labevents` 重建完成）；
- [ ] C-2 eICU lab revised time 语义已验证（专项报告关闭）；
- [ ] C-3 MIMIC 人工记录生命体征录入延迟已处理（itemid 分层 + 双轨落地）；
- [ ] C-4 P/F 使用两部分中较晚 available time（`pf_available_time` 断言通过）；
- [ ] C-5 动态 SOFA available-time 口径已确定（`sofa_realtime_available` 或降级声明）；
- [ ] C-6 聚合记录 `max_available_time` 已定义并接入 Q1；
- [ ] C-7 实时 SOFA 缺失组分规则已锁定（缺失不计 0、≥5/6 最少组分、逐组分回溯上限）。

### D. 标签冻结（6 项）

- [ ] D-1 `outcome_ascertainable` 与 `full_inhospital_followup_24h` 已拆分；
- [ ] D-2 死亡状态冲突规则已固定（`death_time_missing` / `status_conflict` 先 unknown）；
- [ ] D-3 急性转出清单已冻结（两库分别，XOR 互斥验证通过）；
- [ ] D-4 eICU 出院 offset 已转换到 episode 坐标；
- [ ] D-5 全部边界单元测试通过（§3.4，含 `invalid_input` 防护）；
- [ ] D-6 `label_adjudications` 表与 preliminary/final 分离机制已建立。

### E. ECG 冻结（5 项）

- [ ] E-1 pre-admission ECG 的 OR 条件已修正；
- [ ] E-2 ED-to-admission 审计规则已固定（四条件参数预登记）；
- [ ] E-3 结构性 QC 已固定；
- [ ] E-4 数据驱动 QC 只在训练集拟合；
- [ ] E-5 24h 主配对队列定义已冻结（查看测试集结果前）。

---

## 11. 实施顺序（阶段 A → B → C1 → C2 → D，评审 §八调整）

**阶段 A：协议与来源锁定（结束前不查看验证/测试集性能差异）**

1. mimic-code commit、派生 SQL/R 与 checksum 核对（§7.5）；
2. D0 审计与 PI 锁定（§3.1）；3. `2020 - 2022` amendment（§2.4）；
4. episode 定义与合并三规则锁定（C0/C6a）；eICU 事件桥接表建立；
5. 数据可用时间语义（§5.0）；eICU lab 专项报告（§5.3）；MIMIC 生命体征 itemid 多信号分层审计（§5.2）；
6. 经典 SOFA 与 NEE 独立定义（§5.4/§5.5）；实时 SOFA 缺失规则锁定；**eICU 表型规则表 PI 逐项签字**（§2.2 C7）；Go/No-Go 数值预登记；
7. 关闭冻结清单 A 组、B-7、C-2/C-3/C-7。

**阶段 B：仅做 MIMIC 可行性队列（D0 候选口径可并行，不冻结）**

episode 映射（C0 三规则 + 审计）→ episode 级 sepsis 聚合与 index episode（C1–C3）→ landmark（L1）→ 三态标签状态机（L3）→ ECG 归属与五层级 availability（F8）→ 主要 12 个 landmark 患者数/阳性数/ECG 覆盖率（技术文档 §9.1 Go 核对）。

**阶段 C1：MIMIC 特征工程**

available-time 特征（F1–F7：检验重建、P/F 双时间、itemid 分层双轨）；`charlson_prior`；ECG 两层 QC 冻结；NEE 双实现核验；实时 SOFA 重建与完整性评估；标签 adjudication 机制运行；论文 2 人工标签验证（7 环节，PPV >80% 为 Go）。

**阶段 C2：SC-common 跨库合同（先于正式 MIMIC 模型训练，评审 §八）**

变量级单位映射、异常值范围、缺失定义、聚合规则、available-time、MIMIC/eICU 交叉库等价性评级——完成 `sc_common_variable_contract_v2` 与 `qa/sc_common_contract_v2.md`。**未完成 C2 不得开始正式 MIMIC 模型训练**（防止按模型效果选择跨库变量）。

**阶段 D：eICU 表型与可行性**

统一时间坐标 → 按锁定规则构建三套 phenotype → 经典 SOFA 复现 → 医院覆盖与 Go/No-Go 逐项核对 → `SC-common-core`（或 extended）终稿锁定 → 外验命名与层级确定。

**当前可进行**：来源审计；D0 双口径比较；episode 原型；MIMIC 队列规模估算；三态标签原型；ECG 覆盖与归属统计；eICU 时间轴与表型可行性统计；SC-common 覆盖率与合同草拟。

**当前不应进行（冻结清单关闭前禁止）**：正式训练最终模型；选择超参数；查看测试集性能；依据 eICU AUROC 选择表型；依据模型效果决定 core/extended；正式跨库性能结论；把 eICU 称为完全同构 Sepsis-3 外部验证。

---

## 12. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：两库实测核验的可实施提取方案（DAG、输出规范、R1–R11、SQL 模板） |
| v2.0 | 2026-07-30 | 第一轮评审修订：D0 决策门；eICU 方向性配对与三套表型；episode 桥接表；available-time 契约；经典 SOFA CV；ECG 归属；三态标签；SC-common 分层；R12–R18；阶段 A–D |
| v2.1 | 2026-07-30 | 第二轮评审修订：表型时间合同；icustays 锚定 episode；episode 级 sepsis 聚合；available-time 贯穿（vitals/PF/SOFA）；eICU lab 审计；SOFA 阈值修正；标签可判定性拆分；ECG OR 条件；静态表拆分；聚合时间字段；Go/No-Go 数值；冻结清单 28 项；R19–R22 |
| v2.2 | 2026-07-30 | **第三轮评审修订**——①episode 合并三条独立规则（τ_merge=0 确定值、重叠不自动合并、病房/ED 排除）+ `episode_merge_*` 审计字段；②eICU 表型双层结构，规则表全部确定值，P-explicit 定位显式临床诊断表型；③P-clinical 前向算法消除循环定义；④A.1 回填 `hadm_id` 并以 `ROW_NUMBER NULLS LAST` 替代 `ARRAY_AGG`；⑤A.3 完整标签状态机 + `invalid_input` 防护 + `label_adjudications` 分离机制；⑥实时 SOFA 逐组分 7 字段与缺失规则（缺失不计 0、≥5/6 最少组分、回溯上限、GCS/镇静/尿量规则）；⑦MIMIC 生命体征多信号分层 + 双轨输出；⑧`infusion_recorded` 降级与三率分列；⑨事件映射源主键 + 三张专用桥接表，A.5 源主键回连；⑩ECG 四时间字段与部署假设声明；⑪`charlson_prior` 固定窗口与三报告字段；⑫变量级等价性合同 `sc_common_variable_contract_v2`；⑬内部不一致修正（冻结清单 31 项计数、train/validation/test、SC-common 命名统一、时间字段命名规范）；⑭A.0 审计字段显式化、A.2 弃用 TIMESTAMP 哨兵、A.3 冲突优先 unknown；⑮D0 固定 JSON schema、available-time 双轨报告；⑯阶段 C 拆分为 C1/C2，跨库合同先于正式训练；⑰风险 R23–R26。 |

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

> **说明**：附录均为**概念性模板**，用于固定逻辑与边界语义，不构成完整实现；正式实施以各 DAG 节点脚本及 I/O schema 校验为准。附录模板配套 SQL 单元测试（含 `NULLS LAST` 排序、空集、同值并列、类型推断用例）。

### A.0 MIMIC 连续 ICU episode 映射（C0；icustays 锚定 + 三规则 + 审计字段）

```sql
WITH s AS (
  SELECT subject_id, hadm_id, stay_id, intime, outtime,
         LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id) AS prev_outtime
  FROM main.icustays
),
g AS (
  SELECT *,
         EPOCH(intime - prev_outtime) / 60.0 AS gap_minutes
  FROM s
),
ev AS (   -- 路径审计：间隙期间的 transfers 证据（来源键：hadm_id + 间隙区间）
  SELECT g.*,
         (SELECT t.careunit FROM main.transfers t
          WHERE t.hadm_id = g.hadm_id
            AND t.intime >= g.prev_outtime AND t.outtime <= g.intime
          ORDER BY t.intime LIMIT 1)                        AS intervening_careunit,
         CASE
           WHEN g.prev_outtime IS NULL THEN 'none'
           WHEN g.gap_minutes < 0 THEN 'overlap_or_anomaly'
           WHEN EXISTS (SELECT 1 FROM main.transfers t
                        WHERE t.hadm_id = g.hadm_id
                          AND t.intime >= g.prev_outtime AND t.outtime <= g.intime
                          AND t.careunit IN (/* 普通病房清单 */)) THEN 'via_ward'
           WHEN EXISTS (SELECT 1 FROM main.transfers t
                        WHERE t.hadm_id = g.hadm_id
                          AND t.intime >= g.prev_outtime AND t.outtime <= g.intime
                          AND t.careunit IN (/* ED 清单 */)) THEN 'via_ed'
           WHEN g.gap_minutes = 0 THEN 'direct_icu_to_icu'
           ELSE 'brief_icu_exit'
         END                                                 AS transfer_evidence
  FROM g
),
d AS (   -- 三条独立规则 → 合并裁决
  SELECT *,
         CASE WHEN prev_outtime IS NULL THEN FALSE
              WHEN gap_minutes < 0 THEN FALSE                              -- 规则②：重叠不自动合并
              WHEN transfer_evidence IN ('via_ward','via_ed') THEN FALSE   -- 规则③：病房/ED 排除
              WHEN gap_minutes <= 0 THEN TRUE                              -- 规则①：τ_merge = 0 min
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
         CASE WHEN gap_minutes < 0 THEN TRUE ELSE FALSE END AS overlap_flag
  FROM ev
),
e AS (
  SELECT *,
         SUM(CASE WHEN episode_merge_decision IN ('split') OR prev_outtime IS NULL
                  THEN 1 ELSE 0 END)
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
       gap_minutes, transfer_evidence, intervening_careunit,
       episode_merge_eligible, episode_merge_decision, episode_merge_exclusion_reason,
       overlap_flag,
       CASE WHEN gap_minutes < 0 THEN 'needs_review' ELSE 'clean' END AS episode_mapping_status,
       0 AS episode_merge_threshold_min          -- 实际使用 τ_merge 留痕
FROM e;
-- pending_review 记录经 QA 复核后更新 episode_merge_decision 并重跑映射（版本记 episode_mapping_version）
```

### A.1 MIMIC 队列骨架（C1–C3；回填 hadm_id + 窗口函数取源 stay）

```sql
WITH sepsis AS (
  SELECT s.subject_id, i.hadm_id, s.stay_id,          -- 本地 sepsis3 无 hadm_id：经 icustays 回填
         s.suspected_infection_time AS t_sepsis       -- D0 锁定后替换
  FROM mimiciv_derived.sepsis3 s
  JOIN main.icustays i USING (stay_id)
  WHERE s.sepsis3
),
ep_ranked AS (   -- 同一 episode 内多个 sepsis3 stay：透明窗口排序取代表
  SELECT e.episode_id, s.stay_id, s.t_sepsis,
         COUNT(*) OVER (PARTITION BY e.episode_id) AS qualifying_sepsis_count,
         ROW_NUMBER() OVER (
           PARTITION BY e.episode_id
           ORDER BY s.t_sepsis NULLS LAST, s.stay_id
         ) AS rn
  FROM sepsis s
  JOIN mimic_icu_episode_map e USING (subject_id, hadm_id, stay_id)
),
ep_sepsis AS (   -- mimic_episode_sepsis：每 episode 恰好一行
  SELECT episode_id, qualifying_sepsis_count,
         t_sepsis AS t_sepsis_ts,
         stay_id AS t_sepsis_source_stay_id,
         'min_t_sepsis_within_episode' AS t_sepsis_selection_rule
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
    -- 成人 ICU 类别清单（QA 实测后预登记）
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY subject_id
           ORDER BY t_sepsis_ts, admittime, episode_intime_ts, episode_id
         ) AS rn
  FROM eligible
)
SELECT * FROM ranked WHERE rn = 1;   -- 首次合格 sepsis-associated episode
```

### A.2 Landmark 网格与风险集（MIMIC；显式 NULL 逻辑，无哨兵时间）

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
-- 时间倒置（outtime < intime）、出院后死亡异常 → 打标进 QA（§7.2）
```

### A.3 三态 24h 标签状态机（MIMIC；六字段 + 冲突先 unknown + invalid_input 防护）

```sql
WITH disp AS (   -- 派生处置：dischtime + discharge_location 分类，XOR 互斥
  SELECT hadm_id,
         CASE WHEN discharge_location IN (/* 急性转出清单 */) THEN dischtime END AS acute_transfer_time,
         CASE WHEN discharge_location IN (/* 存活出院清单 */) THEN dischtime END AS alive_discharge_time
  FROM main.admissions
),
base AS (
  SELECT l.episode_id, l.k, l.t_landmark_ts,
         l.t_landmark_ts + INTERVAL '24 hours' AS w_end,
         a.hospital_expire_flag, a.deathtime, a.dischtime,
         d.acute_transfer_time, d.alive_discharge_time
  FROM landmarks_v2 l
  JOIN cohort_mimic_v2 c USING (episode_id)
  JOIN main.admissions a USING (hadm_id)
  LEFT JOIN disp d       USING (hadm_id)
),
state AS (
  SELECT *,
    CASE
      WHEN deathtime IS NOT NULL AND deathtime <= t_landmark_ts
        THEN 'invalid_input'                          -- 风险集外输入防护
      WHEN deathtime IS NOT NULL AND hospital_expire_flag = 0
        THEN 'status_conflict'                        -- 先 unknown，待 adjudication
      WHEN hospital_expire_flag = 1 AND deathtime IS NULL
        THEN 'death_time_missing'
      WHEN deathtime > t_landmark_ts AND deathtime <= w_end
        THEN 'event'
      WHEN acute_transfer_time > t_landmark_ts AND acute_transfer_time <= w_end
        THEN 'acute_transfer'
      WHEN dischtime IS NOT NULL AND dischtime >= w_end
        THEN 'non_event_observed'                     -- 完整覆盖窗口
      WHEN dischtime IS NULL                          -- 仍在院（概念性）
        THEN 'non_event_observed'
      WHEN alive_discharge_time > t_landmark_ts AND alive_discharge_time <= w_end
        THEN 'non_event_alive_discharge'
      ELSE 'unascertainable'
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
  label_state                                                     AS label_reason
FROM state;
-- status_conflict / death_time_missing / unascertainable：QA 复核写入 label_adjudications
-- （label_preliminary_status / label_final_status / label_adjudication_status /
--   label_adjudication_source），不改写本自动提取结果
```

### A.4 eICU 住院级时间坐标换算（C6a；含事件源主键）

```sql
SELECT patientunitstayid, patienthealthsystemstayid, uniquepid,
       -hospitaladmitoffset                       AS unit_start_hospital_min,
       -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min
FROM main.patient;
-- 事件映射（每事件类一张专用桥接表，示例 medication）：
--   eicu_medication_time_map(source_row_id, patientunitstayid, local_offset_min,
--                            hospital_offset_min, episode_offset_min)
--   hospital_offset_min = -hospitaladmitoffset + local_offset_min
--   episode_offset_min  = hospital_offset_min - episode_start_hospital_min
-- 结局同步转换：hospital_discharge_episode_min / death_episode_min（§4.1）
```

### A.5 eICU 方向性 suspected infection 配对（C6b；源主键回连）

```sql
WITH ab AS (
  SELECT mtm.episode_id, mtm.episode_offset_min AS ab_time,
         mtm.source_row_id, mtm.patientunitstayid AS source_stay
  FROM eicu_medication_time_map mtm            -- 源主键对应，非 offset 精确匹配
  JOIN main.medication m
    ON m.patientunitstayid = mtm.patientunitstayid
   AND m.drugorderoffset IS NOT NULL           -- 实际模板以 medication 行稳定标识 = source_row_id 回连
  WHERE m.drugname ILIKE ANY (SELECT pattern FROM preregistered_antibiotics)
),
cx AS (
  SELECT xtm.episode_id, xtm.episode_offset_min AS cx_time,
         xtm.source_row_id, xtm.patientunitstayid AS source_stay
  FROM eicu_microbiology_time_map xtm
  JOIN main.micro_lab ml
    ON ml.patientunitstayid = xtm.patientunitstayid
   AND ml.culturetakenoffset IS NOT NULL       -- 同上：以 micro_lab 行稳定标识回连
)
SELECT ab.episode_id, ab.ab_time, cx.cx_time,
       ab.source_stay AS ab_stay, cx.source_stay AS cx_stay,
       ab.source_row_id AS ab_row, cx.source_row_id AS cx_row,
       -- infection_pair_id 由 ab_row + cx_row 生成（全局唯一）
       CONCAT(ab.source_row_id::VARCHAR, '__', cx.source_row_id::VARCHAR) AS infection_pair_id
FROM ab
JOIN cx USING (episode_id)                     -- 允许跨 unit stay（同一 episode 内）
WHERE (ab.ab_time - cx.cx_time) BETWEEN 0 AND 4320   -- 培养先：72h 内首剂抗生素
   OR (cx.cx_time - ab.ab_time) BETWEEN 0 AND 1440   -- 抗生素先：24h 内培养
-- Q1 校验：连接前后各源表行数守恒（无 offset 重复放大）
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

*本方案 v2.2 基于 2026-07-30 对两库的只读结构核验与三轮外部评审《总体评价》生成；与技术文档 v1.9 冲突之处以技术文档为准，需变更技术文档的事项（D0 出口 B、`2020-2022` 处理）须经 protocol amendment 正式登记。§10 冻结清单（31 项）全部关闭且五项冻结验证通过前，本方案不得作为正式主分析提取管线使用。*
