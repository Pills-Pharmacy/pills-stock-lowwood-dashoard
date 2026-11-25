"""Charts for the analysis. Each function takes plain tables and returns an Altair chart.

Colour follows the dataviz method: categorical hues in a fixed order (never cycled), one hue
for magnitude, thin bars with rounded data-ends, a recessive grid, text in ink colours (never
the series colour), and a tooltip on every mark. The palette was run through the validator
(`validate_palette.js`) for both light and dark surfaces: it passes, with a contrast note on
two light hues, which is why every bar here carries a direct value label.

Nothing here reads a file or touches Streamlit, so the charts are testable on their own.
"""

from __future__ import annotations

import altair as alt
import numpy as np
import pandas as pd

# Categorical slots 1-4 (blue, orange, aqua, yellow), fixed order; light and dark steps.
CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"],
}
INK = {
    "light": {"primary": "#0b0b0b", "secondary": "#52514e", "grid": "#e4e3df", "rule": "#52514e"},
    "dark": {"primary": "#f0efec", "secondary": "#c3c2b7", "grid": "#33332f", "rule": "#c3c2b7"},
}
CLASS_ORDER = ["Dead stock", "Overstocked", "Understocked", "Healthy"]
BAR_SIZE = 14
EMPTY = "Nothing to chart."


def _mode(dark: bool) -> str:
    return "dark" if dark else "light"


def _theme(chart: alt.TopLevelMixin, dark: bool, height: int | None = None) -> alt.TopLevelMixin:
    ink = INK[_mode(dark)]
    chart = chart.configure(background="transparent")
    chart = chart.configure_view(stroke=None)
    chart = chart.configure_axis(
        labelColor=ink["secondary"],
        titleColor=ink["secondary"],
        gridColor=ink["grid"],
        domainColor=ink["grid"],
        tickColor=ink["grid"],
        labelFontSize=12,
        titleFontSize=12,
        titleFontWeight="normal",
    )
    chart = chart.configure_legend(
        labelColor=ink["secondary"], titleColor=ink["secondary"], orient="top", title=None
    )
    return chart.properties(height=height) if height else chart


