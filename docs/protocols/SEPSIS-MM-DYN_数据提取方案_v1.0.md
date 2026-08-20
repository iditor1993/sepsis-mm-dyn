# SEPSIS-MM-DYN 数据提取方案 v1.0

- 文档版本：v1.0
- 创建日期：2026-07-30
- 上游依据：《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（本地 DuckDB）、eICU-CRD v2.0（本地 DuckDB）
- 维护方式：与技术文档同库 Git 版本管理；每次数据源、字段口径或流程变更递增版本号
- 状态：**基于两库 DuckDB 实际表结构只读核验（2026-07-30）后的首个可实施版本**。文中所有表名、字段名、行数均为实测值，非官方文档转述。

---

## 0. 本版要点与实测结论

1. 两个数据库均以 DuckDB 单文件形式本地部署，已通过只读连接完成结构核验。MIMIC 库含 2 个 schema（`main` 原始模块 + `mimiciv_derived` 派生概念）共 144 张表；eICU 库含 1 个 schema（`main`）共 53 张表（30 原始表 + 17 pivoted 衍生表 + 6 张其他）。
2. **MIMIC 库已内置 mimic-code 派生表**：`mimiciv_derived.sepsis3`（41,295 行，全部 `sepsis3 = TRUE`；41,295 个 stay_id、31,910 个 subject_id）、`suspicion_of_infection`、小时级 `sofa`、`vasoactive_agent`、`norepinephrine_equivalent_dose`、`vitalsign`、`gcs`、`charlson`、`icustay_detail`、`patient_outcomes` 等。论文 1 队列与临床特征以派生表为主数据源，避免从 `chartevents`（约 4.33 亿行）/`labevents`（约 1.58 亿行）重复派生。
3. **实测发现本地 `sepsis3` 表不含技术文档 §4.1 所述 `sepsis_time` 字段**。实有列：`subject_id, stay_id, antibiotic_time, culture_time, suspected_infection_time, sofa_time, sofa_score, respiration, coagulation, liver, cardiovascular, cns, renal, sepsis3`。本方案将 sepsis index time 操作性定义为 **`suspected_infection_time`（仅 `sepsis3 = TRUE` 记录）**，作为技术文档附件 A「DuckDB 方言迁移修改清单」第 1 项登记，并需核对本地派生表所用 mimic-code 版本与 commit hash（见风险 R1）。
4. **MIMIC 库已集成 MIMIC-IV-ECG 索引**：`main.ecg_records`（800,035 份 ECG / 161,352 名患者；列：`subject_id, study_id, file_name, ecg_time, path`）。`path` 为 WFDB 相对路径（如 `files/p1000/p10000032/s40689238/40689238`），波形实体位于 `E:\clinical_research\MIMIC_IV_3.1\ecg\` 下（已抽样确认 `.hea/.dat` 存在）。`ecg_time` 即采集时间，直接满足技术文档 §7.2 的时间语义要求，ECG-EHR 配对可在库内完成。
5. **eICU 库无 sepsis3 / SOFA 派生表**，且时间体系为「相对 ICU 入科的分钟偏移（offset）」，与 MIMIC 的绝对（年份偏移）时间戳语义完全不同。eICU 的 Sepsis-3 表型、SOFA 组分、landmark 序列均需在提取管线内自建（第 2.2、5.4 节）。实测 `micro_lab` 仅 16,996 行 / 2,923 名患者有培养记录，suspected infection 重建将主要依赖抗生素使用时序与诊断路径——属技术文档 §8.1 要求预登记的不等价项。
6. 实测 `patients.anchor_year_group` 存在 **5 个类别**：`2008 - 2010`（101,607）、`2011 - 2013`（76,266）、`2014 - 2016`（71,640）、`2017 - 2019`（65,941）、**`2020 - 2022`（49,173，技术文档 v1.9 §12.2 预设映射未覆盖）**。处理见第 2.4 节。
7. eICU 血管活性药剂量为高风险提取项：`infusion_drug` 的药名内嵌浓度与单位（实测如 `Norepinephrine (mcg/min)`、`(mg/min)`、`(units/min)`、`(mg/kg/min)`、`(mg/hr)` 及 `STD 32 mg Dextrose 5% 500 ml` 等剂型描述），速率为 VARCHAR 文本，需解析、单位换算与体重标准化；`pivoted_infusion` 仅为 0/1 使用标记，**不能**用于剂量重建（第 5.5 节）。
8. 两库均存在与本项目无关的遗留/试点表（MIMIC：`test_*`、`tmp_*`、`_tmp_cv_mbp`、`crab_modeling_cohort`、`ecg_waveform_features`（仅 100 行试点）；eICU：`gcsfirstday`（0 行）、`uofirstday`（74 行）），一律不进入提取管线（风险 R7）。

---

## 1. 数据源与本地部署核验

### 1.1 数据库文件清单（实测）

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
| 脓毒症表型 | `mimiciv_derived.sepsis3` | 每 stay 一行；`suspected_infection_time, sofa_time, sofa_score, 六组分` | 41,295 |
| 疑似感染 | `mimiciv_derived.suspicion_of_infection` | 每次抗生素-培养配对一行 | 949,901 |
| SOFA（小时级） | `mimiciv_derived.sofa` | `stay_id, starttime/endtime(1h), 组分输入 + 24h 滑动组分` | 8,219,121 |
| 生命体征 | `mimiciv_derived.vitalsign` | `stay_id, charttime`；HR/SBP/DBP/MBP（有创与无创分列）/RR/Temp/SpO2/Glucose | 13,519,533 |
| GCS | `mimiciv_derived.gcs` | `charttime, gcs, gcs_motor/verbal/eyes, gcs_unable` | 2,217,787 |
| 血管活性药 | `mimiciv_derived.vasoactive_agent` | `stay_id, starttime, endtime, 7 药速率列` | 839,543 |
| NEE | `mimiciv_derived.norepinephrine_equivalent_dose` | `stay_id, starttime, endtime, norepinephrine_equivalent_dose` | 783,613 |
| 单药输注 | `mimiciv_derived.{norepinephrine, epinephrine, dopamine, phenylephrine, vasopressin, dobutamine, milrinone}` | `stay_id, linkorderid, vaso_rate, vaso_amount, starttime, endtime` | — |
| 通气 | `mimiciv_derived.ventilation` | `stay_id, starttime, endtime, ventilation_status` | 144,812 |
| 检验（宽表） | `mimiciv_derived.{bg, chemistry, coagulation, complete_blood_count, cardiac_marker, blood_differential, enzyme, inflammation}` | `stay_id/hadm_id + charttime + 项目列` | — |
| 检验（原始） | `main.labevents` + `main.d_labitems` | `itemid, charttime, storetime, valuenum, valueuom` | 158,374,764 |
| 微生物 | `main.microbiologyevents` | `charttime/chartdate, spec_type_desc, org_name, interpretation` | 3,988,224 |
| 合并症 | `mimiciv_derived.charlson` | `hadm_id`；17 组分 + `charlson_comorbidity_index` | 546,028 |
| 体重/身高 | `mimiciv_derived.weight_durations`、`mimiciv_derived.height` | 时段体重；身高 | 401,850 / 43,342 |
| 尿量 | `mimiciv_derived.urine_output` | `stay_id, charttime, urineoutput` | 4,127,634 |
| ICU 汇总 | `mimiciv_derived.icustay_detail` | `stay_id`；年龄、性别、入出院时间、结局、序次 | 94,458 |
| 结局汇总 | `mimiciv_derived.patient_outcomes` | `stay_id`；死亡、SOFA/SOFA-2、通气、RRT、血管活性药时长等 73 列 | 94,458 |
| ECG 索引 | `main.ecg_records` | `subject_id, study_id, ecg_time, path` | 800,035 |
| ECG 机测 | `main.ecg_measurements` | `study_id, ecg_time, RR/间期/电轴` | 800,035 |

> 注：`mimiciv_derived` 同时含 SOFA-2 系列表（`sofa2_*`）。本项目 Sepsis-3 与亚组均基于 **SOFA-1**，禁止混用（风险 R6）。本地派生表由前期 R 脚本生成，其 mimic-code 版本与 commit hash 须在提取启动前补齐登记（技术文档附件 A 占位）。

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
| 微生物 | `main.micro_lab` | `culturetakenoffset, culturesite, organism` | 16,996（仅 2,923 患者） |
| 尿量 | `main.pivoted_uo` | `chartoffset, urineoutput` | 4,088,881 |
| 体重 | `main.pivoted_weight` | `chartoffset, source_table, weight_type, weight` | 501,506 |
| 诊断 | `main.diagnosis` / `main.admission_dx` | `diagnosisoffset, diagnosisstring, icd9code` / `admitdxpath` | 2,710,672 / 626,858 |
| 既往史 | `main.past_history` | `pasthistoryoffset, pasthistorypath, pasthistoryvalue` | 1,149,180 |
| 治疗 | `main.treatment` | `treatmentoffset, treatmentstring`（含机械通气、RRT、ECMO 等路径） | 3,688,745 |
| 氧疗 | `main.pivoted_o2` | `chartoffset, o2_flow, o2_device` | 3,090,312 |
| 呼吸 | `main.respiratory_care` / `main.respiratory_charting` | 气道类型、通气参数；`respchartoffset` 呼吸记录 | 865,381 / 20,168,176 |
| APACHE | `main.apache_aps_var` / `apache_pred_var` / `apache_patient_result` | 首日 APS 输入、预测变量、评分结果 | 171,177 / 171,177 / 297,064 |
| 护理记录 | `main.nurse_charting` | `nursingchartoffset / nursingchartentryoffset`；长表 | 151,604,232 |

eICU 时间体系：全部时间为相对 ICU 入科的分钟偏移（offset），`patient.hospitaladmitoffset` 通常为负值；出院年份仅 2014/2015（实测 95,513 / 105,346），**无绝对日期**。

---

## 2. 队列构建（Cohort）

### 2.1 MIMIC-IV 队列流程（DAG 节点 C1–C5）

- **C1 脓毒症相关 ICU stay 池**：`mimiciv_derived.sepsis3`（`sepsis3 = TRUE`，实测 41,295 stays / 31,910 subjects）⨝ `mimiciv_derived.icustay_detail`（取 `hadm_id, icu_intime, icu_outtime, admission_age, first_icu_stay`）⨝ `main.icustays`（`first_careunit`）。
- **C2 入排初筛**：年龄 ≥18（`icustay_detail.admission_age`）；成人 ICU（`first_careunit` 排除 NICU 等非成人单元，类别清单以 QA 实测为准）。
- **C3 index stay 选择**（技术文档 §4.2 层级规则）：同一 `subject_id` 存在多个 sepsis-associated ICU stays 时，按 `suspected_infection_time` 升序取**首次**满足定义且与目标成人 ICU stay 时间重叠者；其余 stay 不进入主分析。同院 ICU 间转移按连续 ICU episode 规则合并（`main.transfers` 中相邻 ICU careunit 记录 intime/outtime 连续者合并为一个 episode，episode 起点为首个 ICU intime）。
- **C4 敏感性排除标志（不排除，仅打标）**：外院转入（`admissions.admission_location` 含 Transfer 类，类别清单 QA 实测）；首个有效 landmark 前已存在 ECMO（`main.procedureevents` itemid QA 确认 + `mimiciv_derived.patient_outcomes` 无该字段，需从 procedureevents 识别）、近 90 天实体器官移植（`diagnoses_icd`/`procedures_icd` ICD 清单，预登记）；首个有效 landmark 前 DNR/CCO（`main.poe`/`code status` 相关 chartevents itemid，预登记清单）。所有标志**仅使用首个有效 landmark 时点之前的记录**，禁止追溯性排除。
- **C5 队列事实表** `cohort_mimic_v1`（每 stay 一行）：`subject_id, hadm_id, stay_id, t_sepsis(=suspected_infection_time), icu_intime, icu_outtime, admittime, dischtime, deathtime, admission_age, gender, anchor_year_group, first_careunit, hospstay_seq, 敏感性标志若干`。

产出规模预估：C1 后直接可得；C2–C3 后的最终队列患者数、landmark 数与事件数进入月 1 Feasibility Table（技术文档 §9.1）。

### 2.2 eICU-CRD 队列流程（DAG 节点 C6–C9）

eICU 无现成 Sepsis-3 派生表，按技术文档 §8.1「Robustness under phenotype shift」层级自建：

- **C6 suspected infection 重建**（offset 空间，单位：分钟）：抗生素使用时点（`medication` 中抗菌药物清单，药名/HICL 预登记）与培养采样时点（`micro_lab.culturetakenoffset`，注意仅 2,923 患者有记录）按 mimic-code 时间窗规则（培养 ±24h 内首剂抗生素，或抗生素 ±72h 内培养）配对，取 `suspected_infection_offset`。规则与 MIMIC 侧 `suspicion_of_infection` 的逻辑逐条对照，差异写入预登记差异表。
- **C7 SOFA 自建**（见 5.4）：以 ICU 入科为 0 点的滚动 24h 窗口计算六组分，SOFA ≥2 判定脓毒症；`t_sepsis_offset = suspected_infection_offset`。
- **C8 入排与 index stay**：年龄 ≥18（`patient.age` 数值化，`"> 89"` 记 90 并打标）；同一 `uniquepid` 取首次符合条件的 `patientunitstayid`；`unitstaytype = 'readmit'` 且属于同一连续 episode 者按 episode 合并规则处理。
- **C9 队列事实表** `cohort_eicu_v1`（字段与 C5 同构，时间列全部为 offset 分钟）：`patientunitstayid, patienthealthsystemstayid, uniquepid, t_sepsis_offset, unitadmit_offset(=0), unitdischargeoffset, hospitaladmitoffset, hospitaldischargeoffset, hospitaldischargestatus, age_num, gender, unittype, hospitalid, 敏感性标志`。

### 2.3 两库队列字段同构约定

两库队列事实表输出**同名同义列**；时间列分两套命名：MIMIC 为 `*_ts`（TIMESTAMP，年份偏移），eICU 为 `*_offset_min`（INTEGER 分钟）。所有下游节点按「相对 t_sepsis 的小时差」对齐，禁止直接比较两库原始时间列。

### 2.4 内部时间划分（技术文档 §12.2 落地）

实测 `anchor_year_group` 为 5 类。按 v1.9 预设映射的自然延伸固定为：

| 集合 | anchor_year_group | 实测患者数 |
|---|---|---|
| 训练集 | `2008 - 2010`、`2011 - 2013` | 177,873（全库口径） |
| 验证集 | `2014 - 2016` | 71,640 |
| 测试集 | `2017 - 2019` | 65,941 |
| **不进入主分析** | `2020 - 2022` | 49,173 |

`2020 - 2022` 组为 v1.9 未预期类别：主分析不使用（与 v1.9「删除 COVID-era 分析」一致），保留为潜在扩展/敏感性数据，该处理须在提取启动前由 PI 确认并登记（风险 R2）。划分按 `subject_id` 归入，同一患者所有 landmark 同属一个集合；患者级随机划分仅作敏感性分析。划分表 `split_assignments_v1`（`subject_id, set_name`）落盘冻结。

---

## 3. 时间原点与 Landmark 序列

### 3.1 Sepsis index time

- MIMIC：`t_sepsis = sepsis3.suspected_infection_time`（见 §0-3 与风险 R1）。
- eICU：`t_sepsis_offset = suspected_infection_offset`（C6 输出）。
- `Δ_ICU-sepsis = ICU 入科时间 − t_sepsis`（MIMIC 直接相减；eICU 为 `0 − t_sepsis_offset`），作为显式输入特征（技术文档 §4.4）。

### 3.2 Landmark 生成（DAG 节点 L1）

对每个 index stay：

1. `k0 = max(0, ceil((t_ICU − t_sepsis) / 6h))`；eICU 为 `k0 = max(0, ceil((0 − t_sepsis_offset) / 360min))`。
2. landmark 序列 `t_lm(k) = t_sepsis + 6h·k`，`k ∈ [k0, 27]`（[0h, 168h) 半开区间，最多 28 个）。
3. 终止规则：`t_lm(k) < min(ICU 出科时间, 死亡时间)`；ICU 转出至病房后停止生成新 landmark，但已生成 landmark 的 24h 结局随访继续完成（技术文档 §4.2）。
4. 主分析积分网格固定为 `k ∈ [0, 11]`（[0h, 72h)）；72–168h 仅次要/探索。

输出 `landmarks_v1`（每 landmark 一行）：`stay_key, subject_key, k, t_lm, hours_since_sepsis, in_risk_set(bool)`。

### 3.3 风险集（DAG 节点 L2）

landmark t 纳入条件：t 时刻存活且在 ICU。排除：t 前已死亡（MIMIC `admissions.deathtime ≤ t`；eICU `Expired 且 hospitaldischargeoffset ≤ t_offset`）或已出 ICU（`icu_outtime/unitdischargeoffset ≤ t`）。

---

## 4. 结局标签（DAG 节点 L3）

### 4.1 主结局：landmark 后 24h 院内全因死亡

- MIMIC：`deathtime ∈ (t_lm, t_lm + 24h]` → y = 1；否则 y = 0。`admissions.deathtime` 仅记录院内死亡（`hospital_expire_flag = 1` 互查）；`patients.dod` 为日期精度，仅用于 1 年死亡等辅助分析，不进主结局。
- eICU：`hospitaldischargestatus = 'Expired' 且 hospitaldischargeoffset ∈ (t_lm_offset, t_lm_offset + 1440]` → y = 1。实测 `hospitaldischargestatus`：Alive 181,104 / Expired 18,004 / NULL 1,751；NULL 者的院内结局按「出院状态缺失」打标并在 QA 报告，不进主结局阳性。
- 24h 内存活出院：y = 0（技术文档 §2.1）。
- 预测窗内转至其他急性医疗机构：标记 `outcome_unknown = 1`，不编码为阴性，从该 landmark 主分析排除（敏感性分析分别按存活离院与最坏情景编码）。MIMIC 依据 `discharge_location`、eICU 依据 `hospitaldischargelocation` 的转出类别清单——两库类别字符串不同，清单在 QA 步骤实测后预登记（风险 R9）。

### 4.2 次要结局：7 天竞争风险

同一 landmark 输出三分类事件与事件时间：死亡 / 存活出院 / 行政删失（t_lm + 168h），供 DeepHit CIF 使用；eICU 侧存活出院时间 = `hospitaldischargeoffset`。

### 4.3 辅助结局（探索性）

24h 内 SOFA 恶化（组分总分增加 ≥2，基于 5.4 小时级 SOFA）、新启用血管活性药（5.5 NEE 流由 0 转 >0）。

---

## 5. 特征提取模块

统一时间语义（技术文档 §15.3）：生命体征与检验映射至 **landmark 前 24h 的 1h 时间网格**，同小时多条取**中位数**，无记录保留缺失并生成观测 mask 与距上次观测间隔（GRU-D 输入三元组）。t=0 landmark 允许使用 sepsis onset 前数据。所有特征必须满足 `feature_time ≤ t_lm`（charttime 口径；storetime/entryoffset 口径作为「数据可用时刻」敏感性分析，技术文档 §8）。

### 5.1 静态特征（DAG 节点 F1）

| 特征 | MIMIC 来源 | eICU 来源 | 备注 |
|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` 数值化（`"> 89"`→90 并打标） | 岁 |
| 性别 | `patients.gender` | `patient.gender` | 类别对齐 M/F/Other |
| 体重 | `weight_durations`（输注前最近者） | `pivoted_weight` + `admissionweight` | kg；优先级见技术文档 §6.2 体重规则 |
| 身高 | `height` / `omr` | `admissionheight` | cm |
| 入院类型/来源 | `admissions.admission_type, admission_location` | `hospitaladmitsource, unitadmitsource` | 类别映射表预登记 |
| ICU 类型 | `icustays.first_careunit` | `patient.unittype` | 实测 eICU 以 Med-Surg ICU 为主（113,222） |
| Δ_ICU-sepsis | 计算列 | 计算列 | 小时 |
| Charlson 合并症 | `mimiciv_derived.charlson`（**仅用入院时可得口径**：既往住院 ICD；本次住院最终 ICD 版仅敏感性） | `past_history` 路径映射自建近似（无既往住院 ICD，差异预登记） | 技术文档 §15.3 |

