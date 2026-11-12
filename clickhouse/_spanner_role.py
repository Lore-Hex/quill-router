"""The optional Spanner database role the ClickHouse node programs connect as.

Every node program that reads or writes Spanner opens its database through
:func:`open_database`. With ``TR_SPANNER_DATABASE_ROLE`` unset (or blank) the
call is exactly the historical ``instance.database(database_id)``, so the
programs keep connecting with the service account's database-wide access.
With it set, they pass ``database_role=`` and every session they create is
bound to that fine-grained-access role.

Dependency-free on purpose: the node's systemd units run ``python -m
clickhouse.<program>`` with only ``/opt/tr-clickhouse`` on ``sys.path``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

DATABASE_ROLE_ENV = "TR_SPANNER_DATABASE_ROLE"


def database_role(environ: Mapping[str, str] | None = None) -> str | None:
    """The configured role, or ``None`` when the variable is unset or blank."""
    source = os.environ if environ is None else environ
    role = source.get(DATABASE_ROLE_ENV, "").strip()
    return role or None


def open_database(instance: Any, database_id: str, role: str | None) -> Any:
    """``instance.database(...)``, adding ``database_role`` only when one is set."""
    if role is None:
        return instance.database(database_id)
    return instance.database(database_id, database_role=role)