def class_counts(counts: dict[str, int], dark: bool = False) -> alt.TopLevelMixin:
    """One bar per stock class, in a fixed colour order, each labelled with its count."""
    frame = pd.DataFrame(
        {
            "Class": CLASS_ORDER,
            "Items": [
                counts.get(key, 0)
                for key in ("dead_stock", "overstocked", "understocked", "healthy")
            ],
        }
    )
    palette = CATEGORICAL[_mode(dark)]
    ink = INK[_mode(dark)]
    base = alt.Chart(frame).encode(
        y=alt.Y("Class:N", sort=CLASS_ORDER, title=None, axis=alt.Axis(grid=False)),
        x=alt.X("Items:Q", title="Items", axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=["Class", "Items"],
    )
    bars = base.mark_bar(size=BAR_SIZE, cornerRadiusEnd=4).encode(
        color=alt.Color("Class:N", scale=alt.Scale(domain=CLASS_ORDER, range=palette), legend=None)
    )
    labels = base.mark_text(align="left", dx=6, color=ink["primary"], fontSize=12).encode(
        text="Items:Q"
    )
    return _theme(bars + labels, dark, height=170)


def classified_items(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every classified item with a finite days-of-cover figure, with its class."""
    parts = []
    for key, label in (
        ("overstocked", "Overstocked"),
        ("understocked", "Understocked"),
        ("healthy", "Healthy"),
    ):
        table = tables.get(key)
        if table is not None and not table.empty and "Days of cover" in table:
            part = table[["Item", "Days of cover"]].copy()
            part["Class"] = label
            parts.append(part)
    if not parts:
        return pd.DataFrame(columns=["Item", "Days of cover", "Class"])
    frame = pd.concat(parts, ignore_index=True)
    frame["Days of cover"] = pd.to_numeric(frame["Days of cover"], errors="coerce")
    return frame[np.isfinite(frame["Days of cover"])]


def cover_histogram(
    items: pd.DataFrame, under_days: float, over_days: float, dark: bool = False
) -> alt.TopLevelMixin | None:
    """How many items hold how many days of cover, with the two limits drawn as rules.

    Values past three times the upper limit are pooled into the last bin so a few extreme items
    do not flatten the rest.
    """
    if items.empty:
        return None
    ink = INK[_mode(dark)]
    cap = float(over_days) * 3
    frame = items.assign(Cover=items["Days of cover"].clip(upper=cap))
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3, color=CATEGORICAL[_mode(dark)][0])
        .encode(
            x=alt.X(
                "Cover:Q",
                bin=alt.Bin(maxbins=30, extent=[0, cap]),
                axis=alt.Axis(tickCount=8),
                title=f"Days of cover (items above {cap:.0f} pooled in the last bar)",
            ),
            y=alt.Y("count():Q", title="Items", axis=alt.Axis(tickMinStep=1, format="d")),
            tooltip=[alt.Tooltip("count():Q", title="Items")],
        )
    )
    limits = pd.DataFrame(
        {"Days": [under_days, over_days], "Limit": ["Understock limit", "Overstock limit"]}
    )
    rules = (
        alt.Chart(limits)
        .mark_rule(color=ink["rule"], strokeDash=[4, 3], strokeWidth=2)
        .encode(x="Days:Q", tooltip=["Limit", "Days"])
    )
    text = (
        alt.Chart(limits)
        .mark_text(
            align="left", dx=4, dy=-4, color=ink["secondary"], fontSize=11, baseline="bottom"
        )
        .encode(x="Days:Q", y=alt.value(0), text="Limit:N")
    )
    return _theme(bars + rules + text, dark, height=240)


def top_cover(
    table: pd.DataFrame, ascending: bool, n: int = 10, dark: bool = False
) -> alt.TopLevelMixin | None:
    """The n items with the most (or, ascending, the least) days of cover, as bars."""
    if table.empty or "Days of cover" not in table:
        return None
    frame = table[["Item", "Days of cover"]].copy()
    frame["Days of cover"] = pd.to_numeric(frame["Days of cover"], errors="coerce")
    frame = frame.dropna().sort_values("Days of cover", ascending=ascending).head(n)
    return _hbar(
        frame, "Item", "Days of cover", "Days of cover", dark, color_slot=1 if not ascending else 2
    )


def _hbar(
    frame: pd.DataFrame,
    category: str,
    value: str,
    title: str,
    dark: bool,
    color_slot: int = 0,
    fmt: str = ",.0f",
) -> alt.TopLevelMixin:
    ink = INK[_mode(dark)]
    colour = CATEGORICAL[_mode(dark)][color_slot]
    order = list(frame[category])
    base = alt.Chart(frame).encode(
        y=alt.Y(f"{category}:N", sort=order, title=None, axis=alt.Axis(grid=False, labelLimit=260)),
        x=alt.X(f"{value}:Q", title=title),
        tooltip=[category, alt.Tooltip(value, format=fmt)],
    )
    bars = base.mark_bar(size=BAR_SIZE, cornerRadiusEnd=4, color=colour)
    labels = base.mark_text(align="left", dx=5, color=ink["primary"], fontSize=11).encode(
        text=alt.Text(f"{value}:Q", format=fmt)
    )
    return _theme(bars + labels, dark, height=max(60, 26 * len(frame) + 30))


def top_products(
    products: pd.DataFrame, n: int = 15, dark: bool = False
) -> alt.TopLevelMixin | None:
    """The busiest products by scripts in the usage window."""
    if products.empty:
        return None
    frame = products[["Product", "Scripts"]].head(n).copy()
    return _hbar(frame, "Product", "Scripts", "Scripts in the window", dark)


def monthly_scripts(monthly: pd.DataFrame, dark: bool = False) -> alt.TopLevelMixin | None:
    """Scripts per month as thin columns."""
    if monthly.empty:
        return None
    ink = INK[_mode(dark)]
    order = list(monthly["Month"].astype(str))
    frame = monthly.assign(Month=monthly["Month"].astype(str))
    base = alt.Chart(frame).encode(
        x=alt.X("Month:N", sort=order, title=None, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("Scripts:Q", title="Scripts", axis=alt.Axis(format=",d")),
        tooltip=["Month", alt.Tooltip("Scripts", format=","), alt.Tooltip("Products", format=",")],
    )
    bars = base.mark_bar(size=18, cornerRadiusEnd=4, color=CATEGORICAL[_mode(dark)][0])
    labels = (
        base.mark_text(dy=-6, color=ink["secondary"], fontSize=10).encode(
            text=alt.Text("Scripts:Q", format=",d")
        )
        if len(frame) <= 14
        else None
    )
    return _theme(bars + labels if labels is not None else bars, dark, height=260)


def robot_vs_pmr(robot: pd.DataFrame, dark: bool = False) -> alt.TopLevelMixin | None:
    """Robot dispensed per weekday against PMR scripts per weekday, month by month."""
    if robot.empty:
        return None
    long = robot.melt(
        id_vars="Month",
        value_vars=["Robot per weekday", "PMR scripts per weekday"],
        var_name="Series",
        value_name="Per weekday",
    )
    long["Month"] = long["Month"].astype(str)
    order = list(dict.fromkeys(long["Month"]))
    names = ["Robot per weekday", "PMR scripts per weekday"]
    chart = (
        alt.Chart(long)
        .mark_bar(size=10, cornerRadiusEnd=3)
        .encode(
            x=alt.X("Month:N", sort=order, title=None),
            xOffset=alt.XOffset("Series:N", sort=names),
            y=alt.Y("Per weekday:Q", title="Per weekday"),
            color=alt.Color(
                "Series:N",
                scale=alt.Scale(domain=names, range=CATEGORICAL[_mode(dark)][:2]),
            ),
            tooltip=["Month", "Series", alt.Tooltip("Per weekday", format=",.1f")],
        )
    )
    return _theme(chart, dark, height=260)


def activity_lines(daily: pd.DataFrame, dark: bool = False) -> alt.TopLevelMixin | None:
    """Robot dispensed per day, one line per system, with a hover readout."""
    if daily.empty:
        return None
    frame = daily[["activity_date", "system_name", "dispensed"]].copy()
    frame["system_name"] = frame["system_name"].astype(str)
    systems = list(dict.fromkeys(frame["system_name"]))
    palette = CATEGORICAL[_mode(dark)][: len(systems)] or CATEGORICAL[_mode(dark)][:1]
    hover = alt.selection_point(
        fields=["activity_date"], nearest=True, on="pointerover", empty=False
    )
    base = alt.Chart(frame).encode(
        x=alt.X("activity_date:T", title=None),
        y=alt.Y("dispensed:Q", title="Dispensed per day"),
        color=alt.Color("system_name:N", scale=alt.Scale(domain=systems, range=palette)),
    )
    lines = base.mark_line(strokeWidth=2)
    points = (
        base.mark_point(size=60, filled=True, stroke=INK[_mode(dark)]["grid"], strokeWidth=2)
        .encode(
            opacity=alt.condition(hover, alt.value(1), alt.value(0)),
            tooltip=[
                alt.Tooltip("activity_date:T", title="Date"),
                alt.Tooltip("system_name:N", title="System"),
                alt.Tooltip("dispensed:Q", title="Dispensed", format=","),
            ],
        )
        .add_params(hover)
    )
    return _theme(lines + points, dark, height=260)


GROUPS = ["A", "B", "C"]
GROUP_NAMES = {"A": "A: fast movers", "B": "B: medium movers", "C": "C: slow movers"}


def group_share(abc: pd.DataFrame, dark: bool = False) -> alt.TopLevelMixin | None:
    """Share of all scripts held by each sales group (A, B, C), labelled with product counts."""
    if abc.empty:
        return None
    frame = (
        abc.groupby("group")
        .agg(share=("share", "sum"), products=("product", "size"))
        .reindex(GROUPS)
        .fillna(0)
        .reset_index()
    )
    frame["Group"] = [
        f"{GROUP_NAMES[g]} ({int(n):,} products)" for g, n in zip(frame["group"], frame["products"])
    ]
    frame["Label"] = frame["share"].map("{:.0%}".format)
    ink = INK[_mode(dark)]
    order = list(frame["Group"])
    base = alt.Chart(frame).encode(
        y=alt.Y("Group:N", sort=order, title=None, axis=alt.Axis(grid=False)),
        x=alt.X(
            "share:Q",
            title="Share of scripts",
            axis=alt.Axis(format="%"),
            scale=alt.Scale(domain=[0, 1]),
        ),
        tooltip=["Group", alt.Tooltip("share:Q", format=".1%", title="Share"), "products"],
    )
    bars = base.mark_bar(size=BAR_SIZE, cornerRadiusEnd=4).encode(
        color=alt.Color(
            "Group:N",
            scale=alt.Scale(domain=order, range=CATEGORICAL[_mode(dark)][:3]),
            legend=None,
        )
    )
    labels = base.mark_text(align="left", dx=6, color=ink["primary"], fontSize=12).encode(
        text="Label:N"
    )
    return _theme(bars + labels, dark, height=150)


def pareto_curve(
    abc: pd.DataFrame, a_share: float, b_share: float, dark: bool = False
) -> alt.TopLevelMixin | None:
    """Cumulative share of scripts against product rank: how few products make most of the sales."""
    if abc.empty:
        return None
    ink = INK[_mode(dark)]
    frame = abc[["rank", "product", "cumulative_share"]].copy()
    line = (
        alt.Chart(frame)
        .mark_line(strokeWidth=2, color=CATEGORICAL[_mode(dark)][0])
        .encode(
            x=alt.X("rank:Q", title="Products, fastest selling first"),
            y=alt.Y(
                "cumulative_share:Q",
                title="Cumulative share of scripts",
                axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, 1]),
            ),
            tooltip=[
                alt.Tooltip("rank:Q", title="Rank"),
                "product",
                alt.Tooltip("cumulative_share:Q", format=".1%", title="Cumulative share"),
            ],
        )
    )
    limits = pd.DataFrame(
        {
            "Share": [a_share, b_share],
            "Label": [f"A ends ({a_share:.0%})", f"B ends ({b_share:.0%})"],
        }
    )
    rules = (
        alt.Chart(limits)
        .mark_rule(color=ink["rule"], strokeDash=[4, 3], strokeWidth=2)
        .encode(y="Share:Q", tooltip=["Label"])
    )
    text = (
        alt.Chart(limits)
        .mark_text(align="left", dx=4, dy=-5, color=ink["secondary"], fontSize=11)
        .encode(y="Share:Q", x=alt.value(0), text="Label:N")
    )
    return _theme(line + rules + text, dark, height=240)


def add_list(add: pd.DataFrame, n: int = 15, dark: bool = False) -> alt.TopLevelMixin | None:
    """The fastest-selling products missing from the robot, as bars."""
    if add.empty:
        return None
    frame = add[["Product", "Scripts"]].head(n).copy()
    return _hbar(frame, "Product", "Scripts", "Scripts in the window", dark, color_slot=1)
