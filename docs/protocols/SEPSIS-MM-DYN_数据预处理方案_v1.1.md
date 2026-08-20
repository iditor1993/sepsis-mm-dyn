# SEPSIS-MM-DYN 数据预处理方案 v1.1

- 文档版本：v1.1
- 创建日期：2026-07-30
- 上游依据：①《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）；②《SEPSIS-MM-DYN 数据提取方案 v2.4.1》（正式冻结审核修补版，下称「提取方案」）；③《SEPSIS-MM-DYN 数据提取管线实现与运行记录 v1.0》（下称「实现记录」，本次修订的实测依据）
- 实现语言：**Python**（托管 Python 运行时：duckdb / pandas / numpy / pyarrow / scipy / wfdb / pytest / pyyaml）
- 数据源：MIMIC-IV v3.1 + MIMIC-IV-ECG v1.0（经提取方案产出的 Parquet 中间产物）；eICU-CRD v2.0（同管线去 ECG 节点）
- 维护方式：与技术文档、提取方案同库 Git 版本管理；输入契约或处理规则变更递增版本号
- 状态：**可实施候选版**。本方案只做「提取后 → 模型可训练」的预处理，**不重复定义**队列、表型、标签与时间语义（这些已由提取方案冻结）；所有统计量（标准化参数、异常值阈值、插补器、特征筛选、ECG 质量阈值）**仅在训练集拟合并冻结**。与提取方案相同的限制继续有效：冻结清单未关闭前，禁止正式模型训练、超参数选择与测试集评估；本方案的 QA 复测通过是下游训练的前置条件之一。

---

## 0.0 v1.0 → v1.1 修订总览

本版为**对齐修订**：提取管线已按提取方案 v2.4.1 完成重写与全量运行（见实现记录），v1.0 中引用但未产出的输入（split 表、竞争风险标签、landmark_context、vitals 双轨、contracts、eICU 全套）现均已落盘。v1.1 按实测产物修正输入契约描述，不改变任何处理规则、estimand 与统计设计。

| # | 修订点 | 落点 |
|---|---|---|
| 1 | `vitals_hourly_v2` 实为 strict 轨宽表便捷视图；张量化应读 `vitals_realtime_strict_v2` / `vitals_charttime_retro_v2` 长表 | §0.2、§4.1 |
| 2 | 键命名对齐：产物为 `episode_id/subject_id/k`（MIMIC）与 `episode_id/uniquepid/k` + `*_episode_min`（eICU）；P1 装载时统一重命名为 `episode_key/subject_key/landmark_k` | §3.2 |
| 3 | `nee_stream_v2` 为 landmark 级汇总，无 bin 级序列；`nee_current` 逐小时通道由 P2 按窗口从源表重建 | §4.4 |
| 4 | bin 级 labs 无 FiO2 通道（FiO2 仅在 landmark 级 `pf_ratio_v2`）；P/F 通道来源决策登记 | §4.1、§4.4 |
| 5 | `sofa_realtime_strict_24h_cv`（患者级、首个有效 landmark）派生规则显式化 | §4.4、§6.3 |
| 6 | eICU 产物为分钟坐标 + `phenotype_track` 列；P1 换算与保留规则 | §3.2、§11.3 |
| 7 | adjudication pending 期行为写明（按 preliminary 处理并计数） | §6.1 |
| 8 | 风险表更新：PP-1 状态、新增 PP-9（提取产物为原型/可行性版） | §16 |
| 9 | **v1.1.1 状态回写（2026-07-30 冻结后）**：PP-1 改「D0 已锁定出口 B」；PP-9 改「31/31 项关闭、冻结标签生效、MIMIC 包 `training_ready=true`、eICU 包维持 feasibility_only、SCE 前置仅剩滤波/起搏审计」；状态声明一律以 `_meta/freeze_checklist.json` 为唯一来源 | §16 |

---

## 0. 本方案定位与上游契约

### 0.1 定位

提取方案（v2.4.1）产出的是**语义冻结的审计级中间产物**（episode 队列、landmark、三态标签、小时级特征长表、ECG 索引、合同与 QA 报告）；本方案将其转换为**模型可直接消费的数值包**（张量、编码表、样本索引、拟合工件），服务对象为技术文档 §10 的四个核心模型与强基线：

| 模型 | 输入 | 本方案对应打包 |
|---|---|---|
| LR / XGBoost 强基线 | 静态特征 + 时序汇总特征（min/median/max/SD） | 表格特征包（§11.2） |
| SC-common-paired | ECG-available landmarks 的 common 临床变量 | 临床序列包（paired 样本集） |
| SCE-common-paired | 同上 + ECG 波形张量 | 临床序列包 + ECG 张量包（同一批 landmarks） |
| SC-common-all（技术文档命名；本方案以 SC-common-core/extended × 全体 landmarks 实现） | 全体 landmarks 的 common 临床变量 | 临床序列包（deployment 样本集） |
| SCE-deployment | 全体 landmarks + ECG（缺失用 modality mask + availability embedding） | 临床序列包 + ECG 张量包（含缺失模态掩码） |

### 0.2 上游输入契约（只读消费，不回写）

