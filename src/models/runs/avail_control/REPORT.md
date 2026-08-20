# Availability-only control model (SC + ECG-availability indicator)

Post hoc, revision-added analysis on the full deployment cohort. The control model is a clinical-only GRU-D with the binary ECG-availability indicator (`ecg_selected_for_model`, 24-h freshness + two-layer QC) added to the static features, trained with the same hyper-parameters, weights, and per-seed RNG as the frozen SC-common-all model.

Test set: 72,067 landmarks, 1,957 positive, 14,015 ECG-available.

## Ensemble iAUROC (primary grid k=0-11)

| Model | iAUROC |
|---|---|
| SC-common-all | 0.8316 |
| SC + availability indicator | 0.8379 |
| SCE-deployment | 0.8423 |

- Δ(control − SC) = +0.0064 (95% CI +0.0039 to +0.0090)
- Δ(SCE − SC) = +0.0108 (95% CI +0.0073 to +0.0140)
- Δ(SCE − control) = +0.0044 (95% CI +0.0014 to +0.0072) = residual waveform signal beyond availability.
- Per-seed Δ(control − SC) mean ± SD = +0.0052 ± 0.0017.

## Deployment route

- Route with SCE where ECG available: 0.8342
- Route with control where ECG available: 0.8334

## ECG-available subset (primary grid)

- n = 10,546
- SC = 0.8164; control = 0.8267; SCE = 0.8292
- Δ(control − SC) = +0.0103
- Δ(SCE − SC) = +0.0127
- Δ(SCE − control) = +0.0024 (95% CI -0.0038 to +0.0083).