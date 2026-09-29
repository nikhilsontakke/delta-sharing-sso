"""Check that the .env settings work: log in and list the tables you can access.

Run:  delta-sharing-sso-check
"""
import sys

from .config import Settings, create_client


def main() -> int:
    try:
        settings = Settings.from_env()
        print(f"Login method: {settings.auth_method} ({settings.auth_description})")
        client, login = create_client(settings)
        login.get_access_token()   # logs in (opens the browser for AUTH_METHOD=keycloak)
        print("Logged in. Token valid until:", login.expiry_info().get("access_expires_at", "unknown"))
        tables = client.tables_overview()
    except Exception as exc:  # show a readable message instead of a stack trace
        print(f"\nConnection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Connected to {client.base_url}")
    if tables.empty:
        print("No tables are shared with you yet.")
    else:
        print(f"{len(tables)} tables available:")
        for name in tables["full_name"]:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
