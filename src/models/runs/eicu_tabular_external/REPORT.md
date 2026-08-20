# External evaluation of frozen tabular models in eICU

MIMIC-trained logistic regression (C = 1.0) and XGBoost are applied to eICU without refitting, using the same 166-column feature schema and the MIMIC-fitted imputation/standardization/categorical encoders. iAUROC on the primary grid (k = 0-11); 95% CIs from 1,000 patient-level bootstrap resamples. Reference: frozen GRU-D clinical model iAUROC 0.704 (P-clinical) and 0.707 (P-explicit).

## P-clinical

- n = 392,264 landmarks; 31,531 patients; 10,542 positive.
- Logistic regression: iAUROC 0.7920 (95% CI 0.7839 to 0.8002); Brier 0.1028.
- XGBoost: iAUROC 0.8237 (95% CI 0.8165 to 0.8313); Brier 0.1805.

## P-explicit

- n = 275,489 landmarks; 22,881 patients; 8,109 positive.
- Logistic regression: iAUROC 0.7994 (95% CI 0.7904 to 0.8079); Brier 0.1146.
- XGBoost: iAUROC 0.8297 (95% CI 0.8217 to 0.8378); Brier 0.1965.
