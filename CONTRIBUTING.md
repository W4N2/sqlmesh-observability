# Contributing

## Sign-off

Every commit must carry a `Signed-off-by:` line from its human author, certifying the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "feat: ..."
```

Only human authors sign off. Commits must not carry AI or tool attribution trailers (for example a `Co-Authored-By:` line for an assistant).

## Signed commits

Commits must be cryptographically signed (GPG or SSH):

```bash
git config commit.gpgsign true
git commit -S -s -m "..."
```

The hosted repository will enforce this with branch protection ("Require signed commits") and a DCO check on pull requests.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ../sqlmesh -e ".[dev]"   # until the observer API ships in a SQLMesh release
ruff check . && ruff format --check .
mypy src
pytest
```
