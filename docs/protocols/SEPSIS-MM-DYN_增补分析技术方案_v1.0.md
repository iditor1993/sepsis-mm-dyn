# SEPSIS-MM-DYN 增补分析技术方案 v1.0
# ——DeepHit CIF 校准曲线 + eICU 分层细化（按医院/缺失）

- 文档版本：v1.0
- 创建日期：2026-08-05
- 上游依据：《项目技术文档 v1.9》（§14 校准、§12 三级验证、§2.2 竞争风险 estimand）；《模型训练方案 v1.1》；《结果综合分析 v1.0》
- 关联产物：`src/models/runs/deephit/cif_predictions_seed{1..5}.npz`、`src/models/runs/eicu_external/result.json`、`preprocess/artifacts/pp_v1_20260730/p2_clinical/eicu_master/`
- 状态：**待实施候选方案**。两项均不重训模型，只做评估层增补（评估代码 + 报告），不触碰冻结的数据与训练结果。

---

## 0. 总览

| 部分 | 主题 | 性质 | 预计时长 |
|---|---|---|---|
| Part A | DeepHit CIF 校准曲线 | 评估增补（不重训） | 分钟级 |
| Part B | eICU 分层细化（按医院 / 按缺失密度） | 评估增补（推理 + 分层指标） | ~1 小时 |

两部分共用原则：**患者级（MIMIC）/ uniquepid（eICU）聚类 bootstrap 估计置信区间；不重新训练、不查看 MIMIC test 之外的任何调参行为；解读口径预登记，避免事后修饰。**

---

# Part A：DeepHit CIF 校准曲线

## A.1 目的

DeepHit 已输出逐样本各事件的累积发生函数（CIF，28×6h 区间，168h 窗）。判别力（td C-index：死亡 0.768 / 存活出院 0.707）已达标，但**判别力好不代表概率准**——临床决策依赖的是「模型说某患者 24h 内死亡概率 8% 是否真的是 8%」。本部分目的：

1. 检验 DeepHit 的 CIF **校准度**（predicted CIF vs 观察到的实际累积发生率）；
2. 定位失准的区段（哪些时间窗、哪些风险段、哪些 landmark）；
3. 为论文提供「判别 + 校准」双维度的竞争风险证据，并决定是否需要再校准（以及用何种方法）。

## A.2 实现方法

### A.2.1 数据

- 输入：`cif_predictions_seed{1..5}.npz`（每样本 `cif_death / cif_alive_discharge / cif_acute_transfer` + `event_type / event_bin`），5 seeds 的 CIF 取**均值**（集成）；
- 标签：`competing_risk_labels.parquet`（事件类型与 bin，28×6h）；
- 急性转出事件数不足（291 例），**按预登记只做死亡与存活出院两事件的校准**，转出仅附注。

### A.2.2 三个层面的校准

**① 总体校准（calibration-in-the-large）**

对每个事件、每个 horizon（24h/bin4、72h/bin12、168h/bin28）：

```text
cal_in_large = mean(predicted CIF(horizon)) − observed_event_rate(horizon)
```

观察事件率用 Kaplan–Meier 估计（处理删失）。

**② 风险分层校准曲线（decile calibration plot）**

- 按 predicted CIF(168h) 把样本分成 10 个十分位组；
- 每组：x = mean(predicted CIF)，y = KM 观察累积发生率；
- 逐 horizon（24h/72h/168h）绘制，参考 y=x 对角线；
- 拟合校准截距与斜率（logistic recalibration 仅 validation 上评估用；**测试集只报原始校准**）。

**③ Landmark 分层校准**

按 landmark 分段（k0–3 早期 / k4–11 主网格后段 / k12+ 晚期）分别重复②，定位校准是否随病程漂移。

### A.2.3 输出

- `src/models/runs/deephit/calibration/`：
  - `calibration_metrics.json`：每事件 × horizon 的 cal-in-large、decile 校准斜率/截距（bootstrap 95% CI，患者级 2000 次）；
  - `calibration_curves.npz`：曲线坐标数据（绘图用）；
  - `REPORT.md`：校准曲线图描述 + 指标表 + 解读。

## A.3 预期结果

| 情形 | 预期 | 解读 |
|---|---|---|
| **理想** | decile 点贴对角线，斜率≈1、截距≈0 | DeepHit 概率可直接用于临床风险沟通，无需再校准 |
| **轻微过估/低估** | 系统性偏离对角线（斜率≠1 或截距≠0） | 用 logistic recalibration（**仅 validation 拟合**）给出校正因子，报告「原始 vs 校准后」两版；测试集不重新拟合 |
| **区段失准** | 仅某 horizon/某 landmark 段失准 | 报告区段并讨论成因（如晚期稀疏事件、landmark 间分布漂移），不强求全局再校准 |

**重点判读**：

- **cal-in-large 的符号**：>0 表示整体高估事件率，<0 低估；DeepHit 在死亡上若高估（因阳性率低），需结合 Brier 与 DCA 判断临床可用性；
- **校准斜率 <1**：预测区分度过强（高/低估两端拉开），是 recalibration 的适应症；
- **死亡 vs 存活出院的校准差异**：竞争事件间校准通常不同步，分别报告。

---

