"""Offline tests for Table handles, catalog browsing (.table()/.<share>./find()) and read_table()."""
import json

import fsspec
import pandas as pd
import pytest

import delta_sharing_sso as dss
from delta_sharing_sso import DeltaSharingClient, Table
from delta_sharing_sso.delta_sharing_client import ProtocolTable


def read_profile(url: str) -> dict:
    with fsspec.open(url.split("#")[0]) as f:
        return json.load(f)


@pytest.fixture
def catalog(monkeypatch):
    """A DeltaSharingClient whose listing/loading is faked, seeded with a small set of tables."""
    tables = [
        ProtocolTable(name="orders", share="sales", schema="public"),
        ProtocolTable(name="returns", share="sales", schema="public"),
        ProtocolTable(name="orders", share="sales", schema="archive"),   # ambiguous "orders" in "sales"
        ProtocolTable(name="shipments", share="logistics", schema="public"),
    ]
    import delta_sharing_sso.delta_sharing_client as dsc
    monkeypatch.setattr(dsc.delta_sharing, "SharingClient",
                        lambda profile: type("F", (), {"list_all_tables": lambda self: tables})())

    calls = {}

    def fake_load(url, **kwargs):
        calls["url"], calls["kwargs"], calls["profile"] = url, kwargs, read_profile(url)
        return pd.DataFrame({"a": [1, 2]})

    monkeypatch.setattr(dsc.delta_sharing, "load_as_pandas", fake_load)

    class Meta:
        schema_string = json.dumps({"type": "struct", "fields": [
            {"name": "id", "type": "long", "nullable": False},
            {"name": "country", "type": "string", "nullable": True}]})
        partition_columns = ["country"]

    monkeypatch.setattr(dsc.delta_sharing, "get_table_metadata", lambda url: Meta())
    monkeypatch.setattr(dsc.delta_sharing, "get_table_version", lambda url: 7)

    client = DeltaSharingClient("https://x", "t")
    client._load_calls = calls
    return client


def test_table_handle_is_lazy_and_loads_on_demand(catalog):
    t = catalog.table("sales.public.orders")
    assert isinstance(t, Table) and t.full_name == "sales.public.orders"
    assert "url" not in catalog._load_calls          # nothing fetched yet
    assert t.version == 7
    df = t.to_pandas(limit=10)
    assert len(df) == 2 and catalog._load_calls["kwargs"]["limit"] == 10


def test_table_via_two_part_shortcut_when_unambiguous(catalog):
    t = catalog.table("logistics.shipments")
    assert (t.share, t.schema, t.table) == ("logistics", "public", "shipments")


def test_two_part_shortcut_raises_when_ambiguous(catalog):
    with pytest.raises(AttributeError, match="multiple schemas"):
        catalog.table("sales.orders")


def test_attribute_browsing_three_levels(catalog):
    t = catalog.sales.archive.orders
    assert (t.share, t.schema, t.table) == ("sales", "archive", "orders")


def test_attribute_browsing_two_level_shortcut(catalog):
    t = catalog.logistics.shipments
    assert isinstance(t, Table) and t.table == "shipments"


def test_attribute_browsing_unknown_share_raises(catalog):
    with pytest.raises(AttributeError):
        catalog.not_a_share


def test_bracket_access(catalog):
    t = catalog["sales.public.orders"]
    assert t.full_name == "sales.public.orders"


def test_find(catalog):
    assert catalog.find("order") == ["sales.archive.orders", "sales.public.orders"]


def test_where_infers_value_type_and_filters(catalog):
    catalog.table("sales.public.orders").where(country="DE").to_pandas()
    hints = json.loads(catalog._load_calls["kwargs"]["jsonPredicateHints"])
    assert hints == {"op": "equal", "children": [
        {"op": "column", "name": "country", "valueType": "string"},
        {"op": "literal", "value": "DE", "valueType": "string"}]}


def test_between_is_chainable_and_returns_new_handle(catalog):
    base = catalog.table("sales.public.orders")
    filtered = base.between("id", 1, 100)
    assert filtered is not base and base._predicate is None
    filtered.to_pandas()
    hints = json.loads(catalog._load_calls["kwargs"]["jsonPredicateHints"])
    assert hints["op"] == "and"


def test_dir_includes_shares(catalog):
    assert {"sales", "logistics"}.issubset(set(dir(catalog)))


def test_read_table_uses_default_catalog(monkeypatch):
    fake = object.__new__(DeltaSharingClient)
    seen = {}

    class FakeTable:
        def to_pandas(self, limit=5, **kw):
            seen["limit"], seen["kw"] = limit, kw
            return pd.DataFrame({"x": [1]})

    def fake_table(name):
        seen["name"] = name
        return FakeTable()

    fake.table = fake_table
    monkeypatch.setattr(dss, "default_catalog", lambda: fake)
    df = dss.read_table("sales.public.orders", limit=50)
    assert seen == {"name": "sales.public.orders", "limit": 50, "kw": {}}
    assert len(df) == 1
