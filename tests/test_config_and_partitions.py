"""Offline tests for config.py (Keycloak login wrappers) and partition detection."""
import pandas as pd
import pytest

from delta_sharing_sso import config
from delta_sharing_sso.delta_sharing_client import DeltaSharingClient
from delta_sharing_sso.explore_helpers import add_partition_datetime, choose_partition, infer_partition_format

ENV_KEYS = ["AUTH_METHOD", "DELTA_SHARING_URL", "KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID",
            "KEYCLOAK_CLIENT_SECRET", "COMPARE_TABLES", "DEFAULT_TABLE", "PARTITION_COLUMN", "PARTITION_FORMAT"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env file
    for k, v in {"KEYCLOAK_URL": "https://kc", "KEYCLOAK_REALM": "r", "KEYCLOAK_CLIENT_ID": "c",
                 "DELTA_SHARING_URL": "https://srv/ds"}.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


def test_settings(env):
    env.setenv("COMPARE_TABLES", "a.b.c, a.b.d")
    env.setenv("PARTITION_FORMAT", "Epoch_Seconds")
    s = config.Settings.from_env()
    assert s.auth_method == "keycloak"
    assert s.compare_tables == ["a.b.c", "a.b.d"] and s.partition_format == "epoch_seconds"


def test_invalid_method(env):
    env.setenv("AUTH_METHOD", "magic")
    with pytest.raises(ValueError, match="not supported"):
        config.Settings.from_env()


def test_requires_server_url(env):
    env.delenv("DELTA_SHARING_URL")
    with pytest.raises(ValueError, match="DELTA_SHARING_URL"):
        config.create_client()


def test_user_login_logs_in_once_and_refreshes(env, monkeypatch):
    calls = {"browser": 0, "refresh": 0}

    def browser_login(self, open_browser=True):
        calls["browser"] += 1
        return {"access_token": "first", "expires_in": 10, "refresh_token": "r"}   # expires within buffer

    def refresh(self, refresh_token):
        calls["refresh"] += 1
        assert refresh_token == "r"
        return {"access_token": "refreshed", "expires_in": 300, "refresh_token": "r2"}

    monkeypatch.setattr(config.KeycloakClient, "get_full_token", browser_login)
    monkeypatch.setattr("keycloak.KeycloakOpenID.refresh_token", refresh)
    client, login = config.create_client()
    assert isinstance(client, DeltaSharingClient) and isinstance(login, config.KeycloakUserLogin)
    assert client.access_token == "first"
    assert client.access_token == "refreshed" and client.access_token == "refreshed"
    assert calls == {"browser": 1, "refresh": 1}


def test_user_login_falls_back_to_browser_when_session_ended(env, monkeypatch):
    tokens = iter([{"access_token": "a", "expires_in": 1, "refresh_token": "r"},
                   {"access_token": "b", "expires_in": 300}])
    monkeypatch.setattr(config.KeycloakClient, "get_full_token", lambda self, open_browser=True: next(tokens))

    def expired(self, refresh_token):
        raise RuntimeError("Session not active")

    monkeypatch.setattr("keycloak.KeycloakOpenID.refresh_token", expired)
    _, login = config.create_client()
    assert login.get_access_token() == "a"
    assert login.get_access_token() == "b"


def test_service_account(env, monkeypatch):
    env.setenv("AUTH_METHOD", "keycloak_service_account")
    env.setenv("KEYCLOAK_CLIENT_SECRET", "s")
    monkeypatch.setattr("keycloak.KeycloakOpenID.token",
                        lambda self, **kw: {"access_token": "sa", "expires_in": 300})
    client, login = config.create_client()
    assert isinstance(login, config.KeycloakServiceAccountLogin)
    assert client.access_token == "sa" and "access_expires_at" in login.expiry_info()


@pytest.mark.parametrize("values, dtype, expected", [
    ([1779321600, 1779408000], "long", "epoch_seconds"),
    ([1779321600, 1779408000], "integer", "epoch_seconds"),
    ([1779321600000, 1779408000000], "long", "epoch_millis"),
    ([1, 2, 3], "integer", None),
    (["2026-05-21", "2026-05-22"], "string", "string"),
    (["DE", "AT"], "string", None),
    ([None], "date", "date"),
    ([None], "timestamp", "timestamp"),
])
def test_infer_partition_format(values, dtype, expected):
    assert infer_partition_format(pd.Series(values), dtype) == expected


def test_choose_partition_prefers_time_column():
    cols = pd.DataFrame({"name": ["country", "event_ts", "v"], "type": ["string", "long", "double"],
                         "partition_column": [True, True, False]})
    sample = pd.DataFrame({"country": ["DE", "AT"], "event_ts": [1779321600, 1779408000], "v": [1.0, 2.0]})
    part = choose_partition(cols, sample)
    assert (part.column, part.fmt, part.value_type) == ("event_ts", "epoch_seconds", "long")
    out = add_partition_datetime(sample, part)
    assert str(out["partition_time"].iloc[0]) == "2026-05-21 00:00:00+00:00"


def test_choose_partition_overrides_and_non_time():
    cols = pd.DataFrame({"name": ["country"], "type": ["string"], "partition_column": [True]})
    sample = pd.DataFrame({"country": ["DE"]})
    part = choose_partition(cols, sample)
    assert part.column == "country" and not part.is_time and part.value_type == "string"
    cols2 = pd.DataFrame({"name": ["ts"], "type": ["long"], "partition_column": [True]})
    assert choose_partition(cols2, pd.DataFrame({"ts": [1779321600]}), value_type="int").value_type == "int"
    assert choose_partition(cols, sample, column="missing") is None
