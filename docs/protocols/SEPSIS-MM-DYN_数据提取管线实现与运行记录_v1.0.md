# SEPSIS-MM-DYN 数据提取管线实现与运行记录 v1.0

- 文档版本：v1.0
- 创建日期：2026-07-30
- 上游依据：①《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）；②《SEPSIS-MM-DYN 数据提取方案 v2.4.1》（正式冻结审核修补版，下称「提取方案」）
- 文档定位：**实现与运行记录**。本文档记录按上述两份文档重写 `src/data/` 数据提取管线的实现方案、实测发现、运行结果、问题与教训，供项目成员学习与复现。**不替代**上游方案文档；方案条款以提取方案 v2.4.1 为准，本文引用其章节号（§）而不复制正文。
- 当前状态：**原型/可行性提取完成**（提取方案 §11 冻结前许可范围）。`_meta/d0_decision.json` 未锁定前，禁止正式模型训练、超参数选择与测试集评估。

---

## 1. 运行环境与数据源核验

### 1.1 软件环境（实测）

| 组件 | 版本 |
|---|---|
| Python | 3.11.7 |
| DuckDB | 1.5.3 |
| pandas | 2.3.3 |
| wfdb | 已安装（WFDB header 解析用） |
| OS | Windows 10/11 + Git Bash |

连接全程只读（`read_only=True`）；DuckDB 连接参数：`threads=4`、`memory_limit='12GB'`、`preserve_insertion_order=false`（防大聚合 OOM，见 §6.7）、`temp_directory='E:/clinical_research/_duckdb_tmp'`。

### 1.2 数据源与实测 schema 要点

| 库 | 路径 | 关键实测 |
|---|---|---|
| MIMIC-IV v3.1 | `E:\clinical_research\MIMIC_IV_3.1\mimic_iv_3_1.duckdb` | `main` + `mimiciv_derived` |
| eICU-CRD v2.0 | `E:\clinical_research\eICUdatabase\eicu_crd.duckdb` | `main`（53 表） |
| MIMIC-IV-ECG v1.0 | `E:\clinical_research\MIMIC_IV_3.1\ecg\`（WFDB 树） | `main.ecg_records.path` 相对此根目录 |

重写前的只读核验发现（直接影响实现决策）：

1. `mimiciv_derived.sepsis3` 41,295 行全部 `sepsis3=TRUE`，均有 `suspected_infection_time` 与 `sofa_time`，**无 `sepsis_time` 字段** → D0 决策门未锁定，操作口径取 `suspected_infection_time` 并在 `_meta` 登记（提取方案 §3.1）。
2. `main.icustays` 相邻 stay 最小间隙 **0.1 分钟，不存在 gap=0 的边**（v3.1 秒级时间分辨率）→ `main_tau0` 下无合并，零间隙路径核验机制仅对敏感性版本有实际作用（详见 §3.1 与 §6.5）。
3. `main.transfers` 的 `eventtype ∈ {ED, discharge, admit, transfer}`；`careunit` 46 类，其中 ICU 类含「Intensive Care Unit」/「CCU」字样。
4. `admissions.discharge_location` 实测值与旧脚本常量不符：旧清单（`TRANSFER TO OTHER` 等）在 v3.1 不存在；实测急性转出为 `ACUTE HOSPITAL`（2,334 例），存活去向为 `HOME/HOME HEALTH CARE/SNF/REHAB/...`（详见 §6.4）。
5. `chartevents` 中 FiO2 实测仅 itemid **223835**（MetaVision，1,144,289 行）；3420/190 为 CareVue 遗留（0 行）。
6. `patients.anchor_year_group` 为 5 类：`2008 - 2010`、`2011 - 2013`、`2014 - 2016`、`2017 - 2019`、`2020 - 2022`，与提取方案 §2.4 映射一致。
7. `icustays` NULL `outtime` 仅 14 条；eICU 有 2 条负 LOS stay（打 `episode_time_anomaly_flag` 进 QA）。
8. eICU `patient.age` 为 VARCHAR（含 `"> 89"`，数值化时记 90 并打标）；`medication.drugordercancelled` 为 BOOLEAN；`hospitaldischargestatus='Expired'` 与 `hospitaldischargelocation='Death'` 完全一致（18,004 例）。

---

## 2. 管线架构

### 2.1 目录与模块（src/data/，29 个 Python 文件，约 5,900 行）

```
src/data/
  main.py                 # CLI：mimic / eicu / all，--step 分步
  config.py               # 全部契约常量 + pending 登记（每块标注方案 § 来源）
  utils.py                # 连接、Parquet I/O、COPY TO 直写、canonical 序列化、schema 校验
  contracts.py            # sc_common 变量合同 + 临床观察源白名单生成
  mimic/
    c0_episodes.py        # C0 episode 四表（416 行）
    c1_cohort.py          # C1-C5 队列 + 敏感性标志 + 划分表
    landmarks.py          # L1/L2 landmark 网格
    labels.py             # L3 24h 三态 + 观察终点 + 7d 竞争风险
    f1_static.py          # baseline_static + landmark_context
    f2_vitals.py          # 生命体征双轨（strict/retro + hourly 宽表）
    f3_labs.py            # 检验双轨 + P/F 配对
    f4_sofa.py            # 实时 SOFA 六组分 × 两轨（429 行）
    f5_nee.py             # NEE 流 + 血管活性药窗
    f6_vent_urine.py      # 通气 + 尿量
    f8_ecg.py             # ECG 索引（WFDB header 缓存）
    qa.py                 # Q1 断言 + 四份 QA 报告
  eicu/
    c6a_episodes.py       # episode 四表 + canonical 事件时间映射（513 行）
    c6b_antibiotics.py    # 抗生素/培养事件 + 候选配对 + 时间源汇总
    c7_phenotypes.py      # P-explicit / P-clinical / P-strict（545 行）
    c8_cohort.py          # cohort_eicu_v2
    landmarks.py labels.py features.py qa.py
