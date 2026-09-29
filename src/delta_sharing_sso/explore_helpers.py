"""Schema-agnostic helpers to profile and chart a shared table in a notebook.

They work on any DataFrame loaded with DeltaSharingClient; no column names are
hard-coded. Partition handling is driven by the table metadata and the optional
PARTITION_* settings in .env.

Requires the "notebook" extra: pip install delta-sharing-sso[notebook]
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import plotly.express as px

from .predicates import DEFAULT_VALUE_TYPES, DELTA_TO_VALUE_TYPE, PARTITION_FORMATS

COLORS = ["#1D4E89", "#00A6A6", "#F4A259", "#8CB369", "#BC4B51", "#5B8E7D", "#7B6D8D"]
DATE_HINTS = ("date", "time", "_ts", "_at", "day", "month")


@dataclass
class PartitionInfo:
    column: str
    fmt: str | None          # epoch_seconds | epoch_millis | date | timestamp | string, None = not time-based
    value_type: str          # predicate valueType
    string_format: str = "%Y-%m-%d"

    @property
    def is_time(self) -> bool:
        return self.fmt is not None


def infer_partition_format(values: pd.Series, delta_type: str, string_format: str = "%Y-%m-%d") -> str | None:
    """Guess how a column stores time: epoch seconds/millis, date, timestamp, a date string, or not a time."""
    if delta_type == "date":
        return "date"
    if delta_type.startswith("timestamp"):
        return "timestamp"
    sample = values.dropna().head(500)
    if sample.empty:
        return None
    if delta_type in ("long", "integer", "int", "short") and pd.api.types.is_numeric_dtype(sample):
        lo, hi = sample.min(), sample.max()
        if 9.0e8 <= lo and hi <= 4.2e9:          # 1998 .. 2103 in seconds
            return "epoch_seconds"
        if 9.0e11 <= lo and hi <= 4.2e12:        # same range in milliseconds
            return "epoch_millis"
        return None
    if delta_type == "string":
        parsed = pd.to_datetime(sample.astype(str), format=string_format, errors="coerce")
        return "string" if parsed.notna().mean() > 0.95 else None
    return None


def to_datetime(values: pd.Series, fmt: str, string_format: str = "%Y-%m-%d") -> pd.Series:
    """Convert a time-like column in the given format to UTC datetimes."""
    if fmt == "epoch_seconds":
        return pd.to_datetime(values, unit="s", utc=True)
    if fmt == "epoch_millis":
        return pd.to_datetime(values, unit="ms", utc=True)
    if fmt == "string":
        return pd.to_datetime(values.astype(str), format=string_format, errors="coerce", utc=True)
    return pd.to_datetime(values, errors="coerce", utc=True)


def choose_partition(columns: pd.DataFrame, sample: pd.DataFrame, column: str | None = None,
                     fmt: str = "auto", value_type: str | None = None,
                     string_format: str = "%Y-%m-%d") -> PartitionInfo | None:
    """Pick the partition column to filter on and work out its format.

    `columns` is the output of DeltaSharingClient.get_table_columns(). Without an
    explicit column, the first time-like partition column is used, else the first
    partition column.
    """
    types = dict(zip(columns["name"], columns["type"]))
    candidates = [column] if column else columns.loc[columns["partition_column"], "name"].tolist()
    if not candidates or candidates[0] not in types:
        return None
    infos = []
    for name in candidates:
        if name not in sample.columns:
            continue
        col_fmt = infer_partition_format(sample[name], types[name], string_format) if fmt == "auto" else fmt
        if col_fmt is not None and col_fmt not in PARTITION_FORMATS:
            raise ValueError(f"PARTITION_FORMAT={col_fmt!r} is not one of {PARTITION_FORMATS}")
        vtype = value_type or (DEFAULT_VALUE_TYPES[col_fmt] if col_fmt in ("date", "timestamp")
                               else DELTA_TO_VALUE_TYPE.get(types[name], "string"))
        infos.append(PartitionInfo(name, col_fmt, vtype, string_format))
    time_based = [i for i in infos if i.is_time]
    return (time_based or infos or [None])[0]


def add_partition_datetime(df: pd.DataFrame, partition: PartitionInfo | None,
                           new_column: str = "partition_time") -> pd.DataFrame:
    """Add a readable UTC datetime column for a time-based partition column."""
    if partition is None or not partition.is_time or partition.column not in df.columns:
        return df
    out = df.copy()
    out[new_column] = to_datetime(out[partition.column], partition.fmt, partition.string_format)
    return out


def coerce_datetimes(df: pd.DataFrame, exclude: list[str] | tuple[str, ...] = ()) -> pd.DataFrame:
    """Turn date-like object columns (Delta DATE columns arrive as Python dates) into datetimes.

    Columns in `exclude` (e.g. the partition column) are left as they are.
    """
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if col in exclude or s.dtype != object:
            continue
        sample = s.dropna().head(200)
        if not (any(h in col.lower() for h in DATE_HINTS)
                or (len(sample) and all(isinstance(v, date) for v in sample))):
            continue
        parsed = pd.to_datetime(s, errors="coerce")
        if parsed.notna().sum() >= 0.9 * s.notna().sum():
            out[col] = parsed
    return out


def _nunique(s: pd.Series) -> int:
    try:
        return s.nunique(dropna=True)
    except TypeError:  # unhashable values such as struct/array columns
        return -1


def profile(df: pd.DataFrame) -> pd.DataFrame:
    """One row per column: type, null share, distinct values and an example value."""
    rows = []
    for col in df.columns:
        s = df[col]
        non_null = s.dropna()
        rows.append({
            "column": col,
            "dtype": str(s.dtype),
            "null_pct": round(100 * s.isna().mean(), 1),
            "distinct": _nunique(s),
            "example": non_null.iloc[0] if len(non_null) else None,
        })
    return pd.DataFrame(rows)


def column_roles(df: pd.DataFrame, exclude: list[str] | tuple[str, ...] = (),
                 max_categories: int = 50) -> dict[str, list[str]]:
    """Split columns into datetimes, measures (numeric) and categories (low-cardinality text).

    Identifier-like and epoch-timestamp columns (ending in id/_ts) are not treated as measures.
    """
    exclude = set(exclude)
    datetimes = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    numerics = [c for c in df.columns
                if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
                and c not in exclude and not c.lower().endswith(("_id", "id", "_ts"))]
    id_like = {c for c in df.columns if c.lower().endswith(("_id", "id", "_key"))}
    categories = [c for c in df.columns
                  if c not in datetimes and c not in numerics and c not in exclude and c not in id_like
                  and 1 < _nunique(df[c]) <= max_categories]
    return {"datetimes": datetimes, "measures": numerics, "categories": categories}


def _style(fig):
    fig.update_layout(colorway=COLORS, margin=dict(l=10, r=10, t=50, b=10), legend_title_text="")
    return fig


def rows_over_time(df: pd.DataFrame, date_col: str, freq: str = "D", color: str | None = None,
                   title: str | None = None):
    """Line chart of row counts per period (freq: 'h', 'D', 'W-MON', 'MS')."""
    d = df.dropna(subset=[date_col])
    keys = [pd.Grouper(key=date_col, freq=freq)] + ([color] if color else [])
    counts = d.groupby(keys).size().reset_index(name="rows")
    fig = px.line(counts, x=date_col, y="rows", color=color, markers=True,
                  title=title or f"Rows per period by {date_col}")
    return _style(fig)


def measure_over_time(df: pd.DataFrame, date_col: str, measure: str, agg: str = "sum",
                      freq: str = "D", color: str | None = None):
    """Line chart of an aggregated measure per period."""
    d = df.dropna(subset=[date_col])
    keys = [pd.Grouper(key=date_col, freq=freq)] + ([color] if color else [])
    series = d.groupby(keys)[measure].agg(agg).reset_index()
    fig = px.line(series, x=date_col, y=measure, color=color, markers=True,
                  title=f"{agg.title()} of {measure} per period")
    return _style(fig)


def top_values(df: pd.DataFrame, column: str, measure: str | None = None, agg: str = "sum",
               top_n: int = 15):
    """Horizontal bar chart of the most frequent values (or the largest measure totals)."""
    labels = df[column].astype(str)
    if measure:
        data = df.assign(**{column: labels}).groupby(column)[measure].agg(agg)
        x_label = f"{agg} of {measure}"
    else:
        data = labels.value_counts()
        x_label = "rows"
    data = data.sort_values(ascending=False).head(top_n).rename(x_label).reset_index()
    data.columns = [column, x_label]
    fig = px.bar(data, x=x_label, y=column, orientation="h", title=f"Top {column}")
    fig.update_layout(yaxis={"categoryorder": "total ascending"})
    return _style(fig)


def distribution(df: pd.DataFrame, measure: str, color: str | None = None):
    """Histogram with a box plot on top."""
    use_color = color if color and _nunique(df[color]) <= 8 else None
    fig = px.histogram(df, x=measure, color=use_color, nbins=50, marginal="box",
                       barmode="overlay", opacity=0.75, title=f"Distribution of {measure}")
    return _style(fig)


def null_heatmap(df: pd.DataFrame):
    """Bar chart of missing values per column, useful for a first data-quality check."""
    nulls = (df.isna().mean() * 100).round(1).sort_values(ascending=False)
    nulls = nulls[nulls > 0].rename("missing %").reset_index().rename(columns={"index": "column"})
    if nulls.empty:
        return None
    fig = px.bar(nulls, x="missing %", y="column", orientation="h", title="Missing values per column")
    fig.update_layout(yaxis={"categoryorder": "total ascending"})
    return _style(fig)
