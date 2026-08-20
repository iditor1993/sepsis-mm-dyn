# SEPSIS-MM-DYN 数据提取方案 v2.0

- 文档版本：v2.0
- 创建日期：2026-07-30（v1.0 创建于 2026-07-30，同日经外部评审后修订为 v2.0）
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 修订依据：《总体评价》（2026-07-30 外部评审，对 v1.0 结论为「有条件通过，不建议直接按 v1.0 启动正式主分析」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**主分析实施候选版（附冻结前置条件）**。v1.0 的两库 DuckDB 实测结构核验结论（2026-07-30，只读）仍然有效并全部保留；本版落实评审要求的全部 P0 修改。**在阶段 A「协议与来源锁定」（§10）完成并经 PI 确认前，本方案仅允许用于结构审计、可行性统计与原型提取，禁止正式模型训练与测试集评估。**

---

## 0. v1.0 → v2.0 修订总览

本节按评审《总体评价》的问题编号逐项登记修改落点，便于审计对照。完整变更记录见 §11。

### 0.1 P0 级修改（8 项，评审定为「正式启动主分析前必须解决」）

| 评审编号 | 问题 | v2.0 落点 |
|---|---|---|
| P0-1 | 本地 `sepsis3` 表无 `sepsis_time`，v1.0 将 `suspected_infection_time` 静默替代为主分析时间原点，改变了主分析 estimand | §3.1 设立**决策门 D0**：时间原点**未锁定**；给出 A（重新生成符合预注册的 `sepsis_time`）/ B（protocol amendment 正式改主原点）两个合法出口；锁定前仅允许可行性提取。删除 v1.0 将其登记为「附件 A 方言迁移修改」的处理 |
| P0-2 | eICU suspected infection 配对写成对称的「±24h/±72h」，方向与经典规则不一致；医嘱时间与给药时间未区分 | §2.2 C6 改为**方向性不等式**（培养先 → 抗生素 [0, 72h]；抗生素先 → 培养 [0, 24h]，窗口以锁定版 mimic-code 为准）；给药时间字段按优先级取值，只能用医嘱时间时显式标注 phenotype shift |
| P0-3 | eICU `micro_lab` 仅 16,996 行 / 2,923 患者，严格抗生素-培养配对的外验队列可能极小且高度选择 | §2.2 新增**三套可行性表型队列**（P-strict / P-clinical / P-explicit）与 **Go/No-Go 门槛**；外验命名默认降为 **Robustness under phenotype shift**，建模前锁定，不得按模型效果事后修改 |
| P0-4 | eICU 多 unit stay 的 offset 以各自入科为零点，连续 episode 无法直接拼接 | §2.2 新增住院级统一时间坐标与两张桥接表 `eicu_unitstay_timeline` / `eicu_event_time_map`；所有事件先换算到住院/episode 时间轴再合并 |
| P0-5 | MIMIC「连续 episode 合并」与「所有特征按 stay_id 连接」自相矛盾 | §2.1 新增 `mimic_icu_episode_map`（stay_id → episode_id）；下游特征、landmark 终止、风险集一律按 episode 聚合；episode 结束时间取代 index stay outtime |
| P0-6 | 主分析默认 `charttime ≤ t_lm`，检验等数据的采样时间早于结果可见时间，存在实时信息泄漏 | §5.0 新增**数据可用时间契约**：每条记录携带 `event_time / available_time / source_time_type`；主分析断言升级为 `available_time ≤ t_lm`；关键检验从原始 `labevents` 重建以保留 `storetime` |
| P0-7 | eICU 心血管 SOFA 按「MAP + NEE 剂量分层」生成，与经典 SOFA 和 MIMIC `derived.sofa` 不同构 | §5.4 改为**严格经典 SOFA 心血管阈值**（MAP / dopamine / dobutamine / epinephrine / norepinephrine）；`sofa_cv_original`、`nee_current`、`vasopressor_burden` 三者严格分离，NEE 不得生成主分析 SOFA 组分 |
| P0-8 | ECG 仅按 `subject_id + 24h 时间窗`配对，可能跨住院配对 | §5.8 新增就诊归属条件（`admittime ≤ t_ecg ≤ min(t_lm, dischtime)`）与四态 `ecg_encounter_status`；主分析仅纳入 `same_hospitalization` 与 `auditable_pre_admission_encounter` |

### 0.2 标签与风险集修改（评审 §三）

| 修改 | v2.0 落点 |
|---|---|
| 结局标签由 `CASE … ELSE 0` 改为三态（event / non_event / unknown），`y_24h ∈ {1, 0, NULL}` | §4.1；新增 `label_status / label_observable / outcome_unknown_reason / label_reason` 字段 |
| eICU `hospitaldischargestatus IS NULL` 不再一律排除，按是否存在完整 24h 院内观察期判定 | §4.1 |
| 竞争风险增加第三类事件「转至其他急性医疗机构」（0=删失 / 1=死亡 / 2=存活出院 / 3=急性转出），并固定同时刻多状态优先级 | §4.2 |
| 风险集以**连续 episode 结束时间**为准（`t_lm < episode_end`），不再用 index stay outtime | §3.3 |
| 固定全部边界条件（landmark 时刻死亡/出科、死亡/出院恰好落在 t+24h、ECG/特征恰好落在 landmark）并转化为单元测试 | §3.4 |

### 0.3 队列、特征与映射修改（评审 §四–§六）

| 修改 | v2.0 落点 |
|---|---|
| MIMIC index 选择排序固定为 `t_sepsis, admittime, episode_intime, stay_id`，先构造全部合格 episode 再按患者取首次 | §2.1 C3、附录 A.1 |
| `first_icu_stay` 仅描述，不作纳入条件 | §2.1 C3 |
| DNR/CCO、ECMO、移植标志降级为**探索性/敏感性标志**，完成 PPV 抽查前不用于正式排除；出院后最终 ICD 不得证明 landmark 前状态 | §2.1 C4 |
| Charlson 重建为 `charlson_prior`（仅用 index 入院前既往住院 ICD）；本次住院最终 ICD 版仅敏感性；Charlson 移出 SC-common | §5.1、§6 |
| 体重/身高遵守 landmark 时间截断（`t_weight ≤ t_lm`） | §5.1 |
| PaO₂/FiO₂：主分析仅用明确记录的 FiO₂，氧流量换算降级为敏感性且必须联合设备类型；输出 `fio2_source` 四态 | §5.3 |
| eICU 生命体征三来源（pivoted_vital / vital_periodic / vital_aperiodic）建立主来源+缺失补充+去重规则，输出 `source_table` | §5.2 |
| `derived.sofa` 窗口语义需人工抽查 20–50 stay 与原始记录逐项核对 | §5.4 |
| SC-common 由单一候选表拆为 **core（A 层高同构）/ extended（A+B）/ MIMIC-only / 不纳入** 四层 | §6 |

### 0.4 ECG、QA、SQL 与实施顺序修改（评审 §七–§十二）