### 5.2 生命体征时序（DAG 节点 F2）

| 变量 | MIMIC 来源 | eICU 来源（优先级） | 目标单位 |
|---|---|---|---|
| HR | `derived.vitalsign.heart_rate` | `pivoted_vital.heartrate` → `vital_periodic.heartrate` | bpm |
| SBP/DBP/MAP | `sbp/dbp/mbp`（有创）与 `*_ni`（无创），有创优先 | `pivoted_vital.ibp_*` 优先，次 `nibp_*`；`vital_periodic.systemic*`、`vital_aperiodic` 补充 | mmHg |
| RR | `resp_rate` | `pivoted_vital.RespiratoryRate` / `vital_periodic.respiration` | 次/分 |
| SpO2 | `spo2` | `pivoted_vital.spo2` / `vital_periodic.sao2` | % |
| 体温 | `temperature`（派生表已转 °C，QA 验证分布） | `pivoted_vital.temperature`（量纲 QA 验证，°F→°C 转换规则预登记） | °C |

### 5.3 检验（DAG 节点 F3）

SOFA 及 SC-common 所需项目：PaO2、FiO2、胆红素、血小板、肌酐、乳酸、WBC、血红蛋白、血糖、钠、钾、碳酸氢盐、INR/PT。

- MIMIC：`main.labevents`（按 `d_labitems` itemid 清单，清单预登记）或派生宽表（`bg`：PaO2/FiO2；`chemistry`、`coagulation`、`complete_blood_count`）；时间用 `charttime`，`storetime` 作敏感性。
- eICU：`pivoted_lab`（22 项宽表）+ `pivoted_bg`（`fio2` 实测 0–1 量纲、`pao2`）；原始 `lab` 表补充未入宽表项目（`labname` 字符串匹配，`labresultrevisedoffset` 作敏感性）。
- PaO2/FiO2 拼接：MIMIC 用 `derived.bg.pao2fio2ratio`（含 `_novent/_vent` 口径）；eICU 用 `pivoted_bg` 同时间点 `pao2/fio2` 计算，FiO2 缺失时按 `pivoted_o2` 氧流量换算规则（预登记）。两库拼接差异属技术文档 §8 预登记项。

