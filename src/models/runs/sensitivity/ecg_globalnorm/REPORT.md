# ECG 归一化敏感性：global_train_stats

- 全局归一化（训练集 mean/sd，fitted_on=train）SCE 重训
- 逐 seed test iAUROC：0.8005, 0.8218, 0.8006, 0.8075, 0.8168
- **均值±SD：0.8095 ± 0.0086**

对照 per-record z-score 主结果（SCE paired test iAUROC≈0.823±0.010）：评估保留振幅信息的增量/损失。