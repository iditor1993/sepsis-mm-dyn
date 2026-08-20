# SEPSIS-MM-DYN 数据预处理管线实现与运行记录 v1.1

- 文档版本：v1.1
- 创建日期：2026-07-30
- 上游依据：①《SEPSIS-MM-DYN 数据预处理方案 v1.1》（下称「预处理方案」）；②《SEPSIS-MM-DYN 数据提取管线实现与运行记录 v1.0》（提取产物来源）；③《项目技术文档 v1.9》《数据提取方案 v2.4.1》（语义源头）
- 文档定位：**实现与运行记录（入门向）**。记录按预处理方案 v1.1 实现并运行 `preprocess/` 管线的全过程与产物解读，供项目成员学习。处理规则以预处理方案 v1.1 为准，本文引用其章节号（§）而不复制正文。
- 当前状态：**冻结后重跑完成（v1.1 状态回写）**。数据提取管线已正式冻结（标签 `SEPSIS-MM-DYN-data-pipeline-v2.4.1-freeze`，31/31 项关闭，以 `_meta/freeze_checklist.json` 为唯一状态源）；MIMIC 模型包 `training_ready=true`；eICU 包维持 `feasibility_only`（外验评估用）。**v1.1 修订**：补记 E-2（ED 四条件）/E-4（数据驱动 QC）/E-5（冻结配对）后的重跑结果；v1.0 中 paired 相关旧数字（69,231/27,402/14,112、112,066 选片）标注为「冻结前可行性运行」，以本版数字为准。

---

## 1. 预处理是什么（入门向）

### 1.1 为什么需要这一层

数据提取层产出的是**语义冻结的审计级中间产物**（队列、landmark、三态标签、按小时分箱的长表、ECG 索引）——它们回答了「谁在什么时候测了什么值」，但模型不能直接吃：

- 长表是「行 = 一个变量在一个小时桶里的中位数」，模型要的是「矩阵 = 一个患者在一个预测时点上 21 个变量 × 24 小时」；
- 静态特征是文字（性别、科室），模型要数值编码；
- 标签要装配成带权重的训练样本，且任何统计量（均值、插补值）**只能看训练集**。

预处理层就是把这些审计级产物转换成**模型可直接消费的数值包**：张量、编码矩阵、样本索引、拟合工件。

### 1.2 一图流（P0–P10 DAG）

```text
提取产物（src/data/_output/，只读）
   │
   ├─ P0 环境/配置锁定 ─→ _meta/preprocess_code_version.json
   ├─ P1 输入校验装载 ─→ master_index.parquet（44.3万 landmark 样本骨架）
   ├─ P2 时序张量化 ──→ X/M/D_seq [443225, 21, 24] + 汇总特征表
   ├─ P3 静态编码 ────→ static_raw.parquet
   ├─ P4 样本集装配 ──→ 四套样本索引 + 患者等权 + 竞争风险标签
   ├─ P5 ECG 波形 ────→ ecg_cache.npy [36648, 12, 5000] + QC 标志
   ├─ P6 模态装配 ────→ modality_index.parquet
   ├─ P7 训练集拟合 ──→ scalers/encoders/imputers（fitted_on=train）
   │                    + X_seq_scaled.npy
   ├─ P8 通道终稿 ────→ SC-common-core 17 通道
   ├─ P9 模型打包 ────→ 6 套输入包 + 基线表格包
   └─ P10 QA 复测 ────→ 泄漏/形状/纯度断言
```

eICU 外验分支：P1→P2→P4→P9→P10（无 ECG 节点，scaler 用 MIMIC 冻结版）。

### 1.3 三条不可违背原则（预处理方案 §0.3）