```

### 2.2 DAG 与 CLI

```text
MIMIC:  c0 → cohort(c1-c5 + splits) → landmarks → labels
        → f1 → f2 → f3 → f4 → f5 → f6 → f7 → f8 → contracts → qa
eICU:   c6a → c6b → c7 → c8 → landmarks → labels → features → qa
```

```bash
python src/data/main.py mimic                 # 全量 MIMIC
python src/data/main.py mimic --step f4       # 单步
python src/data/main.py eicu --step c6a
python src/data/main.py all
```

设计原则（与提取方案 §8 规范一致）：每 DAG 节点独立脚本、中间产物持久化 Parquet、自动结果与人工裁决表物理分离、大表经 `COPY TO` 流式直写避免 pandas 往返。

---

## 3. MIMIC 侧实现要点与实测结果

### 3.1 C0 连续 ICU episode 映射（阻断项 1 / R23 / R36 / R37）

四表物理分离：`edges_preliminary → map_preliminary → episode_merge_adjudications → map_final`，三个合并阈值参数化独立版本（`main_tau0` / `sensitivity_tau30` / `sensitivity_tau60`，版本字段随行，不混用）。

实现要点：

- **`transfer_sequence` 实际生成**：对每条相邻 stay 边，取 `transfers` 中落在开区间 `(prev_outtime, curr_intime)` 的记录，按 `(intime, transfer_id)` 排序生成有序 JSON 数组（含 `relative_position`）。
- **路径分类**：合法路径 `icu_to_icu` 与 `icu_to_internal_placeholder_to_icu`；异常路径 `icu_to_ward_to_icu` / `icu_to_ed_to_icu` / `missing_left_boundary` / `multiple_conflicting_boundary_events` / `overlapping_transfer_records` / `unknown_careunit`。
- **占位规则（实测驱动）**：两个 ICU 单元之间的非 ICU 记录若时长 ≤ `PLACEHOLDER_MAX_MIN = 30 min`（如 Discharge Lounge 秒级行政记录），判为 `internal_transfer_placeholder`（合法）；更长 ward 停留仍判 `via_ward` 强制 split。该参数为候选值，已在 config 注释标注待预登记。
- **显式 final decision**：pending_review + adjudicated override → 按裁决；pending 未裁决 → `split` + `unresolved_conservative_split=TRUE`；merged/split → 原值；其余 ⇒ 报错（pipeline failure）。
- **全局唯一 ID**：`episode_id = 'MIMIC_<hadm_id>_<episode_seq>'`；断言每 stay 恰好一个 episode、`(episode_id, stay_id)` 无重复、episode_id 不跨 hadm、final_decision 状态空间合法。

实测结果：

| 版本 | 边判定 | episodes | stays |
|---|---|---|---|
| main_tau0 | split 9,216（全部） | 94,458 | 94,458 |
| sensitivity_tau30 | merged 791 / split 8,425 | 93,667 | 94,458 |
| sensitivity_tau60 | merged 792 / split 8,424 | 93,666 | 94,458 |

**重要实测发现**：v3.1 无 gap=0 边（最小 0.1 min），故主版本无合并、每 stay 独立 episode；占位路径合并全部被敏感性版本捕获。此发现已写入 `qa/time_logic_qa_v2.md`，冻结评审需关注 τ=0 主值在该数据版本下的实际语义。

### 3.2 C1–C5 队列与划分

- **C1** `mimic_episode_sepsis`：sepsis3 经 icustays 回填 hadm_id，按 final map 聚合，41,295 个 episode 命中（全部 `t_sepsis_status='ok'`）。
- **C2/C3**：年龄 ≥18（`icustay_detail.admission_age`）+ 每 subject 首次 index episode（排序键 `t_sepsis NULLS LAST, admittime, episode_intime, episode_id`）→ **31,910 index episodes / 31,910 subjects**，与提取方案 §7.7 基线（41,295 stays / 31,910 subjects）一致。
- **C4 敏感性标志**（描述性，不作排除）：外院转入 9,630 例（30.2%）、landmark 前 ECMO 39 例、90 天实体器官移植 119 例、landmark 前 DNR/CCO 561 例。ECMO 用 chartevents itemid (224660, 229270)；DNR 用 code status itemid (223758, 228687, 229784) + `REGEXP_MATCHES` 取值匹配（移植 ICD 清单见 `mimic/c1_cohort.py` 注释）。
- **划分表** `split_assignments_v2`：全库 364,627 subjects 按 anchor_year_group 映射 → train 177,873 / validation 71,640 / test 65,941 / `excluded_amendment_pending` 49,173（2020-2022 不查看结局，§2.4）。

### 3.3 Landmarks（阻断项 3）

- 硬门槛：仅 `episode_outtime_status='ok' AND episode_outtime_ts IS NOT NULL` 生成；`k0 = max(0, ceil((episode_intime - t_sepsis)/6h))`；k ≤ 27；`t_landmark < min(episode_outtime, deathtime)`。
- 实测：**443,225 行 / 31,873 episodes**（37 个 episode 无有效 landmark：t_sepsis 晚于 episode 结束或死亡早于 k0）；k0 分布与间隔 6h 断言通过。

### 3.4 L3 标签（状态机 + 双观察终点 + 竞争风险）

- **双观察终点**：`last_clinically_observed_time` 仅取白名单事件/采集时间（chartevents.charttime、labevents.charttime、urine_output、vasoactive/ventilation 实际起止、微生物采样），封顶 `dischtime`；`last_database_available_time` 含 storetime 仅 QA；`observation_end_source` 与实际选定值一致；临床事件 > dischtime 打 `clinical_event_after_discharge_flag`。
- **状态机**（§4.1/A.3）：invalid_input → status_conflict → death_time_missing → event → acute_transfer → non_event_alive_discharge → non_event_observed → missing_status_left_observation；恰好 t+24h 出院 `full_inhospital_followup_24h=TRUE`（P1-3）。
- 实测分布：event 10,372（2.3%）、non_event_observed 422,551、non_event_alive_discharge 3,649、acute_transfer 487、missing_status_left_observation 6,088、death_time_missing 78（进 `label_adjudications` 待裁决，物理分离）；`outcome_unknown_reason` 枚举校验通过（含 time_anomaly 717）。
- **7d 竞争风险**：event_type 分布 死亡 48,341 / 存活出院 133,923 / 急性转出 2,625 / 删失 258,336；同时刻优先级 死亡>急性转出>存活出院>删失。

### 3.5 F1 静态特征

- `charlson_prior` NULL 口径（R16）：仅既往住院的最终 ICD 派生 Charlson；无既往 → NULL + `charlson_prior_available=FALSE` + `prior_hospital_count=0`（实测 12,940 例有值 / 18,970 例 NULL）。本次住院 ICD 一律不用。
- `landmark_context_v2`：landmark 前最近体重（缺失率 0.9%；极端体重打标不裁剪 9,841 行）、身高、`Δ_ICU-sepsis`（中位 -0.24h）、当前有创通气/血管活性药状态。

### 3.6 F2/F3 时序特征（双轨 + 性能方案）

- **双轨语义**：strict 轨 `available_time = storetime`（缺失降级 charttime 并打 `charttime_fallback` 标）；retro 轨全部按 charttime。vitals 源自 chartevents（itemid 白名单），labs 源自 labevents（14 项 + P/F）。
- **bin 约定**：`hours_before = (t_landmark - event_time)/3600 ∈ [0, 24]`，`bin_hour = LEAST(FLOOR(hours_before), 23)`，**bin0 = 最近一小时**，`bin b` 区间 `[t_lm-(b+1)h, t_lm-bh)`（修复后，见 §6.3）。同 bin 多条取中位数；聚合字段含 `n_source_records / min_event_time / max_event_time / max_available_time / aggregation_method / source_table_set / source_time_type`。
- **性能方案**：chartevents 4.33 亿行、labevents 1.58 亿行 → 先按 itemid 白名单 + 队列 hadm + landmark 时间界过滤，**窗口连接只物化一次**（vitals_win 68.0M 行 / labs_win 8.8M 行），两轨分别聚合，结果经 `COPY TO ... (FORMAT PARQUET, COMPRESSION ZSTD)` 流式直写。全量 F2+F3 约 15 分钟。
- 实测行数：vitals strict 53,652,195 / retro 55,476,905 / hourly 宽表 8,783,063；labs 双轨合计 17,188,578；`pf_ratio_v2` 227,019 对（84.4% strict 合格，中位 P/F 218）。
- 防泄漏断言：`max_available_time ≤ t_landmark`（strict 轨）0 违规。

### 3.7 F4 实时 SOFA（purpose × evidence_track 二维）

- 不直接使用 `mimiciv_derived.sofa` 总分（R11）；六组分从原始窗内输入按经典规则实时计算：呼吸（P/F + 通气修饰）、凝血（血小板 min）、肝（胆红素 max）、心血管（**修正阈值 + 最大分值**：MAP min 与 dopamine/dobutamine/epinephrine/norepinephrine 24h 最大速率）、神经（GCS min，`gcs_unable` 行排除）、肾（肌酐 max 与 24h 尿量取大）。
- 两轨：`strict_24h`（全部输入 24h 窗）与 `carryforward`（胆红素/肌酐/血小板 ≤48h，GCS/PF ≤24h）。
- 完整性规则：缺失组分不计 0；`sofa_total_complete` 仅 6/6 生成；5/6 仅 partial。strict 轨完整性实测：6/6 完整 34,545（7.8%）、5/6 173,988、4/6 178,530、≤3/6 其余；完整总分均值 8.67（IQR 6–11），符合脓毒症 ICU 预期。CV 亚组字段 `sofa_cv_original` 与 `nee_current` 严格分离（R15）。

### 3.8 F5–F7

- `nee_stream_v2`：每 landmark 24h 窗 NEE max/median/AUC（按窗口重叠时长加权的 AUC）+ `nee_current`（含 landmark 的输注记录速率）+ 各药 24h 最大速率；143,413/443,225（32%）landmark 有血管活性药暴露。
- `ventilation_v2`：359,016 行，有创通气率 61.9%（有通气记录者）；`urine_output_v2`：413,001 行（覆盖 93%）。

### 3.9 F8 ECG 索引

- **WFDB header 解析**：队列 subject 的 246,020 份 ECG 全部解析成功（`recording_duration_s = sig_len/fs`），产物缓存 `ecg_recording_duration.parquet`，重跑自动断点续跑。
- **归属（显式 OR）**：`same_hospitalization`（admittime ≤ ecg_time ≤ dischtime）171,520 行 + `auditable_pre_admission_encounter`（入院前 ≤30 天且无其他住院覆盖）39,840 行；后类打 `pre_admission_ecg=TRUE`。
- **四时间字段**：`ecg_acquisition_time` / `recording_duration_s` / `ecg_available_time_assumed = 采集完成时间` / 选片时间；防泄漏与选片统一用 `ecg_available_time_assumed ≤ t_landmark`。
- **五层级 availability**：found_raw → same_encounter → structurally_valid（header 可读、时长 ≥9s、导联数 ≥8）→ pass_frozen_qc（E-4 冻结前恒等于 structural，已打标）→ selected_for_model（24h 时效内最近一份）。
- 实测：211,360 个 landmark（47.7%）72h 内有 ECG；**24h 主时效选片 112,066 个 landmark（25.3%）**；患者级描述队列（t_sepsis ±24h）18,666 subjects。

---

## 4. eICU 侧实现要点与实测结果

### 4.1 C6a episode 四表 + canonical 事件标识（阻断项 2 / P1-1 / R14 / R26 / R32）

- **时间坐标**：`hospital_offset_min = -hospitaladmitoffset + event_offset`；`episode_offset_min = hospital_offset_min - episode_start_hospital_min`。
- **episode 四表**：同一 `patienthealthsystemstayid` 内相邻 stay 边，`edge_path_class` 六类（实测分布：icu_to_icu 11,342 / stepdown_or_ward 21,033 / or_procedure 1,789 / offset_overlap 261→pending / cross_hospital 14 / unknown 65）；合并规则 `gap ≤ 0 且 icu_to_icu`。**200,859 stays → 190,627 final episodes**（merged 10,232）；每 patientunitstayid 恰好一个 episode、不跨 hospital stay 断言通过。
- **canonical JSON + SHA-256**：SQL 内 `TO_JSON(STRUCT_PACK(name := expr, ...))` 固定字段顺序 + 显式类型 + JSON null；raw 指纹（原始值）与 canonical 指纹（字符串 trim+lower）分离；`source_event_id = canonical_clinical_fingerprint`；`raw_exact_duplicate_count` 守恒断言内嵌（SUM = 物理源行数）。
- 四张时间映射表实测守恒：

| 表 | 唯一事件 | 物理行数 | 守恒 |
|---|---|---|---|
| medication | 7,174,079 | 7,301,853 | ✓ |
| lab | 39,094,720 | 39,132,531 | ✓ |
| micro_lab | 15,136 | 16,996 | ✓ |
| infusion_drug | 4,798,564 | 4,803,719 | ✓ |

泛化桥接表 `eicu_event_time_map`（四表 union，51,082,499 行）已补齐。

### 4.2 C6b 抗生素事件与配对

- 四级时间源：validated administration（eICU 结构性不可得，`administration_confirmation_availability='structurally_unavailable'`，事件级 `administration_confirmed=NULL`）→ infusion_recorded（infusion_drug）→ scheduled_start（medication.drugstartoffset，实测全量非空）→ order_time（drugorderoffset，实测 0 例）。同药 ±240min 去重。
- 实测：抗生素事件 155,327（infusion_recorded 仅 186 / scheduled_start 155,141）；培养事件 16,996；**候选配对 9,141 对 / 207 episodes**（同一 final episode 内方向性窗 0–4320 / 0–1440 min）。
- **episode 级抗生素可靠时间覆盖率实测 0.09–0.11%，远低于 30% 正式门槛** → Go/No-Go FAIL（预期内，见 §5.3）。

### 4.3 C7 三套表型（feasibility 级，规则表待 PI 签署）

- **P-explicit**：显式 sepsis 诊断串（diagnosis/admission_dx）→ 30,838 episodes；诊断时间语义分层（`observed_record_time_pending_audit` / `assigned_admission_proxy`）。
- **P-clinical**：感染诊断证据 + 回顾性 ΔSOFA≥2 确认：六组分从 pivoted_lab/bg/gcs/vital/uo + infusion 药名正则按经典规则在 baseline 窗 [t_I-48h, t_I-24h] 与 qualifying 窗 [t_I-24h, t_I+48h] 计分；baseline 无完整观测时 `assumed_zero_by_phenotype_rule` 打标（实测 50,951/51,534 即绝大多数为 assumed-zero，`delta_sofa_observed_complete` 仅 583 例两端完整）。qualified 43,191 episodes。
- **P-strict**：仅输出候选 pair 引用（207 episodes），锁定选对函数 `select_suspected_infection_pairs_locked_v1` 待 mimic-code 审计（R33/B-5），标 `feasibility_only`。
- 可行性近似（已在代码注释标注）：心血管组分 = MAP 规则 + 血管活性药存在即 ≥2（eICU 剂量解析 pending，R5）；肾组分仅肌酐；通气修饰仅 treatment '%ventilat%'。

### 4.4 C8 队列与 landmarks/labels/features

- `cohort_eicu_v2`：**62,251 index episodes**（P-clinical 35,974 / P-explicit 26,079 / P-strict 198）；年龄 `"> 89"`→90 打 `age_was_capped`；含 C9/C10 抗生素事件标志列。
- landmarks：743,002 行（episode 分钟坐标，同 k0 规则与硬门槛）。
- labels：24h 状态机全用 `*_episode_min`；实测 event 18,651（2.5%）、non_event 649,102、acute_transfer 3,386、missing 71,863；`Expired` 即 `hospitaldischargeoffset` 作死亡时间。7d 竞争风险同构。
- features：vitals（pivoted_vital 主）69.2M bin 行、labs（pivoted_lab+pivoted_bg）18.5M、gcs 10.9M、urine 4.06M、support（通气/血管活性药 0/1）743,002 行；一律 `charttime_fallback` 语义（eICU lab revised 审计未关闭，C-2）。

---

## 5. 运行结果总览与验证

### 5.1 合同产物核对（提取方案 §8）

**59 个合同文件全部生成且非空**（adjudication 类允许 0 行但 schema 正确）。关键行数：

| 产物 | 行数 |
|---|---|
| cohort_mimic_v2 | 31,910 |
| landmarks_v2 | 443,225 |
| labels_24h_v2 | 443,225（阳性 10,372） |
| vitals_realtime_strict_v2 | 53,652,195 |
| labs_hourly_v2（双轨） | 17,188,578 |
| sofa_hourly_v2（两轨） | 886,450 |
| ecg_landmark_index_v2 | 211,360 |
| cohort_eicu_v2 | 62,251 |
| eicu_landmarks_v2 | 743,002 |
| eicu_labels_24h_v2 | 743,002（阳性 18,651） |
| eicu_vitals_v2 | 69,205,944 |

### 5.2 Q1 自动断言（33/33 PASS）

覆盖（详见 `qa/leakage_report_v2.md`）：

- Q1-8 episode final map：每版本 stay→episode 一对一、final_decision 状态空间合法（6 项）
- Q1-18：landmark 间隔恒 6h、k≥k0≥0、ECG 归属仅前两类、SOFA 总分仅 6/6、**事件时间落在 bin 区间**（vitals/labs 各 0 违规）
- 防泄漏 #1 ECG `available ≤ landmark`、#2 特征 `max_available ≤ landmark`、#3 结局窗起点 > landmark
- Q1-17 acute_transfer XOR alive_discharge；Q1-14 `outcome_unknown_reason` 枚举
- P1-3 恰好 t+24h 出院 `full_inhospital_followup_24h=TRUE`
- 全部关键产物存在且非空（15 项）

### 5.3 eICU Go/No-Go（`qa/eicu_go_nogo_v2.md`）

| 门槛 | P-clinical | P-explicit | P-strict |
|---|---|---|---|
| 患者数 | PASS 35,974 | PASS 26,079 | FAIL 198 (<500) |
| 医院数 ≥20 | —（204/205） | — | FAIL 12 |
| 最大单医院占比 ≤25% | PASS 4.5% | PASS 3.6% | FAIL 61.6% |
| 院内死亡 ≥100 | PASS 6,434 | PASS 4,905 | FAIL 30 |
| 主要 landmark 可估计 ≥10/12 | PASS 12/12 | PASS 12/12 | N/A（t_sepsis pending） |
| 培养覆盖率 ≥5% | — | — | PASS 100%（候选集内） |
| 抗生素时间源覆盖 ≥30% | **FAIL 0.11%** | **FAIL 0.12%** | FAIL 3.0% |

解读：P-strict 在当前 eICU 数据下不可行（与方案 R3/R18 预判一致）；P-clinical/P-explicit 队列规模与 landmark 可估计性达标，但**抗生素可靠时间覆盖率是共用阻断项**——需阶段 A 完成 `administration_confirmed` 来源评估与 eICU lab 时间语义审计后再评估。全部 track 标 `feasibility_only`。

### 5.4 可行性数字（`qa/feasibility_table_v2.md`）

MIMIC：index episodes 31,910；landmark 阳性率 2.34%；测试集有 ≥1 阳性 landmark 的 episode 数见报告；landmark 级 24h ECG 覆盖率 25.3%（72h 47.7%）；患者级 ECG 描述队列 18,666 subjects。

---

## 6. 问题与教训（学习重点）

每条按「现象 → 根因 → 修法 → 位置」记录。

### 6.1 旧脚本 feature 表全 0 行

- 现象：旧版 vitals/labs/sofa/nee/vent/urine 六个 feature 表全部 0 行，landmarks/labels 正常。
- 根因：`t_from_landmark_h = EPOCH(t_landmark − charttime)/3600` 对历史记录为**正值**，而过滤条件写 `t_from_landmark_h >= -24 AND t_from_landmark_h < 0`（负值区间），自相矛盾清空全部记录。旧 cohort 还因 join 引入 `hadm_id_1` 重复列。
- 修法：统一约定 `hours_before_landmark ∈ [0,24]` 正值 + `bin = LEAST(FLOOR(hours_before), 23)`，全部模块一致；join 前显式去重选列。
- 位置：`mimic/f2_vitals.py`、`f3_labs.py` docstring 固化了该约定。

### 6.2 NaN ≠ None 穿透 SOFA 计分

- 现象：F4 首跑 strict 轨全部 landmark 都是 `complete_6_of_6`（43 万全完整，明显不可能）。
- 根因：SQL 的 NULL 经 `fetchdf()` 变为 float NaN；计分函数用 `is None` 判缺失 → NaN 穿透全部阈值比较落入最低档（如 NaN 血小板被判 4 分），缺失掩码失效。
- 修法：计分入口统一 `_v()` 归一化（NaN→None），缺失语义只在归一化后判定。这类「SQL NULL → pandas NaN → Python 缺失判定」的链条是 DuckDB+pandas 管线的通用陷阱，**任何逐行 Python 计分前必须显式归一化**。
- 位置：`mimic/f4_sofa.py::_score_track`。

### 6.3 bin_start/bin_end 公式不自洽

- 现象（子代理交叉审查发现）：bin 数据本身正确，但 `bin_start = t_lm - 24h + bin*1h` 使事件时间落在自身 bin 区间之外（hours_before=15.4 的事件被标到 [t_lm-9h, t_lm-8h) 的 bin）。
- 根因：公式是从「t_lm-24h 起正向排 bin」的思路写的，与「bin0=最近」的语义相反。
- 修法：改为 `bin_start = t_lm - (bin+1)*1h`、`bin_end = t_lm - bin*1h`；并在 Q1 断言中新增「min/max_event_time 必须落在 [bin_start, bin_end]」固化（53M+17M 行 0 违规）。
- 位置：`mimic/f2_vitals.py::_AGG_SELECT`、`f3_labs.py::_AGG`、`mimic/qa.py`。

### 6.4 出院去向清单与实测不符

- 现象：旧 config 的 `TRANSFER TO OTHER` 等类别在 v3.1 `discharge_location` 中不存在，急性转出分支永远落空。
- 根因：清单照搬了旧版 MIMIC 文档，未对本地数据实测（提取方案 R9 要求两库分别实测后预登记）。
- 修法：按实测值重写清单（急性转出 `ACUTE HOSPITAL`；存活 11 类），config 标注 `DISCHARGE_LIST_PENDING=True`（D-3 冻结项，待预登记）。
- 位置：`config.py::ACUTE_TRANSFER_LOCS/ALIVE_DISCHARGE_LOCS`。

### 6.5 MIMIC 无 gap=0 边与占位规则

- 现象：首版 C0 全部 9,216 条边 split，三个阈值版本完全等价，合并机制形同虚设。
- 根因：①v3.1 最小 stay 间隙 0.1 min，无 gap=0 边；②小间隙边几乎全部是 ICU→Discharge Lounge（约 20 秒行政记录）→ICU，被判 via_ward。
- 修法：按提取方案预登记的 `internal_transfer_placeholder` 合法路径类别实现时长规则（≤30min 占位），tau30/60 版本捕获 791/792 条真实合并；main_tau0 无合并作为实测事实写入 QA 供冻结评审。
- 位置：`mimic/c0_episodes.py::_classify_transfer_path`、`config.py::PLACEHOLDER_MAX_MIN`。

### 6.6 DuckDB 方言坑（两个）

- `SIMILAR TO`：`'DNR / DNI' SIMILAR TO '%dnr%'` 返回 False（方言对子串模式行为与预期不符）→ 全部改用 `REGEXP_MATCHES`。
- `STRUCT_PACK`：DuckDB 1.5.3 要求 `STRUCT_PACK(name := expr)` 语法，`expr AS name` 报 ParserException（子代理修 `utils.struct_json_sql` 时发现）。

### 6.7 双 DuckDB 进程 OOM

- 现象：MIMIC F2 与 eICU c6a 并行时双双 OOM（`could not allocate block`）。
- 根因：两个 DuckDB 进程各按 12GB 上限申请，物理内存不足；大聚合（39M 行 lab 哈希）在内存压力下无法分配。
- 修法：重任务一律串行；连接加 `SET preserve_insertion_order=false`（官方建议的聚合内存优化）；`temp_directory` 指向大容量盘。串行后 c6a lab 映射（39M 行哈希+守恒）一次通过。

### 6.8 GROUP BY 派生列

- 现象：C2 SQL 报 `column "first_careunit" must appear in the GROUP BY clause`。
- 修法：需「按 episode 首 stay 取科室」时用 `ROW_NUMBER + ANY_VALUE(...) FILTER (rn=1)`，而非直接 SELECT 非聚合列。

### 6.9 _meta JSON 编码

- 现象：`code_version.json` 无法按 UTF-8 读取。
- 根因：Windows 下 `Path.write_text` 默认系统编码（GBK），内容含中文。
- 修法：全部显式 `encoding="utf-8"`。

### 6.10 子代理协作方式（工程经验）

本次 eICU 侧 9 个模块由子代理在后台编写（给定了已核验的 schema 事实、合同字段清单、分类器规则与 MIMIC 参考实现），产出质量合格，并通过交叉审查发现了一个主代理没注意到的真实 bug（§6.3 bin 边界）。经验：①给子代理的 brief 必须包含已核验事实而非让它重新探索；②要求「只写代码 + 冒烟验证，不跑全量」避免资源互踩；③子代理报告的「待决策问题」清单价值很高，应逐条处置。

---

## 7. Pending 事项与冻结前限制

`_meta/d0_decision.json`、`code_version.json` 已登记：

1. D0 时间原点未锁定（操作口径 `suspected_infection_time`，禁止代码层与文档层口径分离）；
2. 出院去向清单待预登记（D-3，本次实测 v1 清单）；
3. eICU 锁定选对函数 `select_suspected_infection_pairs_locked_v1` 待 mimic-code commit 审计（当前仅候选 pair）；
4. eICU lab `labresultrevisedoffset` 语义审计未关闭（检验一律 `charttime_fallback`）；
5. ECG 数据驱动 QC 阈值待训练集拟合（当前仅结构性 QC，`pass_frozen_qc` 恒等于 structural 并打标）；
6. eICU 表型规则表待 PI 签署（全部 track `feasibility_only`）。

另需关注：`internal_transfer_placeholder` 的 30min 阈值、eICU 边分类增补清单（'Operating Room'/'ICU (CABG)' 等实测值）、P-clinical SOFA 三处可行性近似（CV 用药存在即 ≥2、肾仅肌酐、vent 修饰仅 treatment）——均已在代码注释标注，升级时逐项关闭。

**限制重申**（提取方案 §11）：冻结清单 31 项未全部关闭且五类冻结验证通过前，当前产物仅用于来源审计、原型提取、人工抽查与可行性分析；禁止正式模型训练、超参数选择、测试集性能查看。

---

## 8. 复现指南

### 8.1 环境准备

```bash
python -V   # 3.11.x
pip install duckdb pandas pyarrow wfdb   # 实测 duckdb 1.5.3 / pandas 2.3.3
```

确认 `src/data/config.py` 中 `MIMIC_DB` / `EICU_DB` / `ECG_WFDB_ROOT` 指向本地数据。

### 8.2 分步运行（推荐首次）

```bash
python src/data/main.py mimic --step c0
python src/data/main.py mimic --step cohort
python src/data/main.py mimic --step landmarks
python src/data/main.py mimic --step labels
python src/data/main.py mimic --step f1   # ... f2..f8 依次
python src/data/main.py mimic --step contracts
python src/data/main.py mimic --step qa
python src/data/main.py eicu --step c6a   # ... c6b c7 c8 landmarks labels features qa
```

或全量：`python src/data/main.py mimic && python src/data/main.py eicu`。

### 8.3 性能与资源

- MIMIC 全量约 25–35 分钟（F2 最重）；eICU 全量约 20–30 分钟（c6a lab 映射最重）。
- **重任务串行**，不要并行跑两条管线；单机内存 <24GB 时适当调低 `memory_limit`。
- ECG WFDB header 结果缓存在 `ecg_index/ecg_recording_duration.parquet`，重跑自动断点续跑；强制重解析用 `run_f8_pipeline(con, force_reparse=True)`。

### 8.4 验收核对表

1. `qa/leakage_report_v2.md` 全部 PASS（当前 33 项）；
2. §5.1 行数表与本次运行对账一致（cohort 31,910 / landmarks 443,225 / eICU cohort 62,251 等）；
3. `qa/eicu_go_nogo_v2.md` 门槛判定与 §5.3 一致；
4. `_meta/` 三文件 UTF-8 可读，pending 清单无遗漏；
5. 59 个合同文件齐备非空。

---

## 9. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：按技术文档 v1.9 + 提取方案 v2.4.1 重写管线的实现与运行全记录——架构、MIMIC/eICU 逐模块要点与实测、33 项 Q1 断言、Go/No-Go、10 条问题与教训、pending 清单、复现指南。 |

---

*本文档与技术文档 v1.9、提取方案 v2.4.1、数据预处理方案 v1.0 构成文档链：技术文档（预注册 estimand）→ 提取方案（语义冻结契约）→ 本文档（实现与运行记录）→ 预处理方案（模型可训练输入）。冲突之处以技术文档与提取方案为准。*
