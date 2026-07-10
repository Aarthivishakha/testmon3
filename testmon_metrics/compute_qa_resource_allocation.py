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
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


TEST_OUTCOME_PATTERN = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors|deselected)"
)
COLLECTED_PATTERN = re.compile(r"collected\s+(?P<count>\d+)\s+items?")


def _env_with_pythonpath(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    root = str(cwd.resolve())
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=_env_with_pythonpath(cwd),
    )


def _pytest_cmd(*extra: str) -> list[str]:
    # Avoid repo pytest.ini addopts (--maxfail / quiet) interfering with counts.
    return [
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        *extra,
    ]


def _parse_outcome_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in TEST_OUTCOME_PATTERN.finditer(output):
        kind = match.group("kind")
        if kind == "errors":
            kind = "error"
        counts[kind] = counts.get(kind, 0) + int(match.group("count"))
    return counts


def _count_collected_tests(pytest_args: list[str], cwd: Path) -> int:
    proc = _run(_pytest_cmd("--collect-only", "-q", *pytest_args), cwd)
    output = f"{proc.stdout}\n{proc.stderr}"
    nodeids = [
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.strip().startswith("<")
    ]
    if nodeids:
        return len(nodeids)

    match = COLLECTED_PATTERN.search(output)
    if match:
        return int(match.group("count"))

    raise RuntimeError(
        "Unable to determine total collected tests from pytest output.\n" + output
    )


def _ensure_testmon_baseline(pytest_args: list[str], cwd: Path, datafile: Path) -> None:
    """Build .testmondata once so later runs can deselect unaffected tests."""
    if datafile.exists() and datafile.stat().st_size > 0:
        return
    proc = _run(_pytest_cmd("--testmon", "-q", *pytest_args), cwd)
    if not datafile.exists():
        raise RuntimeError(
            "testmon baseline failed; .testmondata was not created.\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _count_selected_tests(
    pytest_args: list[str],
    cwd: Path,
    *,
    change_file: Path | None,
) -> tuple[int, int, str]:
    """
    Run testmon after an optional source touch so selection is measurable.

    Returns (tests_run, tests_deselected, raw_output).
    """
    original: str | None = None
    if change_file is not None and change_file.exists():
        original = change_file.read_text(encoding="utf-8")
        # Tiny no-op change forces testmon to reselect dependent tests only.
        change_file.write_text(original + "\n# testmon-metric-touch\n", encoding="utf-8")

    try:
        proc = _run(_pytest_cmd("--testmon", "-rA", *pytest_args), cwd)
        output = f"{proc.stdout}\n{proc.stderr}"
    finally:
        if original is not None and change_file is not None:
            change_file.write_text(original, encoding="utf-8")

    counts = _parse_outcome_counts(output)
    deselected = counts.get("deselected", 0)
    executed = sum(
        counts.get(kind, 0)
        for kind in ("passed", "failed", "skipped", "xfailed", "xpassed", "error")
    )

    if executed or deselected:
        return executed, deselected, output

    collected = COLLECTED_PATTERN.search(output)
    if collected:
        total = int(collected.group("count"))
        return max(total - deselected, 0), deselected, output

    raise RuntimeError(
        "Unable to determine tests selected by testmon from pytest output.\n" + output
    )


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


def _default_change_target(source: Path) -> Path:
    preferred = source / "algorithms" / "numbers" / "factorial.py"
    if preferred.exists():
        return preferred
    py_files = sorted(source.rglob("*.py"))
    for path in py_files:
        if path.name != "__init__.py":
            return path
    raise RuntimeError(f"No Python source files found under {source}")


def compute(
    *,
    cwd: Path,
    source: Path,
    pytest_args: list[str],
    tests_all: int | None,
    tests_run: int | None,
    churn_since: str,
    rebuild_baseline: bool,
) -> dict[str, Any]:
    datafile = cwd / ".testmondata"
    if rebuild_baseline and datafile.exists():
        datafile.unlink()

    all_tests = tests_all if tests_all is not None else _count_collected_tests(pytest_args, cwd)

    if tests_run is None:
        _ensure_testmon_baseline(pytest_args, cwd, datafile)
        change_file = _default_change_target(source)
        selected_tests, deselected_tests, raw_output = _count_selected_tests(
            pytest_args,
            cwd,
            change_file=change_file,
        )
    else:
        selected_tests = tests_run
        deselected_tests = max(all_tests - selected_tests, 0)
        raw_output = ""

    tests_saved = max(all_tests - selected_tests, 0)
    # Prefer explicit deselected count when present.
    if deselected_tests and tests_run is None:
        tests_saved = deselected_tests
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
        "status": "OK",
        "testmondata_present": datafile.exists(),
        "formula": "test_execution_efficiency = tests_saved / max(tests_all, 1)",
        "tests_all": all_tests,
        "tests_run": selected_tests,
        "tests_saved": tests_saved,
        "tests_deselected": deselected_tests,
        "test_execution_efficiency": test_execution_efficiency,
        "metric_covered": bool(all_tests > 0 and tests_saved > 0 and selected_tests >= 0),
        "regression_risk_score_formula": (
            "Count(Modules with MI < 40 AND recently churned) × 25"
        ),
        "normalisation": "MAX(0, 100 – Regression_Risk_Score)",
        "regression_risk_score": regression_risk_score,
        "normalized_regression_risk": normalized_regression_risk,
        "risky_modules": risky_modules,
        "churn_since": churn_since,
        "selection_raw_tail": "\n".join(raw_output.strip().splitlines()[-12:]),
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
    parser.add_argument(
        "--rebuild-baseline",
        action="store_true",
        help="Delete .testmondata and rebuild the testmon dependency database.",
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
        rebuild_baseline=args.rebuild_baseline,
    )
    print(json.dumps(result, indent=2, sort_keys=True))

    if not result.get("metric_covered"):
        raise SystemExit(
            "QA Resource Allocation metric not covered: "
            "testmon did not save/select tests. Re-run with --rebuild-baseline."
        )


if __name__ == "__main__":
    main()