1. **语义不回头**：不改队列成员、时间原点、标签值；发现语义问题退回提取层，不在本层「悄悄修正」。
2. **训练集专属拟合**：均值/方差、插补中位数、类别映射、QC 阈值只在训练集（anchor_year_group 2008-2010、2011-2013）拟合，拟合结果落盘冻结后在验证/测试/eICU 上**原样应用**。
3. **患者级完整性**：同一患者全部 landmark 与全部 ECG 只属于一个集合（train/validation/test 不混）。

---

## 2. 运行环境与两处替代登记

| 项 | 方案设计 | 实际执行 | 原因 |
|---|---|---|---|
| 测试框架 | pytest | **stdlib unittest**（`python -m unittest discover`，10 用例） | pytest 未安装，不向系统环境装包 |
| ECG 缓存 | lmdb | **npy memmap**（单文件 8.8GB + 索引 parquet） | lmdb 未安装；memmap 支持按需懒加载 |

两处替代不影响处理语义，已登记于 `_meta/preprocess_code_version.json` 的 `substitutions` 字段。其余环境：Python 3.11.7、duckdb 1.5.3、pandas 2.3.3、numpy、wfdb、pyyaml。

---

## 3. 逐节点实现与实测

### 3.1 P0 环境与配置锁定

写入 `_meta/preprocess_code_version.json`：配置哈希、依赖版本、9 个上游产物 SHA-256、D0/冻结清单快照、种子表。每次运行的溯源锚点。

### 3.2 P1 输入校验与 master 索引

- **行数对账基线**（与提取实现记录 §5.1 一致，偏差 >0 即失败）：cohort 31,910 / landmarks 443,225 / labels 443,225 / eICU cohort 62,251 / ECG 索引 211,360 / splits 364,627 —— 全过。
- **键重命名**（v1.1 §3.2）：`episode_id→episode_key`、`subject_id→subject_key`、`k→landmark_k`。
- master 索引 = landmarks ∩ cohort ∩ split，443,225 行，含 `set_name`（train 222,370 / validation / test / excluded）。

### 3.3 P2 临床时序张量化（核心节点）

**通道（21）**：vitals 7（hr/sbp/dbp/mbp/rr/spo2/temp）+ labs 13（creatinine/bilirubin/platelets/lactate/wbc/hemoglobin/glucose/sodium/potassium/bicarbonate/inr/pt/pao2）+ `nee_current`（按 v1.1 §4.4 从 `norepinephrine_equivalent_dose` 按窗重建，bin 内 max）。

**张量语义**（每 landmark 一份）：

- `X_seq [N, 21, 24]` float32：变量值。bin0 = landmark 前最近一小时，bin23 = 24 小时前。缺测位置 = 0（由 mask 表达，不是真 0）。
- `M_seq [N, 21, 24]` bool：该 bin 是否有有效观测（strict 轨还要求 `available_time ≤ landmark`）。
- `D_seq [N, 21, 24]` float32：Δt，距该变量上一次有效观测的小时数；无历史记 cap 值 48。读法示例：某行 Δt = `[48, 0, 0, 1, 2, 3, ...]` → bin0 之前无历史（cap），bin1、bin2 有观测（0），bin3 距上次观测 1 小时，依此类推。

**性能设计**：53.6M 行长表与 master 索引 hash-join 后向量化填充（不逐行循环）；张量主存 npy memmap（X 851MB / M 213MB / D 851MB）。实测全量 P2 约 13 分钟。

**汇总特征表** `summary_features.parquet`：每 (episode, k, 变量) 的 min/median/max/sd/n_obs/last_value/last_obs_age_h，供 LR/XGBoost 基线包。

### 3.4 P3 静态与上下文编码

`static_raw.parquet`：数值 5（age、Δ_ICU-sepsis、weight、height、charlson_prior）+ 类别 5（gender、admission_type、admission_location、icu_type、admission_route）+ 标志 5（charlson_available、weight_missing、extreme_weight、invasive_vent_current、vaso_current）+ 描述性敏感性标志 4（不作模型输入）。`charlson_prior` 保持 NULL 语义（提取层口径），不在本层回填。

