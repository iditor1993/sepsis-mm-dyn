# SEPSIS-MM-DYN 模型训练方案 v1.1

- 文档版本：v1.1（预注册候选版）
- 创建日期：2026-07-30
- 上游依据：①《基于心电波形-临床时序多模态融合的脓毒症动态预后预测 项目技术文档 v1.9》（正式预注册版，下称「技术文档」）；②《SEPSIS-MM-DYN 数据提取方案 v2.4.1》（正式冻结版，标签 `SEPSIS-MM-DYN-data-pipeline-v2.4.1-freeze`）；③《SEPSIS-MM-DYN 数据预处理方案 v1.1》与预处理产物（`preprocess/artifacts/pp_v1_20260730/`）
- 状态：**预注册候选版（冻结后首次修订）**。数据管线已正式冻结（31/31 项关闭，以 `_meta/freeze_checklist.json` 为唯一状态源），MIMIC 侧全部模型输入包 `training_ready=true`；eICU 包为外验评估包（Robustness under phenotype shift，`feasibility_only`）。**能否开始训练**：临床分支（GRU-D/TPC/SC/LR/XGBoost/DeepHit）与 **SCE 分支均可正式训练**——C2 合同审计已完成（audited_locked_2026-08-01），ECG 滤波审计已签署（`filter_decision.json`：不加陷波、基线 none、起搏启发式标记，缓存不重生成）。
- 维护方式：与技术文档、提取方案、预处理方案同库 Git 版本管理；超参数、训练流程变更递增版本号。

### v1.0 → v1.1 修订总览（冻结后训练层预登记补齐）

| # | 修订点 | 落点 |
|---|---|---|
| 1 | 类别不平衡策略预登记：主方案 BCE + pos_weight（训练集阳性率倒数）；敏感性 focal loss | §3.5（新增） |
| 2 | 多 seed 设计：≥5 seeds 派生表、逐 seed 计算后取均值的聚合规则、CI 仍由患者级 bootstrap 给出 | §3.6（新增） |
| 3 | 超参固定表 + early stopping 规则（按 validation iAUROC，patience 预登记，锁死不改） | §3.1/§3.2 改写 |
| 4 | ECG 归一化双方案：per-record z-score（主）/ global_train_stats（预设次要分析，论文同报） | §2.3、§4.3 |
| 5 | GRU-D Δt 约定精确描述与 Che et al. 2018 差异声明 | §2.1 附注 |
| 6 | 「能否开始训练」口径、cv_subgroup_missing 联动、eICU 缺失率分层修正（密度≠缺失率）、预警降级模板、验证集复用局限性 | §1.2/§4.2/§4.4/§14 |
| 7 | **v1.1.1 补充（2026-08-01 训练前回顾）**：①引用功效分析结论（MDE≈0.05，预期管理）；②起搏标记可用性登记；③bilirubin C2 评 B 登记；④体温单位修正已入数据层说明 | §1.2/§2.3/§4.3/§4.4 |

---

## 1. 训练对象与输入包

### 1.1 模型输入包（预处理产物，`pp_v1_20260730`）

| 包 | train | validation | test | 内容 |
|---|---|---|---|---|
| `sc_common_paired` | 49,180 | 18,861 | 9,344 | 临床张量 + 静态（无 ECG） |
| `sce_common_paired` | 49,180 | 18,861 | 9,344 | 同上 + ECG 引用（同一样本集） |
| `sc_common_all` | 218,509 | 91,655 | 72,067 | 全体部署队列，临床 |
| `sce_deployment` | 218,509 | 91,655 | 72,067 | 全体 + ECG + modality dropout 组 |
| `baseline_tabular` | 218,509 | 91,655 | 72,067 | 166 列汇总 + 静态表格 |
| `eicu_sc_common` (P-clinical) | — | — | external 392,264 | 外验（仅评估） |
| `eicu_sc_common` (P-explicit) | — | — | external 275,489 | 外验（仅评估） |

- 临床张量：`X_seq_scaled.npy [N, 21, 24]`（包内使用 SC-common-core **17 通道**子集）+ `M_seq` + `D_seq`；按 `row_idx` 索引，不复制。
- 静态矩阵：`static.npy`（44 维；数值 z-score + one-hot + 缺失指示）。
- ECG：`ecg_cache.npy [36,648, 12, 5000]`（per-record z-score）+ `ecg_cache_index`。
- 竞争风险标签：`event_type/event_time_bin`（7 天 28 bin，DeepHit 用）。
- 拟合工件：`p7_fitted/registry.json`（scalers/encoders/imputers/ECG 阈值，全部 `fitted_on=train`）。

