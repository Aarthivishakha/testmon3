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

The companion regression risk score uses Maintainability Index (MI) from `radon` and recent Git
churn. A Python module counts as risky when both conditions hold:

- `MI < 40`
- The module appears in recent Git churn

The normalized score keeps the result on a 0-100 scale:

```text
Regression Risk Score = Count(Modules with MI < 40 AND recently churned) × 25
Normalisation: MAX(0, 100 – Regression_Risk_Score)
```

## Commands

Install the metric dependencies:

```bash
pip install pytest-testmon radon
```

Run the normal test suite once to create testmon data:

```bash
pytest
```

Run tests through testmon and compute QA Resource Allocation:

```bash
pytest --testmon
python testmon_metrics/compute_qa_resource_allocation.py --source src --tests tests
```

If a CI job already has test counts, pass them directly:

```bash
python testmon_metrics/compute_qa_resource_allocation.py --tests-all 100 --tests-run 35
```

## Repository Policy

This repository is single-branch only: `master`.