### 5.4 SOFA 组分（DAG 节点 F4）

- MIMIC：直接使用 `mimiciv_derived.sofa`（1h 粒度，含 24h 滑动最差组分 `*_24hours` 与 `sofa_24hours`）。landmark t 的 SOFA = `endtime ≤ t` 的最后一行。首个有效 landmark 的 `cardiovascular` 组分用于 CV-SOFA≥3 亚组固定分层（技术文档 §15.1）。
- eICU：自建六组分，输入来源——呼吸（`pivoted_bg` P/F + `respiratory_care`/`treatment` 通气标志）、凝血（`pivoted_lab.platelets`）、肝脏（`bilirubin`）、心血管（MAP + 5.5 NEE 剂量分层）、神经（`pivoted_gcs`，镇静处理规则与 MIMIC `gcs_unable` 口径的差异预登记）、肾脏（`creatinine` + `pivoted_uo` 24h 尿量）。窗口：24h 滑动最差，与 MIMIC `sofa` 表口径对齐。
- `sepsis3` 表内的静态 SOFA 组分（`respiration … renal`）仅用于表型判定，**禁止**作为 landmark 级特征（风险 R11）。

### 5.5 血管活性药与 NEE（DAG 节点 F5；论文 1 特征 + 论文 2 标签基础）

