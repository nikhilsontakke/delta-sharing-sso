import json
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import delta_sharing
import fsspec
import pandas as pd
from delta_sharing.protocol import Schema, Share, Table

# A token can be passed as a plain string, as an OAuth token response dict,
# as a callable returning a fresh token string, or as any object with a
# get_access_token() method (see config.py). Callables and providers are asked
# for a token on every request, so long-running jobs always use a valid token.
TokenSource = str | dict[str, Any] | Callable[[], str] | Any


class DeltaSharingClient:
    def __init__(self, base_url: str | None, access_token: TokenSource | None):
        if not base_url or not access_token:
            raise ValueError("base_url and access_token are required")

        self.base_url = base_url.rstrip("/")
        self._token_source = access_token

    @property
    def access_token(self) -> str:
        """The current bearer token, resolved from the configured token source."""
        source = self._token_source
        if hasattr(source, "get_access_token"):
            return source.get_access_token()
        if callable(source):
            return source()
        if isinstance(source, dict):
            return source["access_token"]
        return source

    @access_token.setter
    def access_token(self, value: TokenSource) -> None:
        self._token_source = value

    # ------------------------------------------------------------------ profile

    @contextmanager
    def _profile(self):
        """Context manager that yields the path of a temporary sharing profile.

        The profile is kept in an in-memory filesystem, so the bearer token is
        never written to disk, and it is removed again on exit.
        """
        profile = {
            "shareCredentialsVersion": 1,
            "endpoint": self.base_url,
            "bearerToken": self.access_token,
        }
        path = f"memory://delta_sharing_profiles/{uuid.uuid4().hex}.share"
        fs = fsspec.filesystem("memory")
        with fs.open(path, "w") as f:
            json.dump(profile, f)
        try:
            yield path
        finally:
            if fs.exists(path):
                fs.rm(path)

    @contextmanager
    def _sharing_client(self):
        """Context manager that yields a SharingClient and cleans up the profile on exit."""
        with self._profile() as profile_path:
            yield delta_sharing.SharingClient(profile_path)

    @contextmanager
    def _table_url(self, share: str, schema: str, table: str):
        with self._profile() as profile_path:
            yield f"{profile_path}#{share}.{schema}.{table}"

    # ---------------------------------------------------------------- discovery

    def list_shares(self) -> list[Share]:
        """Return all available shares."""
        with self._sharing_client() as client:
            return list(client.list_shares())

    def list_schemas(self, share: Share | str) -> list[Schema]:
        """Return all schemas in the given share (a Share object or its name)."""
        if isinstance(share, str):
            share = Share(name=share)
        with self._sharing_client() as client:
            return list(client.list_schemas(share))

    def list_tables(self, schema: Schema | str) -> list[Table]:
        """Return all tables in the given schema (a Schema object or "share.schema")."""
        if isinstance(schema, str):
            share_name, schema_name = schema.split(".", 1)
            schema = Schema(name=schema_name, share=share_name)
        with self._sharing_client() as client:
            return list(client.list_tables(schema))

    def list_all_tables(self) -> list[Table]:
        """Return every table the caller can access, across all shares and schemas."""
        with self._sharing_client() as client:
            return list(client.list_all_tables())

    def tables_overview(self) -> pd.DataFrame:
        """All accessible tables as a DataFrame with share, schema, table and full name."""
        rows = [
            {"share": t.share, "schema": t.schema, "table": t.name,
             "full_name": f"{t.share}.{t.schema}.{t.name}"}
            for t in self.list_all_tables()
        ]
        return pd.DataFrame(rows, columns=["share", "schema", "table", "full_name"])

    # ----------------------------------------------------------------- metadata

    def get_table_metadata(self, share: str, schema: str, table: str):
        """Return the table metadata (schema string, partition columns, size, ...).

        This does not download any data.
        """
        with self._table_url(share, schema, table) as url:
            return delta_sharing.get_table_metadata(url)

    def get_table_columns(self, share: str, schema: str, table: str) -> pd.DataFrame:
        """Return the table's columns with type, nullability and partition flag."""
        metadata = self.get_table_metadata(share, schema, table)
        fields = json.loads(metadata.schema_string)["fields"]
        partitions = set(metadata.partition_columns or [])
        return pd.DataFrame(
            [
                {
                    "name": f["name"],
                    "type": f["type"] if isinstance(f["type"], str) else f["type"].get("type", "struct"),
                    "nullable": f.get("nullable", True),
                    "partition_column": f["name"] in partitions,
                }
                for f in fields
            ]
        )

    def get_table_version(self, share: str, schema: str, table: str) -> int:
        """Return the current version of the shared table."""
        with self._table_url(share, schema, table) as url:
            return delta_sharing.get_table_version(url)

    # --------------------------------------------------------------------- data

    def load_table_as_dataframe(
            self,
            share: str,
            schema: str,
            table: str,
            limit: int | None = 5,
            json_predicate_hints: dict[str, Any] | str | None = None,
            version: int | None = None,
    ) -> pd.DataFrame:
        """Load a table as a pandas DataFrame.

        Parameters:
          - share, schema, table: identify the table to load.
          - limit: maximum number of rows to fetch. Pass None to load the whole table.
          - json_predicate_hints: optional predicate hints dict per the Delta Sharing protocol.
            See: https://github.com/delta-io/delta-sharing/blob/main/PROTOCOL.md#json-predicates-for-filtering
            Hints let the server skip files; they are not guaranteed to filter every row,
            so filter the result again in pandas when exact results matter.
          - version: optional table version to read (time travel), if history is shared.
        """
        if json_predicate_hints is not None:
            if isinstance(json_predicate_hints, str):
                json_predicate_hints = json.loads(json_predicate_hints)
            json_predicate_str = json.dumps(json_predicate_hints, separators=(",", ":"))
        else:
            json_predicate_str = None

        with self._table_url(share, schema, table) as table_url:
            return delta_sharing.load_as_pandas(
                table_url,
                jsonPredicateHints=json_predicate_str,
                use_delta_format=True,
                convert_in_batches=False,
                limit=limit,
                version=version,
            )

    def load_table_changes_as_dataframe(
            self,
            share: str,
            schema: str,
            table: str,
            starting_version: int | None = None,
            ending_version: int | None = None,
            starting_timestamp: str | None = None,
            ending_timestamp: str | None = None,
    ) -> pd.DataFrame:
        """Load only the rows that changed between two versions or timestamps.

        Requires change data feed to be enabled on the shared table. The result has
        extra columns such as _change_type and _commit_version.
        """
        with self._table_url(share, schema, table) as table_url:
            return delta_sharing.load_table_changes_as_pandas(
                table_url,
                starting_version=starting_version,
                ending_version=ending_version,
                starting_timestamp=starting_timestamp,
                ending_timestamp=ending_timestamp,
                use_delta_format=True,
            )
