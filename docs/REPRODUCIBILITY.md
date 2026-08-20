# Reproducibility guide

This guide reproduces the full analysis chain:
credentialed data → DuckDB → extraction → preprocessing → training →
analyses → figures. All seeds and protocol decisions are frozen; see
`configs/frozen/` and the protocol documents under `docs/protocols/`.

## 0. Prerequisites

- Credentialed access to MIMIC-IV v3.1, MIMIC-IV-ECG v1.0, eICU-CRD v2.0
  ([docs/DATA_ACCESS.md](DATA_ACCESS.md)).
- Local DuckDB databases laid out as described there.
- Python 3.11 and the dependencies in `requirements.txt`.
- Storage: the ECG cache alone is ~8.3 GB; budget ≥ 50 GB free space.
- Time: a full cold start (all analyses, 5 seeds) takes on the order of
  several days on a single GPU-less workstation; a smoke run takes minutes.

## 1. Environment

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python notebooks/week1_env_check.ipynb   # optional smoke test
```

## 2. Configure paths

```bash
cp configs/local_paths.example.yaml preprocess/configs/local_paths.yaml
# edit the three paths in preprocess/configs/local_paths.yaml
```

## 3. Stage A — extraction (src/data)

Runs DuckDB SQL/DataFrame extraction and writes Parquet under
`src/data/_output/` (gitignored).

```bash
python src/data/main.py mimic                  # full MIMIC pipeline
python src/data/main.py eicu                   # full eICU pipeline
python src/data/main.py all                    # both
```

Individual steps (see `python src/data/main.py --help`):

```bash
python src/data/main.py mimic --step c0        # episodes
python src/data/main.py mimic --step cohort    # cohort
python src/data/main.py mimic --step landmarks # landmarks
python src/data/main.py mimic --step labels    # 24-h mortality labels
python src/data/main.py mimic --step f1        # f1..f8 feature modules
python src/data/main.py mimic --step contracts # schema contracts
python src/data/main.py mimic --step qa        # QA reports
python src/data/main.py eicu  --step c6a       # eICU: c6a/c6b/c7/c8/landmarks/labels/features/qa
```

Run the extraction unit tests afterwards:

```bash
python -m pytest src/data/tests
```

## 4. Stage B — preprocessing (preprocess)

Tensorization, ECG caching/QC, fitting scalers/imputers, packaging into
model-ready splits, and the eICU branch. Outputs go to
`preprocess/artifacts/` (gitignored).

```bash
cd preprocess
python src/run.py                 # P0 -> P10 (MIMIC)
python src/run.py --step p2       # or a single node
python src/run.py --from p5       # or resume from a node
python src/run.py --step eicu     # eICU external branch
cd ..
```

Node reference: `p0` environment validation, `p1` input validation,
`p2` tensorization, `p3` static features, `p4` sample set, `p5` ECG cache,
`p6` modality alignment, `p7` fitting, `p8` contracts, `p9` packaging,
`p10` leakage/QA.

## 5. Stage C — model training

The main comparison is GRU-D (SC, clinical only) vs SCE-GRU-D (multimodal
ECG) on mirrored paired samples, with TPC as a second encoder family.

```bash
python scripts/run_experiment.py --mode smoke     # 1 model/1 seed, minutes
python scripts/run_experiment.py --mode quick     # 1 seed per model
python scripts/run_experiment.py --mode full      # 5 seeds per model (paper)
python scripts/run_experiment.py --mode aggregate_only   # rebuild REPORT.md from existing predictions
```

Seeds are `1..5`; the paired bootstrap uses `np.random.default_rng(20260730)`.
Outputs: `src/models/runs/{sc_common_paired,sce_common_paired}/...` and
`src/models/runs/REPORT.md` + `summary.csv`.

## 6. Stage D — analyses

One script per evidence stream. Each writes into `src/models/runs/`.

| Script | Evidence stream | Main output |
|---|---|---|
| `scripts/run_tabular_baselines.py` | LR / XGBoost baselines (MIMIC) | `runs/baselines/results.json` |
| `scripts/run_ecg_tabular_baselines.py` | ECG-feature + tabular baselines | `runs/ecg_tabular/results.json` |
| `scripts/run_avail_control.py` | availability-only control | `runs/avail_control/result.json` |
| `scripts/run_deployment.py` | deployment cohort + route | `runs/deployment/deployment_result.json` |
| `scripts/run_deephit.py --mode full` | DeepHit competing risk | `runs/deephit/` |
| `scripts/run_sensitivity.py --mode freshness_48h` | ECG freshness 48 h | `runs/sensitivity/freshness_48h/result.json` |
| `scripts/run_sensitivity.py --mode freshness_72h` | ECG freshness 72 h | `runs/sensitivity/freshness_72h/result.json` |
| `scripts/run_sensitivity.py --mode sofa_carryforward` | SOFA carryforward | `runs/sensitivity/sofa_carryforward/result.json` |
| `scripts/run_ecg_globalnorm.py --mode full` | ECG global-norm ablation | `runs/sensitivity/ecg_globalnorm/result.json` |
| `scripts/run_ssl_inductive.py --mode full` | ECG SSL-init ablation | `runs/sensitivity/ssl_inductive/result.json` |
| `scripts/run_eicu_external.py` | deep-model eICU evaluation | `runs/eicu_external/` |
| `scripts/run_eicu_tabular_external.py` | tabular eICU evaluation | `runs/eicu_tabular_external/results.json` |
| `scripts/run_fairness_audit.py` | sex/age/ethnicity subgroups | `runs/fairness/results.json` |
| `scripts/run_patient_dca.py` | patient-level DCA | `runs/patient_dca/results.json` |

Calibration and utility helpers:

```bash
python -m src.evaluation.main_calibration    # runs/main_calibration/
python -m src.evaluation.dca                 # runs/dca/
python -m src.evaluation.cv_subgroup_interaction   # SOFA-stratified interaction
python -m src.evaluation.power_analysis      # paired-design power
```

Convenience orchestrators:

```bash
python scripts/run_all_analysis.py   # DeepHit → sensitivity → globalnorm → SSL → eICU
python scripts/run_remaining.py      # eICU → SSL finetune → globalnorm
```

Rough runtimes (from the scripts' own documentation, single machine):
freshness 2–4 h each; global-norm overnight; inductive SSL 1–2 days;
eICU inference minutes–hours.

## 7. Stage E — figures

Figures 2–8 are regenerated from the aggregate result snapshots shipped in
this repository (no data or weights needed):

```bash
python figures/make_paper_figures.py
```

PNG (300 dpi) and PDF files are written to `figures/`. Figure 1 (study flow)
was designed separately and is not generated by this script.

## 8. Verification targets

The shipped snapshots in `src/models/runs/**` are the reference values for
the manuscript. After a rerun, the new outputs should match them within
seed-level stochastic tolerance:

| Metric (paper) | Reference location |
|---|---|
| Paired ΔiAUROC +0.0063 (95% CI −0.0023, +0.0183) | `runs/REPORT.md` |
| LR iAUROC 0.8657 / XGBoost 0.8760 | `runs/baselines/results.json` |
| Frozen SCE paired iAUROC 0.8423 | `runs/sce_common_paired/**/result.json` |
| eICU XGBoost 0.8237–0.8297 vs deep 0.704–0.707 | `runs/eicu_tabular_external/results.json`, `runs/eicu_external/result.json` |

Full figure/table-to-script mapping: [docs/RESULTS_MAP.md](RESULTS_MAP.md).

## 9. Frozen protocol metadata

- `configs/frozen/d0_decision.json` — time-origin decision
  (`suspected_infection_time`).
- `configs/frozen/freeze_checklist.json` — protocol freeze checklist.
- `configs/frozen/extraction_code_version.json` — pipeline version
  (v2.4.1) and data versions.
- `configs/frozen/mimic_code_reference_manifest.json` — mimic-code commit
  and file checksums.
- `preprocess/_meta/preprocess_code_version.json` — preprocessing version.

Model training protocols are specified in
`docs/protocols/SEPSIS-MM-DYN_模型训练方案_v1.1.md`; statistical design in
`docs/protocols/SEPSIS-MM-DYN_paired队列功效分析方案_v1.0.md`.