- MIMIC：`mimiciv_derived.vasoactive_agent`（NE/EPI/DA/PE/VAS/DOB/MIL 速率时段）→ 按技术文档 §6.2 公式合成 NEE；`norepinephrine_equivalent_dose` 表作为交叉校验（其系数口径须与附件 B 核对一致后锁定）。体重按 §6.2 优先级；`weight_durations` 提供时段体重。
- eICU：`infusion_drug` 解析管线：①药名正则归类（实测含 `Norepinephrine (mcg/min)`、`(mg/min)`、`(mg/kg/min)`、`(mg/hr)`、`Vasopressin (units/min)` 等形式，完整清单 QA 枚举后预登记）；②`drugrate`/`infusionrate` 文本数值化；③单位→μg/kg/min（VAS 保持 U/min）；④体重优先级：`infusion_drug.patientweight`（实测常缺失）→ `pivoted_weight` → `admissionweight` → 理想体重；⑤按 NEE 公式求和。`pivoted_infusion` 仅作存在性交叉校验（风险 R5）。
- 输注 episode 规则：短间隙 <30min 合并；重叠记录按 order ID/通路判重（MIMIC `linkorderid`；eICU 无 order ID，按药名+时间连续性）——技术文档 §6.2 实施规则逐条落地。

### 5.6 机械通气与氧合支持（DAG 节点 F6）

