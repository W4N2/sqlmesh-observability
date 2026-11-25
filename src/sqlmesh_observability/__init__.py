"""Elementary-style data observability for SQLMesh projects.

Enable it from a project's Python ``config.py``::

    from sqlmesh_observability import install

    config = install(Config(...), "warehouse")

See the documentation in docs/ for configuration and commands.
"""

from sqlmesh_observability.hook import install

__all__ = ["install"]
