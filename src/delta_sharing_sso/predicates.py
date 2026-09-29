"""Helpers to build Delta Sharing JSON predicate hints without writing the JSON by hand.

See: https://github.com/delta-io/delta-sharing/blob/main/PROTOCOL.md#json-predicates-for-filtering

Examples:
    from delta_sharing_sso.predicates import equal, between, time_range

    equal("country", "DE")
    between("amount", 100, 500, value_type="double")
    # Date range on a partition column, whatever its format:
    time_range("event_date", "2026-05-01", "2026-05-31", fmt="date")
    time_range("interval_start_ts", "2026-05-01", "2026-05-31", fmt="epoch_seconds", value_type="long")
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

# How a partition column stores time values, and the predicate value type that fits.
PARTITION_FORMATS = ("epoch_seconds", "epoch_millis", "date", "timestamp", "string")
DEFAULT_VALUE_TYPES = {"epoch_seconds": "long", "epoch_millis": "long", "date": "date",
                       "timestamp": "timestamp", "string": "string"}

# Delta column type -> predicate valueType
DELTA_TO_VALUE_TYPE = {"byte": "int", "short": "int", "integer": "int", "long": "long",
                       "float": "float", "double": "double", "boolean": "boolean",
                       "date": "date", "timestamp": "timestamp", "timestamp_ntz": "timestamp",
                       "string": "string"}


def _leaf(op: str, column: str, value: Any, value_type: str) -> dict[str, Any]:
    return {
        "op": op,
        "children": [
            {"op": "column", "name": column, "valueType": value_type},
            {"op": "literal", "value": value, "valueType": value_type},
        ],
    }


def equal(column: str, value: Any, value_type: str = "string") -> dict[str, Any]:
    return _leaf("equal", column, value, value_type)


def greater_or_equal(column: str, value: Any, value_type: str = "string") -> dict[str, Any]:
    return _leaf("greaterThanOrEqual", column, value, value_type)


def less_or_equal(column: str, value: Any, value_type: str = "string") -> dict[str, Any]:
    return _leaf("lessThanOrEqual", column, value, value_type)


def and_(*children: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "children": list(children)}


def or_(*children: dict[str, Any]) -> dict[str, Any]:
    return {"op": "or", "children": list(children)}


def between(column: str, low: Any, high: Any, value_type: str = "string") -> dict[str, Any]:
    """Inclusive range filter: low <= column <= high."""
    return and_(greater_or_equal(column, low, value_type), less_or_equal(column, high, value_type))


def _utc(value: str | date | datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize(timezone.utc) if ts.tzinfo is None else ts.tz_convert(timezone.utc)


def to_epoch_seconds(value: str | date | datetime) -> int:
    """Convert '2026-05-21', a date or a datetime (naive = UTC) to epoch seconds."""
    return int(_utc(value).timestamp())


def to_partition_value(value: str | date | datetime, fmt: str, string_format: str = "%Y-%m-%d") -> Any:
    """Convert a date/time to the representation a partition column uses."""
    if fmt == "epoch_seconds":
        return to_epoch_seconds(value)
    if fmt == "epoch_millis":
        return to_epoch_seconds(value) * 1000
    if fmt == "date":
        return _utc(value).strftime("%Y-%m-%d")
    if fmt == "timestamp":
        return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    if fmt == "string":
        return _utc(value).strftime(string_format)
    raise ValueError(f"Unknown partition format {fmt!r}; use one of {PARTITION_FORMATS}")


def time_range(column: str, start: str | date | datetime, end: str | date | datetime,
               fmt: str = "date", value_type: str | None = None,
               string_format: str = "%Y-%m-%d") -> dict[str, Any]:
    """Inclusive date/time range on a column stored in the given format."""
    value_type = value_type or DEFAULT_VALUE_TYPES[fmt]
    return between(column, to_partition_value(start, fmt, string_format),
                   to_partition_value(end, fmt, string_format), value_type)


def time_equal(column: str, value: str | date | datetime, fmt: str = "date",
               value_type: str | None = None, string_format: str = "%Y-%m-%d") -> dict[str, Any]:
    """Select one partition by date/time, e.g. time_equal("event_date", "2026-05-21")."""
    value_type = value_type or DEFAULT_VALUE_TYPES[fmt]
    return equal(column, to_partition_value(value, fmt, string_format), value_type)