- MIMIC：`derived.ventilation`（`ventilation_status` 时段）；`oxygen_delivery` 补充 HFNC。
- eICU：`respiratory_care.airwaytype/venttype`、`treatment` 通气路径、`pivoted_o2` 设备与流量。

### 5.7 尿量与液体平衡（DAG 节点 F7）

- MIMIC：`derived.urine_output`（必要时 `outputevents` 补充非尿出量）。
- eICU：`pivoted_uo.urineoutput`；`intake_output` 计算 24h 液体平衡。

### 5.8 ECG 模态（DAG 节点 F8；仅 MIMIC）

1. ECG 索引联接：`ecg_records.subject_id` ⨝ 队列；输出每 landmark 的 ECG availability：`∃ ecg_time ≤ t_lm 且 t_lm − ecg_time ≤ 24h`（主分析时效），48h/72h 为敏感性阈值。
2. 多份 ECG：取时效窗内**最近一份**通过 QC 的记录（主分析）；序列编码作敏感性。
3. 患者级 ECG 描述队列：`t_sepsis ± 24h` 内 ≥1 份 ECG（仅描述/可行性，不作纳入条件）。
4. 波形定位：`E:\clinical_research\MIMIC_IV_3.1\ecg\` + `ecg_records.path` + `.hea/.dat`（已抽样验证）。预处理按技术文档 §20 规范（500 Hz、10 s、12 导联标准顺序、训练集拟合归一化参数）。
5. `ecg_measurements`（机测间期/电轴）作为廉价试金石特征与 QC 辅助；`ecg_waveform_features` 为 100 行试点表，不进管线。
6. 防泄漏：`ecg_time` 为采集时间（§7.2），QC 阈值仅训练集确定。

---

## 6. SC-common 跨库变量映射总表（v1 草案）

下表为 SC-common-all / SC-common-paired 的候选公共变量集，两库均可可靠获得者方可进入（技术文档 §8.2）。每变量须补齐：时间戳类型、单位转换、异常值范围、聚合规则、缺失定义、泄漏风险等级（§8 数据字典字段）。

| 临床概念 | MIMIC 来源 | eICU 来源 | 单位 | 泄漏风险 |
|---|---|---|---|---|
| 年龄 | `icustay_detail.admission_age` | `patient.age` | 岁 | 低 |
| 性别 | `patients.gender` | `patient.gender` | — | 低 |
| 体重 | `weight_durations` | `pivoted_weight`/`admissionweight` | kg | 低 |
| HR | `derived.vitalsign` | `pivoted_vital`/`vital_periodic` | bpm | 低 |
| MAP（有创/无创） | `mbp`/`mbp_ni` | `ibp_mean`/`nibp_mean`/`systemicmean` | mmHg | 低 |
| RR | `resp_rate` | `RespiratoryRate`/`respiration` | /min | 低 |
| SpO2 | `spo2` | `spo2`/`sao2` | % | 低 |
| 体温 | `temperature` | `temperature` | °C（eICU 量纲 QA） | 低 |
| GCS | `derived.gcs` | `pivoted_gcs`/`pivoted_score` | 分 | 中（镇静口径） |
| PaO2/FiO2 | `derived.bg` | `pivoted_bg` | mmHg / 0–1 | 中（拼接规则） |
| 胆红素 | `labevents`/宽表 | `pivoted_lab.bilirubin` | mg/dL | 低 |
| 肌酐 | 同上 | `pivoted_lab.creatinine` | mg/dL | 低 |
| 血小板 | 同上 | `pivoted_lab.platelets` | K/μL | 低 |
| 乳酸 | 同上 | `pivoted_lab.lactate` | mmol/L | 低 |
| WBC | 同上 | `pivoted_lab.wbc` | K/μL | 低 |
| 尿量（24h） | `derived.urine_output` | `pivoted_uo` | mL | 低 |
| NEE | `vasoactive_agent` 合成 | `infusion_drug` 解析合成 | μg/kg/min | **高**（剂量协议差异，须预注册） |
| 机械通气 | `derived.ventilation` | `respiratory_care`/`treatment` | 0/1 | 中 |
| SOFA 六组分 | `derived.sofa` | 自建 | 分 | 中 |
| Charlson | `derived.charlson`（入院时口径） | `past_history` 自建近似 | 分 | **高**（数据源不等价） |
| 感染源 | `microbiologyevents`（仅事后描述） | `micro_lab`（稀疏，仅事后描述） | — | 高（不进主模型，§15.3） |

---

## 7. 防泄漏与质量控制

### 7.1 防泄漏断言（技术文档 §7.3 十条 → 管线自动测试 Q1）

每次提取后自动运行并输出 `qa/leakage_report.md`：

1. `ecg_time ≤ t_lm`；2. 全部特征 `feature_time ≤ t_lm`；3. 结局窗起点 > t_lm；4. 同一患者不跨 train/val/test；5. 同一患者 landmark 不跨 calibration/test；6–8. 标准化/异常值阈值/插补器仅训练集拟合；9. 特征筛选仅训练集；10. ECG 质量阈值仅训练集。

附加断言（本方案新增）：landmark 单调递增且间隔 6h；`k0 ≥ 0`；结局标签与 `deathtime/hospitaldischargestatus` 双向一致抽查 ≥100 例；eICU offset→小时换算零误差（分钟整除校验）。

### 7.2 QA 输出（Q2/Q3）

- 队列流程图计数（每 DAG 节点的纳入/排除人数，两库分别）；
- 月 1 Feasibility Table（技术文档 §9.1 全项；当前已知原始基线：MIMIC sepsis3 stays 41,295 / subjects 31,910，ECG 总覆盖 161,352 subjects，eICU 全库 200,859 stays / 院内死亡 18,004——队列过滤后的正式数字由管线产出）；
- 变量级缺失率、异常值命中率、单位分布（仅训练集统计）。

---

## 8. 输出物与目录规范

```
data_pipeline/
  cohorts/   cohort_mimic_v1.parquet, cohort_eicu_v1.parquet
  splits/    split_assignments_v1.parquet
  landmarks/ landmarks_v1.parquet            # 每 landmark 一行，含风险集标志
  labels/    labels_24h_v1.parquet, labels_competing_7d_v1.parquet
  features/  static_v1.parquet               # 每 stay 一行
             vitals_hourly_v1.parquet        # stay × landmark × 变量 × 小时（长表 + mask + Δt）
             labs_hourly_v1.parquet
             sofa_hourly_v1.parquet
             nee_stream_v1.parquet           # stay × 时间（5min 网格）NEE
  ecg_index/ ecg_landmark_index_v1.parquet   # landmark × 最近合格 ECG（study_id, ecg_time, path, 时效）
  qa/        cohort_flow_v1.md, feasibility_table_v1.md, leakage_report_v1.md
  _meta/     code_version.json               # mimic-code 版本/commit、本地修改清单、DuckDB 版本、提取时间
