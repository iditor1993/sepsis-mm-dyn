# ECG inductive SSL 预训练→微调（Tier 2）

- SSL：SimCLR（NT-Xent, T=0.1），20 epochs，仅训练患者 ECG（inductive）
- 微调逐 seed test iAUROC：0.8173, 0.8152, 0.8097, 0.8176, 0.8335
- **均值±SD：0.8187 ± 0.0079**

对照从头训练主结果（SCE paired test iAUROC≈0.823±0.010）：评估 SSL 初始化是否带来增益。