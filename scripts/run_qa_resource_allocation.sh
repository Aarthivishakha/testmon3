#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}${PYTHONPATH:+:$PYTHONPATH}"
export PY_COLORS=0
export NO_COLOR=1

rm -f .testmondata

python testmon_metrics/compute_qa_resource_allocation.py \
  --source src \
  --tests tests \
  --rebuild-baseline \
  --output reports/qa_resource_allocation.json