| 修改 | v2.0 落点 |
|---|---|
| ECG QC 分两层：固定结构性 QC（全集统一）+ 数据驱动 QC（阈值仅训练集确定并冻结） | §5.8 |
| ECG availability 输出五层级（`ecg_found_raw → ecg_same_encounter → ecg_structurally_valid → ecg_pass_frozen_qc → ecg_selected_for_model`） | §5.8 |
| 主配对队列 ECG-available 定义在 QC 后、查看测试集结果前冻结 | §5.8 |
| 论文 2 NEE 双实现核验字段（`nee_project_formula / nee_mimic_derived / nee_difference / nee_source_drug_components`）；eICU 论文 2 人工审核拆为 7 个环节分别审核 | §5.5 |
| SQL 模板修正：动态 interval 稳定写法、显式 `TIMESTAMP '9999-01-01 00:00:00'`、NULL/时间倒置处理、三态标签 SQL、A.1 标注为概念性模板 | 附录 A |
| 新增 QA：时间逻辑 QA、队列表型 QA（MIMIC 随机 + eICU 分层）、ECG 配对 QA、结局分层抽查、派生表来源验证（checksum/commit/分布比对） | §7 |
| 内部时间划分节修正表述（177,873 等为全库 `patients` 表人数而非脓毒症队列人数）；`2020 - 2022` 排除须正式 amendment；对外命名「基于 anchor_year_group 的时间组外验证」 | §2.4 |
| 风险清单新增 R12–R18，R1 改写为 D0 决策门 | §9 |
| 实施顺序由 v1.0 的线性 6 步改为**阶段 A（协议与来源锁定）→ B（MIMIC 可行性）→ C（MIMIC 特征与论文 2 标签）→ D（eICU 外验可行性）** | §10 |

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
| ICU | `main.icustays` | `stay_id`；`first_careunit, last_careunit, intime, outtime, los` | 94,458 |
| 转科 | `main.transfers` | `transfer_id`；`eventtype, careunit, intime, outtime` | 2,413,581 |
| 脓毒症表型 | `mimiciv_derived.sepsis3` | 每 stay 一行；`suspected_infection_time, sofa_time, sofa_score, 六组分`（**无 `sepsis_time`**，见 §3.1 D0） | 41,295 |
| 疑似感染 | `mimiciv_derived.suspicion_of_infection` | 每次抗生素-培养配对一行 | 949,901 |
| SOFA（小时级） | `mimiciv_derived.sofa` | `stay_id, starttime/endtime(1h), 组分输入 + 24h 滑动组分` | 8,219,121 |
| 生命体征 | `mimiciv_derived.vitalsign` | `stay_id, charttime`；HR/SBP/DBP/MBP（有创与无创分列）/RR/Temp/SpO2/Glucose | 13,519,533 |
| GCS | `mimiciv_derived.gcs` | `charttime, gcs, gcs_motor/verbal/eyes, gcs_unable` | 2,217,787 |
| 血管活性药 | `mimiciv_derived.vasoactive_agent` | `stay_id, starttime, endtime, 7 药速率列` | 839,543 |
| NEE | `mimiciv_derived.norepinephrine_equivalent_dose` | `stay_id, starttime, endtime, norepinephrine_equivalent_dose` | 783,613 |
| 单药输注 | `mimiciv_derived.{norepinephrine, epinephrine, dopamine, phenylephrine, vasopressin, dobutamine, milrinone}` | `stay_id, linkorderid, vaso_rate, vaso_amount, starttime, endtime` | — |
| 通气 | `mimiciv_derived.ventilation` | `stay_id, starttime, endtime, ventilation_status` | 144,812 |
| 检验（宽表） | `mimiciv_derived.{bg, chemistry, coagulation, complete_blood_count, cardiac_marker, blood_differential, enzyme, inflammation}` | `stay_id/hadm_id + charttime + 项目列`（**多无 `storetime`，见 §5.0/§5.3**） | — |
| 检验（原始） | `main.labevents` + `main.d_labitems` | `itemid, charttime, storetime, valuenum, valueuom` | 158,374,764 |
| 微生物 | `main.microbiologyevents` | `charttime/chartdate, spec_type_desc, org_name, interpretation` | 3,988,224 |
| 合并症 | `mimiciv_derived.charlson` | `hadm_id`；17 组分 + `charlson_comorbidity_index`（**基于本次住院最终 ICD，见 §5.1**） | 546,028 |
| 体重/身高 | `mimiciv_derived.weight_durations`、`mimiciv_derived.height` | 时段体重；身高 | 401,850 / 43,342 |
| 尿量 | `mimiciv_derived.urine_output` | `stay_id, charttime, urineoutput` | 4,127,634 |
| ICU 汇总 | `mimiciv_derived.icustay_detail` | `stay_id`；年龄、性别、入出院时间、结局、序次 | 94,458 |
| 结局汇总 | `mimiciv_derived.patient_outcomes` | `stay_id`；死亡、SOFA/SOFA-2、通气、RRT、血管活性药时长等 73 列 | 94,458 |
| ECG 索引 | `main.ecg_records` | `subject_id, study_id, ecg_time, path` | 800,035 |
| ECG 机测 | `main.ecg_measurements` | `study_id, ecg_time, RR/间期/电轴` | 800,035 |

> 注：`mimiciv_derived` 同时含 SOFA-2 系列表（`sofa2_*`）。本项目 Sepsis-3 与亚组均基于 **SOFA-1**，禁止混用（风险 R6）。本地派生表由前期 R 脚本生成；其 mimic-code 版本、commit hash、SQL/R 文件清单与本地修改须在**阶段 A** 完成登记（§10，D0 前置）。

### 1.3 eICU 库关键表（本项目涉及部分）

| 域 | 表 | 粒度 / 关键列 | 实测行数 |
|---|---|---|---|
| ICU 入住 | `main.patient` | `patientunitstayid`；三级 ID、年龄（VARCHAR，含 `"> 89"`）、入出院 offset 与状态、身高体重 | 200,859 |
| 医院 | `main.hospital` | `hospitalid`；床位数、教学状态、region | 208 |
| ICU 汇总 | `main.icustay_detail` | `patientunitstayid`；`hosp_mort, icu_los_hours, apache_iv, region` 等 | 200,859 |
| 生命体征（监护仪） | `main.vital_periodic` | `observationoffset`（分钟）；HR/RR/SpO2/有创血压/Temp 等 | 146,671,642 |
| 生命体征（非周期） | `main.vital_aperiodic` | `observationoffset`；无创/有创血压、CO/CI/SVR | 25,075,074 |
| 生命体征（护理宽表） | `main.pivoted_vital` | `chartoffset`；HR/RR/SpO2/NIBP/IBP/Temp | 21,038,216 |
| 检验（原始） | `main.lab` | `labresultoffset, labname, labresult, labresultrevisedoffset` | 39,132,531 |
| 检验（宽表） | `main.pivoted_lab` | `chartoffset`；肌酐/胆红素/血小板/乳酸/WBC 等 22 项 | 5,314,163 |
| 血气 | `main.pivoted_bg` | `chartoffset`；`fio2`（实测 0–1 量纲）、`pao2, paco2, pH` | 1,464,012 |
| GCS | `main.pivoted_gcs` / `main.pivoted_score` | `chartoffset`；GCS 总分/分项；`gcs_unable, gcs_intub` | 3,451,788 / 5,709,678 |
| 输注药（原始） | `main.infusion_drug` | `infusionoffset, drugname(内嵌单位/浓度), drugrate, infusionrate, drugamount, volumeoffluid, patientweight` | 4,803,719 |
| 输注标记 | `main.pivoted_infusion` | `chartoffset`；8 药 0/1 标记（**无剂量**） | 1,083,074 |
| 用药医嘱 | `main.medication` | `drugorderoffset, drugstartoffset, drugstopoffset, drugname, routeadmin` | 7,301,853 |
| 微生物 | `main.micro_lab` | `culturetakenoffset, culturesite, organism` | 16,996（仅 2,923 患者；**见 §2.2 Go/No-Go**） |
| 尿量 | `main.pivoted_uo` | `chartoffset, urineoutput` | 4,088,881 |
| 体重 | `main.pivoted_weight` | `chartoffset, source_table, weight_type, weight` | 501,506 |
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath` | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring`（含机械通气、RRT、ECMO 等路径） | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数；`respchartoffset` 呼吸记录 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部原始事件时间为**相对各 unit stay 入科的分钟偏移（offset）**，`patient.hospitaladmitoffset` 为相对住院入院的偏移（通常为负值）；出院年份仅 2014/2015（实测 95,513 / 105,346），**无绝对日期**。多 unit stay 合并前必须先换算到统一住院级时间坐标（§2.2）。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C0–C5）

- **C0 连续 ICU episode 映射（新增，P0-5）**：基于 `main.transfers`（`eventtype` 与 `careunit` 类别清单 QA 实测后预登记）将同一 `hadm_id` 内 intime/outtime 首尾相接（允许间隙阈值预登记，默认 0 分钟；>0 阈值作敏感性）的 ICU 记录合并为连续 episode，输出桥接表：

  ```text
  mimic_icu_episode_map
  - subject_id, hadm_id, episode_id
  - stay_id, stay_seq_in_episode        -- episode 内序号（按 intime 升序）
  - episode_intime                      -- 首个 ICU stay 的 intime
  - episode_outtime                     -- 末个 ICU stay 的 outtime
  ```

  下游所有 ICU 数据一律按 `stay_id → episode_id` 聚合，不再只使用 index `stay_id`：后续 stay 的生命体征、检验、药物、尿量全部纳入同一 episode；landmark 终止与风险集以 `episode_outtime` 为准（§3.2/§3.3）；ICU 结束时间取 `episode_outtime`。`episode_id` 为主分析的真正队列主键，`stay_id` 保留用于溯源。

- **C1 脓毒症相关 episode 池**：`mimiciv_derived.sepsis3`（`sepsis3 = TRUE`，实测 41,295 stays / 31,910 subjects）⨝ C0 的 `mimic_icu_episode_map`（按 stay_id 归属 episode）⨝ `mimiciv_derived.icustay_detail` ⨝ `main.icustays`（`first_careunit`）。同一 episode 内任一 stay 命中 sepsis3 即标记该 episode 为 sepsis-associated，并记录命中 stay 与 `t_sepsis` 候选（D0 锁定后生效，§3.1）。

- **C2 入排初筛**：年龄 ≥18（`icustay_detail.admission_age`）；成人 ICU（episode 首个 stay 的 `first_careunit` 排除 NICU 等非成人单元，类别清单以 QA 实测为准）。

- **C3 index episode 选择**（技术文档 §4.2 层级规则，修订版）：先按 C1–C2 构造**全部合格 episode**，再按 `subject_id` 选择**首次合格 episode**，排序键固定为 `t_sepsis, admittime, episode_intime, stay_id`（完全确定性，杜绝并列随机）。`first_icu_stay` **仅作描述性变量，不作纳入条件**——技术文档要求的是「首次符合条件的 sepsis-associated ICU stay」，不等于患者首次 ICU stay。其余合格 episode 不进入主分析。

