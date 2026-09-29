# delta-sharing-sso

A lightweight Python client for [Delta Sharing](https://delta.io/sharing/) tables, authenticated via Keycloak (OIDC single sign-on) — plus a ready-to-run notebook to explore and visualise whatever's shared with you.

Everything specific to a deployment (Keycloak realm/client, server URL, default table, partition column) is read from a `.env` file, so the same code works for any share.

## Install

```bash
pip install delta-sharing-sso              # just the client library
pip install delta-sharing-sso[notebook]    # + Jupyter, Plotly, Parquet/Excel export (for examples/quickstart)
```

Requires Python 3.10+.

## Quickstart (for people who just want to explore data)

Grab the [`examples/quickstart`](examples/quickstart) folder — it's the only thing you need, no other files:

```bash
pip install delta-sharing-sso[notebook]
cp .env.example .env       # fill in your Keycloak and Delta Sharing details
delta-sharing-sso-check    # sanity-check the login and list your tables
jupyter lab                # open explore_and_visualise.ipynb
```

## Usage in Python

```python
from delta_sharing_sso import create_client
from delta_sharing_sso.predicates import time_range

ds, login = create_client()          # reads .env; opens the browser for AUTH_METHOD=keycloak
print(ds.tables_overview())

cols = ds.get_table_columns("my_share", "my_schema", "my_table")
df = ds.load_table_as_dataframe(
    "my_share", "my_schema", "my_table", limit=None,
    json_predicate_hints=time_range("event_date", "2026-05-01", "2026-05-31", fmt="date"),
)
```

The pieces can also be used directly, without `.env` or `create_client`:

```python
from delta_sharing_sso import KeycloakClient, DeltaSharingClient

kc = KeycloakClient(server_url="...", client_id="...", realm="...")
ds = DeltaSharingClient(base_url="<DELTA_SHARING_URL>", access_token=kc.get_access_token())
```

## Configuration (`.env`)

**Login** (required):

| Variable | Description |
|---|---|
| `AUTH_METHOD` | `keycloak` for a browser login (authorization code flow with PKCE), or `keycloak_service_account` for a service account (client credentials), e.g. for scheduled jobs |
| `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID` | Keycloak server, realm and client |
| `KEYCLOAK_CLIENT_SECRET` | Only for `keycloak_service_account`. The Keycloak client must be confidential (Client authentication = On) with *Service accounts roles* enabled |
| `DELTA_SHARING_URL` | Delta Sharing server URL |
| `KEYCLOAK_REDIRECT_URI`, `KEYCLOAK_SCOPE`, `KEYCLOAK_CALLBACK_TIMEOUT` | Optional; defaults `http://localhost:8000/callback`, `openid`, `180` seconds |

**Data** (all optional; discovered from the share when empty):

| Variable | Description |
|---|---|
| `DEFAULT_TABLE` | Table the notebook opens first, as `share.schema.table` |
| `COMPARE_TABLES` | Comma-separated tables to compare in the notebook |
| `PARTITION_COLUMN` | Partition column to filter on. Default: detected from the table metadata, preferring time-based partitions |
| `PARTITION_FORMAT` | How the partition column stores time: `auto` (default), `epoch_seconds`, `epoch_millis`, `date`, `timestamp` or `string` |
| `PARTITION_VALUE_TYPE` | Override the predicate value type if the server expects a different one, e.g. `int` for an epoch column stored as `long` |
| `PARTITION_STRING_FORMAT` | For `string` partitions: how dates are written, e.g. `%Y-%m-%d` (default) or `%Y%m%d` |

### Certificate configuration

If the Delta Sharing server or Keycloak instance uses a custom CA certificate (e.g. an internal corporate CA or a self-signed certificate), append it to the `certifi` bundle used by Python:

```bash
cat /path/to/your/ca-certificate.crt >> $(python -c "import certifi; print(certifi.where())")
```

> This must be repeated whenever the environment is recreated or the `certifi` package is updated.

## Project structure

### `delta_sharing_sso.keycloak_client`

Two independent clients for authenticating against Keycloak:

- `KeycloakClient` — interactive user login using the OAuth2 Authorization Code flow with PKCE. It opens a browser for user login, listens for the callback on a local HTTP server, and exchanges the authorization code for an access token.
- `ServiceAccountKeycloakClient` — non-interactive machine-to-machine authentication using the OAuth2 Client Credentials grant. Tokens are cached in memory and refreshed automatically shortly before they expire.

Both can be initialized manually or via environment variables using `from_env()`, and both expose `get_access_token()` and `get_full_token()`.

### `delta_sharing_sso.config`

Reads all settings from `.env` (`Settings.from_env()`) and creates a ready-to-use client with `create_client()`. It wraps the Keycloak clients as token providers:

- `KeycloakUserLogin` logs in once in the browser, reuses the token, and renews it silently with the refresh token when it expires. The browser opens again only when the Keycloak session has ended.
- `KeycloakServiceAccountLogin` uses the service account client, which already refreshes tokens.

### `delta_sharing_sso.delta_sharing_client`

`DeltaSharingClient` wraps the `delta-sharing` library. It builds a temporary sharing profile from the server URL and a bearer token, keeping it in an in-memory filesystem so the token is never written to disk. It provides methods to:

- list shares, schemas and tables, or every table at once (`list_all_tables()`, `tables_overview()`);
- inspect a table without downloading data: columns and partition columns (`get_table_columns()`), metadata and version;
- load a table as a pandas DataFrame, with optional predicate filtering, row limits (`limit=None` loads everything) and time travel (`version=`);
- load only changed rows (`load_table_changes_as_dataframe()`), where change data feed is enabled.

`access_token` can be a token string, a token response dict, a callable, or a token provider with `get_access_token()`. Callables and providers are asked for a token on every request, so long-running jobs always use a valid token.

### `delta_sharing_sso.predicates`

Builds Delta Sharing JSON predicate hints without hand-writing JSON: `equal`, `between`, `and_`, `or_`, and `time_range` / `time_equal`, which convert dates to whatever format the partition column uses.

### `delta_sharing_sso.explore_helpers`

Schema-agnostic helpers: profiling a DataFrame, detecting date/measure/category columns, detecting how a partition column stores time (`choose_partition`), and Plotly charts (rows over time, measure over time, top values, distribution, missing values). Needs the `notebook` extra.

### `delta_sharing_sso.check_connection` (`delta-sharing-sso-check`)

Verifies the `.env` settings: logs in and lists the tables you can access, with readable error messages if something is wrong.

### `examples/quickstart/`

The only folder an end user needs: `explore_and_visualise.ipynb` and `.env.example`. The notebook walks through logging in, finding tables, inspecting columns, loading a sample, detecting the partition column, loading only a time range, visualising, comparing tables, reading earlier versions and exporting to Parquet, CSV or Excel.

### `tests/`

Offline tests (Keycloak and the Delta Sharing library are mocked, so no server or login is needed).

```bash
pip install -e ".[dev]"
pytest -q
```

Predicate hints let the server skip files but are not guaranteed to be exact, so filter again in pandas when precise results matter (the example notebook does this).

## License

MIT — see [LICENSE](LICENSE).
