"""All customer-specific settings, read from environment variables / the .env file.

    from delta_sharing_sso import Settings, create_client

    settings = Settings.from_env()
    ds_client, login = create_client(settings)

See .env.example for every setting.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from .delta_sharing_client import DeltaSharingClient
from .keycloak_client import KeycloakClient, ServiceAccountKeycloakClient, token_expiry_info

AUTH_METHODS = {
    "keycloak": "Browser login with Keycloak (authorization code flow with PKCE)",
    "keycloak_service_account": "Service account login with Keycloak (client credentials)",
}
REFRESH_BUFFER_SECONDS = 30


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


@dataclass
class Settings:
    auth_method: str = "keycloak"
    delta_sharing_url: str | None = None

    # data defaults (all optional; discovered at runtime when empty)
    default_table: str | None = None          # share.schema.table
    compare_tables: list[str] = field(default_factory=list)
    partition_column: str | None = None
    partition_format: str = "auto"            # auto | epoch_seconds | epoch_millis | date | timestamp | string
    partition_value_type: str | None = None   # override the predicate valueType, e.g. int
    partition_string_format: str = "%Y-%m-%d"

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> "Settings":
        load_dotenv(dotenv_path or ".env", override=False)
        method = (_env("AUTH_METHOD", "keycloak") or "").lower()
        if method not in AUTH_METHODS:
            raise ValueError(f"AUTH_METHOD={method!r} is not supported. Use one of: {', '.join(AUTH_METHODS)}")
        compare = [t.strip() for t in (_env("COMPARE_TABLES", "") or "").split(",") if t.strip()]
        return cls(
            auth_method=method,
            delta_sharing_url=_env("DELTA_SHARING_URL"),
            default_table=_env("DEFAULT_TABLE"),
            compare_tables=compare,
            partition_column=_env("PARTITION_COLUMN"),
            partition_format=(_env("PARTITION_FORMAT", "auto") or "auto").lower(),
            partition_value_type=_env("PARTITION_VALUE_TYPE"),
            partition_string_format=_env("PARTITION_STRING_FORMAT", "%Y-%m-%d"),
        )

    @property
    def auth_description(self) -> str:
        return AUTH_METHODS[self.auth_method]


class KeycloakUserLogin:
    """Token provider around KeycloakClient that logs in once and reuses the token.

    KeycloakClient opens the browser on every call, so this caches the token and,
    when it expires, renews it silently with the refresh token. The browser opens
    again only when the Keycloak session itself has ended.
    """

    def __init__(self, client: KeycloakClient | None = None):
        self._client = client or KeycloakClient.from_env()
        self._lock = threading.Lock()
        self._token: dict[str, Any] | None = None
        self._issued_at = 0.0

    def get_access_token(self) -> str:
        with self._lock:
            if not self._valid():
                self._token = self._refresh() or self._client.get_full_token()
                self._issued_at = time.time()
            return self._token["access_token"]

    def get_full_token(self) -> dict[str, Any]:
        self.get_access_token()
        return self._token

    def expiry_info(self) -> dict[str, Any]:
        return token_expiry_info(self._token) if self._token else {}

    def _valid(self) -> bool:
        if not self._token:
            return False
        lifetime = self._token.get("expires_in")
        if not isinstance(lifetime, (int, float)):
            return False
        return time.time() - self._issued_at < float(lifetime) - REFRESH_BUFFER_SECONDS

    def _refresh(self) -> dict[str, Any] | None:
        refresh_token = (self._token or {}).get("refresh_token")
        if not refresh_token:
            return None
        try:
            return self._client._openid.refresh_token(refresh_token)
        except Exception:  # session ended: fall back to a new browser login
            return None


class KeycloakServiceAccountLogin:
    """Token provider around ServiceAccountKeycloakClient (which already caches and refreshes)."""

    def __init__(self, client: ServiceAccountKeycloakClient | None = None):
        self._client = client or ServiceAccountKeycloakClient.from_env()

    def get_access_token(self) -> str:
        return self._client.get_access_token()

    def get_full_token(self) -> dict[str, Any]:
        return self._client.get_full_token()

    def expiry_info(self) -> dict[str, Any]:
        return token_expiry_info(self._client.get_full_token())


def create_client(settings: Settings | None = None):
    """Return (DeltaSharingClient, login) for the configured AUTH_METHOD.

    The client asks `login` for a token on every request, so expired tokens are
    renewed automatically.
    """
    settings = settings or Settings.from_env()
    if not settings.delta_sharing_url:
        raise ValueError("Set DELTA_SHARING_URL in .env to the Delta Sharing server URL")
    login = KeycloakServiceAccountLogin() if settings.auth_method == "keycloak_service_account" \
        else KeycloakUserLogin()
    return DeltaSharingClient(base_url=settings.delta_sharing_url, access_token=login), login
