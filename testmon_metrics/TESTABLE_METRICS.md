# Testable Metric: QA Resource Allocation

Source mapping: Testable metric "QA Resource Allocation" using `testmon`.

| Field | Value |
|-------|-------|
| Technique | Cyclomatic Complexity |
| Classification | Test Prioritization |
| Metric | QA Resource Allocation |
| Tool | testmon |
| Formula | `test_execution_efficiency = tests_saved / max(tests_all, 1)` |

## Required Regression Risk Metric

Regression Risk Score = Count(Modules with MI < 40 AND recently churned) × 25

Normalisation: MAX(0, 100 – Regression_Risk_Score)

## Interpretation

`pytest-testmon` prioritizes and limits test execution to tests affected by recent code changes.
For QA Resource Allocation, the useful signal is how many tests can be skipped without losing
targeted regression coverage:

```text
tests_saved = tests_all - tests_selected_by_testmon
test_execution_efficiency = tests_saved / max(tests_all, 1)
```

**Important:** a single fresh `--testmon` run only builds `.testmondata` and executes all tests.
Selection (and a non-zero `tests_saved`) requires a **two-phase** setup:

1. Baseline: `pytest --testmon` → creates `.testmondata`
2. Change + rerun: touch one source file, then `pytest --testmon` again → only affected tests run

`compute_qa_resource_allocation.py` performs this automatically.

## Commands

Install the metric dependencies:

```bash
pip install -r requirements.txt
```

Set `PYTHONPATH` to the repo root (tests import `from src...`):

```bash
# Windows CMD
set PYTHONPATH=%CD%

# Linux/macOS
export PYTHONPATH=.
```

Compute QA Resource Allocation (builds baseline if needed, then selective run):

```bash
python testmon_metrics/compute_qa_resource_allocation.py --source src --tests tests --rebuild-baseline
```

Or use the helper script:

```bash
scripts\run_qa_resource_allocation.bat
```

Expected JSON fields when the metric is covered (Testable raw schema + aliases):

```text
status = OK
testmondata_present = true
tests_total_count > 0          # alias: tests_all
tests_selected_count > 0       # alias: tests_run
tests_deselected_count > 0     # alias: tests_saved / tests_deselected
selection_ratio = selected / total   # e.g. 0.043 (~4.32%)
metric_covered = true
test_execution_efficiency = tests_saved / tests_all
```

Artifacts written:

- `reports/qa_resource_allocation.json` — full metric report
- `reports/testmon.json` — Testable-shaped payload (`tests_*_count`, `selection_ratio`, `raw_stdout_tail`)

**Note:** A single `pytest --testmon` without the two-phase baseline+touch flow will not produce meaningful selection counts. Always use `compute_qa_resource_allocation.py`.

If a CI job already has test counts, pass them directly:

```bash
python testmon_metrics/compute_qa_resource_allocation.py --tests-all 100 --tests-run 35
```

## Repository Policy

This repository is single-branch only: `master`.