- **C4 探索性/敏感性标志（降级，评审 §四.3）**：外院转入（`admissions.admission_location` 含 Transfer 类）；首个有效 landmark 前已存在 ECMO（`main.procedureevents` itemid QA 确认）；近 90 天实体器官移植（`diagnoses_icd`/`procedures_icd` ICD 清单，预登记）；首个有效 landmark 前 DNR/CCO（`main.poe`/`code status` 相关 chartevents itemid，预登记清单）。**在完成 PPV 人工抽查前，以上标志一律仅作探索性/敏感性标志，不用于正式排除。** 注意：`diagnoses_icd`/`procedures_icd` 为出院后最终编码，**不得**用来证明 landmark 前已存在某状态；凡以 ICD 为依据的标志，其「landmark 前已存在」的口径仅指**既往住院**的 ICD 记录。所有标志仅使用首个有效 landmark 时点之前的记录，禁止追溯性排除。

- **C5 队列事实表** `cohort_mimic_v2`（每 **episode** 一行）：`subject_id, hadm_id, episode_id, index_stay_id, t_sepsis（D0 锁定后填）, episode_intime, episode_outtime, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

产出规模预估：C1 后直接可得；C2–C3 后的最终队列患者数、landmark 数与事件数进入月 1 Feasibility Table（技术文档 §9.1）。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C10）

eICU 无现成 Sepsis-3 派生表，且培养覆盖极低（`micro_lab` 仅 2,923 患者），按「Robustness under phenotype shift」层级自建，**先建时间坐标，再建表型**。

- **C6a 住院级统一时间坐标（新增，P0-4）**：每个 `patientunitstayid` 的 offset 以该 stay 入科为零点，多 stay 合并前统一换算为**住院级分钟坐标**：

  ```text
  t_hospital_min = -hospitaladmitoffset + eventoffset        -- 住院入院为 0 点
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

  episode 合并规则：同一 `patienthealthsystemstayid` 内，前一 stay 的 `unit_end_hospital_min` 与后一 stay 的 `unit_start_hospital_min` 之差 ≤ 预登记间隙阈值（默认 0 分钟，敏感性阈值预登记）者合并；`unitstaytype = 'readmit'` 是否纳入同一 episode 按同一规则判定并单独打标。合并后，第二个及后续 stay 的生命体征、检验、药物、尿量经 `eicu_event_time_map` 换算到 episode 坐标后全部纳入；`t_sepsis_episode_offset` 与所有 landmark 均定义在 episode 坐标上。

- **C6b suspected infection 重建（方向性规则修订，P0-2）**：在 episode 坐标上执行，抗生素使用时点与培养采样时点（`micro_lab.culturetakenoffset`）按**锁定版 mimic-code 的方向性规则**配对，**禁止**写成对称的「±24h/±72h」：

  ```text
  培养先发生：  t_antibiotic - t_culture  ∈ [0, 72h]
  抗生素先发生：t_culture - t_antibiotic  ∈ [0, 24h]
  （具体窗口数值以阶段 A 锁定的 mimic-code 版本为准）
  ```

  抗生素时间字段优先级：**可确认的实际给药时间 > `drugstartoffset` > `drugorderoffset`**。医嘱时间不等于给药时间；若某药物/医院只能得到 `drugorderoffset`，该记录显式打 `order_time_only = TRUE` 并计入 phenotype shift 差异登记。抗菌药物清单（药名/HICL）预登记；规则与 MIMIC 侧 `suspicion_of_infection` 的逻辑逐条对照，差异写入预登记差异表。

- **C7 三套可行性表型队列（新增，P0-3）**：在锁定 eICU 外验定义**之前**，并行输出三套候选队列并做可行性对比：

  | 队列 | 定义 | 预期风险 |
  |---|---|---|
  | **P-strict** | 严格抗生素-培养配对（C6b）+ 自建 SOFA ≥2（§5.4） | 受 `micro_lab` 覆盖（2,923 患者）限制，队列可能极小且偏向微生物记录完整的医院 |
  | **P-clinical** | 临床诊断/ICD 感染证据（`diagnosis`/`admission_dx` 感染路径，清单预登记）+ 自建 SOFA ≥2 | 与 MIMIC Sepsis-3 不同构，属 phenotype shift |
  | **P-explicit** | 显式 sepsis / severe sepsis / septic shock 诊断字符串（`diagnosis.diagnosisstring`/`admission_dx`，清单预登记） | 依赖诊断记录习惯，医院间差异大 |

  分别报告：**患者数、医院数、院内死亡数、各 landmark 阳性数、SC-common 特征覆盖率、与 MIMIC 主队列的基线差异**。

  **Go/No-Go 门槛（月 1 锁定具体数值，技术文档 §9.1 同口径）**：①严格表型覆盖医院数；②患者数与死亡事件数；③主要 12 个 landmark 可估计比例（阳性 ≥20 且阴性 ≥100）；④培养覆盖率；⑤抗生素时间可靠率（实际给药时间可得比例）；⑥SOFA 六组分可计算率。门槛不达标时**在建模前**决定替代表型，禁止根据模型效果事后修改。

  **外验命名决策（建模前锁定）**：根据三套队列可行性结果，命名为 `Transportability validation` / `Robustness under phenotype shift` / 探索性跨库验证之一。基于当前实测（培养覆盖 2,923/200,859），默认预期为 **Robustness under phenotype shift**，不得宣称为完全同构的 Sepsis-3 外部验证。

- **C8 入排与 index episode**：年龄 ≥18（`patient.age` 数值化，`"> 89"` 记 90 并打标）；同一 `uniquepid` 按 `t_sepsis_episode_offset, hospitaladmitoffset, episode_start_hospital_min, patientunitstayid` 确定性排序取**首次合格 episode**；多 unit stay 一律经 C6a 的 episode 坐标处理。

- **C9/C10 队列事实表** `cohort_eicu_v2`（字段与 C5 同构，时间列全部为 episode 坐标分钟）：`patientunitstayid(index), patienthealthsystemstayid, uniquepid, episode_id, t_sepsis_episode_offset, episode_start_min(=0), episode_end_min, hospitaladmitoffset, hospitaldischargeoffset, hospitaldischargestatus, hospitaldischargelocation, age_num, gender, unittype, hospitalid, phenotype_track(P-strict/P-clinical/P-explicit), 敏感性标志`。

### 2.3 两库队列字段同构约定

两库队列事实表输出**同名同义列**；时间列分两套命名：MIMIC 为 `*_ts`（TIMESTAMP，年份偏移），eICU 为 `*_offset_min`（INTEGER 分钟，episode 坐标）。所有下游节点按「相对 t_sepsis 的小时差」对齐，禁止直接比较两库原始时间列。

### 2.4 内部时间划分（技术文档 §12.2 落地，修订表述）

实测 `anchor_year_group` 为 5 类。按 v1.9 预设映射的自然延伸固定为（下表人数为**全库 `patients` 表人数，非脓毒症队列人数**；队列口径的正式数字由阶段 B 管线产出）：

| 集合 | anchor_year_group | 全库 patients 表人数（参考） |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873 |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 组为 v1.9 未预期类别：主分析不使用（与 v1.9「删除 COVID-era 分析」一致）。该处理须在**阶段 A** 形成正式 protocol amendment 或技术文档 v2.0 修订，明确：①排除理由（MIMIC-IV-ECG 时间覆盖、COVID 时期诊疗模式漂移、预注册映射未覆盖）；②该组数据**完全不查看结局与模型性能**；③是否仅保留为潜在扩展/敏感性数据（风险 R2）。

划分按 `subject_id` 归入，同一患者所有 landmark 同属一个集合；患者级随机划分仅作敏感性分析。划分表 `split_assignments_v2`（`subject_id, set_name`）落盘冻结。

最终冻结时输出（技术文档 §9.1 同口径）：脓毒症患者数、ECG-available 患者数、ECG-available landmark 数、死亡患者数、主要 12 个 landmark 的阳性/阴性数。

> 对外表述规范：`anchor_year_group` 是去标识化年份组，不等于真实自然年份。论文中称为「**基于 anchor_year_group 的时间组外验证**」，不得过度解释为精确日历年份上的时间外验证。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time —— 决策门 D0（P0-1，未锁定）

**当前状态：t_sepsis 未锁定。** 实测本地 `mimiciv_derived.sepsis3` 不含技术文档 §4.1 规定的 `sepsis_time` 字段（实有 `suspected_infection_time` 与 `sofa_time`）。v1.0 将其操作性定义为 `suspected_infection_time` 并登记为「方言迁移修改」的做法**已撤销**——时间原点决定 landmark 生成、风险集、ECG 时效窗、历史窗、24h 标签与 0–72h 主要 iAUROC，属于方案级 estimand 变更，不是字段别名问题。

**D0 前置审计（阶段 A 第 1–2 步完成）**：

1. 找到本地 `mimiciv_derived.sepsis3` 的生成 SQL/R 脚本；
2. 记录 mimic-code 的：版本、commit hash、原始 SQL 文件、本地全部修改；
3. 明确 `sofa_time`、`suspected_infection_time` 各自的生成逻辑；
4. 确认技术文档所称 `sepsis_time` 实际应为：`suspected_infection_time`、`sofa_time`，或应由两者按某规则构造。

**D0 两个合法出口（PI 确认后二选一）**：

- **出口 A**：重新生成符合预注册定义的 `sepsis_time`（保留技术文档不变）；
- **出口 B**：通过 protocol amendment（技术文档修订版）将主时间原点正式改为 `suspected_infection_time`，并同步更新预注册记录。

**明确禁止**：技术文档写「主分析为 `sepsis_time`」而代码实际使用 `suspected_infection_time`——审稿时构成严重方案偏离。

