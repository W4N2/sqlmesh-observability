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

The [DCO GitHub App](https://github.com/apps/dco) fails a pull request when any of its commits has no `Signed-off-by:` line. Cryptographic signing is not enforced yet.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium   # for the report's browser tests
scripts/ci.sh    # lint, format, types, Python 3.9 syntax, and tests
```

`tests/test_report_browser.py` opens generated reports in headless Chromium and checks what the
page draws: every view, filters from the URL, the model tree, long lists, expanding a row to its
chart, the theme switch, and phone widths. Without Playwright's Chromium these tests skip; CI
installs it and fails if they are skipped.

To test against an unreleased SQLMesh, install a local checkout first
(`pip install -e /path/to/sqlmesh -e ".[dev]"`); `scripts/test-against-sqlmesh-main.sh` runs the
suite against SQLMesh `main`.

See [AGENTS.md](AGENTS.md) for the project's design rules.

### Snowflake

`tests/test_snowflake.py` runs one short history on a real Snowflake account and is skipped
unless the `SNOWFLAKE_*` variables listed in that file are set. Point it at a scratch database: it
creates schemas with a random suffix and drops them afterwards. To run it locally:

```bash
pip install "snowflake-connector-python[secure-local-storage]"
export SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_WAREHOUSE=... SNOWFLAKE_DATABASE=...
export SNOWFLAKE_AUTHENTICATOR=externalbrowser   # browser (SSO) login; or SNOWFLAKE_PASSWORD=...,
                                                # or SNOWFLAKE_PRIVATE_KEY_PATH=... (key pair)
pytest tests/test_snowflake.py -v
```

The `snowflake` workflow runs it weekly and on demand from the same names as repository secrets.

## Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a version tag is pushed.
The tag sets the version (setuptools_scm), and PyPI trusted publishing authorizes the upload, so
no API token is stored anywhere.

Before tagging, move `CHANGELOG.md`'s `[Unreleased]` entries under the new version with
today's date, and update the compare links at the bottom.

```bash
git tag -s v0.1.0a1 -m "v0.1.0a1"   # or -a if signing isn't set up
git push origin v0.1.0a1
```

The workflow runs lint, types, and tests, builds the sdist and wheel, checks that the wheel's
version matches the tag and that it ships the report and audit files, then publishes from the
`pypi` GitHub environment. Use a pre-release tag (`a`, `b`, `rc`) until the package works against
a released SQLMesh.