### 1.2 主任务与评估单位

- 主任务：每 landmark 后 24h 院内全因死亡（二分类），评估单位 landmark；bootstrap 以**患者**为重采样单位。
- 主积分网格：`k ∈ [0, 11]`（[0h, 72h)，12 个 landmark，等时间权重）；72–168h 次要/探索。
- 唯一主要效应量：**SCE-common-paired 相对 SC-common-paired 的 ΔiAUROC**（两者在完全相同的 77,385 个 ECG-available landmarks 上训练和测试）。
- **功效预期（v1.1.1 引用）**：冻结 paired 测试集主网格阳性 landmark 152 个，**MDE @80% 功效 ≈ ΔiAUROC 0.05**（`docs/SEPSIS-MM-DYN_paired队列功效分析方案_v1.0.md`）——文献级预期效应（0.01–0.03）下主比较大概率 CI 跨 0，按 Go/No-Go §18.1 预案（亚组研究或负结果报告）管理预期；部署队列为关键次要证据链。
- 数据层附注：MIMIC 体温单位修正（°F→°C，C2 审计发现）已入 F2/P2/P7/P9 重建产物，张量与拟合工件均为修正后版本。

---

## 2. 模型架构

### 2.1 临床时序编码器 A：GRU-D（主架构，锁定）

输入 `[V=17, T=24]` 三元组（X 值、M mask、D Δt）：

```text
对每个通道 v：x̂_v = γ_v(Δt) ⊙ x_v + (1 − γ_v(Δt)) ⊙ m̄_v
  γ_v(Δt) = exp(−max(0, W_γ·Δt + b_γ))，m̄_v = 训练集通道均值（P7 工件）
拼接 [x̂, M] → GRU(hidden=128) → 取末态 h_T
静态特征（44 维）→ MLP(64) → 与 h_T 拼接 → 2 层 MLP(64→32→1, sigmoid)
```

- 参数量约 20 万级；输入已 z-score 标准化（P7 训练集工件）。
- 不引入验证集架构选择（技术文档 §10.3 锁定）。

> **Δt 约定（v1.1 精确化）**：本实现的 Δt 定义为「观测 bin 记 0、其后缺失 bin 递增至上次观测的小时数、无任何历史观测时记 cap 值 48h」。与 Che et al. 2018 原版 GRU-D 的 δ 定义存在一个 bin 的平移差异（原版 δ 在观测点为上一条记录的间隔时长，本实现在观测点恒为 0）。两者行为基本等价（衰减曲线仅相位差一格），论文方法部分按本实现精确描述，不复称「与 Che et al. 完全一致」。

### 2.2 临床时序编码器 B：TPC（预注册对照架构）

技术文档 §10.3 将 TPC 列为 GRU-D 的**预设敏感性对照架构**；与 GRU-D **共享同一输入包、同一样本索引、同一评估协议**，保证对照有效性。

```text
输入 [V=17, T=24]（X 与 M 拼接为 34 通道；D 作为附加通道可消融）
① Channel-wise LSTM：每通道独立双向 LSTM（hidden=16，共享结构不共享权重）
② Temporal Convolution：kernel=3、C_out=64 的因果卷积 + ReLU
③ Pointwise Convolution（1×1，C_out=64）融合通道信息
④ 残差连接（各 block 输入输出相加）×2 个 block
⑤ 全局池化 + 静态特征 MLP(64) 拼接 → 2 层 MLP(64→32→1, sigmoid)
```

- 参数量约 100 万级；与 GRU-D 同训练配置（§3.1）。
- 对照报告：TPC vs GRU-D 的 iAUROC 差值（敏感性分析，不影响主结论）。

### 2.3 ECG 编码器：1D ResNet-18（从头训练，锁定）

输入 `[12, 5000]`（500 Hz × 10 s，per-record z-score）：

```text
Conv1d(12→64, k=15, s=2) + BN + ReLU + MaxPool(k=3, s=2)
4 个 stage（64/128/256/512，各 2 个 BasicBlock，首 block stride=2）
GlobalAvgPool → 512 维 ECG embedding
```

- 从头随机初始化（主结果）；inductive SSL 预训练为预设次要分析（Tier 2，仅训练患者 ECG）。
- `lead_mask` 处理：缺失导联零张量输入，不单独编码。
- **归一化双方案（v1.1 升级）**：主方案 per-record z-score（现状）；**预设次要分析 global_train_stats**（均值/SD 由训练集估计并冻结，保留振幅信息——低电压与 QRS 振幅变化的预后价值在该方案下可表达）。两方案在论文中同报（SCE 主比较各跑一次），归一化差异作为预设敏感性而非事后选择。
- **起搏标记（v1.1.1 登记）**：`ecg_qc_flags.parquet` 已含 `pacing_flag`（启发式检出，全量 7.81%），仅作 QC 分层与敏感性分析协变量，不进模型输入、不剔除样本。