```

规范：①统一 Parquet（DuckDB 原生写出）；②所有表携带 `subject_key / stay_key / landmark_k` 三级键，eICU 侧键为 `uniquepid / patientunitstayid / k`；③患者级 ID 管理与划分表冻结后不得重算；④每个 DAG 节点独立脚本、I/O schema 校验、中间产物持久化（技术文档 §19.1）；⑤`code_version.json` 记录 mimic-code 版本与 commit hash、附件 A 方言修改清单——**当前为待补齐占位**。

---

## 9. 已识别风险与待决事项

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| R1 | 本地 `sepsis3` 表无 `sepsis_time` 字段 | 时间原点定义 | 以 `suspected_infection_time` 为操作性定义（本方案 §0-3）；核对派生表 mimic-code 版本/commit 并登记附件 A |
| R2 | `anchor_year_group` 含 `2020 - 2022`（49,173 患者），v1.9 未规定 | 时间划分 | 主分析不用；PI 确认后登记（§2.4） |
| R3 | eICU 无 Sepsis-3 派生表，`micro_lab` 仅 2,923 患者有培养 | 外验队列表型 | C6 自建；差异写入 §8.1 预登记，验证层级标注 Robustness under phenotype shift |
| R4 | eICU SOFA 需自建，GCS 镇静口径差异 | SOFA 可比性 | F4 口径对齐；差异预登记 |
| R5 | eICU 输注速率为内嵌单位的文本字段，`pivoted_infusion` 无剂量 | NEE/论文 2 标签 | F5 解析管线；与临床药师核对规则（技术文档 §17.6） |
| R6 | 库内并存 SOFA-1（`sofa`）与 SOFA-2（`sofa2_*`） | 误用风险 | 仅用 SOFA-1；命名检查进 Q1 |
| R7 | 遗留/试点表（`test_*`、`tmp_*`、`crab_modeling_cohort`、`ecg_waveform_features` 等） | 误用风险 | 白名单制，未列入本方案的表一律不用 |
| R8 | eICU 无 ECG，availability 与库来源共线 | 门控外推 | 按 v1.9 §11.1：eICU 仅走 SC-common-all 独立路径 |
| R9 | 转急性医疗机构类别字符串两库不一致 | 结局未知标记 | QA 实测类别清单后预登记（§4.1） |
| R10 | 体重缺失/极端值（<40 / >150 kg） | NEE 与论文 2 标签 | 技术文档 §6.2 规则；缺失体重仅进敏感性分析 |
| R11 | `sepsis3` 表静态 SOFA 组分被误用作 landmark 特征 | 泄漏/口径错误 | 禁用；landmark SOFA 一律取 `derived.sofa` 小时表（§5.4） |

---

## 10. 实施顺序建议（月 1）

1. 登记 mimic-code 版本/commit、补齐附件 A（R1）；PI 确认 R2。
2. C1–C5 MIMIC 队列 + L1–L3 landmark/标签 + Q1 断言 → 产出 MIMIC 侧 Feasibility Table。
3. F8 ECG 索引联接 → landmark 级 ECG 覆盖率（Go 条件核对）。
4. F5 MIMIC NEE 流 → 论文 2 停药时点候选集与事件数预估（§9.1 Go 条件）。
5. C6–C9 eICU 队列 + SOFA 自建 → 同质性门槛检查表（§8.1）逐项标注。
6. F1–F4/F6–F7 特征网格 → Q2 缺失/异常报告；锁定 SC-common 变量终稿。

---

## 附录 A：关键 SQL 模板（DuckDB 方言）

### A.1 MIMIC 队列骨架（C1–C3）

```sql
WITH sepsis AS (
  SELECT subject_id, stay_id, suspected_infection_time AS t_sepsis
  FROM mimiciv_derived.sepsis3
  WHERE sepsis3
),
ranked AS (
  SELECT s.subject_id, d.hadm_id, s.stay_id, s.t_sepsis,
         d.icu_intime, d.icu_outtime, d.admission_age,
         ROW_NUMBER() OVER (PARTITION BY s.subject_id ORDER BY s.t_sepsis) AS rn
  FROM sepsis s
  JOIN mimiciv_derived.icustay_detail d USING (subject_id, stay_id)
  WHERE d.admission_age >= 18
)
SELECT * FROM ranked WHERE rn = 1;   -- 首次 sepsis-associated index stay
```

### A.2 Landmark 网格与 24h 标签（MIMIC）

```sql
SELECT c.stay_id, k,
       c.t_sepsis + INTERVAL (6 * k) HOUR AS t_lm,
       CASE WHEN a.deathtime >  c.t_sepsis + INTERVAL (6 * k) HOUR
             AND a.deathtime <= c.t_sepsis + INTERVAL (6 * k + 24) HOUR
            THEN 1 ELSE 0 END AS y_24h