**D0 锁定前的许可范围**：仅允许结构审计、可行性统计与原型提取（阶段 B 的可行性队列可在候选时间原点下并行跑通两套口径做对比）；**禁止**正式模型训练与测试集评估。

eICU 侧：`t_sepsis_episode_offset` 随 D0 结论同构定义（C6b/C7 输出，episode 坐标分钟）。敏感性分析（技术文档 §4.1/§15.2）保留三种时间原点对比：锁定版 sepsis_time / suspected infection time / ICU admission。

`Δ_ICU-sepsis = ICU 入科时间 − t_sepsis`（MIMIC 直接相减；eICU 为 `0 − t_sepsis_episode_offset`），作为显式输入特征（技术文档 §4.4）。

### 3.2 Landmark 生成（DAG 节点 L1，修订：episode 终止）

对每个 index episode：

1. `k0 = max(0, ceil((t_ICU − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_episode_offset) / 360min))`。
2. landmark 序列 `t_lm(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. 终止规则：`t_lm(k) < min(episode_end, 死亡时间)`——**以连续 episode 结束时间为准**（P0-5/C0、C6a），不再使用 index stay 的 outtime；ICU 转出至病房后停止生成新 landmark，但已生成 landmark 的 24h 结局随访继续完成（技术文档 §4.2）。
4. 主分析积分网格固定为 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v2`（每 landmark 一行）：`episode_key, subject_key, k, t_lm, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2，修订：episode 口径）

landmark t 纳入条件：t 时刻存活且仍处于连续 ICU episode 内。排除：

- t 前或 t 时刻已死亡（MIMIC `admissions.deathtime ≤ t`；eICU `Expired 且 hospitaldischargeoffset ≤ t_episode`）；
- t 前或 t 时刻连续 episode 已结束（`episode_end ≤ t`）。

### 3.4 边界条件（新增，全部转化为单元测试）

以下边界语义在本版固定，任何下游实现不得另行解释：

| 情形 | 判定 |
|---|---|
| landmark 时刻恰好死亡 | 不进入风险集 |
| landmark 时刻恰好 ICU 出科（episode 结束） | 不进入风险集 |
| 死亡发生在 `(t, t+24h]` | 阳性（含恰好 `t+24h`） |
| 出院恰好发生在 `t+24h` | 按存活至窗口终点处理（阴性，存活出院） |
| ECG 恰好发生在 landmark 时刻 | 允许使用（`ecg_time ≤ t_lm`） |
| 特征恰好在 landmark 时刻可获得 | 允许使用（`available_time ≤ t_lm`） |
| 死亡时间早于 admittime 或晚于 dischtime 且无院内死亡标志 | 时间异常，打标进入 QA，不直接参与标签判定 |

---

## 4. 结局标签（DAG 节点 L3，三态重构）

### 4.1 主结局：landmark 后 24h 院内全因死亡（三态标签）

**标签字段（两库同构）**：

```text
y_24h            : 1 / 0 / NULL        -- 主分析仅使用非 NULL
label_status     : event / non_event / unknown
label_observable : TRUE / FALSE        -- 是否存在完整 (t, t+24h] 院内观察
outcome_unknown_reason : NULL / acute_transfer / missing_status_left_observation / time_anomaly
label_reason     : 触发分支的文本说明（审计用）
```

**判定逻辑（按序执行，首个命中分支生效）**：

1. `(t, t+24h]` 内院内死亡 → `y_24h = 1`（event）；
2. `(t, t+24h]` 内转至其他急性医疗机构 → `NULL`（unknown，`acute_transfer`）——不编码为阴性，从该 landmark 主分析排除；敏感性分析分别按存活离院与最坏情景编码；
3. 院内可观测期完整覆盖至 `t+24h`（仍在住院）且未死亡 → `y_24h = 0`（non_event）；
4. `(t, t+24h]` 内明确存活出院（回家/康复机构等非急性转出类别）→ `y_24h = 0`（non_event，技术文档 §2.1）；
5. 结局状态缺失且患者在预测窗前已离开可观测范围 → `NULL`（unknown，`missing_status_left_observation`）；
6. 出院状态缺失但患者在本院持续被观察至 `t+24h` → 可判 `y_24h = 0`（non_event），**不必一律排除**。

**MIMIC 实现要点**：`admissions.deathtime` 仅记录院内死亡（`hospital_expire_flag = 1` 互查）；急性转出依据 `discharge_location` 类别清单（QA 实测后预登记，风险 R9）；`patients.dod` 为日期精度，仅用于 1 年死亡等辅助分析，不进主结局。

**eICU 实现要点**：`hospitaldischargestatus = 'Expired'` 且 `hospitaldischargeoffset ∈ (t_episode, t_episode + 1440]` → 1；实测 `hospitaldischargestatus`：Alive 181,104 / Expired 18,004 / NULL 1,751。**NULL 者不再一律排除**：按上述分支 5/6 依据完整 24h 院内可观测性判定（可观测依据：unit 出院 offset、后续护理/检验记录存在性等，规则预登记）；急性转出依据 `hospitaldischargelocation` 类别清单（与 MIMIC 字符串不同，QA 实测后预登记，风险 R9）。

### 4.2 次要结局：7 天竞争风险（四类事件，修订）

同一 landmark 输出事件类型与事件时间，供 DeepHit CIF 使用：

```text
event_type:
  0 = administrative censoring        -- t_lm + 168h 行政截尾
  1 = in-hospital death
  2 = alive discharge
  3 = transfer to another acute hospital   -- 新增（技术文档 §15.2）
```

**同时刻多状态优先级（预登记）**：死亡 > 急性转出 > 存活出院 > 删失。若急性转出事件数不足，按技术文档 §15.2 降级为状态未知删失并明确报告。eICU 侧存活出院/转出时间 = `hospitaldischargeoffset`。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（组分总分增加 ≥2，基于 §5.4 小时级 SOFA）、新启用血管活性药（§5.5 NEE 流由 0 转 >0）。

---

## 5. 特征提取模块

### 5.0 数据可用时间契约（新增，P0-6）

**每条特征记录必须携带三个时间字段**，贯穿全部特征表：

```text
event_time       -- 临床事件发生/测量时刻
available_time   -- 临床实际可见（可获得）时刻
source_time_type -- 可用时间的来源类型（见下表）
```

**主分析断言：`available_time ≤ t_lm`**（取代 v1.0 的 `feature_time(charttime) ≤ t_lm`）。event_time 与 available_time 的差异分布进入 QA 报告（§7.3）。

| 数据域 | 主时间语义（available_time 口径） | source_time_type 取值 |
|---|---|---|
| 床旁连续生命体征 | 测量/观察时间 | `measured` |
| 护理手工记录 | 审计 chart time 与 entry/store time 后取较晚且可确认者 | `charted` / `entry_verified` |
| 检验 | **结果可用时间优先**（MIMIC `storetime`；eICU `labresultrevisedoffset` 辅助）；无法获得时显式降级并打标 | `result_available` / `charttime_fallback` |
| 药物输注 | 实际 start/end time，非 order time | `infusion_actual` / `order_time_only` |
| 微生物 | 初步/最终结果分别使用各自可用时间 | `preliminary` / `final` |
| ECG | 采集开始时间（技术文档 §7.2） | `acquired` |
| 诊断 | 仅使用 landmark 前明确可见的诊断记录 | `recorded_pre_landmark` |
| 治疗限制（DNR/CCO） | 实际记录/生效时间 | `order_effective` |

**MIMIC 关键检验处理**：本地 `mimiciv_derived` 检验宽表多数丢失 `storetime`。主分析所需的 SOFA/SC-common 关键检验（§5.3 清单）**从原始 `main.labevents` 重建**以同时保留 `charttime` 与 `storetime`；派生宽表仅用于交叉校验。若 `storetime` 经 QA 证实不可用或不可靠，必须在论文中明确声明本模型为「**按测量时间的回顾性预测**」，而非严格实时可部署预测，并将该限制写入局限性（风险 R12）。

统一时间语义（技术文档 §15.3，不变）：生命体征与检验映射至 **landmark 前 24h 的 1h 时间网格**，同小时多条取**中位数**，无记录保留缺失并生成观测 mask 与距上次观测间隔（GRU-D 输入三元组）。t=0 landmark 允许使用 sepsis onset 前数据。

### 5.1 静态特征（DAG 节点 F1，修订）

| 特征 | MIMIC 来源 | eICU 来源 | 备注 |
|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` 数值化（`"> 89"`→90 并打标） | 岁 |
| 性别 | `patients.gender` | `patient.gender` | 类别对齐 M/F/Other |
| 体重 | `weight_durations`（输注前/landmark 前最近者） | `pivoted_weight` + `admissionweight` | kg；**必须满足 `t_weight ≤ t_lm`**（见下） |
| 身高 | `height` / `omr` | `admissionheight` | cm；同样遵守 landmark 截断 |
| 入院类型/来源 | `admissions.admission_type, admission_location` | `hospitaladmitsource, unitadmitsource` | 类别映射表预登记；C 层低同构（§6） |
| ICU 类型 | `icustays.first_careunit`（episode 首 stay） | `patient.unittype` | C 层低同构（§6） |
| Δ_ICU-sepsis | 计算列 | 计算列 | 小时 |
| Charlson 合并症 | **重建 `charlson_prior`：仅用 index 入院前已完成的既往住院 ICD**；本次住院最终 ICD 计算的 `charlson_discharge_coded` 仅敏感性 | `past_history` 路径映射自建近似（与既往住院 ICD 不同构，差异预登记） | **移出 SC-common**，见 §6 |

