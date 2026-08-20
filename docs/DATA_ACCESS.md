# Data access

This repository contains **no patient data**. The analyses use three
credentialed PhysioNet databases. You must complete the steps below before
any pipeline stage will run.

## 1. Required credentialed datasets

| Dataset | Version | PhysioNet project | Role |
|---|---|---|---|
| MIMIC-IV | v3.1 | [MIMIC-IV](https://physionet.org/content/mimiciv/3.1/) | Development cohort |
| MIMIC-IV-ECG | v1.0 | [MIMIC-IV-ECG](https://physionet.org/content/mimic-iv-ecg/1.0/) | 12-lead waveforms |
| eICU-CRD | v2.0 | [eICU-CRD](https://physionet.org/content/eicu-crd/2.0/) | External evaluation |

## 2. Getting credentialed access

1. Create a [PhysioNet](https://physionet.org) account.
2. Complete the required CITI "Data or Specimens Only Research" training
   (the training link is shown on each dataset page).
3. For each of the three datasets, click **Request access** on its page,
   agree to the Data Use Agreement (DUA), and wait for approval.

The DUAs prohibit redistribution of any patient-derived file. That is why
extraction outputs, tensors, predictions, and checkpoints are excluded from
this repository (see `.gitignore`).

## 3. Database layout expected by the pipeline

The extraction code (`src/data`) reads **DuckDB** databases, not the raw CSV
files:

- `mimic_db` — MIMIC-IV v3.1 as a single DuckDB file with two schemas:
  - `main` — the raw MIMIC-IV modules (`hosp`, `icu`, `ed`, …);
  - `mimiciv_derived` — derived concepts from
    [MIT-LCP/mimic-code](https://github.com/MIT-LCP/mimic-code)
    (reference commit `a0af19c18a66b6d96935058ebfa830608989bd7c`; the project
    uses `sepsis3` and related concept tables — see
    `configs/frozen/mimic_code_reference_manifest.json`).
- `eicu_db` — eICU-CRD v2.0 as a single DuckDB file with a `main` schema.
- `ecg_wfdb_root` — the extracted MIMIC-IV-ECG WFDB records
  (`<subject>/<study>.hea` / `.dat` hierarchy as distributed by PhysioNet).

The exact table and column inventory against which this pipeline was verified
is listed in `docs/protocols/SEPSIS-MM-DYN_数据提取方案_v2.4.1.md`.

## 4. Pointing the code at your databases

Either:

```bash
# Windows PowerShell
$env:MIMIC_DB      = "D:/mimic/mimic_iv_3_1.duckdb"
$env:EICU_DB       = "D:/eicu/eicu_crd.duckdb"
$env:ECG_WFDB_ROOT = "D:/mimic/ecg"
```

or copy the template and edit it:

```bash
cp configs/local_paths.example.yaml preprocess/configs/local_paths.yaml
```

`preprocess/configs/local_paths.yaml` is gitignored and never committed.

## 5. Citing the datasets

If you publish results obtained with these datasets, cite them as requested
on their PhysioNet pages (Johnson et al. for MIMIC-IV and MIMIC-IV-ECG;
Pollard et al. for eICU-CRD).