### 2.4 融合与预测头（锁定）

```text
SC 模型：h_clin → MLP head → p̂
SCE 模型：[h_clin ; h_ecg] 简单晚期拼接 → 2 层 MLP(576→64→1, sigmoid)
```

- 主融合 = 简单晚期拼接（锁定）；内容依赖门控与 MoE 仅作 Tier 2 消融。
- SCE-deployment：训练期 modality dropout（p=0.3，按 P6 预分配的 `modality_dropout_group`）+ 可学习 availability embedding（初始化种子固定）；SCE-common-paired 不做任何缺失模态训练。

### 2.5 次要分析：DeepHit 竞争风险

- 输入同 SC 模型；输出头改为 4 类事件 × 28 个 6h 离散区间（DeepHit 联合分布）。
- 评估：time-dependent C-index（竞争风险校正）、CIF 校准曲线（次要结局，不与主 estimand 混用）。

---

## 3. 训练配置

### 3.1 临床模型（GRU-D / TPC / SC-*）

| 项 | 值（**固定，不再验证集网格搜索**） |
|---|---|
| 优化器 | AdamW（lr 1e-3, weight_decay 1e-4） |
| 批大小 | 512 |
| epochs | ≤100，early stop **按 validation iAUROC，patience=10**（预登记） |
| 损失 | BCE + pos_weight（§3.5 主方案；患者等权 `weight` 列，方案锁定） |
| 精度 | FP32 |
| hidden（GRU-D） | 128（锁定） |
| 静态 MLP | 64（锁定） |

### 3.2 多模态模型（SCE-*）

| 项 | 值（固定） |
|---|---|
| 优化器 | AdamW（lr 3e-4, weight_decay 1e-4, cosine decay） |
| 批大小 | 64（ECG 主导显存） |
| epochs | ≤50，early stop **按 validation iAUROC，patience=8**（预登记） |
| 损失 | BCE + pos_weight（§3.5 主方案；患者等权） |
| 精度 | **AMP bf16** |
| DataLoader | workers=6，ECG 按 `study_id → cache_row` memmap 懒加载；临床张量按 `row_idx` memmap |
| modality dropout | 仅 SCE-deployment（p=0.3，P6 预分配组） |
| ECG 增强（仅训练集） | 时间平移 ≤0.5s、振幅缩放 0.9–1.1、轻噪、lead dropout p=0.1（同步清 lead_mask） |

> **超参纪律（v1.1 预登记）**：上表超参数一次固定后不再调整；不实施验证集网格搜索或逐点调参。early stopping 仅决定「在第几个 epoch 取权重」，不改变任何结构或训练规则；选择指标固定为 validation iAUROC（不改用 Brier 或其他）。

### 3.3 基线

- LR：L2（C=1.0），166 列基线表格包；
- XGBoost：depth 6、eta 0.05、subsample 0.8、500 树（CPU）；
- ECG 特征 + LR（试金石）：ECG embedding 用预训练前的统计特征（per-lead mean/sd）。

### 3.4 DeepHit

批 256、Adam lr 1e-3、损失 = ranking log-likelihood + cause-specific 组合（pycox 参考实现）。

### 3.5 类别不平衡策略（v1.1 预登记）

主任务阳性率 2.34%，训练策略预登记如下，禁止训练时临时试探：

| 方案 | 规则 | 地位 |
|---|---|---|
| **主方案** | BCE + `pos_weight = (1 − p̄) / p̄`，其中 `p̄` = **训练集**阳性率（逐样本集计算并登记；不逐 batch 动态调整） | 主 |
| 敏感性 | focal loss（γ = 2，α 取主方案 pos_weight 同源值） | 预设敏感性分析 |

- 患者等权 `weight` 列与 pos_weight 正交叠加：`loss = weight × BCE_pos_weighted`；
- 不做阳性过采样/欠采样（会改变 landmark 分布与校准语义）；
- 校准评估前如需阈值调整，仅在 validation 上拟合（§14）。

### 3.6 多 seed 设计（v1.1 预登记）

深度学习运行间变异对「ΔiAUROC 95% CI 下限 > 0」的成功标准可能是决定性的，预登记：

