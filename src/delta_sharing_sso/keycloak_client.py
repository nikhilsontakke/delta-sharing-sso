import os
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from keycloak import KeycloakOpenID
from keycloak.pkce_utils import generate_code_challenge, generate_code_verifier


def token_expiry_info(token: dict[str, Any]) -> dict[str, Any]:
    """Return computed expiry information for a given token response dict."""
    now_ts = time.time()
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    info: dict[str, Any] = {"now": now_iso}

    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        access_expires_at = datetime.fromtimestamp(now_ts + float(expires_in), tz=timezone.utc).isoformat()
        info.update({"expires_in": int(expires_in), "access_expires_at": access_expires_at})

    refresh_expires_in = token.get("refresh_expires_in")
    if isinstance(refresh_expires_in, (int, float)):
        refresh_expires_at = datetime.fromtimestamp(now_ts + float(refresh_expires_in), tz=timezone.utc).isoformat()
        info.update({"refresh_expires_in": int(refresh_expires_in), "refresh_expires_at": refresh_expires_at})

    return info


class KeycloakClient:
    """Authenticates an interactive user via the OAuth2 authorization code flow with PKCE."""

    def __init__(
            self,
            server_url: str | None,
            client_id: str | None,
            realm: str | None,
            redirect_uri: str = "http://localhost:8000/callback",
            scope: str = "openid",
            callback_timeout_seconds: int = 180,
    ):
        if not server_url:
            raise ValueError("server_url is required")
        if not client_id:
            raise ValueError("client_id is required")

        self.server_url = server_url
        self.client_id = client_id
        self.realm = realm
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.callback_timeout_seconds = callback_timeout_seconds

        self._openid = KeycloakOpenID(
            server_url=self.server_url,
            client_id=self.client_id,
            realm_name=self.realm,
        )

    @classmethod
    def from_env(cls) -> "KeycloakClient":
        try:
            load_dotenv()
        except Exception:
            pass

        return cls(
            server_url=os.getenv("KEYCLOAK_URL"),
            client_id=os.getenv("KEYCLOAK_CLIENT_ID"),
            realm=os.getenv("KEYCLOAK_REALM"),
            redirect_uri=os.getenv("KEYCLOAK_REDIRECT_URI", "http://localhost:8000/callback"),
            scope=os.getenv("KEYCLOAK_SCOPE", "openid"),
            callback_timeout_seconds=int(os.getenv("KEYCLOAK_CALLBACK_TIMEOUT", "180")),
        )

    def build_auth_url(self, code_challenge: str, code_challenge_method: str) -> str:
        return self._openid.auth_url(
            redirect_uri=self.redirect_uri,
            scope=self.scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )

    def get_access_token(self, open_browser: bool = True) -> str:
        """Perform the PKCE auth flow and return the access token string."""
        token = self._run_auth_flow(open_browser, label="Opening browser for login...")
        return token["access_token"]

    def get_full_token(self, open_browser: bool = True) -> dict[str, Any]:
        """Perform the PKCE auth flow and return the full token response dict.

        Includes access_token, refresh_token, expires_in, refresh_expires_in, etc.
        """
        return self._run_auth_flow(open_browser, label="Opening browser for login (full token)...")

    def _run_auth_flow(self, open_browser: bool, label: str) -> dict[str, Any]:
        """Run the OAuth2 PKCE authorization code flow and return the token response."""
        callback_code_event = threading.Event()
        callback_state = {"authorization_code": None}

        handler_cls = self._build_callback_handler(callback_state, callback_code_event)
        threading.Thread(
            target=self._start_callback_server,
            args=(handler_cls,),
            daemon=True,
        ).start()

        code_verifier = generate_code_verifier()
        code_challenge, code_challenge_method = generate_code_challenge(code_verifier)
        auth_url = self.build_auth_url(code_challenge, code_challenge_method)

        print(label)
        if open_browser:
            webbrowser.open(auth_url)
        else:
            print(f"Open this URL manually: {auth_url}")

        if not callback_code_event.wait(self.callback_timeout_seconds):
            raise TimeoutError(
                f"No authorization code received within {self.callback_timeout_seconds} seconds"
            )

        code: str | None = callback_state["authorization_code"]
        if not code:
            raise RuntimeError("Authorization code was not set by callback handler")

        return self._openid.token(
            grant_type="authorization_code",
            code=code,
            redirect_uri=self.redirect_uri,
            code_verifier=code_verifier,
        )

    def token_expiry_info(self, token: dict[str, Any]) -> dict[str, Any]:
        """Return computed expiry information for a given token response dict."""
        return token_expiry_info(token)

    def _start_callback_server(self, handler_cls):
        parsed_redirect = urlparse(self.redirect_uri)
        if parsed_redirect.hostname != "localhost":
            raise ValueError("Only localhost redirect URIs are supported for local callback")

        port = parsed_redirect.port
        if port is None:
            raise ValueError("redirect_uri must include an explicit port")

        server = HTTPServer(("localhost", port), handler_cls)
        server.handle_request()
        server.server_close()

    @staticmethod
    def _build_callback_handler(callback_state: dict, callback_code_event: threading.Event):
        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                if "code" in query:
                    callback_state["authorization_code"] = query["code"][0]
                    callback_code_event.set()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Login successful. You can close this window.")
                else:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, format, *args):
                return

        return OAuthCallbackHandler


