"""delta-sharing-sso: a lightweight Delta Sharing client authenticated via Keycloak (OIDC SSO).

    from delta_sharing_sso import Settings, create_client

    ds_client, login = create_client()          # reads .env
    print(ds_client.tables_overview())

See the README and examples/quickstart/ for the full walkthrough.
"""

from .config import Settings, create_client
from .delta_sharing_client import DeltaSharingClient
from .keycloak_client import KeycloakClient, ServiceAccountKeycloakClient, token_expiry_info

__all__ = [
    "DeltaSharingClient",
    "KeycloakClient",
    "ServiceAccountKeycloakClient",
    "token_expiry_info",
    "Settings",
    "create_client",
]

__version__ = "0.1.0"
