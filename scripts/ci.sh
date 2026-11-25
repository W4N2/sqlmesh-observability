#!/usr/bin/env bash
# Every check a change must pass. Run from anywhere:
#   scripts/ci.sh
# Exits non-zero on the first failure.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
bin="${VENV_BIN:-$here/.venv/bin}"

step() { printf '\n== %s\n' "$*"; }

step "guards: no sqlmesh.materializations or set_console in src/"
hits="$(grep -rnE 'sqlmesh\.materializations|set_console' --include='*.py' src || true)"
if [ -n "$hits" ]; then
  echo "$hits"
  echo "guard failed: src/ must not use sqlmesh.materializations or set_console" >&2
  exit 1
fi
echo "ok"

step "ruff check"
"$bin/ruff" check .

step "ruff format --check"
"$bin/ruff" format --check src tests

step "mypy"
"$bin/mypy" src

step "Python 3.9 syntax"
"$bin/python" - <<'PY'
import ast, pathlib, sys
bad = []
for path in sorted(pathlib.Path("src").rglob("*.py")) + sorted(pathlib.Path("tests").rglob("*.py")):
    try:
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
    except SyntaxError as error:
        bad.append(f"{path}: {error}")
print("\n".join(bad) or "ok")
sys.exit(1 if bad else 0)
PY

step "pytest (installed SQLMesh)"
"$bin/python" -m pytest -q -p no:warnings

step "pytest (SQLMesh main)"
scripts/test-against-sqlmesh-main.sh -q -p no:warnings

printf '\nall checks passed\n'
