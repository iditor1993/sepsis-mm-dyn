# DCA — ECG-available subset of the deployment test set (supplementary)

n = 14,015 landmarks; positive = 393 (0.0280). Calibrated probabilities; patient-level bootstrap 2000 (seed 20260730).

| Threshold | SC TP/1000 | SC FP/1000 | SCE TP/1000 | SCE FP/1000 | ΔNB/1000 | ΔNB 95% CI/1000 |
|---|---|---|---|---|---|---|
| 2% | 24.40 | 407.71 | 25.12 | 368.82 | +1.507 | +0.853 to +2.052 |
| 5% | 18.91 | 182.66 | 18.62 | 164.11 | +0.691 | +0.079 to +1.296 |
| 10% | 12.84 | 70.64 | 12.34 | 64.07 | +0.230 | -0.568 to +1.007 |

Thresholds with 95% CI excluding zero: **1.5%-5.0%**
