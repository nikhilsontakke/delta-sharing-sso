"""Offline tests: the delta_sharing library is mocked, so no server or login is needed.

Run with:  python -m pytest -q
"""
import json

import fsspec
import pandas as pd
import pytest

from delta_sharing_sso import delta_sharing_client as dsc
from delta_sharing_sso.delta_sharing_client import DeltaSharingClient
from delta_sharing_sso.predicates import between, time_equal, time_range, to_epoch_seconds


def read_profile(url: str) -> dict:
    profile_path = url.split("#")[0]
    with fsspec.open(profile_path) as f:
        return json.load(f)


@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def fake_load(url, **kwargs):
        calls["url"], calls["kwargs"], calls["profile"] = url, kwargs, read_profile(url)
        return pd.DataFrame({"a": [1, 2]})

    monkeypatch.setattr(dsc.delta_sharing, "load_as_pandas", fake_load)
    return calls


def test_requires_url_and_token():
    with pytest.raises(ValueError):
        DeltaSharingClient(None, "t")
    with pytest.raises(ValueError):
        DeltaSharingClient("https://x", None)


def test_string_token_and_table_url(captured):
    DeltaSharingClient("https://server/delta-sharing/", "abc").load_table_as_dataframe("s", "sc", "t")
    assert captured["url"].endswith("#s.sc.t")
    assert captured["url"].startswith("memory://")          # never written to disk
    assert captured["profile"] == {"shareCredentialsVersion": 1,
                                   "endpoint": "https://server/delta-sharing", "bearerToken": "abc"}
    assert captured["kwargs"]["limit"] == 5                  # original default kept


def test_profile_removed_after_use(captured):
    DeltaSharingClient("https://x", "abc").load_table_as_dataframe("s", "sc", "t")
    assert not fsspec.filesystem("memory").exists(captured["url"].split("#")[0])


def test_callable_token_is_called_per_request(captured):
    tokens = iter(["first", "second"])
    client = DeltaSharingClient("https://x", lambda: next(tokens))
    client.load_table_as_dataframe("s", "sc", "t")
    assert captured["profile"]["bearerToken"] == "first"
    client.load_table_as_dataframe("s", "sc", "t")
    assert captured["profile"]["bearerToken"] == "second"


def test_dict_token(captured):
    DeltaSharingClient("https://x", {"access_token": "from-dict"}).load_table_as_dataframe("s", "sc", "t")
    assert captured["profile"]["bearerToken"] == "from-dict"


def test_predicates_and_full_load(captured):
    DeltaSharingClient("https://x", "t").load_table_as_dataframe(
        "s", "sc", "t", limit=None, json_predicate_hints=time_equal("ts", "2026-05-21", "epoch_seconds", "int"),
        version=3)
    assert captured["kwargs"]["limit"] is None and captured["kwargs"]["version"] == 3
    assert json.loads(captured["kwargs"]["jsonPredicateHints"]) == {
        "op": "equal", "children": [
            {"op": "column", "name": "ts", "valueType": "int"},
            {"op": "literal", "value": 1779321600, "valueType": "int"}]}


def test_string_predicate_still_accepted(captured):
    DeltaSharingClient("https://x", "t").load_table_as_dataframe(
        "s", "sc", "t", json_predicate_hints=json.dumps(between("c", "a", "b")))
    assert json.loads(captured["kwargs"]["jsonPredicateHints"])["op"] == "and"


@pytest.mark.parametrize("fmt, expected", [
    ("epoch_seconds", [1777593600, 1780185600]),
    ("epoch_millis", [1777593600000, 1780185600000]),
    ("date", ["2026-05-01", "2026-05-31"]),
    ("timestamp", ["2026-05-01T00:00:00Z", "2026-05-31T00:00:00Z"]),
])
def test_time_range_formats(fmt, expected):
    p = time_range("c", "2026-05-01", "2026-05-31", fmt)
    assert [c["children"][1]["value"] for c in p["children"]] == expected
    assert to_epoch_seconds("2026-05-21") == 1779321600


def test_time_range_string_format():
    p = time_range("c", "2026-05-01", "2026-05-31", "string", string_format="%Y%m%d")
    assert [c["children"][1]["value"] for c in p["children"]] == ["20260501", "20260531"]


def test_token_provider_object(captured):
    class Provider:
        def get_access_token(self):
            return "from-provider"

    DeltaSharingClient("https://x", Provider()).load_table_as_dataframe("s", "sc", "t")
    assert captured["profile"]["bearerToken"] == "from-provider"


def test_list_tables_accepts_string(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, profile): pass
        def list_tables(self, schema):
            seen["schema"] = schema
            return []

    monkeypatch.setattr(dsc.delta_sharing, "SharingClient", FakeClient)
    DeltaSharingClient("https://x", "t").list_tables("sales.orders")
    assert (seen["schema"].share, seen["schema"].name) == ("sales", "orders")


def test_get_table_columns(monkeypatch):
    class Meta:
        schema_string = json.dumps({"type": "struct", "fields": [
            {"name": "id", "type": "long", "nullable": False},
            {"name": "tags", "type": {"type": "array", "elementType": "string"}, "nullable": True},
            {"name": "event_ts", "type": "long", "nullable": False}]})
        partition_columns = ["event_ts"]

    monkeypatch.setattr(dsc.delta_sharing, "get_table_metadata", lambda url: Meta())
    cols = DeltaSharingClient("https://x", "t").get_table_columns("s", "sc", "t")
    assert cols["type"].tolist() == ["long", "array", "long"]
    assert cols["partition_column"].tolist() == [False, False, True]
