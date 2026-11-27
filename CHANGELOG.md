# Changelog

All notable changes to `sqlmesh-observability`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, any release may include breaking changes.

The package needs SQLMesh's execution-observation API
([SQLMesh #6051](https://github.com/SQLMesh/sqlmesh/pull/6051), merged 31 October 2026), which is
not in a SQLMesh release yet. The pre-releases below are tested against SQLMesh `main`.

## [Unreleased]

### Added

- CI runs the test suite on Python 3.9 to 3.13.
- A nightly workflow tests against SQLMesh `main` and opens an issue when a SQLMesh release ships
  the execution-observation API.
- PyPI project links and classifiers, and a `py.typed` marker so type checkers use the package's
  annotations.
- An end-to-end test on Snowflake (`tests/test_snowflake.py`), run weekly by the `snowflake`
  workflow once `SNOWFLAKE_*` repository secrets are set. Locally it also accepts browser (SSO)
  login with `SNOWFLAKE_AUTHENTICATOR=externalbrowser`.

### Fixed

- **Snowflake:** nothing was logged. The package created its tables with quoted lower-case names
  that its own reads, unquoted, could not find. Table and column names are now in the
  warehouse's case (upper case on Snowflake), so `OBSERVABILITY.INVOCATIONS` works unquoted.
- **Snowflake:** passive volume, freshness, and column metrics failed, because their queries
  dropped the quotes around SQLMesh's lower-case `sqlmesh__<schema>` physical schemas.
- Without Playwright installed (Python 3.9), the browser tests failed to collect instead of
  skipping.

## [0.1.0a4] - 2026-11-27

### Fixed

- Standalone audits were logged with no model, and the report showed them as a model named
  `None`. They are now logged under their own name and appear in lineage as `AUDIT` nodes.
- Model names drop a shared catalog even when some names (standalone audits) have none.
- Lineage card names use the room left beside the downstream count instead of a fixed length.

## [0.1.0a3] - 2026-11-27 [YANKED]

Yanked: standalone audits were logged without a model name. Use 0.1.0a4.

### Added

- **Lineage view** built from SQLMesh state: a layered graph around one model with depth,
  direction, and issues-only controls, at most 150 models (20 per column) with the rest folded
  into "+N more"; cards show status, schema, kind, owner, downstream count, and failing audits and
  anomalous metrics by name.
- **Impact view**: models with open issues ranked by how many models depend on them.
- Downstream counts on open issues, the models list, and model pages; a lineage panel on each
  model page; downstream names in the no-JavaScript fallback.
- Browser tests (Playwright, headless Chromium) for the report renderer.
- CI for lint, types, and tests on every push and pull request.

### Changed

- **Breaking:** the report's data format is now `schema_version` 2 (adds lineage and per-issue
  impact). A page that meets an unknown version shows only the no-JavaScript summary.

### Fixed

- Re-sorting or filtering a long list started mid-list at the old scroll position.

## [0.1.0a2] - 2026-11-26

First release on PyPI.

### Added

- Warehouse log of plans, runs, model runs, and audit results, keyed by model version, written
  through SQLMesh's execution-observation API (`install()` in `config.py`).
- Package anomaly audits (`volume_anomaly`, `freshness_anomaly`, `column_anomaly`) and passive
  volume, freshness, schema, and column monitors scored at read time.
- `sqlmesh-observe report`: one offline HTML file for 500 to 1,500+ models, with the data embedded
  as versioned JSON (`schema_version` 1) and drawn in the browser: dashboard, model tree, windowed
  lists, model pages, charts with expected bands, and a no-JavaScript fallback. `--history`
  limits stored intervals per model (default 30).
- `sqlmesh-observe serve`: the same page as a read-only web app, plus `GET /data.json`.
- `sqlmesh-observe monitor`: one grouped message per owner for new failures and anomalies.
- A tag-triggered release workflow with PyPI trusted publishing.

[Unreleased]: https://github.com/W4N2/sqlmesh-observability/compare/v0.1.0a4...HEAD
[0.1.0a4]: https://github.com/W4N2/sqlmesh-observability/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/W4N2/sqlmesh-observability/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/W4N2/sqlmesh-observability/releases/tag/v0.1.0a2
