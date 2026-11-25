"""SQL audit files shipped with the package.

They are copied into a project's ``audits/`` directory, with the macros they call in ``macros/``,
so they are versioned with the project and need no custom loader.
"""

from __future__ import annotations

import typing as t
from importlib import resources
from pathlib import Path

MACROS_FILE = "macros/observability_audits.py"


def shipped_audits() -> t.Dict[str, str]:
    """Map of file name to contents for every shipped ``.sql`` audit."""
    root = resources.files(__name__)
    return {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in sorted(root.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".sql")
    }


def shipped_files() -> t.Dict[str, str]:
    """Project-relative path to contents: the audits plus the macros they call."""
    files = {f"audits/{name}": text for name, text in shipped_audits().items()}
    files[MACROS_FILE] = resources.files(__name__).joinpath("macros.py").read_text(encoding="utf-8")
    return files


def copy_audits(project: t.Union[str, Path], force: bool = False) -> t.List[Path]:
    """Copy the shipped audits to ``<project>/audits/`` and their macros to ``<project>/macros/``.

    Files that already match are left alone. A file that differs (for example a local edit) is
    only overwritten with ``force``.

    Returns:
        The files that were written.

    Raises:
        FileExistsError: a file differs and ``force`` is false. Nothing is written in that case.
    """
    root = Path(project)
    files = {root / relative: text for relative, text in shipped_files().items()}
    conflicts = [
        path
        for path, text in files.items()
        if path.exists() and path.read_text(encoding="utf-8") != text
    ]
    if conflicts and not force:
        raise FileExistsError(
            "These files differ from the shipped audits (use --force to overwrite): "
            + ", ".join(str(path) for path in conflicts)
        )
    written = []
    for path, text in files.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written
