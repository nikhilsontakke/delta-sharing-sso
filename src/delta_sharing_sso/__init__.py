"""delta-sharing-sso: a lightweight Delta Sharing client authenticated via Keycloak (OIDC SSO).

    from delta_sharing_sso import Settings, create_client

    catalog, login = create_client()          # reads .env
    print(catalog.tables_overview())

    catalog.sales.orders.to_pandas(limit=100)          # browse it like a bucket
    catalog.table("sales.orders").where(country="DE").to_pandas()

Or skip the client entirely for a one-off read:

    from delta_sharing_sso import read_table
    df = read_table("sales.orders", limit=1000)        # reads .env, caches the login

See the README and examples/quickstart/ for the full walkthrough.
"""
import pandas as pd

from .config import Settings, create_client
from .delta_sharing_client import DeltaSharingClient
from .keycloak_client import KeycloakClient, ServiceAccountKeycloakClient, token_expiry_info
from .table import Table

__all__ = [
    "DeltaSharingClient",
    "Table",
    "KeycloakClient",
    "ServiceAccountKeycloakClient",
    "token_expiry_info",
    "Settings",
    "create_client",
    "default_catalog",
    "read_table",
]

__version__ = "0.2.0"

_default_catalog: DeltaSharingClient | None = None
_default_login = None


def default_catalog() -> DeltaSharingClient:
    """A lazily-created DeltaSharingClient built from .env, shared across calls to read_table().

    Logs in on first use only; the same client (and its cached login) is reused after that.
    """
    global _default_catalog, _default_login
    if _default_catalog is None:
        _default_catalog, _default_login = create_client()
    return _default_catalog


def read_table(name: str, limit: int | None = 5, **kwargs) -> pd.DataFrame:
    """Load a table as a DataFrame in one call, using .env for login (client reused across calls).

        from delta_sharing_sso import read_table
        df = read_table("sales.orders", limit=1000)

    `name` is "share.schema.table" or, if unique within its share, "share.table". Extra keyword
    arguments (version=, json_predicate_hints=) are passed through to Table.to_pandas().
    """
    return default_catalog().table(name).to_pandas(limit=limit, **kwargs)
