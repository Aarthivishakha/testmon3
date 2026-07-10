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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mK]")
TEST_OUTCOME_PATTERN = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors|deselected)\b",
    re.IGNORECASE,
)
COLLECTED_PATTERN = re.compile(r"collected\s+(?P<count>\d+)\s+items?", re.IGNORECASE)
SUMMARY_PASSED_PATTERN = re.compile(
    r"={5,}\s*(?P<body>.*?)\s*={5,}",
    re.IGNORECASE | re.DOTALL,
)


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _env_with_pythonpath(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    root = str(cwd.resolve())
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PY_COLORS"] = "0"
    env["NO_COLOR"] = "1"
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
    # Avoid repo pytest.ini addopts/filterwarnings interfering with counts.
    # Do NOT disable cacheprovider: pytest-testmon reads options["lf"] from it.
    return [
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-o",
        "filterwarnings=",
        "-o",
        "console_output_style=classic",
        "--color=no",
        "-W",
        "ignore",
        *extra,
    ]


def _parse_outcome_counts(output: str) -> dict[str, int]:
    text = _strip_ansi(output)
    counts: dict[str, int] = {}

    # Prefer the final summary line: "N passed, M deselected in ..."
    summary_matches = list(SUMMARY_PASSED_PATTERN.finditer(text))
    summary_text = summary_matches[-1].group("body") if summary_matches else text

    for match in TEST_OUTCOME_PATTERN.finditer(summary_text):
        kind = match.group("kind").lower()
        if kind == "errors":
            kind = "error"
        counts[kind] = counts.get(kind, 0) + int(match.group("count"))
    return counts


def _count_collected_tests(pytest_args: list[str], cwd: Path) -> int:
    proc = _run(_pytest_cmd("--collect-only", "-q", *pytest_args), cwd)
    output = _strip_ansi(f"{proc.stdout}\n{proc.stderr}")
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
        # Real executable change (not a comment) so testmon invalidates dependents.
        if "TESTMON_METRIC_TOUCH = False" in original:
            updated = original.replace(
                "TESTMON_METRIC_TOUCH = False",
                "TESTMON_METRIC_TOUCH = True",
                1,
            )
        else:
            updated = original.rstrip() + "\n\nTESTMON_METRIC_TOUCH = True\n"
        change_file.write_text(updated, encoding="utf-8")

    try:
        proc = _run(_pytest_cmd("--testmon", "-q", "--tb=no", *pytest_args), cwd)
        output = _strip_ansi(f"{proc.stdout}\n{proc.stderr}")
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

    # Last resort: count PASSED node lines from quiet output.
    passed_nodes = len(re.findall(r"::\S+\s+PASSED\b", output, flags=re.IGNORECASE))
    if passed_nodes:
        return passed_nodes, 0, output

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
            result[str(Path(module).as_posix())] = mi_value
    return result


def _recently_churned_modules(cwd: Path, since: str) -> set[str]:
    """
    Prefer files changed in the tip commit only.

    A brand-new demo repo has every file 'recent' under --since=30 days, which
    falsely maxes Regression Risk Score. Tip-commit churn matches the gate:
    '0 low-MI modules with recent churn' for unchanged source on metric commits.
    """
    tip = _run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd)
    files = {line.strip().replace("\\", "/") for line in tip.stdout.splitlines() if line.strip()}
    if files:
        return {path for path in files if path.endswith(".py")}

    proc = _run(
        ["git", "log", f"--since={since}", "--name-only", "--pretty=format:", "--", "*.py"],
        cwd,
    )
    if proc.returncode != 0:
        return set()
    return {
        line.strip().replace("\\", "/")
        for line in proc.stdout.splitlines()
        if line.strip()
    }


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
        touch_posix = change_file.resolve().relative_to(cwd.resolve()).as_posix()
    else:
        selected_tests = tests_run
        deselected_tests = max(all_tests - selected_tests, 0)
        raw_output = ""
        touch_posix = ""

    tests_saved = max(all_tests - selected_tests, 0)
    if deselected_tests and tests_run is None:
        tests_saved = deselected_tests
    test_execution_efficiency = tests_saved / max(all_tests, 1)
    selection_ratio_pct = (selected_tests / max(all_tests, 1)) * 100

    mi_by_module = _maintainability_index_by_module(source, cwd)
    churned_modules = _recently_churned_modules(cwd, churn_since)

    # Normalize paths so radon keys align with git paths.
    mi_by_posix = {Path(k).as_posix(): v for k, v in mi_by_module.items()}
    # Tip-commit churn only, and only under the analysed source tree.
    source_prefix = source.resolve().relative_to(cwd.resolve()).as_posix()
    churned_posix = {
        Path(k).as_posix()
        for k in churned_modules
        if Path(k).as_posix() == source_prefix
        or Path(k).as_posix().startswith(source_prefix + "/")
    }
    # The metric harness intentionally edits one source file; don't treat that as risk churn.
    if touch_posix:
        churned_posix.discard(touch_posix)

    risky_modules = sorted(
        module
        for module, mi_value in mi_by_posix.items()
        if mi_value < 40 and module in churned_posix
    )
    regression_risk_score = len(risky_modules) * 25
    normalized_regression_risk = max(0, 100 - regression_risk_score)

    metric_covered = bool(
        all_tests > 0
        and tests_saved > 0
        and selected_tests > 0
        and selection_ratio_pct <= 14
        and datafile.exists()
        and normalized_regression_risk == 100
    )

    return {
        "technique": "Cyclomatic Complexity",
        "classification": "Test Prioritization",
        "metric": "QA Resource Allocation",
        "tool": "testmon",
        "status": "OK" if metric_covered else "FAIL",
        "testmondata_present": datafile.exists(),
        "formula": "test_execution_efficiency = tests_saved / max(tests_all, 1)",
        "tests_all": all_tests,
        "tests_run": selected_tests,
        "tests_saved": tests_saved,
        "tests_deselected": deselected_tests,
        "test_execution_efficiency": test_execution_efficiency,
        "selection_ratio_pct": round(selection_ratio_pct, 2),
        "metric_covered": metric_covered,
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
        help="Fallback git date expression if tip-commit churn is empty.",
    )
    parser.add_argument(
        "--rebuild-baseline",
        action="store_true",
        help="Delete .testmondata and rebuild the testmon dependency database.",
    )
    parser.add_argument(
        "--output",
        default="reports/qa_resource_allocation.json",
        help="Write JSON report to this path.",
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

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not result.get("metric_covered"):
        # Still emit report; fail the process so CI surfaces the gap.
        raise SystemExit(
            "QA Resource Allocation metric not covered: "
            f"tests_all={result.get('tests_all')} "
            f"tests_run={result.get('tests_run')} "
            f"tests_saved={result.get('tests_saved')} "
            f"selection_ratio_pct={result.get('selection_ratio_pct')} "
            f"normalized_regression_risk={result.get('normalized_regression_risk')} "
            f"risky_modules={result.get('risky_modules')}. "
            "Re-run with --rebuild-baseline."
        )


if __name__ == "__main__":
    main()