FROM cohort_mimic_v1 c
JOIN main.admissions a USING (hadm_id)
CROSS JOIN generate_series(
  CAST(GREATEST(0, CEIL(EPOCH(c.icu_intime - c.t_sepsis) / 21600)) AS INTEGER),
  27) AS t(k)
WHERE c.t_sepsis + INTERVAL (6 * k) HOUR < LEAST(c.icu_outtime, COALESCE(a.deathtime, '9999-01-01'));
```

### A.3 eICU 时间换算与结局

```sql
-- offset 均为分钟；ICU 入科 = 0；24h = 1440
SELECT patientunitstayid,
       hospitaldischargestatus = 'Expired'                       AS hosp_expired,
       hospitaldischargeoffset                                   AS hosp_end_offset,
       -- landmark k 的 offset 与 24h 标签（t_sepsis_offset 来自 C7）
       t_sepsis_offset + 360 * k                                 AS t_lm_offset,
       CASE WHEN hospitaldischargestatus = 'Expired'
             AND hospitaldischargeoffset >  t_sepsis_offset + 360 * k
             AND hospitaldischargeoffset <= t_sepsis_offset + 360 * k + 1440
            THEN 1 ELSE 0 END AS y_24h
FROM cohort_eicu_v1;
```

### A.4 eICU 去甲肾上腺素速率解析（片段）

```sql
SELECT patientunitstayid, infusionoffset,
       TRY_CAST(drugrate AS DOUBLE) AS rate_value,
       REGEXP_EXTRACT(drugname, '\(([^)]*)\)', 1) AS unit_hint   -- 如 mcg/min、mg/hr
FROM main.infusion_drug
WHERE drugname ILIKE 'Norepinephrine%';
-- 后续：unit_hint → μg/kg/min 换算 × 体重优先级（F5）
```

---

*本方案基于 2026-07-30 对两库的只读结构核验生成；与技术文档 v1.9 冲突之处以技术文档为准，差异项已在 §9 登记。*
