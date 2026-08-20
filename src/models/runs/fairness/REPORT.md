# Subgroup (fairness) audit - sex, age, race/ethnicity

Post hoc, revision-added analysis. iAUROC on the primary grid (k = 0-11); SC/SCE use the five-seed ensemble scores from the frozen models. 95% CIs are patient-level bootstrap (1,000 resamples) for the SCE-vs-SC difference.

## paired test (matched 9,344 landmarks)

### sex
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| F | 3,765 | 711 | 94 | 0.713 | 0.719 | +0.005 (-0.006, +0.023) |
| M | 5,579 | 1007 | 129 | 0.852 | 0.856 | +0.004 (-0.015, +0.017) |

### age_group
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| <65 | 4,020 | 720 | 98 | 0.857 | 0.866 | +0.009 (-0.009, +0.021) |
| 65-79 | 3,461 | 628 | 72 | 0.836 | 0.852 | +0.016 (-0.002, +0.033) |
| >=80 | 1,863 | 370 | 53 | 0.694 | 0.722 | +0.028 (+0.013, +0.043) |
| Unknown | 0 | - | - | n<100 | - | - |

### race_group
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| White | 5,584 | 1026 | 89 | 0.848 | 0.854 | +0.006 (-0.009, +0.021) |
| Black | 466 | 99 | 12 | n/a | n/a | n/a |
| Asian | 173 | 36 | 7 | n/a | n/a | n/a |
| Hispanic | 228 | 41 | 2 | n/a | n/a | n/a |
| Other/Unknown | 2,601 | 454 | 105 | 0.751 | 0.761 | +0.011 (-0.009, +0.027) |

## deployment test (matched 72,067 landmarks)

### sex
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| F | 29,631 | 2029 | 841 | 0.812 | 0.825 | +0.013 (+0.007, +0.019) |
| M | 42,436 | 2832 | 1116 | 0.845 | 0.855 | +0.010 (+0.005, +0.014) |

### age_group
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| <65 | 31,940 | 2073 | 746 | 0.851 | 0.862 | +0.011 (+0.007, +0.016) |
| 65-79 | 25,986 | 1739 | 637 | 0.839 | 0.847 | +0.008 (+0.002, +0.013) |
| >=80 | 14,141 | 1049 | 574 | 0.777 | 0.795 | +0.017 (+0.008, +0.027) |
| Unknown | 0 | - | - | n<100 | - | - |

### race_group
| Subgroup | n landmarks | n patients | events | SC iAUROC | SCE iAUROC | Δ (95% CI) |
|---|---|---|---|---|---|---|
| White | 41,615 | 2901 | 1003 | 0.825 | 0.832 | +0.007 (+0.003, +0.012) |
| Black | 5,152 | 365 | 157 | 0.831 | 0.843 | +0.012 (-0.004, +0.028) |
| Asian | 1,677 | 123 | 36 | 0.814 | 0.820 | +0.006 (-0.004, +0.017) |
| Hispanic | 1,968 | 125 | 28 | n/a | n/a | n/a |
| Other/Unknown | 18,546 | 1138 | 670 | 0.821 | 0.840 | +0.018 (+0.011, +0.025) |