**体重/身高 landmark 截断（新增）**：`weight_durations`/`height` 可能含 landmark 后记录。论文 1 使用时必须满足 `t_weight ≤ t_lm`；定义为 admission-time 静态变量时，必须限定为入院初始测量并记录其 available_time。**禁止**为早期 landmark 使用住院后较晚测得的体重。

### 5.2 生命体征时序（DAG 节点 F2，修订：来源去重）

| 变量 | MIMIC 来源 | eICU 来源（主来源 → 缺失补充） | 目标单位 |
|---|---|---|---|
| HR | `derived.vitalsign.heart_rate` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | `sbp/dbp/mbp`（有创）与 `*_ni`（无创），有创优先 | `pivoted_vital.ibp_*` 优先，次 `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 仅在主来源缺失时补充 | mmHg |
| RR | `resp_rate` | `pivoted_vital.RespiratoryRate` → `vital_periodic.respiration` | 次/分 |
| SpO2 | `spo2` | `pivoted_vital.spo2` → `vital_periodic.sao2` | % |
| 体温 | `temperature`（派生表已转 °C，QA 验证分布） | `pivoted_vital.temperature`（量纲 QA 验证，°F→°C 转换规则预登记） | °C |

**eICU 三来源去重规则（新增）**：`pivoted_vital` / `vital_periodic` / `vital_aperiodic` 可能来自相同底层记录。①明确每变量主来源；②其他来源仅在主来源缺失时补充，**不得**与主来源记录一起取中位数（避免重复计数）；③建立记录级去重规则（同患者、同时刻、同值判重）；④每条记录输出 `source_table`；⑤抽查同一时间同一变量的跨表重复率并写入 QA 报告。

### 5.3 检验（DAG 节点 F3，修订：原始重建 + fio2_source）

SOFA 及 SC-common 所需项目：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- **MIMIC**：关键项目**从 `main.labevents` 重建**（按 `d_labitems` itemid 清单，清单预登记），同时保留 `charttime`（event_time）与 `storetime`（available_time 主口径）；派生宽表（`bg`/`chemistry`/`coagulation`/`complete_blood_count`）仅交叉校验（§5.0）。
- **eICU**：`pivoted_lab`（22 项宽表）+ `pivoted_bg`（`fio2` 实测 0–1 量纲、`pao2`）；原始 `lab` 表补充未入宽表项目（`labname` 字符串匹配）；`labresultrevisedoffset` 作 available_time 辅助。
- **PaO₂/FiO₂（修订）**：MIMIC 用 `derived.bg.pao2fio2ratio`（含 `_novent/_vent` 口径）；eICU 用 `pivoted_bg` 同时间点 `pao2/fio2` 计算。**FiO₂ 使用规则**：
  - 主分析**优先且仅默认使用明确记录的 FiO₂**；
  - 氧流量→FiO₂ 换算**降级为敏感性分析**，且**必须联合设备类型**（`pivoted_o2.o2_device`）；无设备类型时不得仅按流量直接换算；
  - 每记录输出 `fio2_source ∈ {measured, ventilator_setting, device_based_estimated, flow_only_estimated}`。

### 5.4 SOFA 组分（DAG 节点 F4，修订：经典心血管规则）

- **MIMIC**：直接使用 `mimiciv_derived.sofa`（1h 粒度，含 24h 滑动最差组分 `*_24hours` 与 `sofa_24hours`）。landmark t 的 SOFA = `endtime ≤ t` 的最后一行。首个有效 landmark 的 `cardiovascular` 组分用于 CV-SOFA≥3 亚组固定分层（技术文档 §15.1）。
  **窗口语义验证（新增）**：QA 阶段人工抽查 **20–50 个 stay**，将派生 SOFA 与原始输入记录逐项核对：①`starttime/endtime` 对应哪个 1h 区间；②`*_24hours` 是否只使用 `endtime` 之前数据；③是否可能使用本小时后半段信息；④landmark 恰在小时边界时取哪一行。核对结果写入派生表来源验证报告（§7.5）。
- **eICU**：自建六组分，窗口为 24h 滑动最差（与 MIMIC `sofa` 口径对齐），输入来源——呼吸（`pivoted_bg` P/F + `respiratory_care`/`treatment` 通气标志）、凝血（`pivoted_lab.platelets`）、肝脏（`bilirubin`）、**心血管（经典规则，见下）**、神经（`pivoted_gcs`，镇静处理规则与 MIMIC `gcs_unable` 口径的差异预登记）、肾脏（`creatinine` + `pivoted_uo` 24h 尿量）。
- **心血管组分经典规则（P0-7，替换 v1.0 的「MAP + NEE 分层」）**：严格按原始 SOFA 阈值计分，仅使用 MAP 与 **dopamine / dobutamine / epinephrine / norepinephrine** 四种药物剂量：

  | 分值 | 标准 |
  |---|---|
  | 0 | MAP ≥ 70 mmHg，无血管活性药 |
  | 1 | MAP < 70 mmHg |
  | 2 | dopamine ≤ 5 μg/kg/min，或任意剂量 dobutamine |
  | 3 | dopamine > 5 μg/kg/min，或 epinephrine ≤ 0.1 μg/kg/min，或 norepinephrine ≤ 0.1 μg/kg/min |
  | 4 | dopamine > 15 μg/kg/min，或 epinephrine > 0.1 μg/kg/min，或 norepinephrine > 0.1 μg/kg/min |

  **三者严格分离**：①`sofa_cv_original`＝上述经典评分（表型判定、CV-SOFA≥3 亚组、跨库可比性的唯一口径）；②`nee_current`＝模型输入特征与论文 2 标签基础（§5.5）；③`vasopressor_burden`＝探索性扩展变量。Vasopressin、phenylephrine 对论文 2 与模型特征有价值，但**不进入经典 SOFA 心血管计分**，**禁止**用 NEE 直接生成主分析 SOFA 心血管组分（否则 eICU SOFA 与 MIMIC `derived.sofa` 不同构、Sepsis-3 表型改变、CV-SOFA≥3 亚组不可比，风险 R15）。
- `sepsis3` 表内的静态 SOFA 组分（`respiration … renal`）仅用于表型判定，**禁止**作为 landmark 级特征（风险 R11）。

### 5.5 血管活性药与 NEE（DAG 节点 F5；论文 1 特征 + 论文 2 标签基础）

- **MIMIC**：`mimiciv_derived.vasoactive_agent`（NE/EPI/DA/PE/VAS/DOB/MIL 速率时段）→ 按技术文档 §6.2 公式合成 NEE。**NEE 双实现核验（新增）**：逐时点同时保存并比较——

  ```text
  nee_project_formula        -- 按技术文档附件 B 公式本项目自算
  nee_mimic_derived          -- 本地 norepinephrine_equivalent_dose 表
  nee_difference             -- 两者差值
  nee_source_drug_components -- 各单药贡献分量
  ```

  核验指标：一致率、绝对误差分布、误差来源归因、vasopressin 单位、dopamine/phenylephrine 换算、重叠输注处理。两套实现不一致时以附件 B 公式为准并记录差异原因。体重按 §6.2 优先级（且遵守 §5.1 landmark 截断）；`weight_durations` 提供时段体重。
- **eICU**：`infusion_drug` 解析管线：①药名正则归类（实测含 `Norepinephrine (mcg/min)`、`(mg/min)`、`(mg/kg/min)`、`(mg/hr)`、`Vasopressin (units/min)` 等形式，完整清单 QA 枚举后预登记）；②`drugrate`/`infusionrate` 文本数值化；③单位→μg/kg/min（VAS 保持 U/min）；④体重优先级：`infusion_drug.patientweight`（实测常缺失）→ `pivoted_weight` → `admissionweight` → 理想体重；⑤按 NEE 公式求和。`pivoted_infusion` 仅作存在性交叉校验（风险 R5）。
- 输注 episode 规则：短间隙 <30min 合并；重叠记录按 order ID/通路判重（MIMIC `linkorderid`；eICU 无 order ID，按药名+时间连续性）——技术文档 §6.2 实施规则逐条落地。
- **论文 2 标签人工审核拆分（新增）**：月 1 人工审核不仅抽查最终标签，按以下 7 个环节**分别**审核并各自报告一致性：①药物归类；②单位解析；③速率标准化；④episode 合并；⑤`t_stop`；⑥`t_0`；⑦48h 复用事件。eICU 侧难度更高（无可靠 order ID、文本单位、重复输注、药物交接、速率缺失、体重缺失、停药记录与护理断点），eICU 论文 2 标签在 MIMIC 侧双实现核验通过前暂缓实施。

### 5.6 机械通气与氧合支持（DAG 节点 F6）

- MIMIC：`derived.ventilation`（`ventilation_status` 时段）；`oxygen_delivery` 补充 HFNC。
- eICU：`respiratory_care.airwaytype/venttype`、`treatment` 通气路径、`pivoted_o2` 设备与流量。

### 5.7 尿量与液体平衡（DAG 节点 F7）

- MIMIC：`derived.urine_output`（必要时 `outputevents` 补充非尿出量）。
- eICU：`pivoted_uo.urineoutput`；`intake_output` 计算 24h 液体平衡。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC；就诊归属 + 分层 QC 重构）

1. **就诊归属条件（新增，P0-8）**：ECG、临床数据与 sepsis 必须属于同一次住院或可审计连续就诊过程（技术文档 §4.2）。主分析要求：

   ```text
   admittime ≤ t_ecg ≤ min(t_lm, dischtime)
   ```

   每份候选 ECG 输出四态归属：

   ```text
   ecg_encounter_status:
     same_hospitalization                  -- 主分析纳入
     auditable_pre_admission_encounter     -- 主分析纳入（见下）
     uncertain                             -- 仅敏感性分析
     outside_index_encounter               -- 排除
   ```

   入院前 ECG 仅在可审计窗口内允许：与 index admission 同一 ED-to-hospital encounter，或 admission 前不超过预登记小时数且期间无其他就诊间隔；单独打 `pre_admission_ecg = TRUE` 标志。
2. **ECG availability 五层级（新增）**，逐级输出，区分「没做 / 不属本次就诊 / 文件损坏 / 质量差 / 被时间窗淘汰」：

   ```text
   ecg_found_raw            -- subject 级存在任意 ECG
   ecg_same_encounter       -- 通过就诊归属（同上四态前两类）
   ecg_structurally_valid   -- 通过固定结构性 QC
   ecg_pass_frozen_qc       -- 通过冻结的数据驱动 QC 阈值
   ecg_selected_for_model   -- 时效窗内最近一份且通过上述全部层级
   ```

3. **两层 QC（新增）**：
   - **固定结构性 QC**（全部集合统一应用）：文件可读取、时长满足最低要求、采样率与增益可解析、至少规定数量导联、非全平线、无明显文件损坏；
   - **数据驱动 QC**（阈值**仅训练集确定**，验证/测试集使用冻结阈值）：SNR、基线漂移、饱和比例、极端振幅、导联相关性阈值。
4. **时效与选片（不变 + 冻结要求）**：landmark 级 availability＝`∃ ecg_time ≤ t_lm 且 t_lm − ecg_time ≤ 24h`（主分析），48h/72h 敏感性；多份取时效窗内**最近一份**通过 QC 者（主分析），序列编码作敏感性。**主配对队列定义在 QC 完成后、查看测试集结果前冻结**：

   ```text
   ECG available = same encounter ∩ within 24h ∩ structurally valid ∩ pass frozen QC
   ```

5. 患者级 ECG 描述队列：`t_sepsis ± 24h` 内 ≥1 份 ECG（仅描述/可行性，不作纳入条件，不变）。
6. 波形定位：`E:\clinical_research\MIMIC_IV_3.1\ecg\` + `ecg_records.path` + `.hea/.dat`（已抽样验证）。预处理按技术文档 §20 规范（500 Hz、10 s、12 导联标准顺序、训练集拟合归一化参数）。
7. `ecg_measurements`（机测间期/电轴）作为廉价试金石特征与 QC 辅助；`ecg_waveform_features` 为 100 行试点表，不进管线。
8. 防泄漏：`ecg_time` 为采集开始时间（§7.2）；QC 阈值仅训练集确定。

---

## 6. SC-common 跨库变量分层映射总表（v2 重构）

v1.0 的单一候选表**只能称为候选集**（评审 §六.6）。本版按跨库同构程度将变量分四层；每变量仍须补齐技术文档 §8 数据字典字段（时间戳类型、单位转换、异常值范围、聚合规则、缺失定义、数据可用时刻、泄漏风险等级）。

### A 层：高同构变量 → `SC-common-core`（主外验模型候选）

| 临床概念 | MIMIC 来源 | eICU 来源 | 单位 | 泄漏风险 |
|---|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` | 岁 | 低 |
| 性别 | `patients.gender` | `patient.gender` | — | 低 |
| HR | `derived.vitalsign` | `pivoted_vital`/`vital_periodic` | bpm | 低 |
| MAP（有创/无创） | `mbp`/`mbp_ni` | `ibp_mean`/`nibp_mean`/`systemicmean` | mmHg | 低 |
| RR | `resp_rate` | `RespiratoryRate`/`respiration` | /min | 低 |
| SpO2 | `spo2` | `spo2`/`sao2` | % | 低 |
| 体温 | `temperature` | `temperature` | °C（eICU 量纲 QA） | 低 |
| 肌酐 | `labevents` 重建 | `pivoted_lab.creatinine` | mg/dL | 低 |
| 胆红素 | 同上 | `pivoted_lab.bilirubin` | mg/dL | 低 |
| 血小板 | 同上 | `pivoted_lab.platelets` | K/μL | 低 |
| 乳酸 | 同上 | `pivoted_lab.lactate` | mmol/L | 低 |
| WBC | 同上 | `pivoted_lab.wbc` | K/μL | 低 |