### 3.5 P4 样本集装配

| 样本集 | 定义 | 行数 | 患者数 | 阳性 |
|---|---|---|---|---|
| idx_deployment_all | 风险集 ∩ 标签可判定 | 436,572 | — | — |
| idx_paired_ecg | 上 ∩ ECG 24h 时效可选 | 112,066→按集合拆分 | — | — |
| idx_ecg_sensitivity_48h/72h | 时效放宽 | 174,806 / 211,360 | — | — |
| competing_risk_labels | 7 天四类事件 28 bin | 443,225 | — | — |

- **患者等权**：`w = 1 / n_landmarks(patient)`，每位患者总权重 = 1（断言通过）。
- `in_main_grid`（k ≤ 11，主积分网格）随索引落盘。

### 3.6 P5 ECG 波形预处理

- 范围：paired 队列关联的 **36,648 个唯一 study**（不全量处理 80 万份）。
- 流程（本 run 生效部分）：WFDB 读取 → 12 导联标准顺序重排 + lead_mask → 原生 500Hz → 截长补短 10s（[12, 5000]）→ mV 物理单位 → 结构性 QC（可读/时长≥9s/导联数≥8/非全平线）→ per-record z-score。
- **数据驱动 QC（E-4，2026-07-30 已完成）**：5 项质量指标（SNR/基线漂移/饱和/极端振幅/导联相关）阈值仅由 **22,662 份训练集 study** 拟合（`fitted_on=train`，零膨胀指标 p99 上界、其余 median±3MAD）；应用后 **25,599/36,648（69.9%）通过**。
- **仍 pending（方案分期）**：陷波、基线滤波、起搏检测（技术文档 §20，P7 审计决策项，SCE 训练前置）。
- 实测：36,648/36,648 可读，**36,639 结构合格（99.98%）**，缓存 8.8GB memmap + study 索引，断点续跑。

### 3.7 P6 模态装配

`modality_index.parquet`（E-2/E-4 后重跑）209,508 行：landmark × 选中 ECG 的五层级 availability + `data_qc_pass` + `modality_dropout_group`（仅训练集，p=0.3，种子派生——实测 39,240 行标 drop）。`ecg_selected_for_model`（结构层）111,343；`ecg_selected_for_model_frozen`（结构+数据驱动两层 QC）**78,305**。

### 3.8 P7 训练集专属拟合

- 训练集行 222,370（set_name='train'）。
- 拟合工件（全部 `fitted_on='train'` + SHA-256 登记 `registry.json`）：`scaler_clinical_seq`（21 通道，流式两趟估算）、`scaler_static`（5 数值）、`categorical_encoders`（5 类别，频率 <1% 并 Other + Unknown 桶）、`imputers`（中位数 + 缺失指示）。
- 应用产出 `X_seq_scaled.npy`（缺失位置保持 0）。
- eICU/validation/test **只应用不重拟合**——外部验证与内部评估用的是同一份冻结参数。

### 3.9 P8 SC-common 通道终稿

core = 17 通道（vitals 7 + labs 10：creatinine/bilirubin/platelets/lactate/wbc/hemoglobin/glucose/sodium/potassium/bicarbonate）；extended = core + inr/pt/pao2（合同评级 pending）；nee_current 为 MIMIC-only。

### 3.10 P9 模型输入打包

每包 = `index.parquet`（样本键 + 标签 + 权重 + row_idx 引用）+ `static.npy` + `manifest.json`：

| 包 | train | validation | test | 说明 |
|---|---|---|---|---|
| sc_common_paired | **49,180** | **18,861** | **9,344** | 唯一主要比较参照（纯临床） |
| sce_common_paired | **49,180** | **18,861** | **9,344** | 同上样本 + ECG 引用 |
| sc_common_all | 218,509 | 91,655 | 72,067 | 全队列无 ECG 分支 |
| sce_deployment | 218,509 | 91,655 | 72,067 | 全队列 + ECG + dropout 组 |
| baseline_tabular | 218,509×166列 | 91,655 | 72,067 | LR/XGBoost 强基线 |
| eicu_sc_common (P-clinical) | — | — | external 392,264 | 外验，feasibility_only |
| eicu_sc_common (P-explicit) | — | — | external 275,489 | 外验，feasibility_only |

