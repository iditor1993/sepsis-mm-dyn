# eICU 外部验证报告（Robustness under phenotype shift）

生成时间：2026-08-05 12:32；模型：MIMIC SC-common-all 冻结权重（5 seeds 集成）；原始冻结性能，未重新校准

## P-clinical

- 样本 392,264 / 患者 31,531 / 阳性 10,542
- **iAUROC = 0.7039，患者级 bootstrap 95% CI [+0.6948, +0.7127]**
- Brier = 0.0581

变量观测密度（mask 密度，缺失模式偏移证据）：

| 通道 | 密度 |
|---|---|
| hr | 0.657 |
| sbp | 0.636 |
| dbp | 0.635 |
| mbp | 0.627 |
| rr | 0.620 |
| spo2 | 0.542 |
| temp | 0.275 |
| creatinine | 0.060 |
| bilirubin | 0.024 |
| platelets | 0.051 |
| lactate | 0.024 |
| wbc | 0.051 |
| hemoglobin | 0.057 |
| glucose | 0.191 |
| sodium | 0.064 |
| potassium | 0.069 |
| bicarbonate | 0.057 |

## P-explicit

- 样本 275,489 / 患者 22,881 / 阳性 8,109
- **iAUROC = 0.7072，患者级 bootstrap 95% CI [+0.6977, +0.7168]**
- Brier = 0.0639

变量观测密度（mask 密度，缺失模式偏移证据）：

| 通道 | 密度 |
|---|---|
| hr | 0.636 |
| sbp | 0.616 |
| dbp | 0.616 |
| mbp | 0.608 |
| rr | 0.601 |
| spo2 | 0.514 |
| temp | 0.274 |
| creatinine | 0.063 |
| bilirubin | 0.028 |
| platelets | 0.053 |
| lactate | 0.029 |
| wbc | 0.053 |
| hemoglobin | 0.059 |
| glucose | 0.194 |
| sodium | 0.067 |
| potassium | 0.072 |
| bicarbonate | 0.059 |
