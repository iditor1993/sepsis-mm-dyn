# ECG + tabular baselines (prespecified ECG+LR trial)

ECG features: machine measurements (RR/HR/P/QRS/T intervals and axes, derived QRS/PR/QT/QTc) + per-lead mean/SD waveform statistics + binary ECG-availability indicator. Clinical-only LR/XGBoost are re-fit on identical rows; iAUROC on the primary grid (k = 0-11); 95% CIs from 2,000 patient-level bootstrap resamples for the ECG-minus-clinical difference.

## paired

| Model | Clinical iAUROC | ECG+clinical iAUROC | Δ (95% CI) |
|---|---|---|---|
| Logistic regression | 0.8159 | 0.7974 | -0.0185 (-0.0368 to +0.0002) |
| XGBoost | 0.8409 | 0.8278 | -0.0131 (-0.0313 to +0.0060) |

## deployment

| Model | Clinical iAUROC | ECG+clinical iAUROC | Δ (95% CI) |
|---|---|---|---|
| Logistic regression | 0.8657 | 0.8647 | -0.0010 (-0.0032 to +0.0017) |
| XGBoost | 0.8760 | 0.8755 | -0.0005 (-0.0032 to +0.0023) |

## Deployment, XGBoost + availability flag only (post hoc)

- Clinical XGBoost iAUROC = 0.8760; XGBoost + availability = 0.8780; Δ = +0.0020 (95% CI -0.0010 to +0.0051).