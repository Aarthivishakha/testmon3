"""Compute QA Resource Allocation metrics with pytest-testmon and radon.

Required Testable mapping:
  Technique: Cyclomatic Complexity
  Classification: Test Prioritization
  Metric: QA Resource Allocation
  Tool: testmon
  Formula: test_execution_efficiency = tests_saved / max(tests_all, 1)

Regression Risk Score = Count(Modules with MI < 40 AND recently churned) × 25
Normalisation: MAX(0, 100 – Regression_Risk_Score)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


TEST_OUTCOME_PATTERN = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors|deselected)"
)


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _count_collected_tests(pytest_args: list[str], cwd: Path) -> int:
    proc = _run([sys.executable, "-m", "pytest", "--collect-only", "-q", *pytest_args], cwd)
    output = f"{proc.stdout}\n{proc.stderr}"
    nodeids = [
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.strip().startswith("<")
    ]
    if nodeids:
        return len(nodeids)

    match = re.search(r"collected\s+(?P<count>\d+)\s+items?", output)
    if match:
        return int(match.group("count"))

    raise RuntimeError("Unable to determine total collected tests from pytest output.")


def _count_selected_tests(pytest_args: list[str], cwd: Path) -> int:
    proc = _run([sys.executable, "-m", "pytest", "--testmon", "-q", *pytest_args], cwd)
    output = f"{proc.stdout}\n{proc.stderr}"
    counts: dict[str, int] = {}
    for match in TEST_OUTCOME_PATTERN.finditer(output):
        kind = match.group("kind")
        if kind == "errors":
            kind = "error"
        counts[kind] = counts.get(kind, 0) + int(match.group("count"))

    deselected = counts.get("deselected", 0)
    executed = sum(
        counts.get(kind, 0)
        for kind in ("passed", "failed", "skipped", "xfailed", "xpassed", "error")
    )
    if executed:
        return executed

    collected = re.search(r"collected\s+(?P<count>\d+)\s+items?", output)
    if collected:
        return max(int(collected.group("count")) - deselected, 0)

    raise RuntimeError("Unable to determine tests selected by testmon from pytest output.")


def _parse_mi_value(payload: Any) -> float | None:
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for key in ("mi", "maintainability_index"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _maintainability_index_by_module(source: Path, cwd: Path) -> dict[str, float]:
    proc = _run([sys.executable, "-m", "radon", "mi", "-j", str(source)], cwd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "radon MI analysis failed.")

    raw = json.loads(proc.stdout or "{}")
    result: dict[str, float] = {}
    for module, payload in raw.items():
        mi_value = _parse_mi_value(payload)
        if mi_value is not None:
            result[str(Path(module))] = mi_value
    return result


def _recently_churned_modules(cwd: Path, since: str) -> set[str]:
    proc = _run(
        ["git", "log", f"--since={since}", "--name-only", "--pretty=format:", "--", "*.py"],
        cwd,
    )
    if proc.returncode != 0:
        return set()
    return {str(Path(line.strip())) for line in proc.stdout.splitlines() if line.strip()}


def compute(
    *,
    cwd: Path,
    source: Path,
    pytest_args: list[str],
    tests_all: int | None,
    tests_run: int | None,
    churn_since: str,
) -> dict[str, Any]:
    all_tests = tests_all if tests_all is not None else _count_collected_tests(pytest_args, cwd)
    selected_tests = tests_run if tests_run is not None else _count_selected_tests(pytest_args, cwd)
    tests_saved = max(all_tests - selected_tests, 0)
    test_execution_efficiency = tests_saved / max(all_tests, 1)

    mi_by_module = _maintainability_index_by_module(source, cwd)
    churned_modules = _recently_churned_modules(cwd, churn_since)
    risky_modules = sorted(
        module
        for module, mi_value in mi_by_module.items()
        if mi_value < 40 and module in churned_modules
    )
    regression_risk_score = len(risky_modules) * 25
    normalized_regression_risk = max(0, 100 - regression_risk_score)

    return {
        "technique": "Cyclomatic Complexity",
        "classification": "Test Prioritization",
        "metric": "QA Resource Allocation",
        "tool": "testmon",
        "formula": "test_execution_efficiency = tests_saved / max(tests_all, 1)",
        "tests_all": all_tests,
        "tests_run": selected_tests,
        "tests_saved": tests_saved,
        "test_execution_efficiency": test_execution_efficiency,
        "regression_risk_score_formula": (
            "Count(Modules with MI < 40 AND recently churned) × 25"
        ),
        "normalisation": "MAX(0, 100 – Regression_Risk_Score)",
        "regression_risk_score": regression_risk_score,
        "normalized_regression_risk": normalized_regression_risk,
        "risky_modules": risky_modules,
        "churn_since": churn_since,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="src", help="Python source directory for radon MI.")
    parser.add_argument("--tests", default="tests", help="Pytest tests path.")
    parser.add_argument("--tests-all", type=int, help="Known total test count.")
    parser.add_argument("--tests-run", type=int, help="Known testmon-selected test count.")
    parser.add_argument(
        "--churn-since",
        default="30 days ago",
        help="Git date expression used to identify recently churned modules.",
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    pytest_args = [args.tests] if args.tests else []
    result = compute(
        cwd=cwd,
        source=Path(args.source),
        pytest_args=pytest_args,
        tests_all=args.tests_all,
        tests_run=args.tests_run,
        churn_since=args.churn_since,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