> 注：paired 数字为 **E-5 冻结选片后**（结构+数据驱动两层 QC，frozen 78,305 landmarks → 经标签可判定过滤 77,385）的重跑结果；v1.0 中的 69,231/27,402/14,112（基于 112,066 结构层选片）为**冻结前可行性运行**数字，已被取代。

paired 两包样本完全一致（P10 哈希对账通过），保证「完全相同的 landmarks 上训练和测试」（技术文档 §10.4）。

### 3.11 P10 QA（全过）

MIMIC 9 项：工件 fitted_on=train（4 件）、患者级划分纯度、X_seq NaN/mask 策略（抽样 5000 行）、张量形状 [443225, 21, 24]、paired 跨包哈希一致、ECG available ≤ landmark、两模型包计数>0、training_ready=false。eICU 3 项：NaN/mask 策略、通道缺失率（hr 64.6% / temp 27.1% / labs 2.5–19%）、core 通道合同差异登记（无 pt/inr）。

---

## 4. eICU 外验分支要点

- **坐标换算**：eICU 产物为分钟 offset，P1 统一 `/60` 转小时后走同一套张量化代码。
- **通道映射**：eICU vitals 已同名（hr/sbp/...）；labs 同名 10 项 + INR→inr；**pt 通道不可用**（eICU `pivoted_lab.ptt` 是 aPTT≠PT，合同差异登记，不强映射）。
- **track 叉乘 bug（已修）**：eICU 一个 episode 可同时属于 P-clinical 与 P-explicit 两套表型（labels 带 `phenotype_track` 列，270,135 个 (episode,k) 重复）。样本装配按 (episode,k) 连接时发生 track×track 叉乘，样本数膨胀到 1.16M；改为 (episode,k,track) 三键连接后恢复 **667,753**（与标签可判定数精确一致）。
- scaler 不重拟合：eICU 张量直接用 MIMIC 训练集冻结的 `scaler_clinical_seq` 变换。

---

## 5. 输出结果解读（学习重点）

### 5.1 目录树

```
preprocess/
  configs/preprocess_v1.yaml        # 全部参数的单一事实源
  src/                              # lib(8) + nodes(11) + run.py
  tests/test_core.py                # 10 个 unittest
  _meta/preprocess_code_version.json
  artifacts/pp_v1_20260730/         # 注意多一层 run_id
    p1_validate/master_index.parquet          # 样本骨架 443,225 行
    p2_clinical/master/{X,M,D}_seq.npy        # 原始张量（未标准化）
    p2_clinical/summary_features.parquet      # 基线用汇总特征
    p2_clinical/eicu_master/                  # eICU 张量（17 通道）
    p3_static/static_raw.parquet
    p4_samples/{idx_*.parquet, competing_risk_labels.parquet, eicu_idx_all.parquet}
    p5_ecg_cache/{ecg_cache.npy, ecg_cache_index.parquet, ecg_qc_flags.parquet}
    p6_modality/modality_index.parquet
    p7_fitted/{scaler_*.json, categorical_encoders.json, imputers.json,
               X_seq_scaled.npy, registry.json}
    p8_contracts/sc_common_channels_v1.parquet
    p9_packages/<model>/<set>/{index.parquet, static.npy, manifest.json}
  qa_preprocess/{p10_leakage_report, eicu_p10_report}.{md,json}
```

### 5.2 张量三件套怎么读（X/M/D_seq）

以 `sc_common_paired/train` 第一行为例（row_idx=5，hr 通道）：

