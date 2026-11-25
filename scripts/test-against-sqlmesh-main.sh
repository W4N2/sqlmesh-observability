#!/usr/bin/env bash
# Run the test suite against a clean copy of the SQLMesh checkout's committed HEAD.
#
# ../sqlmesh may carry uncommitted spike edits. This exports HEAD with `git archive` (read-only:
# nothing in ../sqlmesh is stashed, edited, or written) into .sqlmesh-main/ and puts it first on
# PYTHONPATH, so `import sqlmesh` and `import web` resolve to committed code only.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
sqlmesh_repo="${SQLMESH_REPO:-$here/../sqlmesh}"
export_dir="$here/.sqlmesh-main"
python="${PYTHON:-$here/.venv/bin/python}"

rm -rf "$export_dir"
mkdir -p "$export_dir"
git -C "$sqlmesh_repo" archive HEAD sqlmesh web | tar -x -C "$export_dir"
# _version.py is generated at install time and is not tracked; copy it so imports work.
cp "$sqlmesh_repo/sqlmesh/_version.py" "$export_dir/sqlmesh/_version.py"

echo "SQLMesh HEAD: $(git -C "$sqlmesh_repo" rev-parse --short HEAD)"
PYTHONPATH="$export_dir${PYTHONPATH:+:$PYTHONPATH}" "$python" - <<'PY'
import sqlmesh.core.console as c
assert not hasattr(c, "register_execution_observer"), "export still contains spike edits"
print("sqlmesh from", c.__file__)
PY
PYTHONPATH="$export_dir${PYTHONPATH:+:$PYTHONPATH}" "$python" -m pytest "$@"
