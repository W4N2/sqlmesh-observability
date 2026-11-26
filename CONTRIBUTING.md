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
pip install -e ".[dev]"
scripts/ci.sh    # lint, format, types, Python 3.9 syntax, and tests
```

To test against an unreleased SQLMesh, install a local checkout first
(`pip install -e /path/to/sqlmesh -e ".[dev]"`); `scripts/test-against-sqlmesh-main.sh` runs the
suite against SQLMesh `main`.

See [AGENTS.md](AGENTS.md) for the project's design rules.

## Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a version tag is pushed.
The tag sets the version (setuptools_scm), and PyPI trusted publishing authorizes the upload, so
no API token is stored anywhere.

```bash
git tag -s v0.1.0a1 -m "v0.1.0a1"   # or -a if signing isn't set up
git push origin v0.1.0a1
```

The workflow runs lint, types, and tests, builds the sdist and wheel, checks that the wheel's
version matches the tag and that it ships the report and audit files, then publishes from the
`pypi` GitHub environment. Use a pre-release tag (`a`, `b`, `rc`) until the package works against
a released SQLMesh.