```python
import numpy as np
x = np.load('artifacts/.../p7_fitted/.../X_seq_scaled.npy', mmap_mode='r')
m = np.load('.../p2_clinical/.../master/M_seq.npy', mmap_mode='r')
d = np.load('.../p2_clinical/.../master/D_seq.npy', mmap_mode='r')
r, ch = 5, 0                    # 第 6 个样本，hr 通道
x[r, ch, :][m[r, ch, :]]        # → array([-0.25, -0.15])  两个有效观测（z 分数）
m[r, ch, :].mean()              # → 0.083  24 个 bin 里 2 个有观测
d[r, ch, :8]                    # → [48, 0, 0, 1, 2, 3, 4, 5]
```

读法：该 landmark 前 24 小时里患者只有 2 次心率记录（bin1、bin2）；bin0 之前无历史所以 Δt 封顶 48；bin3 距上次观测 1 小时、bin4 距 2 小时……**模型靠 M 分辨「真 0 值」与「无数据」，靠 D 感知「数据有多旧」**——这就是 GRU-D 的三元组输入。

### 5.3 index.parquet 每列含义

| 列 | 含义 |
|---|---|
| `episode_key / subject_key / landmark_k` | 样本主键（episode、患者、6h 网格序号） |
| `hours_since_sepsis` | landmark 距脓毒症原点小时数 = 6k |
| `y_24h / label_status` | 主标签（24h 院内死亡 0/1）与三态 |
| `weight` | 患者等权（该患者 landmark 数的倒数） |
| `in_main_grid` | 是否主积分网格（k≤11，0–72h） |
| `event_type / event_time_bin` | 竞争风险标签（DeepHit 用） |
| `row_idx` | 指向张量主存的行号（**不复制张量**，训练时按此索引） |
| `study_id / modality_dropout_group`（SCE 包） | ECG 缓存键 / 训练期模态丢弃组 |

### 5.4 static.npy 44 维构成

数值 5（标准化后）+ 各自缺失指示 5 + 类别 one-hot（gender 4 + admission_type 5 + admission_location ~10 + icu_type ~9 + admission_route 2，合计约 29）+ 标志 5。特征名清单在 `p9_packages/static_feature_names.json`。所有编码参数来自训练集拟合工件。

### 5.5 manifest.json 怎么读

```json
{
  "model": "sce_common_paired", "set_name": "train",
  "n_samples": 69231, "n_patients": ..., "n_positive": ...,
  "channels": ["hr","sbp",...],                  // 17 core 通道顺序
  "tensor_ref": {"path": "p7_fitted/X_seq_scaled.npy", "selector": "row_idx"},
  "artifact_hashes": {"scaler_clinical_seq": "...", ...},  // 工件哈希（防错配）
  "training_ready": true,
  "training_blockers": [],
  "training_ready_note": "冻结标签生效（2026-07-30，31/31 关闭，D0 已锁定），P10 复测全绿"
}
```

`training_ready=false` 是**门禁标记**：提取方案冻结清单 31 项未全部关闭 + D0 时间原点未锁定前，训练入口脚本必须拒绝启动。当前产物用于流程联调、可行性分析与人工抽查。

### 5.6 baseline_tabular 166 列构成

样本键与标签列 + 静态原始列 + 每个变量 7 个汇总统计（min/median/max/sd/n_obs/last_value/last_obs_age_h）× 20 变量 ≈ 140 列 + 其余键列。LR/XGBoost 直接读 `features.parquet`。

### 5.7 eICU 包与 MIMIC 包的差异

通道只有 17 core（无 nee、无 pt/inr）；`set_name='external'`；`phenotype_track` 区分 P-clinical/P-explicit 两个包；scaler 是 MIMIC 训练集冻结版（**跨库泛化评估的意义就在于「同一把尺」**）；`feasibility_only=true`——eICU 表型规则表待 PI 签署，当前外验数字只作可行性参考。

---

## 6. 问题与教训