# Part B：eICU 分层细化（按医院 / 按缺失密度）

## B.1 目的

eICU 外验（层级 2）总 iAUROC = 0.704（P-clinical）/ 0.707（P-explicit），相比 MIMIC 内部（0.82）存在预期衰减。但**总衰减掩盖了两个关键问题**：

1. **医院异质性**：208 家医院中，性能是否被少数医院主导？是否存在「某些医院近乎失效」的亚群？这直接决定外验结论的稳健性（Go/No-Go 中「最大单医院占比 ≤25%」的姊妹问题）；
2. **缺失密度梯度**：衰减是否随变量缺失程度加剧？这检验「缺失模式偏移是主要 phenotype shift 机制」的假设，并指导是否需要缺失感知的再校准/亚群分析。

目的：**把「0.82→0.70」这个数字拆开，说明衰减来自哪里（哪些医院、哪些缺失水平的患者）。**

## B.2 实现方法

### B.2.1 按医院分层

- 分组键：`hospitalid`（eICU `patient` 表，208 家）；
- 每医院：iAUROC（主网格 k≤11）、样本数、阳性数、阳性率；仅纳入 `n_samples ≥ 500 且 n_positive ≥ 20` 的医院（预登记最低可估计阈）；
- 汇总：
  - 医院数 vs 可估计医院数；
  - iAUROC 分布（中位、IQR、min/max）；
  - **最大单医院患者占比**（对照 Go/No-Go ≤25% 精神）；
  - 失败医院（iAUROC < 0.55）的数量与占比；
  - 患者级 bootstrap（按 uniquepid）给出总 iAUROC 的 CI，医院级给出描述分布（不做医院级 CI，避免小样本噪声）。

### B.2.2 按缺失密度分层

- 每样本缺失密度 = 17 core 通道的 mask 密度均值（`eicu_master/M_seq`）；
- 按密度三分位（预登记，不按结果选切点）分成 低/中/高 三组；
- 每组 × 每 track（P-clinical/P-explicit）：iAUROC、Brier、样本数、阳性数；患者级 bootstrap 95% CI；
- 同时报告各组的关键通道密度（vitals/labs 分组均值），把「密度水平」与「哪些变量缺」对应起来。

### B.2.3 输出

- `src/models/runs/eicu_external/stratified/`：
  - `by_hospital.csv`：逐医院 iAUROC/样本数/阳性数；
  - `by_hospital_summary.json`：医院数、iAUROC 分布、最大单院占比、失败医院占比；
  - `by_density.json`：三分位 × track 的 iAUROC/Brier/CI + 通道密度；
  - `REPORT.md`：两维度结果表 + 解读。

## B.3 预期结果与解读

### B.3.1 按医院

| 情形 | 解读 |
|---|---|
| iAUROC 分布集中（IQR 窄、无大量 <0.55 医院） | 外验性能**稳健**，衰减是人群/数据层面的普遍现象而非个别医院拖累；支持「跨库可迁移（有衰减）」的结论 |
| 少数医院显著偏低 / 最大单院占比高 | 需报告「性能存在医院异质性」，并做剔除主导医院后的敏感性；外验结论降级为「部分医院可迁移」 |
| 大量医院样本不足无法估计 | 说明 eICU 单中心规模有限，报告可估计医院比例，讨论多中心稀疏性 |

### B.3.2 按缺失密度

| 情形 | 解读 |
|---|---|
| **iAUROC 随密度上升单调改善** | **强证据**支持「缺失模式偏移 = 主要衰减机制」——GRU-D mask 机制有效但信息缺失有上限；建议在论文中给出该梯度图，并讨论「缺失感知阈值部署」或 eICU 侧密度匹配亚群的再校准（仅 calibration subset 拟合） |
| 三组差异小 | 衰减不主要由缺失驱动，需转向变量语义/实践差异（单位、采样、SOFA 口径）找机制 |
| 中密度组反常 | 报告分布并人工抽查，不强行解释 |

**判读纪律**：分层是**机制归因**用途，不用于挑选「表现好的亚群」反向优化；任何「在高密度亚群重训/再校准」的想法必须走独立的 calibration subset（技术文档 §12.3），不得用测试集结论直接改模型。

---

## 4. 实施顺序与工程

| 步骤 | 内容 | 产物 |
|---|---|---|
| 1 | `src/evaluation/deephit_calibration.py`（A 全部） | `deephit/calibration/` |
| 2 | `src/evaluation/eicu_stratified.py`（B 全部） | `eicu_external/stratified/` |
| 3 | 两份 `REPORT.md` + 汇总到 `结果综合分析` 增补版（后续版本） | docs |

工程约束：全部只读评估、不重训、患者级 bootstrap CI、急性转出仅附注；脚本沿用项目现有 `metrics/labels` 工具与 `run_*` 一键风格（顶部 CONFIG、可在 VSCode 直接运行、产出 REPORT.md）。

## 5. 变更日志

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1.0 | 2026-08-05 | 首版：DeepHit CIF 校准（三层：总体/十分位/landmark 分段）与 eICU 分层细化（按医院/按缺失密度）的目的、实现方法、预期结果与解读口径。 |

---

*本方案为评估层增补，不改变冻结数据与训练结果；解读口径预登记，机制归因不作反向优化依据。*