### B 层：中等同构变量 → 与 A 层合并构成 `SC-common-extended`

| 临床概念 | MIMIC 来源 | eICU 来源 | 主要差异 | 泄漏风险 |
|---|---|---|---|---|
| GCS | `derived.gcs` | `pivoted_gcs`/`pivoted_score` | 镇静口径（`gcs_unable` 规则） | 中 |
| PaO2/FiO2 | `derived.bg` | `pivoted_bg` | 拼接规则、`fio2_source` 层级 | 中 |
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

首版外验模型默认使用 `SC-common-core`；是否升级为 `extended` 由阶段 D 的同构性核验结果决定（建模前锁定）。感染源（`microbiologyevents`/`micro_lab`）稀疏且结果滞后，不进主模型，仅事后描述与亚组分析（技术文档 §15.3，泄漏风险高）。

---

## 7. 防泄漏与质量控制

### 7.1 防泄漏断言（技术文档 §7.3 十条 → 管线自动测试 Q1，主断言升级）

每次提取后自动运行并输出 `qa/leakage_report.md`：

1. `ecg_time ≤ t_lm`；
2. **全部特征 `available_time ≤ t_lm`（主断言，P0-6）**；同时报告 `event_time ≤ t_lm` 与两者差异分布；
3. 结局窗起点 > t_lm；
4. 同一患者不跨 train/val/test；
5. 同一患者 landmark 不跨 calibration/test；
6–8. 标准化/异常值阈值/插补器仅训练集拟合；
9. 特征筛选仅训练集；
10. ECG 数据驱动 QC 阈值仅训练集确定。

附加断言（沿 v1.0 并修订）：landmark 单调递增且间隔 6h；`k0 ≥ 0`；结局标签三态判定与 `deathtime/hospitaldischargestatus` 双向一致抽查（分层，见 §7.4）；eICU offset→小时换算零误差（分钟整除校验）；**§3.4 全部边界条件单元测试**；`ecg_encounter_status` 四态分布与非空校验；`sofa_cv_original` 与 `nee_current` 字段共存且来源列不同（防混用）。

### 7.2 时间逻辑 QA（新增）

- `admittime ≤ icu_intime < icu_outtime ≤ dischtime` 成立比例；
- `t_sepsis` 相对 ICU 入科的分布；`k0` 分布；t=0 不在 ICU 的患者比例；
- 每个 k 的风险集人数；landmark 后仍在 ICU（episode 内）的验证；
- 每变量 `event_time` 与 `available_time` 差异分布；
- eICU 多 unit stay 时间映射连续性（`eicu_event_time_map` 单调性、episode 间隙分布）。

### 7.3 队列表型 QA（新增）

- **MIMIC 随机抽查**：suspected infection 配对、SOFA ≥2、index episode、连续 episode 合并、`t_sepsis`；
- **eICU 分层抽查**：抗生素识别阳性/阴性、培养识别、配对方向、SOFA 六组分、sepsis time、多 unit stay 时间映射。

### 7.4 结局 QA（分层抽查，替换 v1.0「总体 ≥100 例」）

分层抽样并分别报告一致性：24h 死亡阳性、24h 明确阴性、24h 内存活出院、急性转出、eICU 出院状态缺失、ICU 转出后院内死亡、死亡恰好在边界（`t+24h` 附近）附近。

### 7.5 派生表来源验证（新增，D0 前置）

本地 `mimiciv_derived` 由前期 R 脚本生成，须补齐：SQL/R 文件 checksum、mimic-code commit、DuckDB 版本、生成日期、源表版本、随机样本与原始表回溯验证、行数与主键唯一性、与官方参考输出的统计分布比较（含 §5.4 SOFA 窗口语义 20–50 stay 人工核对）。仅记录表名和行数不满足要求。

### 7.6 ECG 配对 QA（新增）

至少抽查：同一住院内 ECG、入院前 ECG（`auditable_pre_admission_encounter`）、出院后 ECG（应判 `outside_index_encounter`）、多份 ECG 取最近者、landmark 恰好等于 ECG 时间、文件路径与 study_id 一致、header 导联顺序与单位。

### 7.7 常规 QA 输出（Q2/Q3，沿 v1.0）