| 项 | 规则 |
|---|---|
| seeds | **5 个**：`seed_root=20260730` 派生（`train/seed_{1..5}`，落盘登记） |
| 主结果报告 | 逐 seed 完整训练并计算 iAUROC；主结果报告 **5 seeds 的均值 ± SD**；ΔiAUROC 按「先逐 seed 计算再取均值」规则聚合 |
| CI | 患者级 bootstrap 2000 次在**均值预测**上给出（percentile）；单 seed 结论不作为成功标准 |
| 方差报告 | seed 间 SD 单独列出；若某 seed 使 ΔiAUROC CI 下限符号翻转，按预登记如实报告并在讨论中说明 |

---

## 4. 实验矩阵

### 4.1 主分析（唯一主要比较）

| 比较 | 样本 | 输入 | 效应量 |
|---|---|---|---|
| SCE-common-paired vs SC-common-paired | 同一批 ECG-available landmarks（paired 包） | ±ECG | **ΔiAUROC（K=12 等权）** |

成功标准：ΔiAUROC 95% CI 下限 > 0（患者级 bootstrap 2000 次，percentile）。

### 4.2 关键次要分析

1. 全队列部署策略：ECG available → SCE-deployment，否则 → SC-common-all（deployment 样本集）；
2. Δintegrated Brier、calibration intercept/slope（每主要 landmark）、DCA（2%/5%/10% 阈值）；
3. **CV-SOFA≥3 亚组交互**（`sofa_realtime_strict_24h_cv`，患者级首个有效 landmark 值固定分层）；`cv_subgroup_missing` 比例与交互功效联动在月 1 报告（`qa/cv_subgroup_completeness.md`）；
4. 动态预警指标：预警 episode PPV、患者级 PPV、死亡预警成功率、lead time（阈值仅用 validation 锁定）；**预警可行性降级模板（v1.1 预登记）**：若「患者级 sensitivity ≥80% 且 false alerts 最低」的非平凡阈值不存在，按预登记措辞报告「预警可行性目标未达到」+ 可达到最高 sensitivity 的阈值与对应 false alerts 数，不再另行措辞。

### 4.3 消融与对照（预登记）

| 项 | 内容 |
|---|---|
| 编码器对照 | **TPC vs GRU-D**（同输入包，本方案 §2.2） |
| 融合消融 | 拼接 → 平均/加权 → 固定门控 → 内容依赖门控 → MoE |
| 模态消融 | SCE 完整 → 仅临床 → 仅 ECG → availability-only |
| ECG 编码器 | 从头（主）→ inductive SSL → 外部权重 → transductive SSL（敏感性） |
| ECG 归一化 | per-record z-score（主）/ **global_train_stats（预设次要分析，论文同报）** |
| ECG 时效 | 24h（主）/ 48h / 72h（敏感性样本集已备） |
| 加权 | 患者等权（主）/ landmark 等权（敏感性） |
| SOFA 轨 | strict_24h（主）/ carryforward（敏感性） |
| 时间原点 | suspected_infection_time（主）/ max(sofa_time, si_time) / icu_admission（敏感性，独立 run_id） |

### 4.4 外部验证（层级 2）

- eICU P-clinical / P-explicit 两包，SC-common-all 冻结模型直接评估（原始冻结性能）；
- 命名：**Robustness under phenotype shift**；报告 iAUROC / Brier / 漂移量；
- **缺失模式偏移（v1.1 修正与预登记）**：eICU 各变量 mask **观测密度**低于 MIMIC（如 hr 观测密度 64.6%，即缺失 35.4%；注意密度 ≠ 缺失率的表述区分），模型学到的 mask 语义在外部改变——属 phenotype shift 的机制性组成。外验报告**按变量缺失率分层呈现**，并在讨论中明确该机制；GRU-D 原生 mask 处理在此成为优势，应作为方法学卖点写明；
- 重新校准仅在样本量充足时按预注册规则进行（calibration subset 独立评估）；
- **C2 合同登记（v1.1.1）**：SC-common-core 17 变量经 C2 审计锁定（`audited_locked_2026-08-01`）；bilirubin 评 B（中位差 30%，队列构成差异），保留于 core 并在外验讨论中登记该差异（`qa/sc_common_contract_v2.md`）；
- **验证集复用声明（v1.1 预登记）**：内部验证集同时用于超参 early stopping、报警阈值锁定与校准拟合，在局限性中声明验证集复用。

---

## 5. 评估协议（与技术文档 §10.4/§13/§14 一致）

