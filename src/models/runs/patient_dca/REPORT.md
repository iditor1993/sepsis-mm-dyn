# Patient-level decision-curve analysis (post hoc)

Landmark-level net benefit can overstate per-patient benefit because one patient contributes multiple landmarks. Here NB is recomputed on one observation per patient: (1) the first landmark, and (2) the per-patient highest-risk landmark (max SCE calibrated probability; both models scored at that landmark). dNB is reported per 1,000 patients with patient-level bootstrap 95% CIs.

## paired test

### first landmark (n patients = 1,718, events = 29)

- Patient-level AUC: SC = 0.7396; SCE = 0.7700.

| Threshold | SC: TP / FP | SCE: TP / FP | dNB per 1,000 patients (95% CI) |
|---|---|---|---|
| 2% | 20 / 628 | 23 / 641 | +1.59 (-0.13, +3.91) |
| 5% | 14 / 297 | 14 / 273 | +0.74 (+0.18, +1.35) |
| 10% | 11 / 99 | 10 / 90 | +0.00 (-2.20, +2.13) |

### highest landmark (n patients = 1,718, events = 63)

- Patient-level AUC: SC = 0.8376; SCE = 0.8414.

| Threshold | SC: TP / FP | SCE: TP / FP | dNB per 1,000 patients (95% CI) |
|---|---|---|---|
| 2% | 56 / 785 | 57 / 796 | +0.45 (-0.34, +1.81) |
| 5% | 50 / 445 | 47 / 414 | -0.80 (-3.43, +1.72) |
| 10% | 42 / 191 | 39 / 180 | -1.03 (-3.95, +1.55) |

## deployment test

### first landmark (n patients = 4,861, events = 151)

- Patient-level AUC: SC = 0.7880; SCE = 0.7902.

| Threshold | SC: TP / FP | SCE: TP / FP | dNB per 1,000 patients (95% CI) |
|---|---|---|---|
| 2% | 134 / 2477 | 132 / 2303 | +0.32 (-0.34, +0.80) |
| 5% | 101 / 1198 | 94 / 1004 | +0.66 (-0.90, +2.07) |
| 10% | 63 / 438 | 60 / 345 | +1.51 (+0.05, +2.83) |

### highest landmark (n patients = 4,861, events = 346)

- Patient-level AUC: SC = 0.8106; SCE = 0.8332.

| Threshold | SC: TP / FP | SCE: TP / FP | dNB per 1,000 patients (95% CI) |
|---|---|---|---|
| 2% | 326 / 2962 | 331 / 2904 | +1.27 (+0.14, +2.57) |
| 5% | 281 / 1683 | 290 / 1635 | +2.37 (+0.79, +4.06) |
| 10% | 205 / 710 | 232 / 746 | +4.73 (+1.78, +7.68) |