1. **P10 路径少一层 run_id**：打包产物在 `p9_packages/<run_id>/<model>/<set>/` 下，QA 读取时漏了 run_id → FileNotFoundError。教训：所有产物路径一律经 `io.artifact_dir(cfg, node)` 生成，不手拼。
2. **eICU track 叉乘**：多表型 episode 在 labels 中一行一 track，按 (episode,k) 连接必然叉乘。教训：凡「一个实体可属多个分组」的表，连接键必须含分组列；并用「结果行数 ≤ 源行数」做即时自检。
3. **pt 检查误写**：QA 断言写成了「pt 通道全缺失」，但 pt 本就不在 core 17 通道里（合同差异是「不纳入」而非「纳入但缺失」）。教训：QA 断言要先核对设计清单，别把设计意图当成数据缺陷。
4. **PROJECT_ROOT 层级**：`parents[2]` vs `parents[3]` 一级之差导致配置找不到。教训：路径常量写完后立刻用一个真实文件验证。
5. **向量化填充 vs 逐行循环**：P2 若按 landmark 逐行查询 53.6M 行长表会慢到不可用；改为「长表 join master 索引拿 row_idx → numpy 花式索引一次性填充」后全程约 13 分钟。教训：大表处理先想「怎么变成 join + 数组赋值」，再写代码。
6. **SQL NULL → pandas NaN → Python 缺失判定**（呼应提取记录 §6.2）：任何把数据库结果交给逐行 Python 判定的地方，先显式 NaN→None 归一化。

---

## 7. 复现与使用指南

### 7.1 运行

```bash
cd preprocess
python -m unittest discover -s tests -v     # 前置测试（10 用例）
python src/run.py                           # 全量 P0→P10
python src/run.py --step p2                 # 单步
python src/run.py --from p5                 # 从 P5 续跑
python src/run.py --step eicu               # eICU 外验分支
```

MIMIC 全量约 30–35 分钟（P2 约 13 分钟、P5 约 15 分钟多进程）；eICU 约 10 分钟。ECG 缓存支持断点续跑（已处理 study 自动跳过）。

### 7.2 验收核对表

1. unittest 10/10；2. P1 行数对账全过；3. P10 九项 + eICU 三项断言全过；4. paired 两包 train 样本数一致（49,180，冻结选片）；5. `registry.json` 四件拟合工件 `fitted_on=train`；6. MIMIC 全部 manifest `training_ready=true`（冻结生效后）；7. 提取 `_output/` 无改动（只读纪律）。

### 7.3 加载示例

```python
import numpy as np, pandas as pd
rid = 'pp_v1_20260730'
base = f'preprocess/artifacts'
idx = pd.read_parquet(f'{base}/p9_packages/{rid}/sce_common_paired/train/index.parquet')
X = np.load(f'{base}/p7_fitted/{rid}/X_seq_scaled.npy', mmap_mode='r')
M = np.load(f'{base}/p2_clinical/{rid}/master/M_seq.npy', mmap_mode='r')
S = np.load(f'{base}/p9_packages/{rid}/sce_common_paired/train/static.npy')
ecg = np.load(f'{base}/p5_ecg_cache/{rid}/ecg_cache.npy', mmap_mode='r')
sample = idx.iloc[0]
x_seq = X[sample.row_idx]          # [21, 24] 临床张量
# ECG：经 ecg_cache_index 查 study_id → cache_row 后取 ecg[cache_row] → [12, 5000]
```

---

## 8. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：预处理管线实现与运行全记录——P0–P10 逐节点实现与实测、eICU 外验分支、产物解读（张量/索引/静态矩阵/manifest/基线包）、6 条问题与教训、复现指南。 |

---

*本文档与《数据提取管线实现与运行记录 v1.0》《数据预处理方案 v1.1》配套阅读：提取记录回答「数据从哪来」，本文档回答「数据怎么变成模型输入」。*