1. **iAUROC**：测试集逐 landmark AUROC × 12 等权平均；某 landmark 缺任一结局类别 → 主要 ΔiAUROC 标记无法完整估计，不事后替换 estimand；
2. **bootstrap**：患者级有放回重采样 2000 次，保留患者全部 landmark，配对差值同次计算，percentile 95% CI；无效重复（任一主 landmark 缺类别）重抽，报告无效比例（>5% 提示不稳定）；
3. **校准**：logistic recalibration 仅 validation 拟合，测试集原样评估；报告未校准/校准后两版；
4. **预警**：报警阈值仅 validation 锁定（sensitivity ≥80% 下 false alerts/100 patient-days 最低的最高风险阈值）；
5. **报告口径**：「基于 anchor_year_group 的时间组外验证」，不作精确日历时间外验证解读。

---

## 6. 防泄漏与复现控制

1. 训练入口强制校验包 `manifest.training_ready == true` 与工件哈希（`registry.json`）一致；
2. 一切统计量（scaler/编码器/插补器/ECG 阈值）仅训练集拟合（`fitted_on=train` 断言）；
3. 同一患者不跨集合（划分纯度断言）；SSL 仅用训练患者 ECG（inductive）；
4. 全部随机源由 `seed_root` 派生并登记；ECG 增强/dropout 仅训练集；
5. 测试集仅用于最终评估一次；不依据测试集结果回头改任何设计；
6. 运行环境：PyTorch ≥ cu128（Blackwell 5060 Ti 必需）；`environment.yml` 锁定。

---

## 7. 算力与时间预算（本地 RTX 5060 Ti 16G）

| 任务 | 配置 | 估算 |
|---|---|---|
| GRU-D / TPC（SC 两模型 ×2 架构） | 批 512 FP32 | 各 ~0.5–1h |
| SCE-paired | 批 64 bf16，69k 样本 | 1.5–5h |
| SCE-deployment | 批 64 bf16，218k 样本 | 4–12h |
| LR / XGBoost / ECG+LR | CPU | 分钟级 |
| DeepHit | 批 256 | <1h |
| bootstrap 2000 CI | 冻结预测 + CPU 统计 | 小时级 |
| inductive SSL（次要） | 训练患者 ECG，SimCLR | 10–30h |

**合计**：主分析 + 基线 + 次要分析约 1–2 天；含消融/敏感性矩阵约 3–5 天。显存峰值（批 64 ECG + AMP）约 4–6GB，16GB 余量充足。

---

## 8. 交付物与里程碑

| # | 交付物 | 验收 |
|---|---|---|
| M1 | 训练代码（`src/models/`：GRU-D、TPC、ResNet-18、融合头、DataLoader、训练入口） | 单元测试通过；冒烟 epoch 数值健康 |
| M2 | 主分析结果（paired 两模型 test 指标 + ΔiAUROC + bootstrap CI） | 与预注册报告模板一致 |
| M3 | 基线对照（LR/XGBoost/ECG+LR/TPC） | 同表报告 |
| M4 | 部署策略 + 消融 + 敏感性矩阵 | 全部预登记项覆盖 |
| M5 | eICU 外验（P-clinical/P-explicit） | Robustness under phenotype shift 报告 |
| M6 | DeepHit 竞争风险（次要） | C-index + CIF 校准 |

## 9. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-07-30 | 首版：基于技术文档 v1.9 与冻结管线产物的模型训练方案——四核心模型 + TPC 对照架构规格、训练配置、实验矩阵、评估协议、5060 Ti 算力预算、防泄漏与复现控制。 |
| v1.1 | 2026-07-30 | 冻结后训练层预登记补齐（外部评审 P1 处置）：①类别不平衡主方案 BCE + pos_weight（训练集阳性率倒数）+ focal 敏感性；②多 seed（5 个）设计与聚合规则；③超参固定表 + early stopping 规则（validation iAUROC、patience 预登记、禁网格搜索）；④ECG 归一化双方案（per-record 主 / global_train_stats 预设次要分析）；⑤Δt 约定精确化与 Che et al. 差异声明；⑥「能否开始训练」口径（临床分支可训 / SCE 待滤波审计）；⑦cv_subgroup_missing 联动、预警降级模板、eICU 缺失率分层（密度≠缺失率修正）、验证集复用声明。 |
| v1.1.1 | 2026-08-01 | 训练前回顾补丁：①引用功效分析结论（MDE≈0.05 预期管理，§1.2）；②起搏标记可用性登记（§2.3）；③bilirubin C2 评 B 登记（§4.4）；④体温单位修正入数据层附注（§1.2）；⑤状态更新：C2 与滤波审计完成后临床与 SCE 分支均可正式训练。 |

---

*本方案与技术文档 v1.9（预注册 estimand）、提取方案 v2.4.1（冻结数据契约）、预处理方案 v1.1（输入包）构成完整文档链；任何与本方案冲突之处以技术文档 v1.9 为准。*
