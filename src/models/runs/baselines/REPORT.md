# Tabular baselines (LR / XGBoost)

Trained on the frozen `baseline_tabular` train package (218,509 landmarks; 166 columns), evaluated on the deployment test set (72,067 landmarks) and the paired ECG test subset (9,344 landmarks). iAUROC is computed on the primary grid k = 0-11.

| Model | Deployment iAUROC | Paired iAUROC | Deployment Brier | Paired Brier |
|---|---|---|---|---|
| Logistic regression | 0.8647 | 0.8540 | 0.1613 | 0.1946 |
| XGBoost (gradient-boosted trees) | 0.8760 | 0.8566 | 0.1617 | 0.1837 |

- Logistic regression: validation iAUROC = 0.8759; test iAUROC = 0.8647.
- XGBoost: validation iAUROC = 0.8949; test iAUROC = 0.8760 (best iteration 138).

For reference (frozen GRU-D models):
- SC-common-all deployment iAUROC = 0.8316; SC-common-paired iAUROC = 0.8194.
- SCE-deployment iAUROC = 0.8423; SCE-common-paired iAUROC = 0.8279.

Per-landmark AUROC (deployment test):

| k | LR | XGBoost |
|---|---|---|
| 0 | 0.813 | 0.813 |
| 1 | 0.848 | 0.873 |
| 2 | 0.879 | 0.892 |
| 3 | 0.891 | 0.893 |
| 4 | 0.885 | 0.881 |
| 5 | 0.890 | 0.888 |
| 6 | 0.894 | 0.885 |
| 7 | 0.875 | 0.879 |
| 8 | 0.864 | 0.883 |
| 9 | 0.854 | 0.882 |
| 10 | 0.854 | 0.876 |
| 11 | 0.831 | 0.866 |