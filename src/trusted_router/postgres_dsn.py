"""Shared parsing of Postgres-wire DSNs, including Aurora DSQL IAM details.

This exists because it was reimplemented once and the copy was wrong in a way
no test caught.  The control plane deploys a libpq **keyword/value** DSN::

    host=CLUSTER.dsql.eu-west-3.on.aws port=5432 user=admin dbname=postgres

The store parses that with :func:`psycopg.conninfo.conninfo_to_dict`, which is
libpq's own parser and accepts both that form and the ``postgresql://`` URI
form.  The ClickHouse drain parsed it with :func:`urllib.parse.urlsplit`, which
accepts only the URI form — against the deployed DSN it returned no hostname at
all and the drain raised before it could ever connect.

So there is one parser, here, and both callers import it.  Nothing in this
module does IO or touches a cloud SDK: it is pure string handling, which keeps
it importable from the ClickHouse host's slim virtualenv (no ``psycopg_pool``,
no store).
"""

from __future__ import annotations

import re

import psycopg.conninfo

#: Aurora DSQL cluster endpoints are ``<cluster-id>.dsql.<region>.on.aws``.
AWS_DSQL_HOST_RE = re.compile(
    r"^[^.]+\.dsql\.(?P<region>[a-z0-9-]+)\.on\.aws$",
    re.IGNORECASE,
)

#: The DSQL role that ``generate_db_connect_admin_auth_token`` authenticates.
#: Any other role needs the non-admin token, which is the whole point of
#: :func:`dsql_token_is_admin`.
DSQL_ADMIN_ROLE = "admin"


def dsn_field(dsn: str, field: str) -> str:
    """Return one libpq connection parameter, or ``""`` when unset."""
    return str(psycopg.conninfo.conninfo_to_dict(dsn).get(field) or "")


def dsn_has_password(dsn: str) -> bool:
    return bool(dsn_field(dsn, "password"))


def dsql_token_is_admin(dsn: str) -> bool:
    """Whether this DSN's role requires the *admin* connect token.

    DSQL mints tokens per role: ``generate_db_connect_admin_auth_token`` for
    ``admin`` and ``generate_db_connect_auth_token`` for every other role.
    Deriving this from the DSN's own ``user`` means a deployment can drop to a
    least-privilege role by changing only the DSN.
    """
    user = dsn_field(dsn, "user")
    return not user or user.lower() == DSQL_ADMIN_ROLE


def aws_dsql_connection_details(
    dsn: str,
    *,
    region_override: str = "",
    setting: str = "TR_POSTGRES_DSN",
) -> tuple[str, str]:
    """Return ``(hostname, region)`` for Aurora DSQL IAM authentication.

    `setting` only names the offending knob in error messages, so the store and
    the drain can each blame the input the operator actually set.
    """
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    hostname = str(params.get("host") or "").rstrip(".")
    if not hostname:
        raise ValueError(f"AWS DSQL IAM auth requires a hostname in {setting}")
    if params.get("password"):
        raise ValueError(
            f"{setting} must not contain a password when TR_POSTGRES_IAM_AUTH=aws-dsql"
        )

    region = region_override.strip()
    if not region:
        match = AWS_DSQL_HOST_RE.fullmatch(hostname)
        if match is None:
            raise ValueError(
                f"Could not infer the AWS region from {setting} host "
                f"{hostname!r}; set TR_POSTGRES_IAM_REGION"
            )
        region = match.group("region")
    return hostname, region