class ServiceAccountKeycloakClient:
    """Authenticates a service account via the OAuth2 client credentials grant.

    Tokens are cached in memory and refreshed automatically shortly before expiry.
    """

    def __init__(
            self,
            server_url: str | None,
            client_id: str | None,
            realm: str | None,
            client_secret: str | None,
            scope: str = "openid",
    ):
        if not server_url:
            raise ValueError("server_url is required")
        if not client_id:
            raise ValueError("client_id is required")
        if not client_secret:
            raise ValueError("client_secret is required")

        self.server_url = server_url
        self.client_id = client_id
        self.realm = realm
        self.scope = scope

        self._openid = KeycloakOpenID(
            server_url=server_url,
            client_id=client_id,
            realm_name=realm,
            client_secret_key=client_secret,
        )

        self._lock = threading.Lock()
        self._token: dict[str, Any] | None = None
        self._token_issued_at: float | None = None

    @classmethod
    def from_env(cls) -> "ServiceAccountKeycloakClient":
        try:
            load_dotenv()
        except Exception:
            pass

        return cls(
            server_url=os.getenv("KEYCLOAK_URL"),
            client_id=os.getenv("KEYCLOAK_CLIENT_ID"),
            realm=os.getenv("KEYCLOAK_REALM"),
            client_secret=os.getenv("KEYCLOAK_CLIENT_SECRET"),
            scope=os.getenv("KEYCLOAK_SCOPE", "openid"),
        )

    def get_access_token(self, refresh_buffer_seconds: int = 30) -> str:
        """Return a valid access token string, fetching a new one when needed."""
        return self.get_full_token(refresh_buffer_seconds)["access_token"]

    def get_full_token(self, refresh_buffer_seconds: int = 30) -> dict[str, Any]:
        """Return the cached token response dict, fetching a new one when needed."""
        with self._lock:
            if self._token is None or self._is_expired(refresh_buffer_seconds):
                self._fetch_token()
            return self._token

    def force_refresh(self) -> dict[str, Any]:
        """Discard the cached token and fetch a new one."""
        with self._lock:
            self._fetch_token()
            return self._token

    def token_expiry_info(self, token: dict[str, Any]) -> dict[str, Any]:
        """Return computed expiry information for a given token response dict."""
        return token_expiry_info(token)

    def _fetch_token(self) -> None:
        self._token = self._openid.token(grant_type="client_credentials", scope=self.scope)
        self._token_issued_at = time.time()

    def _is_expired(self, buffer_seconds: int) -> bool:
        if self._token is None or self._token_issued_at is None:
            return True

        expires_in = self._token.get("expires_in")
        if not isinstance(expires_in, (int, float)):
            # Unknown lifetime: treat as expired so we never reuse a stale token.
            return True

        elapsed = time.time() - self._token_issued_at
        return elapsed >= (float(expires_in) - buffer_seconds)