| 输入（提取方案 §8） | 内容 | 本方案用途 |
|---|---|---|
| `cohorts/cohort_mimic_v2.parquet`、`cohort_eicu_v2.parquet` | episode 级队列事实表 | 样本主表、静态特征源 |
| `episodes/mimic_icu_episode_map_final.parquet`（`episode_mapping_version = 'main_tau0'`） | final episode 映射 | 主键与版本固定 |
| `splits/split_assignments_v2.parquet` | 患者级 train/validation/test 划分 | P7 划分应用 |
| `landmarks/landmarks_v2.parquet` | landmark 网格 + 风险集标志 | 样本骨架 |
| `labels/labels_24h_v2.parquet`、`labels_competing_7d_v2.parquet`、`label_adjudications.parquet` | 三态 24h 标签、竞争风险标签、人工裁决表 | P4 标签装配 |
| `features/baseline_static_v2.parquet`、`landmark_context_v2.parquet` | 静态与 landmark 上下文特征 | P3 编码 |
| `features/vitals_realtime_strict_v2.parquet`、`vitals_charttime_retro_v2.parquet` | 生命体征双轨**长表**（bin 聚合字段 + `source_time_type`） | P2 张量化（主/敏感性） |
| `features/vitals_hourly_v2.parquet` | strict 轨**宽表便捷视图**（每 bin 一行，各变量 median 列） | QA 与基线抽查，不作张量化主输入 |
| `features/labs_hourly_v2.parquet`（含 `time_track` 列双轨）、`features/sofa_hourly_v2.parquet`、`features/nee_stream_v2.parquet`（landmark 级汇总）、`features/pf_ratio_v2.parquet` | 检验双轨长表、SOFA 两轨组分、NEE 汇总、P/F 配对 | P2 张量化 / P3 / 亚组 |
| `labels/observation_endpoints_v2.parquet` | 双观察终点（白名单口径） | P4 可观察性复核 |
| `landmarks/eicu_landmarks_v2.parquet`、`labels/eicu_labels_24h_v2.parquet`、`labels/eicu_labels_competing_7d_v2.parquet` | eICU landmark 与标签（`*_episode_min` 分钟坐标 + `phenotype_track`） | eICU 外验包 |
| `features/eicu_vitals_v2.parquet`、`eicu_labs_v2.parquet`、`eicu_gcs_v2.parquet`、`eicu_urine_v2.parquet`、`eicu_support_v2.parquet` | eICU bin 级长表（`charttime_fallback` 语义） | eICU P2 |
| `episodes/eicu_event_time_map.parquet` 及四张专用时间映射 | eICU 规范化事件标识与 episode 坐标 | eICU 溯源 |
| `phenotypes/eicu_phenotype_event_v2.parquet`、`eicu_phenotype_tracks_v2.parquet`、`eicu_infection_pairs.parquet`、`eicu_antibiotic_time_source_summary.parquet` | eICU 三套表型（`feasibility_only`）与候选配对 | eICU 外验分层 |
| `ecg_index/ecg_recording_duration.parquet`、`ecg_index/ecg_patient_describe_v2.parquet` | ECG 时长缓存、患者级 ECG 描述队列 | P5 / 描述性分析 |
| `ecg_index/ecg_landmark_index_v2.parquet` | landmark × 最近合格 ECG（四时间字段、归属、五层级标志） | P6 模态装配 |
| `contracts/sc_common_variable_contract_v2.parquet`、`clinical_observation_whitelist_v2.parquet` | 变量等价合同、观察源白名单 | P8 变量终稿 |
| `qa/*`、`_meta/*` | QA 报告、D0 决策、冻结清单状态 | 前置门禁检查 |
| `E:\clinical_research\MIMIC_IV_3.1\ecg\`（WFDB 文件树） | ECG 波形实体 | P5 波形预处理 |

> 注：`phenotypes/eicu_suspected_infection_events.parquet` 未产出（锁定选对函数 `select_suspected_infection_pairs_locked_v1` 待 mimic-code 审计，实现记录 §7）；当前仅有候选 pair，P-strict 一律 `feasibility_only`。

### 0.3 三条不可违背原则

1. **语义不回头**：本方案不修改队列成员、时间原点、标签值、episode 归属；发现语义问题一律退回提取方案版本升级，不在预处理层「悄悄修正」。
2. **训练集专属拟合**：标准化均值/方差、异常值阈值、插补器、特征筛选、ECG 数据驱动 QC 阈值、归一化参数**只在训练集（`2008 - 2010`、`2011 - 2013`）拟合**，拟合结果落盘冻结后在 validation/test/eICU 上原样应用（技术文档 §7.3 第 6–10 条）。
3. **患者级完整性**：同一 `subject_id` 的全部 landmark 与全部 ECG 只属于一个集合；所有重采样（bootstrap）以患者为单位。

---

## 1. 预处理总览与 DAG

### 1.1 DAG 节点总表

| 节点 | 名称 | 输入 → 输出 | 关键约束 |
|---|---|---|---|
| P0 | 环境与配置锁定 | `configs/preprocess_v1.yaml` → `_meta/preprocess_code_version.json` | 版本、种子、路径固定 |
| P1 | 输入校验与装载 | 上游 Parquet → 内存/视图 + `p1_validation_report` | schema/主键/版本一致性；`main_tau0` 固定 |
| P2 | 临床时序张量化 | 小时级长表 → GRU-D 三元组张量 + 汇总特征表 | 24h×1h 网格；中位数；mask/Δt |
| P3 | 静态与上下文编码 | baseline_static + landmark_context → 静态特征矩阵 | NULL 语义保留；类别编码训练集拟合 |
| P4 | 标签与样本集装配 | 三态标签 + 竞争风险标签 → 样本索引 + 权重 | `outcome_ascertainable = TRUE` 过滤；患者等权 |
| P5 | ECG 波形预处理 | WFDB 文件树 → 12×5000 张量缓存 + QC 标志 | 技术文档 §20 全规范；阈值仅训练集 |
| P6 | ECG 配对与模态装配 | ECG 索引 + 张量缓存 → 模态掩码 + availability | 五层级 availability；最近一份 |
| P7 | 划分应用与训练集拟合 | split 表 + P2–P6 产物 → 拟合工件（scalers/imputers/thresholds） | 仅训练集拟合；工件注册留痕 |
| P8 | SC-common 变量终稿 | 等价合同 → 变量清单 + 单位换算表 | core/extended；合同评级通过前置 |
| P9 | 模型输入打包 | P2–P8 产物 → 四个模型输入包 + 基线表格包 | 包级 manifest + checksum |
| P10 | 预处理 QA 与防泄漏复测 | 全部产物 → `qa_preprocess/*` | 防泄漏十条复测 + 形状/掩码/划分纯度 |

### 1.2 目录规范

```
preprocess/
  configs/     preprocess_v1.yaml
  src/         nodes/p0_env.py … p10_qa.py, lib/{grid.py, ecg.py, scalers.py, leakage.py}
  artifacts/   p2_clinical/, p3_static/, p4_samples/, p5_ecg_cache/, p6_modality/,
               p7_fitted/, p8_contracts/, p9_packages/
  qa_preprocess/  p1_validation_report.md, p10_leakage_report.md, shapes_report.md,
                  split_purity_report.md, ecg_preprocess_qa.md
  tests/       test_grid.py, test_ecg.py, test_labels.py, test_leakage.py, fixtures/
  _meta/       preprocess_code_version.json
```

规范：①每节点独立脚本、I/O schema 校验、中间产物持久化（与提取方案同一工程标准）；②所有拟合工件带 `fitted_on = train` 戳记与内容哈希；③eICU 外验输入包复用同一管线（去 P5/P6，加 P8 eICU 分支），输出 `p9_packages/eicu_sc_common/`；④全部随机行为（打乱、增强、modality dropout）由 P0 注册的种子派生，逐节点记录实际使用种子。

---

## 2. P0 环境与配置锁定

### 2.1 Python 环境

```text
解释器：Kimi Work 托管 Python（项目 .venv 存在时优先项目环境）
核心依赖：duckdb ≥ 实测版本, pandas, numpy, pyarrow, scipy, wfdb, pyyaml, pytest
可选加速：numba（网格聚合）、lmdb（ECG 张量缓存）、pytorch（仅下游训练，预处理不依赖）
```

`requirements-preprocess.txt` 锁定全部版本；`python -m pytest tests/` 为每次运行前置。

### 2.2 配置 Schema（`configs/preprocess_v1.yaml` 关键键）

```yaml
run_id: pp_v1_20260730
seed_root: 20260730
paths:
  data_pipeline_root: data_pipeline/
  ecg_wfdb_root: E:/clinical_research/MIMIC_IV_3.1/ecg/
  out_root: preprocess/artifacts/
episode_mapping_version: main_tau0          # 敏感性：sensitivity_tau30 / sensitivity_tau60 独立运行
time_semantics: strict_available_time        # 双轨：strict_available_time / chart_or_event_time
grid: {window_hours: 24, bin_hours: 1, aggregation: median}
ecg:
  target_fs: 500
  duration_s: 10
  leads: [I, II, III, aVR, aVL, aVF, V1, V2, V3, V4, V5, V6]
  qc_thresholds_from: train
  freshness_hours_main: 24                   # 敏感性：48 / 72
samples:
  label_filter: outcome_ascertainable_true
  weighting: patient_equal                   # 敏感性：landmark_equal
sofa_track: strict_24h                       # 敏感性：carryforward
```

每次运行生成 `_meta/preprocess_code_version.json`：配置哈希、代码 commit、依赖版本、上游产物哈希清单、D0/冻结清单状态快照、实际种子表。

---

## 3. P1 输入校验与装载

### 3.1 校验项（失败即停止，不降级跳过）

1. **schema 一致性**：上游每张表的实际列与提取方案 §8 契约逐列比对（列名、类型、可空性）；
2. **主键与版本**：`episode_mapping_version = 'main_tau0'` 固定过滤；`(episode_id, episode_mapping_version)` 全局唯一；`landmarks_v2` 的 `(episode_key, k)` 唯一；
3. **门禁状态**：`_meta/d0_decision.json` 存在性与主原点声明；`_meta/freeze_checklist.json` 中与本批处理相关的关闭状态记录进报告（未关闭项不阻塞可行性预处理，但阻断正式训练打包标记 `training_ready = false`）；
4. **物理分离**：adjudication 表只读引用，不与自动标签表合并写回；
5. **计数对账**：cohort/landmark/label/feature/ECG 索引的行数与提取方案 `qa/cohort_flow_v2.md` 对账，偏差 >0 即失败。

### 3.2 装载方式

大表（vitals/labs/sofa/nee）用 DuckDB 视图按 `(episode_key, landmark_k)` 分区流式读取，不全量入内存；`pyarrow.dataset` 谓词下推。装载即注册每表 `content_hash`，供 P10 溯源。

**键重命名映射（v1.1 新增，P1 装载时统一执行）**：

| 本方案键 | MIMIC 产物列 | eICU 产物列 |
|---|---|---|
| `episode_key` | `episode_id` | `episode_id` |
| `subject_key` | `subject_id` | `uniquepid` |
| `landmark_k` | `k` | `k` |
| landmark 时间 | `t_landmark_ts`（TIMESTAMP） | `t_landmark_offset_min`（分钟 → /60 转小时） |

eICU 一律先将 `*_episode_min` / `*_offset_min` 换算为小时再入张量化；`phenotype_track` 列随样本骨架保留（P-strict/P-clinical/P-explicit 分开评估）。

**行数对账基线（v1.1 新增，P1 校验项）**：cohort_mimic 31,910；landmarks 443,225；labels_24h 443,225；cohort_eicu 62,251（P-clinical 35,974 / P-explicit 26,079 / P-strict 198）；eicu_landmarks 743,002；ecg_landmark_index 211,360。偏差 >0 即 P1 失败（与提取实现记录 §5.1 对账）。

---

## 4. P2 临床时序张量化（GRU-D 三元组 + 汇总特征）

### 4.1 网格与取值（技术文档 §15.3）

- 每个 landmark：取 `t_landmark` 前 24h，按 1h 分箱 → 24 个 bin；t=0 landmark 允许使用 sepsis onset 前数据（沿用提取产物的 bin 定义，不重新截断）。
- **输入文件（v1.1 更正）**：vitals 读 `vitals_realtime_strict_v2`（主）/ `vitals_charttime_retro_v2`（敏感性）长表；labs 读 `labs_hourly_v2`（`time_track` 列双轨）。`vitals_hourly_v2` 为 strict 轨宽表便捷视图，仅用于 QA 与基线抽查。
- 同 bin 多条：已由提取层聚合（中位数），本层仅校验 `aggregation_method = 'median'` 与 `max_available_time ≤ t_landmark`（strict 轨）；不再二次聚合。提取产物 bin 区间约定为 `bin b = [t_lm-(b+1)h, t_lm-bh)`（bin0 最近），本层直接沿用。
- 变量通道顺序由 P8 合同清单冻结，通道索引写入 manifest。
- **FiO2 通道决策点（v1.1 登记）**：bin 级 labs 无 FiO2 通道（13 项 lab_name；FiO2 仅在 landmark 级 `pf_ratio_v2`）。主方案：P/F 以 landmark 级 `pf_ratio_v2` 派生为 landmark 级特征（`pf_ratio / pf_pairing_gap_min / fio2_source`）；bin 级 P/F 时序通道仅作敏感性，需在 P2 从 chartevents FiO2 itemid 与 labevents PaO2 按窗重建。

### 4.2 输出张量（每 landmark 一份，按样本索引组装）

```text
X_seq   : float32 [V, 24]   -- 变量值（未标准化；标准化在 P7）
M_seq   : bool    [V, 24]   -- 观测 mask（1 = 该 bin 有有效观测）
D_seq   : float32 [V, 24]   -- Δt：距该变量上一次有效观测的小时数（GRU-D 三元组之三）
meta    : episode_key, landmark_k, hours_since_sepsis, set_name
```

**Δt 计算规则**：沿 episode 内该 landmark 之前的时间轴，逐变量计算距上次有效观测的间隔；无任何历史观测时记 `Δt = NaN → 编码为 cap 值（48h）+ mask = 0`；超过 cap 按 cap 截断并保留真实值于 `D_seq_raw`（审计用）。双轨：`time_semantics = strict_available_time` 时观测有效性以 `available_time` 判定；`chart_or_event_time` 轨以 `event_time` 判定（敏感性）。

### 4.3 汇总特征表（基线用）

每 landmark × 变量输出 `min / median / max / sd / n_obs / last_value / last_obs_age_h`（24h 窗内）；无观测变量整行缺失 + 缺失指示列。与静态特征（P3）横向拼接成基线表格包（§11.2）。

### 4.4 SOFA 与 NEE 流

- SOFA：按 `sofa_track` 选择 `strict_24h`（主）或 `carryforward`（敏感性）轨的逐组分与 `sofa_total_complete`；`partial` 总分不进主输入（提取方案 §5.4）；CV 亚组字段固定 `sofa_realtime_strict_24h_cv`（首个有效 landmark、患者级）。**派生规则（v1.1 显式化）**：提取产物 `sofa_hourly_v2` 为 per-landmark 组分表（`sofa_purpose='realtime_feature'` × `sofa_evidence_track` 两轨），本层取 strict 轨、每患者（episode）最小 k 的 `sofa_cv_original` 作为患者级 `sofa_realtime_strict_24h_cv`，在 P4 装配时生成并由 P7 冻结；缺失（首 landmark 无 CV 输入）记 `cv_subgroup_missing` 并单独计数，不得用后续 landmark 回填。
- NEE 流：提取产物 `nee_stream_v2` 为 landmark 级汇总（`nee_max_24h / nee_median_24h / nee_auc_24h / nee_current` + 各药 24h 最大速率），**无 bin 级序列**；`nee_current` 逐小时通道由 P2 按窗口从 `mimiciv_derived.norepinephrine_equivalent_dose` 重建（5min 网格重采样至 1h，bin 内最大值为该 bin 代表值，与「24h 最差」语义一致；提取层不改动）；`vasopressor_burden` 为探索通道，默认关闭。

---

## 5. P3 静态与 landmark 上下文编码

### 5.1 输入与合并

`baseline_static_v2`（每 episode 一行）左连接 `landmark_context_v2`（每 episode × landmark 一行）至样本骨架（`landmarks_v2` ∩ 风险集）。连接键 `(episode_key, landmark_k)`；任何一对多连接直接失败。

### 5.2 编码规则

| 特征 | 处理 | 缺失语义 |
|---|---|---|
| 年龄 | 数值，截尾至 [18, 110]（截尾标志保留） | 不应缺失；缺失即失败 |
| 性别 | 类别 → one-hot（M/F/Other/Unknown） | Unknown 为独立类 |
| 体重/身高 | 数值；极端值按技术文档 §6.2 规则打标不裁剪（<40 / >150 kg 打 `extreme_weight_flag`） | 保留缺失 + `weight_missing` 指示；**不得**用住院后测量回填早期 landmark（上游已保证） |
| 入院类型/来源、ICU 类型 | 类别 → 训练集频率表 + 低频合并（阈值训练集定）→ one-hot | Unknown 独立类 |
| Δ_ICU-sepsis | 数值（小时，可正可负） | 不应缺失 |
| `charlson_prior` | 数值 + `charlson_prior_available` 指示 | **NULL 保留**；数值填充仅由 P7 训练集拟合的缺失处理器完成（主方案：训练集中位数 + 指示变量）；`charlson_discharge_coded` 不进任何主输入 |
| 敏感性标志（外院转入/ECMO/移植/DNR-CCO） | 0/1 描述性列，仅分层与敏感性分析，**不进主模型特征** | — |
| 当前支持状态（通气/血管活性药/尿量不足） | 0/1 数值 | 按上游定义 |

### 5.3 类别编码器冻结

所有类别映射（含低频合并清单、Unknown 桶）在 P7 于训练集拟合后落盘 `p7_fitted/categorical_encoders.json`；validation/test/eICU 未见类别一律映射 Unknown 并计数报警。

---

## 6. P4 标签与样本集装配

### 6.1 主标签过滤（技术文档 §2.1/§15.2，提取方案 §4.1）

- 主分析样本：`outcome_ascertainable = TRUE` 的 landmark；`y_24h ∈ {0, 1}`。
- `label_adjudications` 中 `adjudication_status = 'adjudicated'` 者以 `label_final_status` 覆盖（覆盖比例进 QA）；preliminary 与 final 双列保留。**pending 期行为（v1.1 写明）**：当前 `label_adjudications` 为 78 条 `pending`、尚无 `adjudicated` 行——pending 期一律按 preliminary 标签处理（即这些 landmark 维持 `outcome_ascertainable = FALSE` 被过滤），pending 计数进 QA；待人工裁决完成并重跑标签后按覆盖规则生效。
- 敏感性轨：急性转出分别按「存活离院」与「最坏情景」重编码（两套独立样本索引，不与主索引混用）。

### 6.2 竞争风险标签（DeepHit 用）

- 事件时间离散化：`(t_landmark, t_landmark + 168h]` 按 6h 分 28 个离散区间；`event_type ∈ {0,1,2,3}`（0 删失 / 1 死亡 / 2 存活出院 / 3 急性转出）。
- 输出 `event_time_bin`（首个事件落入的区间；删失为 28）与 `event_type`；eICU 侧时间已为 `*_episode_min`，按同一规则离散。

### 6.3 样本集（四套索引，独立落盘）

| 样本集 | 定义 | 用途 |
|---|---|---|
| `idx_deployment_all` | 全体部署队列：风险集 ∩ 标签可判定 | SC-common-all / SCE-deployment 训练与评估 |
| `idx_paired_ecg` | `idx_deployment_all` ∩ ECG-available（`ecg_selected_for_model = TRUE`，主 24h 时效） | **唯一主要比较**（SC-common-paired vs SCE-common-paired） |
| `idx_ecg_sensitivity_48h / _72h` | 时效窗放宽的 ECG-available | 敏感性 |
| `idx_describe_patient_ecg` | 患者级 ECG 描述队列（t_sepsis ±24h 有 ECG） | 仅描述，不训练 |

主积分网格 `k ∈ [0, 11]` 的标志列 `in_main_grid` 随索引落盘；72–168h landmark 单独 `idx_explore_late`。

### 6.4 样本权重（技术文档 §15.2）

- 主训练：**患者等权**——`w = 1 / n_landmarks(patient)`，使每位患者总损失权重为 1；
- 敏感性：landmark 等权 `w = 1`；
- 权重列随样本索引落盘，训练代码禁止重算。

---

## 7. P5 ECG 波形预处理（技术文档 §20 全规范落地）

### 7.1 处理管线（`src/lib/ecg.py`，逐 study 处理）

```text
① WFDB header 解析：fs、增益、基线、单位、导联名、样本数、设备滤波状态
② 导联重排：统一 12 导联标准顺序 [I, II, III, aVR, aVL, aVF, V1–V6]；
   缺失导联 → 零张量填充 + 12 维 lead_mask（0 = 缺失）
③ 重采样至 500 Hz（原生 500 Hz 则恒等；外部库适配规则见 §20，本项目 MIMIC-IV-ECG 原生即 500 Hz）
④ 截长补短至 10 s（12 × 5000）；不足 10 s 者按结构性 QC 判不合格（§7.2）
⑤ 增益恢复物理单位 mV（按 header 增益/基线）
⑥ 工频陷波：先评估设备是否已预滤波；仅在**训练集**质量评估证明必要时应用 60 Hz 陷波，
   无陷波版本保留为敏感性（决策记录进 ecg_preprocess_qa.md）
⑦ 基线漂移：不默认 0.5 Hz 高通；先审计设备滤波状态，仅用**训练集**比较
   {无额外高通, 0.05 Hz 高通, 0.5 Hz 高通} 三方案的信号质量与形态保真度；
   滤波器类型/截止/阶数/相位模式/边界处理在模型训练前锁定，锁定后全集一致
⑧ 极端振幅与平线/饱和/导联脱落检测（结构性 QC，全集统一规则）
⑨ 起搏信号检测与标记（pacing_flag）
⑩ 信号质量指标计算：SNR、基线漂移幅度、饱和比例、极端振幅比例、导联相关性
   ——阈值仅训练集确定并冻结（数据驱动 QC）
⑪ 归一化：per-record z-score（均值/SD 由该记录自身计算）或全局统计（训练集估计），
   主方案与备选在配置中显式选择并锁定
⑫ 张量缓存：float32 [12, 5000] + lead_mask + qc 标志 + 处理版本戳
```

交叉检查：NeuroKit2 `ecg_clean()` / `ecg_peaks()` 仅作参照，不作为正确性标准（技术文档 §20）。

### 7.2 两层 QC 的执行（与提取方案 §5.8 对齐）

- **结构性 QC**（全集统一，P5 内执行并输出 `qc_structural_*` 标志）：文件可读、时长 ≥10 s、fs/增益可解析、导联数 ≥ 预登记下限、非全平线、无损坏；
- **数据驱动 QC**（阈值训练集定，P7 冻结后应用）：SNR、基线漂移、饱和比例、极端振幅、导联相关性；
- P6 使用提取索引中的五层级 availability；P5 的输出标志与之对账，不一致进 QA（索引冻结在先，P5 不重判 availability，仅提供波形级证据）。

### 7.3 缓存与性能

- 缓存键：`study_id`；内容：`signal float32[12,5000]`、`lead_mask bool[12]`、`qc_flags`、`header_meta`、`preprocess_version`；
- 规模估算：每份约 240 KB（float32）；仅张量化「队列关联」的 ECG（`ecg_landmark_index_v2` 引用集 + 训练患者 ECG 全集用于 inductive SSL 次要分析），不全量处理 800,035 份；
- 存储：`lmdb`（随机读）或分片 `npz`（按 set_name 分片）；训练 DataLoader 懒加载；多进程（`multiprocessing`，进程数可配）+ 断点续跑（已完成 study 跳过校验）。

### 7.4 数据增强（仅训练集；技术文档 §20-13）

小范围时间平移（≤0.5 s）、振幅缩放（0.9–1.1）、轻度加噪、随机导联 dropout（同步清 lead_mask）；**禁止**改变临床形态的大幅裁剪或需重采样回 10 s 的操作；validation/test/外部集一律不增强。增强在 DataLoader 在线执行，种子由 P0 派生。

---

## 8. P6 ECG 配对与模态装配

### 8.1 配对（不变规则，防泄漏复测）

- landmark 级：`ecg_selected_for_model = TRUE` 且 `ecg_available_time_assumed ≤ t_landmark`（Q1 复测）；多份取最近一份通过 QC 者；
- 时效窗：主 24h；敏感性 48h/72h 独立索引（§6.3）；
- 归属：仅 `same_hospitalization` 与 `auditable_pre_admission_encounter`（后者打 `pre_admission_ecg` 标志）。

### 8.2 模态掩码与 availability embedding（技术文档 §11）

- SCE-common-paired：**不使用**任何缺失模态训练（样本恒有 ECG）；
- SCE-deployment：训练期 modality dropout（概率 0.3，仅训练集、在线执行）+ availability embedding（可学习向量，初始化种子固定）；
- eICU：ECG availability 恒 0，不走门控退化——eICU 输入包仅含 SC 临床分支（技术文档 §11.1）。

### 8.3 输出

`p6_modality/modality_index.parquet`：`episode_key, landmark_k, study_id, ecg_available_time_assumed, lead_mask_path, availability_flag, modality_dropout_group(train only)`。

---

## 9. P7 划分应用与训练集专属拟合

### 9.1 划分应用

`split_assignments_v2` 按 `subject_id` 连接全部样本索引；校验：同一患者仅一个集合；同一患者 landmark 不跨集合；`2020 - 2022` 组患者不出现在任何训练/验证/测试索引。

### 9.2 拟合工件（全部 `fitted_on = train`，内容哈希注册）

| 工件 | 内容 | 应用范围 |
|---|---|---|
| `scaler_clinical_seq.json` | 时序通道标准化参数（均值/SD 或分位数，主方案 z-score；按通道、仅训练集有观测值计算） | 全集 |
| `scaler_static.json` | 静态数值特征标准化参数 | 全集 |
| `outlier_thresholds.json` | 生理范围外取值处理阈值（技术文档 §8 数据字典） | 全集 |
| `imputers.json` | 静态特征缺失处理器（训练集中位数/众数 + 指示变量清单）；时序不做值插补（mask 机制） | 全集 |
| `categorical_encoders.json` | 类别映射与低频合并清单 | 全集 |
| `feature_selection.json` | 若启用特征筛选，仅在训练集完成并冻结清单 | 全集 |
| `ecg_quality_thresholds.json` | ECG 数据驱动 QC 阈值 | 全集 |
| `ecg_norm_params.json` | ECG 归一化全局参数（若选全局方案） | 全集 |
| `filter_decision.json` | 陷波/基线滤波审计结论与最终参数 | 全集 |

任何工件在 validation/test 上重新拟合的行为视为管线失败（P10 静态扫描 + 运行时断言）。

---

## 10. P8 SC-common 变量终稿与单位统一

- 依据 `contracts/sc_common_variable_contract_v2.parquet` 的逐变量评级，输出本批处理的变量清单：`sc_common_core`（默认）或 `sc_common_extended`（合同通过后）；清单带版本戳，四模型共享同一清单（配对/deployment 仅样本集不同）。
- 单位统一按合同 `conversion_rule` 在 P2/P3 前执行（如 eICU 体温 °F→°C、FiO₂ 0–1→% 显示换算）；换算记录进 `p8_contracts/unit_conversion_log.md`。
- eICU 分支：同清单映射 eICU 列；不满足合同的变量不得静默纳入，降级进 `qa/sc_common_contract_v2.md` 差异表。

---

## 11. P9 模型输入打包

### 11.1 序列模型包（SC/SCE 四模型）

每模型 × 每集合（train/validation/test）输出：

```text
p9_packages/<model>/<set_name>/
  index.parquet        -- episode_key, subject_key, landmark_k, k, hours_since_sepsis,
                       -- y_24h, label_status, weight, in_main_grid,
                       -- event_type, event_time_bin（竞争风险）,
                       -- study_id / availability_flag（SCE）, modality_dropout_group
  seq/                 -- X_seq/M_seq/D_seq（float32 npz 分片，键 = sample_uid）
  static/              -- 静态矩阵（float32 npz）
  ecg/                 -- SCE 包：指向 p5 缓存的懒加载映射（不复制波形）
  manifest.json        -- 变量清单版本、通道顺序、拟合工件哈希、样本数、事件数、生成时间
```

样本主键 `sample_uid = SHA256(episode_key | landmark_k)`；四模型的 paired 变体共享同一 `idx_paired_ecg`，保证「完全相同的 landmarks 上训练和测试」（技术文档 §10.4）。

### 11.2 基线表格包（LR / XGBoost）

`p9_packages/baseline_tabular/<set_name>/features.parquet`：静态特征 + P2 汇总特征 + 标签/权重列；列级字典 `columns.json`（来源节点、单位、缺失指示列集合）。

### 11.3 eICU 外验包

`p9_packages/eicu_sc_common/`：同结构（无 ecg/）；`time_semantics` 与拟合工件沿用 MIMIC 训练集冻结版本；`phenotype_track` 列保留（P-strict/P-clinical/P-explicit 分开评估）；包级 `external = true` 标记。

### 11.4 训练就绪标记

仅当提取方案冻结清单相关项关闭且 P10 复测全绿时，manifest 写 `training_ready = true`；否则 `false` 并附阻塞清单——训练入口脚本须校验该标记。

## 12. P10 预处理 QA 与防泄漏复测

### 12.1 防泄漏十条复测（技术文档 §7.3 → 本层证据）

| # | 断言 | 本层证据 |
|---|---|---|
| 1 | `ecg_available_time_assumed ≤ t_landmark` | P6 索引全量断言 |
| 2 | 全部特征 `available_time ≤ t_landmark`（strict 轨） | P2 输入校验（`max_available_time` 列）+ 抽样重算 |
| 3 | 结局窗起点 > landmark | P4 标签复算抽查 |
| 4 | 同一患者不跨 train/validation/test | P7 划分纯度报告 |
| 5 | 同一患者 landmark 不跨 calibration/test | 当前无独立 calibration 集，报告 N/A 并留检测点 |
| 6–8 | 标准化/异常值阈值/插补器仅训练集拟合 | 工件 `fitted_on` 戳 + 静态扫描 + 运行时断言 |
| 9 | 特征筛选仅训练集 | 工件清单 |
| 10 | ECG 质量阈值仅训练集 | `ecg_quality_thresholds.json` 戳记 + 复算 |

### 12.2 形状与完整性报告（`shapes_report.md`）

- 每包样本数、患者数、landmark 数、阳性 landmark 数（与提取 Feasibility Table 对账）；
- 张量形状一致性（`[V, 24]`、`[12, 5000]`）、dtype、NaN 策略审计（X_seq 缺失位置必须 mask=0 且值为 0）；
- Δt 分布、mask 密度（按变量 × set_name）；汇总特征缺失率；
- ECG 缓存命中率、QC 各级通过率（按 set_name 分列，阈值版本一致）。

### 12.3 划分纯度与患者级完整性（`split_purity_report.md`）

- 患者 → 集合一对一校验；paired 索引在两模型包间逐样本一致（哈希对账）；
- paired 样本的 SC/SCE 输入差异仅为 ECG 通道（结构断言）。

### 12.4 ECG 预处理 QA（`ecg_preprocess_qa.md`）

- 滤波审计结论（陷波必要性、基线三方案比较）与最终参数；
- 导联缺失/平线/饱和/起搏标记分布；`recording_duration` 计算抽查；
- 训练集 vs 全体的质量指标分布（阈值合理性留痕，不回改）。

---

## 13. 敏感性分析开关（预登记，全部经配置驱动、独立 run_id）

| 开关 | 取值 | 说明 |
|---|---|---|
| `time_origin` | locked_sepsis_time / suspected_infection / icu_admission | 技术文档 §4.1/§15.2；随 D0 锁定主值 |
| `time_semantics` | strict_available_time / chart_or_event_time | 双轨主/敏感性 |
| `ecg.freshness_hours_main` | 24 / 48 / 72 | ECG 时效窗 |
| `sofa_track` | strict_24h / carryforward | SOFA 证据轨 |
| `episode_mapping_version` | main_tau0 / sensitivity_tau30 / sensitivity_tau60 | episode 合并阈值 |
| `samples.weighting` | patient_equal / landmark_equal | 训练加权 |
| `missing.static` | median_indicator（主） / mice（仅静态，敏感性） | 技术文档 §15.2 |
| `vitals_track` | realtime_strict / charttime_retro | 生命体征双轨 |
| `ecg.multi` | most_recent（主） / sequence_encoding（敏感性） | 多份 ECG 聚合 |
| `ecg.notch` / `ecg.baseline_filter` | 按 P7 审计锁定值 / 备选方案 | 仅训练集决定 |

每一开关组合 = 独立 `run_id` + 独立 artifacts 目录 + 独立 manifest；禁止在同一 run 内混用。

## 14. 输出目录与工件注册

```
preprocess/artifacts/
  p2_clinical/<run_id>/        # seq npz 分片 + 汇总特征 parquet
  p3_static/<run_id>/          # 静态矩阵 + 编码字典
  p4_samples/<run_id>/         # 四套样本索引 + 权重 + 竞争风险标签
  p5_ecg_cache/<version>/      # lmdb/npz 波形缓存 + QC 标志（跨 run 共享，按版本）
  p6_modality/<run_id>/        # modality_index.parquet
  p7_fitted/<run_id>/          # 全部训练集拟合工件（含哈希注册表 registry.json）
  p8_contracts/<run_id>/       # 变量清单 + 单位换算日志
  p9_packages/<model>/<set>/   # 模型输入包（含 manifest）
  p10_qa/<run_id>/             # 全部 QA 报告（与 qa_preprocess/ 同步）
```

`registry.json` 记录每个工件的：内容哈希、生成节点、输入哈希、`fitted_on`、生成时间、代码 commit——任何下游训练 run 必须引用 registry 中的工件哈希。

## 15. Python 工程规范

1. **测试**：`pytest` 全覆盖关键纯函数（网格、Δt、编码、标签装配、ECG header/滤波、泄漏断言）；`tests/fixtures/` 内置合成小数据（含边界案例：t+24h 出院、缺失体重、无 ECG、跨 stay episode、NULL t_sepsis 排除样本）；CI 级别：每次 run 前 `pytest -q` 全绿方可执行。
2. **确定性**：所有随机源（shuffle、augmentation、dropout、MICE）由 `seed_root` 派生（`numpy.random.Generator(PCG64(seed_root, node_id))`）；禁止全局 `random.seed`。
3. **dtype 约定**：数值 float32，mask bool，计数 int32，时间戳保留上游类型（不落 float）；NaN 只允许出现在审计列，不允许进入模型输入张量（缺失由 mask 表达）。
4. **性能**：大表流式（DuckDB 视图 + pyarrow 谓词下推）；ECG 多进程 + 断点续跑；分片大小可配（默认 4096 样本/片）；全程峰值内存上报 QA。
5. **日志与中断**：结构化日志（node、run_id、样本计数、耗时、警告码）；任何 schema/断言失败 = 非零退出，不产出部分包（除标记 `partial_debug` 的调试运行）。
6. **只读纪律**：对 `data_pipeline/` 与源库零写入；ECG 缓存写入仅限 `p5_ecg_cache/`。

## 16. 风险与待决（与提取方案冻结清单联动）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| PP-1 | D0 已锁定（出口 B：`suspected_infection_time`，PI 批准 2026-07-30；见 `_meta/d0_decision.json`） | 无（主口径固定） | 敏感性轨（`max(sofa_time, si_time)` / `icu_admission`）独立 run_id |
| PP-2 | 提取层专项语义审计未关闭（eICU lab/diagnosis 时间、storetime 策略） | strict 轨完整性 | P2 按 `source_time_type` 过滤降级变量；QA 单列 |
| PP-3 | ECG 滤波审计需训练集信号统计 | 滤波决策 | P5 先跑训练集子集审计再全量；决策冻结前 `training_ready = false` |
| PP-4 | 拟合工件被误在验证/测试重拟合 | 泄漏 | P10 静态扫描 + 运行时断言 + registry 哈希引用 |
| PP-5 | ECG 缓存体积与 IO 瓶颈 | 训练吞吐 | lmdb + 分片 + 懒加载；仅队列关联集张量化 |
| PP-6 | eICU 变量合同评级未完成 | 外验包 | eICU 包仅在合同评级通过后生成；此前仅可行性统计 |
| PP-7 | paired 索引在两模型包漂移 | 主要比较有效性 | P10 哈希对账；不一致即失败 |
| PP-8 | 增强/丢弃种子不可复现 | 复现性 | 种子注册表 + 每 epoch 派生记录 |
| PP-9 | **（已关闭，2026-07-30）** 提取层 31 项冻结清单已全部关闭，冻结标签 `SEPSIS-MM-DYN-data-pipeline-v2.4.1-freeze` 生效（以 `_meta/freeze_checklist.json` 为唯一状态源）；eICU 表型经 PI 签署但外验包维持 `feasibility_only`（Robustness under phenotype shift）；剩余阶段项：陷波/基线滤波/起搏检测审计（P5/P7 波形层，SCE 训练前置） | eICU 外验包定位与 SCE 波形层 | MIMIC 包 `training_ready=true`；eICU 包仅评估用；SCE 训练前完成滤波审计 |

## 17. 验收标准

本方案一次运行视为通过，当且仅当：

1. `pytest -q` 全绿；2. P1 校验零失败；3. P10 十条防泄漏复测全过；4. 四模型包 + 基线包 + eICU 包（合同通过后）manifest 完整、计数与提取层 Feasibility Table 对账一致；5. paired 索引跨包哈希一致；6. 拟合工件全部 `fitted_on = train` 且 registry 完整；7. 敏感性轨与主轨物理分离。

## 18. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：基于技术文档 v1.9 与提取方案 v2.4.1 的提取后预处理方案——P0–P10 DAG；GRU-D 三元组张量化与汇总特征；静态/上下文编码（charlson NULL 语义保留）；三态标签过滤与四套样本索引、患者等权；ECG §20 全规范 Python 落地（两层 QC、滤波审计、增强仅训练集、版本化缓存）；模态掩码与 availability embedding；训练集专属拟合工件注册；SC-common 合同驱动变量终稿；四模型 + 基线 + eICU 输入包；P10 防泄漏复测；敏感性开关矩阵；工程规范与验收标准。 |
| v1.1 | 2026-07-30 | **对齐修订**（依据《数据提取管线实现与运行记录 v1.0》实测产物）：①更正 vitals 三文件角色（张量化读双轨长表，`vitals_hourly_v2` 为宽表便捷视图）；②P1 键重命名映射与行数对账基线（31,910 / 443,225 / 62,251 / 743,002）；③NEE 产物为 landmark 级汇总，`nee_current` 逐小时通道由 P2 按窗重建；④登记 FiO2 通道决策点（bin 级 labs 无 FiO2，P/F 默认 landmark 级）；⑤`sofa_realtime_strict_24h_cv` 派生规则显式化（strict 轨、患者级最小 k、缺失不回填）；⑥adjudication pending 期行为写明；⑦风险表 PP-1 状态更新、新增 PP-9（提取产物原型/可行性版，`training_ready=false` 门禁维持）；⑧输入契约补 pf_ratio_v2、observation_endpoints、eICU 全套与 event_time_map，标注 suspected_infection_events 未产出。处理规则、estimand 与统计设计不变。 |

---

## 附录 A：关键 Python 代码骨架

### A.1 GRU-D 三元组构建（`src/lib/grid.py`）

```python
import numpy as np
import pandas as pd

CAP_HOURS = 48.0

def build_triplet(df_var: pd.DataFrame, t_landmark, window_h: int = 24):
    """df_var: 单 episode 单变量的 bin 级记录
    （列：bin_start, value, max_available_time），按 bin_start 排序。
    返回 X, M, D（长度 window_h 的 float32 数组）。"""
    x = np.zeros(window_h, dtype=np.float32)
    m = np.zeros(window_h, dtype=bool)
    d = np.full(window_h, np.nan, dtype=np.float32)
    grid_start = t_landmark - pd.Timedelta(hours=window_h)
    last_obs_t = None
    for b in range(window_h):
        bin_start = grid_start + pd.Timedelta(hours=b)
        row = df_var[df_var["bin_start"] == bin_start]
        if last_obs_t is not None:
            d[b] = (bin_start - last_obs_t).total_seconds() / 3600.0
        if len(row) and row["max_available_time"].iloc[0] <= t_landmark:
            x[b] = np.float32(row["value"].iloc[0])
            m[b] = True
            last_obs_t = bin_start
            d[b] = 0.0
    d = np.where(np.isnan(d), CAP_HOURS, np.minimum(d, CAP_HOURS)).astype(np.float32)
    x[~m] = 0.0
    return x, m, d
```

### A.2 训练集专属标准化（`src/lib/scalers.py`）

```python
import json, hashlib
import numpy as np

def fit_channel_scaler(x: np.ndarray, m: np.ndarray) -> dict:
    """x: [N, V, T], m: [N, V, T]（仅训练集）。按通道用有观测值估计。"""
    params = {}
    for v in range(x.shape[1]):
        vals = x[:, v, :][m[:, v, :]]
        params[str(v)] = {"mean": float(vals.mean()), "sd": float(vals.std() + 1e-8)}
    params["fitted_on"] = "train"
    blob = json.dumps(params, sort_keys=True).encode()
    params["content_hash"] = hashlib.sha256(blob).hexdigest()
    return params

def apply_channel_scaler(x, m, params):
    for v in range(x.shape[1]):
        p = params[str(v)]
        x[:, v, :] = np.where(m[:, v, :], (x[:, v, :] - p["mean"]) / p["sd"], 0.0)
    return x
```

### A.3 ECG 单份处理（`src/lib/ecg.py`）

```python
import numpy as np
import wfdb

LEADS = ["I","II","III","aVR","aVL","aVF","V1","V2","V3","V4","V5","V6"]

def load_and_standardize(record_path: str, target_fs: int = 500, dur_s: int = 10):
    rec = wfdb.rdrecord(record_path)
    fs, sig, units = rec.fs, rec.p_signal, rec.units
    gains, baselines = rec.adc_gain, rec.baseline
    # ③ 重采样到 500 Hz（原生 500 Hz 时恒等；否则 scipy.signal.resample_poly）
    # ⑤ 增益恢复 mV：(raw - baseline) / gain 在 rdrecord 已完成物理量化时跳过
    out = np.zeros((12, target_fs * dur_s), dtype=np.float32)
    lead_mask = np.zeros(12, dtype=bool)
    name_to_idx = {n.strip(): i for i, n in enumerate(rec.sig_name)}
    for li, lead in enumerate(LEADS):
        if lead in name_to_idx:
            s = sig[:, name_to_idx[lead]][: target_fs * dur_s]
            if len(s) == target_fs * dur_s:
                out[li] = s.astype(np.float32)
                lead_mask[li] = True
    qc = {"flatline": bool((out.std(axis=1) < 1e-4).all()),
          "n_leads": int(lead_mask.sum())}
    return out, lead_mask, qc, {"fs": fs, "units": units}
```

### A.4 防泄漏运行时断言（`src/lib/leakage.py`）

```python
def assert_train_fitted(artifact: dict):
    assert artifact.get("fitted_on") == "train", "拟合工件非训练集来源"

def assert_split_purity(sample_df):
    dup = sample_df.groupby("subject_key")["set_name"].nunique()
    assert (dup == 1).all(), "存在跨集合患者"

def assert_ecg_leakage_free(modality_index):
    bad = modality_index[
        modality_index["ecg_available_time_assumed"] > modality_index["t_landmark_ts"]]
    assert len(bad) == 0, "ECG 可用时间晚于 landmark"
```

---

## 附录 B：配置样例（`configs/preprocess_v1.yaml`，节选）

```yaml
run_id: pp_v1_20260730
seed_root: 20260730
episode_mapping_version: main_tau0
time_origin: locked_sepsis_time        # D0 锁定后回填实际字段名
time_semantics: strict_available_time
grid: {window_hours: 24, bin_hours: 1, aggregation: median, delta_cap_hours: 48}
sofa_track: strict_24h
samples: {label_filter: outcome_ascertainable_true, weighting: patient_equal}
ecg:
  target_fs: 500
  duration_s: 10
  leads: [I, II, III, aVR, aVL, aVF, V1, V2, V3, V4, V5, V6]
  freshness_hours_main: 24
  qc_thresholds_from: train
  notch_decision: pending_p7_audit
  baseline_filter_decision: pending_p7_audit
  normalization: per_record_zscore      # 备选：global_train_stats
  augmentation: {time_shift_s: 0.5, amp_scale: [0.9, 1.1], noise_snr_db: 20, lead_dropout_p: 0.1}
sce_deployment: {modality_dropout_p: 0.3}
```

---

## 附录 C：单元测试清单（`tests/`）

| 测试 | 关键用例 |
|---|---|
| `test_grid.py` | 中位数聚合校验；mask/Δt 计算（含无历史观测 cap）；t=0 使用 onset 前数据；`max_available_time > t_landmark` 的记录被剔除 |
| `test_static.py` | charlson NULL + 指示列；类别 Unknown 桶；极端体重打标不裁剪 |
| `test_labels.py` | 三态过滤（`outcome_ascertainable`）；adjudicated 覆盖；急性转出两套敏感性索引互斥；患者等权权重和 = 1 |
| `test_ecg.py` | header 解析；导联重排与缺失 lead_mask；长度不足判 QC 不合格；recording_duration = N/fs；增强不改变张量形状且仅训练集启用 |
| `test_split.py` | 患者级纯度；`2020 - 2022` 不出现；paired 索引跨包一致 |
| `test_leakage.py` | 工件 `fitted_on` 断言；`ecg_available_time_assumed ≤ t_landmark`；标准化前后 mask 不变 |

---

*本方案 v1.0 与技术文档 v1.9、提取方案 v2.4.1 构成三级文档链：技术文档（预注册 estimand）→ 提取方案（语义冻结的数据产物）→ 本方案（模型可训练输入）。冲突之处以技术文档为准；涉及提取语义的问题退回提取方案升级，不在本层修正。*
