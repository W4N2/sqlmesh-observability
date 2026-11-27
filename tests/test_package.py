import importlib


def test_package_imports() -> None:
    assert importlib.import_module("sqlmesh_observability") is not None


def test_report_page_assets_ship_with_the_package() -> None:
    from importlib import resources

    web = resources.files("sqlmesh_observability").joinpath("web")
    assert "SCHEMA_VERSION = 2" in web.joinpath("report.js").read_text("utf-8")
    assert ":root" in web.joinpath("report.css").read_text("utf-8")