- 队列流程图计数（每 DAG 节点纳入/排除人数，两库分别；eICU 三套表型分列）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；当前已知原始基线：MIMIC sepsis3 stays 41,295 / subjects 31,910，ECG 总覆盖 161,352 subjects，eICU 全库 200,859 stays / 院内死亡 18,004——队列过滤后的正式数字由管线产出）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集统计）；
- eICU Go/No-Go 门槛检查表（§2.2 C7）。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v2.parquet, cohort_eicu_v2.parquet   # 每 episode 一行
  episodes/  mimic_icu_episode_map.parquet                     # 新增（C0）
             eicu_unitstay_timeline.parquet                    # 新增（C6a）
             eicu_event_time_map.parquet                       # 新增（C6a）
  phenotypes/ eicu_phenotype_tracks_v2.parquet                 # 新增（C7：P-strict/P-clinical/P-explicit）
  splits/    split_assignments_v2.parquet
  landmarks/ landmarks_v2.parquet            # 每 landmark 一行，含风险集标志
  labels/    labels_24h_v2.parquet           # 三态：y_24h, label_status, label_observable,
                                             #        outcome_unknown_reason, label_reason
             labels_competing_7d_v2.parquet  # event_type 0/1/2/3 + event_time
  features/  static_v2.parquet               # 每 episode 一行（含 charlson_prior / charlson_discharge_coded）
             vitals_hourly_v2.parquet        # episode × landmark × 变量 × 小时（长表 + mask + Δt
                                             #   + event_time/available_time/source_time_type + source_table）
             labs_hourly_v2.parquet          # 同上；检验含 storetime/revisedoffset 口径与 fio2_source
             sofa_hourly_v2.parquet          # 含 sofa_cv_original（两库同构经典口径）
             nee_stream_v2.parquet           # episode × 时间（5min 网格）NEE；
                                             #   含 nee_project_formula / nee_mimic_derived /
                                             #       nee_difference / nee_source_drug_components
  ecg_index/ ecg_landmark_index_v2.parquet   # landmark × 最近合格 ECG（study_id, ecg_time, path, 时效,
                                             #   ecg_encounter_status, 五层级 availability 标志）
  qa/        cohort_flow_v2.md, feasibility_table_v2.md, leakage_report_v2.md,
             time_logic_qa_v2.md, phenotype_qa_v2.md, outcome_stratified_qa_v2.md,
             ecg_pairing_qa_v2.md, derived_provenance_v2.md, eicu_go_nogo_v2.md
  _meta/     code_version.json               # mimic-code 版本/commit、SQL/R checksum、本地修改清单、
                                             # DuckDB 版本、提取时间；D0 决策记录（d0_decision.json）
```

规范：①统一 Parquet（DuckDB 原生写出）；②所有表携带 `subject_key / episode_key / landmark_k` 三级键，eICU 侧键为 `uniquepid / episode_id(patienthealthsystemstayid 内) / k`，原始 `stay_id / patientunitstayid` 保留用于溯源；③患者级 ID 管理与划分表冻结后不得重算；④每个 DAG 节点独立脚本、I/O schema 校验、中间产物持久化（技术文档 §19.1）；⑤`code_version.json` 记录 mimic-code 版本与 commit hash、SQL/R checksum——**阶段 A 补齐，当前为待办占位**；⑥D0 锁定结论（出口 A/B、依据、日期、PI 确认）写入 `_meta/d0_decision.json`。

---

## 9. 已识别风险与待决事项（R1–R18）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| R1 | 本地 `sepsis3` 表无 `sepsis_time` 字段 | 主分析时间原点/estimand | **决策门 D0（§3.1）**：阶段 A 完成派生脚本审计后 PI 锁定出口 A/B；锁定前仅可行性提取，禁止正式训练与测试集评估 |
| R2 | `anchor_year_group` 含 `2020 - 2022`（49,173 患者），v1.9 未规定 | 时间划分 | 主分析不用；阶段 A 形成正式 amendment，明确排除理由与不查看结局/性能（§2.4） |
| R3 | eICU 无 Sepsis-3 派生表，`micro_lab` 仅 2,923 患者有培养 | 外验队列表型 | C6b 方向性配对 + C7 三套可行性队列 + Go/No-Go 门槛；默认命名 Robustness under phenotype shift（§2.2） |
| R4 | eICU SOFA 需自建，GCS 镇静口径差异 | SOFA 可比性 | F4 口径对齐；差异预登记；窗口语义人工核对（§5.4/§7.5） |
| R5 | eICU 输注速率为内嵌单位的文本字段，`pivoted_infusion` 无剂量 | NEE/论文 2 标签 | F5 解析管线；与临床药师核对规则（技术文档 §17.6）；人工审核 7 环节拆分（§5.5） |
| R6 | 库内并存 SOFA-1（`sofa`）与 SOFA-2（`sofa2_*`） | 误用风险 | 仅用 SOFA-1；命名检查进 Q1 |
| R7 | 遗留/试点表（`test_*`、`tmp_*`、`crab_modeling_cohort`、`ecg_waveform_features` 等） | 误用风险 | 白名单制，未列入本方案的表一律不用 |
| R8 | eICU 无 ECG，availability 与库来源共线 | 门控外推 | 按 v1.9 §11.1：eICU 仅走 SC-common-all 独立路径 |
| R9 | 转急性医疗机构类别字符串两库不一致 | 结局 unknown 标记 | QA 实测类别清单后预登记（§4.1） |
| R10 | 体重缺失/极端值（<40 / >150 kg） | NEE 与论文 2 标签 | 技术文档 §6.2 规则；缺失体重仅进敏感性分析；landmark 截断（§5.1） |
| R11 | `sepsis3` 表静态 SOFA 组分被误用作 landmark 特征 | 泄漏/口径错误 | 禁用；landmark SOFA 一律取 `derived.sofa` 小时表（§5.4） |
| **R12** | **检验 `charttime` 早于结果可用时间导致实时信息泄漏** | 主分析时间语义 | §5.0 数据可用时间契约；关键检验从 `labevents` 重建保留 `storetime`；不可用时声明「按测量时间的回顾性预测」 |
| **R13** | **ECG 仅按 subject/time 配对可能跨住院** | ECG-EHR 配对正确性 | §5.8 就诊归属条件 + `ecg_encounter_status` 四态；`uncertain` 仅敏感性 |
| **R14** | **eICU 多 unit stay offset 坐标不一致** | 连续 episode 时间正确性 | C6a 住院级统一坐标 + 两张桥接表；合并前一律换算（§2.2） |
| **R15** | **使用 NEE 替代经典 SOFA 心血管评分** | 表型/亚组可比性 | §5.4 经典阈值；`sofa_cv_original`/`nee_current`/`vasopressor_burden` 分离；Q1 字段共存检查 |
| **R16** | **Charlson 派生表包含本次住院最终 ICD** | 静态特征泄漏 | 重建 `charlson_prior`（既往住院 ICD）；`charlson_discharge_coded` 仅敏感性；移出 SC-common（§5.1/§6） |
| **R17** | **标签 SQL 将未知结局误编码为阴性** | 标签正确性 | §4.1 三态标签 + `label_observable`/`outcome_unknown_reason`；eICU 出院状态缺失按 24h 可观测性判定 |
| **R18** | **eICU 培养覆盖过低导致严重 phenotype selection** | 外验有效性 | C7 三套队列对比 + Go/No-Go 门槛；命名建模前锁定（§2.2） |

---

## 10. 实施顺序（阶段 A–D，替换 v1.0 线性 6 步）

**阶段 A：协议与来源锁定（冻结前置，本阶段结束前不查看验证/测试集性能差异）**

1. 核对 mimic-code commit、本地派生 SQL/R 脚本与 checksum（§7.5）；
2. 解决 `sepsis_time`：完成 D0 审计并由 PI 锁定出口 A/B（§3.1）；
3. 正式处理 `2020 - 2022`：amendment 或技术文档修订（§2.4）；
4. 锁定连续 ICU episode 定义（C0/C6a：合并规则、间隙阈值、主键）；
5. 锁定数据可用时间语义（§5.0 契约表 + 各域 `source_time_type`）；
6. 锁定经典 SOFA 与 NEE 的独立定义（§5.4/§5.5）。

**阶段 B：仅做 MIMIC 可行性队列（可在 D0 候选口径下并行两套，但不冻结）**

1. episode 映射（C0）；2. index episode（C1–C3）；3. landmark（L1）；4. 三态标签（L3）；5. ECG 就诊归属与 availability 五层级（F8）；6. 输出主要 12 个 landmark 的患者数、阳性数、ECG 覆盖率——**决定 ECG 主要比较是否有足够事件数**（技术文档 §9.1 Go 条件）。

**阶段 C：MIMIC 特征与论文 2 标签**

1. 按 available_time 提取特征（F1–F7，关键检验原始重建）；2. 重建 `charlson_prior`；3. ECG 两层 QC 与阈值冻结；4. NEE 双实现核验；5. 论文 2 人工标签验证（7 环节拆分，PPV >80% 为 Go 条件）。

**阶段 D：eICU 外验可行性**

1. 统一住院时间坐标（C6a）；2. 构建三套 sepsis phenotype（C7）；3. 严格复现经典 SOFA（F4）；4. 评估各医院数据覆盖；5. 锁定 `SC-common-core`（或 extended）终稿；6. 决定外验命名与层级（§2.2 C7）。

---

## 11. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：基于两库 DuckDB 实测结构核验的可实施提取方案（DAG、输出规范、风险 R1–R11、SQL 模板） |
| v2.0 | 2026-07-30 | **外部评审后全面修订**——①设立 D0 决策门，`t_sepsis` 撤销静默替代、恢复未锁定状态（P0-1）；②eICU suspected infection 改方向性配对 + 给药时间字段优先级（P0-2）；③新增 eICU 三套可行性表型队列与 Go/No-Go 门槛，外验默认命名 Robustness under phenotype shift（P0-3）；④新增 eICU 住院级统一时间坐标与两张桥接表（P0-4）；⑤新增 MIMIC `mimic_icu_episode_map`，下游一律按 episode 聚合（P0-5）；⑥新增数据可用时间契约，主断言升级为 `available_time ≤ t_lm`，关键检验原始重建（P0-6）；⑦eICU 心血管 SOFA 恢复经典阈值，`sofa_cv_original`/`nee_current`/`vasopressor_burden` 分离（P0-7）；⑧ECG 新增就诊归属四态、五层级 availability、两层 QC、主配对定义冻结点（P0-8）；⑨结局标签三态化 + 急性转出竞争事件 + 边界条件单元测试；⑩SC-common 分 core/extended/MIMIC-only/不纳入四层，Charlson 移出并重建 `charlson_prior`；⑪QA 新增时间逻辑/表型/ECG 配对/结局分层/派生来源验证；⑫风险清单扩至 R18；⑬实施顺序改阶段 A–D；⑭SQL 模板修正（稳定 interval、显式 TIMESTAMP、三态标签、概念性标注）；⑮§2.4 修正全库人数表述并要求 `2020-2022` 正式 amendment。 |

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

> **说明**：A.1/A.4/A.5 为**概念性模板**，用于固定逻辑与边界语义，不构成 C0–C10 的完整实现；正式实施以各 DAG 节点脚本及 I/O schema 校验为准。

### A.0 MIMIC 连续 ICU episode 映射（C0，概念性）

```sql
-- 将同一 hadm_id 内相邻 ICU 转科合并为连续 episode
WITH icu_moves AS (
  SELECT subject_id, hadm_id, careunit, intime, outtime,
         LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime) AS prev_outtime
  FROM main.transfers
  WHERE eventtype <> 'discharge' AND careunit IS NOT NULL
    -- careunit 限 ICU 类别清单（QA 实测后预登记）
),
grp AS (
  SELECT *,
         SUM(CASE WHEN prev_outtime IS NULL OR intime > prev_outtime THEN 1 ELSE 0 END)
           OVER (PARTITION BY hadm_id ORDER BY intime) AS episode_grp
  FROM icu_moves
)
SELECT subject_id, hadm_id,
       hadm_id::VARCHAR || '_EP' || episode_grp::VARCHAR      AS episode_id,
       MIN(intime)  AS episode_intime,
       MAX(outtime) AS episode_outtime
FROM grp
GROUP BY subject_id, hadm_id, episode_grp;
-- 与 icustays 按 [intime, outtime] 区间匹配回填 stay_id 与 stay_seq_in_episode
```

### A.1 MIMIC 队列骨架（C1–C3，概念性；确定性排序）

```sql
WITH sepsis AS (
  SELECT subject_id, stay_id, suspected_infection_time AS t_sepsis   -- D0 锁定后替换
  FROM mimiciv_derived.sepsis3
  WHERE sepsis3
),
eligible AS (
  SELECT s.subject_id, d.hadm_id, s.stay_id, s.t_sepsis,
         e.episode_id, e.episode_intime, e.episode_outtime,
         a.admittime, d.admission_age
  FROM sepsis s
  JOIN mimiciv_derived.icustay_detail d USING (subject_id, stay_id)
  JOIN mimic_icu_episode_map e      USING (subject_id, hadm_id, stay_id)
  JOIN main.admissions a            USING (hadm_id)
  WHERE d.admission_age >= 18
    -- 成人 ICU 类别清单（QA 实测后预登记）
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY subject_id
           ORDER BY t_sepsis, admittime, episode_intime, stay_id   -- 完全确定性
         ) AS rn
  FROM eligible
)
SELECT * FROM ranked WHERE rn = 1;   -- 首次合格 sepsis-associated episode
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
-- 另需显式处理：icu_outtime IS NULL、deathtime IS NULL、
-- 时间倒置（outtime < intime）、出院后死亡时间异常 → 打标进 QA（§7.2）
```

### A.3 三态 24h 标签（MIMIC；含 label_reason）

```sql
SELECT l.episode_id, l.k, l.t_lm,
  CASE
    WHEN a.deathtime >  l.t_lm
     AND a.deathtime <= l.t_lm + INTERVAL '24 hours'
      THEN 1                                            -- event
    WHEN tr.acute_transfer_time >  l.t_lm
     AND tr.acute_transfer_time <= l.t_lm + INTERVAL '24 hours'
      THEN NULL                                         -- unknown: acute_transfer
    WHEN COALESCE(a.dischtime, TIMESTAMP '9999-01-01 00:00:00')
         >= l.t_lm + INTERVAL '24 hours'
      THEN 0                                            -- 观察期完整覆盖窗口
    WHEN tr.alive_discharge_time >  l.t_lm
     AND tr.alive_discharge_time <= l.t_lm + INTERVAL '24 hours'
      THEN 0                                            -- non_event: 存活出院
    ELSE NULL                                           -- unknown
  END AS y_24h,
  CASE
    WHEN a.deathtime >  l.t_lm
     AND a.deathtime <= l.t_lm + INTERVAL '24 hours' THEN 'event'
    WHEN tr.acute_transfer_time >  l.t_lm
     AND tr.acute_transfer_time <= l.t_lm + INTERVAL '24 hours' THEN 'unknown'
    WHEN COALESCE(a.dischtime, TIMESTAMP '9999-01-01 00:00:00')
         >= l.t_lm + INTERVAL '24 hours' THEN 'non_event'
    WHEN tr.alive_discharge_time >  l.t_lm
     AND tr.alive_discharge_time <= l.t_lm + INTERVAL '24 hours' THEN 'non_event'
    ELSE 'unknown'
  END AS label_status
  -- 同步输出 label_observable / outcome_unknown_reason / label_reason（§4.1）
FROM landmarks_v2 l
JOIN cohort_mimic_v2 c USING (episode_id)
JOIN main.admissions a USING (hadm_id)
LEFT JOIN discharge_disposition tr USING (hadm_id);
-- tr: discharge_location 类别清单解析（QA 实测后预登记，风险 R9）
```

### A.4 eICU 住院级时间坐标换算（C6a，概念性）

```sql
SELECT patientunitstayid, patienthealthsystemstayid, uniquepid,
       -hospitaladmitoffset                       AS unit_start_hospital_min,
       -hospitaladmitoffset + unitdischargeoffset AS unit_end_hospital_min,
       -hospitaladmitoffset                       AS hospital_admit_zero_ref
FROM main.patient;
-- 任意事件：hospital_offset_min = -hospitaladmitoffset + event_offset_min
-- episode 合并：同一 patienthealthsystemstayid 内按 unit_start_hospital_min 排序，
-- 相邻间隙 ≤ 预登记阈值者并入同一 episode_id，生成 episode_offset_min（episode 起点为 0）
```

### A.5 eICU 方向性 suspected infection 配对（C6b，概念性）

```sql
WITH ab AS (
  SELECT patientunitstayid, drugstartoffset AS ab_time   -- 优先级：实际给药 > drugstartoffset > drugorderoffset
  FROM main.medication
  WHERE drugname ILIKE ANY (SELECT pattern FROM preregistered_antibiotics)  -- 清单预登记
),
cx AS (
  SELECT patientunitstayid, culturetakenoffset AS cx_time
  FROM main.micro_lab
)
SELECT a.patientunitstayid, a.ab_time, c.cx_time
FROM ab a JOIN cx c USING (patientunitstayid)
WHERE (a.ab_time - c.cx_time) BETWEEN 0 AND 4320   -- 培养先：72h 内首剂抗生素
   OR (c.cx_time - a.ab_time) BETWEEN 0 AND 1440   -- 抗生素先：24h 内培养
-- 窗口数值以阶段 A 锁定版 mimic-code 为准；配对后在 episode 坐标上取 suspected_infection_offset
```

### A.6 eICU 去甲肾上腺素速率解析（片段，沿 v1.0）

```sql
SELECT patientunitstayid, infusionoffset,
       TRY_CAST(drugrate AS DOUBLE) AS rate_value,
       REGEXP_EXTRACT(drugname, '\(([^)]*)\)', 1) AS unit_hint   -- 如 mcg/min、mg/hr
FROM main.infusion_drug
WHERE drugname ILIKE 'Norepinephrine%';
-- 后续：unit_hint → μg/kg/min 换算 × 体重优先级（F5）；双实现核验字段见 §5.5
```

---

*本方案 v2.0 基于 2026-07-30 对两库的只读结构核验与同日外部评审《总体评价》生成；与技术文档 v1.9 冲突之处以技术文档为准，需变更技术文档的事项（D0 出口 B、`2020-2022` 处理）须经 protocol amendment 正式登记。*
