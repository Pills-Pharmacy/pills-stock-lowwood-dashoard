"""Streamlit page: one tab per pharmacy, each with its own uploads, results and decisions.

Each pharmacy (Lowood, McGregor) uploads its own robot, PMR and activity files, because the
files differ between pharmacies. The pharmacy's name is the branch, so no file needs a branch
column. Criteria and options in the sidebar apply to every pharmacy.

All logic lives in pipeline.py and the stage modules. Uploaded files are processed in memory
and never written to disk; the only file the app can write is a pharmacy's confirmed-match
lookup, and only when someone presses a save button on its Review tab.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import charts
import checks
import config
import eligible
import export
import load
import match
import pipeline
from config import (
    CLASSIFICATION_LEVELS,
    DEFAULT_SETTINGS,
    PHARMACIES,
    STOCK_QTY_IS_PACK_COUNT,
    Pharmacy,
    Settings,
)
from schema import ACTIVITY_SOURCE, OPTIONAL, REQUIRED

AUTOMATIC = "(automatic)"
LEVEL_LABELS = {"combined_per_branch": "Combined per branch", "per_robot": "Per robot"}
CLASS_TABS = {
    "dead_stock": ("Dead stock", "In the robot with no usage in the period."),
    "overstocked": ("Overstock", "Days of cover above the upper limit."),
    "understocked": ("Understock", "Days of cover below the lower limit."),
    "healthy": ("Healthy", "Within both limits."),
}
COLUMN_CONFIG = {
    "Days of cover": st.column_config.NumberColumn(format="%.1f"),
    "Usage per day": st.column_config.NumberColumn(format="%.2f"),
    "Earliest expiry": st.column_config.DateColumn(format="YYYY-MM-DD"),
}


def slot_label(pharmacy: Pharmacy, source: str) -> str:
    if source == pipeline.PMR:
        return f"{pharmacy.label} PMR usage export"
    if source == ACTIVITY_SOURCE:
        return f"{pharmacy.label} robot activity statistics"
    if len(pharmacy.robot_sources) > 1:
        return f"{pharmacy.label} robot {pharmacy.robot_sources.index(source) + 1} stock export"
    return f"{pharmacy.label} robot stock export"


# ---- sidebar: shared by every pharmacy ------------------------------------------------------------


def sidebar_criteria() -> Settings:
    st.sidebar.header("Criteria")
    st.sidebar.caption(
        "Placeholders until the pharmacy confirms them. Change freely. They apply to every "
        "pharmacy tab."
    )
    defaults = DEFAULT_SETTINGS.values()
    settings = DEFAULT_SETTINGS
    fields = {
        "period_days": "Usage window (days)",
        "overstock_days": "Overstocked above (days of cover)",
        "understock_days": "Understocked below (days of cover)",
    }
    for name, label in fields.items():
        value = st.sidebar.number_input(
            label, min_value=1, max_value=3650, value=int(defaults[name]), step=1, key=f"set_{name}"
        )
        settings = settings.with_value(name, int(value))
        st.sidebar.caption(getattr(settings, name).label)
    level = st.sidebar.selectbox(
        "Classify",
        CLASSIFICATION_LEVELS,
        index=CLASSIFICATION_LEVELS.index(defaults["classification_level"]),
        format_func=LEVEL_LABELS.get,
        key="set_classification_level",
    )
    settings = settings.with_value("classification_level", level)
    st.sidebar.caption(settings.classification_level.label)
    return settings


def sidebar_options() -> tuple[bool, bool, bool]:
    """(match on product code, stock count is packs, count the latest month)."""
    with st.sidebar.expander("Matching and stock options"):
        use_code = st.checkbox(
            "Match on product code",
            value=False,
            help="Only switch on once both systems are confirmed to share a product code.",
            key="opt_code_match",
        )
        packs = st.checkbox(
            "Stock count is a number of packs",
            value=STOCK_QTY_IS_PACK_COUNT,
            help="Multiplies each stock count by its pack size so stock is in units. Leave off "
            "until someone confirms what the robot's count means.",
            key="opt_stock_packs",
        )
        latest = st.checkbox(
            "Count the latest month when the PMR has only months",
            value=False,
            help="A report run part-way through a month has an incomplete latest month, so it is "
            "left out by default.",
            key="opt_latest_month",
        )
    return use_code, packs, latest


# ---- one pharmacy tab -------------------------------------------------------------------------------


@st.cache_data(show_spinner="Reading the Excel file...", max_entries=8)
def _xlsx_as_csv(content: bytes) -> bytes:
    return load.xlsx_to_csv_bytes(content)


def uploaded_bytes(uploaded) -> bytes:
    """The upload as CSV bytes. An Excel workbook is converted; a bad one is reported and skipped."""
    content = uploaded.getvalue()
    if uploaded.name.lower().endswith(".xlsx"):
        try:
            return _xlsx_as_csv(content)
        except ValueError as exc:
            st.error(f"{uploaded.name}: {exc}")
            return b""
    return content


UPLOAD_VIEW = "1 · Upload and check"
ANALYSIS_VIEW = "2 · Analysis"


def stored_files(pharmacy: Pharmacy) -> dict[str, tuple[str, bytes]]:
    """{source: (file name, CSV bytes)} uploaded for this pharmacy. Kept in the session so the
    uploads survive switching between the Upload and Analysis views (widgets do not)."""
    return st.session_state.setdefault("files", {}).setdefault(pharmacy.key, {})


def stored_pulls(pharmacy: Pharmacy) -> list[tuple[str, bytes]]:
    return st.session_state.setdefault("pulls", {}).setdefault(pharmacy.key, [])


def _forget(pharmacy: Pharmacy, source: str) -> None:
    stored_files(pharmacy).pop(source, None)


def _forget_pull(pharmacy: Pharmacy, index: int) -> None:
    pulls = stored_pulls(pharmacy)
    if index < len(pulls):
        del pulls[index]


def pharmacy_uploads(pharmacy: Pharmacy) -> None:
    """Upload boxes. A stored file shows as a line with a Remove button instead of a box."""
    files, pulls = stored_files(pharmacy), stored_pulls(pharmacy)
    with st.expander(f"Upload {pharmacy.label} files", expanded=True):
        for source in (*pharmacy.robot_sources, pipeline.PMR):
            label = slot_label(pharmacy, source)
            if source in files:
                left, right = st.columns([5, 1])
                left.markdown(f"**{label}**: {files[source][0]}")
                right.button(
                    "Remove",
                    key=f"{pharmacy.key}_rm_{source}",
                    on_click=_forget,
                    args=(pharmacy, source),
                )
                continue
            uploaded = st.file_uploader(
                label, type=["csv", "xlsx"], key=f"{pharmacy.key}_up_{source}"
            )
            if uploaded is not None and (content := uploaded_bytes(uploaded)):
                files[source] = (uploaded.name, content)
                st.rerun()
        for index, (name, _) in enumerate(pulls):
            left, right = st.columns([5, 1])
            left.markdown(f"**{slot_label(pharmacy, ACTIVITY_SOURCE)}**: {name}")
            right.button(
                "Remove",
                key=f"{pharmacy.key}_rm_pull_{index}",
                on_click=_forget_pull,
                args=(pharmacy, index),
            )
        more = st.file_uploader(
            f"{slot_label(pharmacy, ACTIVITY_SOURCE)} (optional, several files allowed)",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key=f"{pharmacy.key}_up_activity_{len(pulls)}",
        )
        if more is not None and not isinstance(more, list):
            more = [more]
        known = {content for _, content in pulls}
        added = []
        for m in more or []:
            content = uploaded_bytes(m)
            if content and content not in known:  # the same file twice is kept once
                known.add(content)
                added.append((m.name, content))
        if added:
            pulls.extend(added)
            st.rerun()
        st.caption(
            "Files are processed in memory and not stored. Patient columns are dropped on upload."
        )


def map_key(pharmacy: Pharmacy, source: str, internal: str) -> str:
    return f"{pharmacy.key}_map_{source}_{internal}"


def chosen_overrides(pharmacy: Pharmacy, sources: list[str]) -> dict[str, dict[str, str]]:
    """The headers the user picked, read from the widget state at the start of the run (so the
    check above the mapping widgets is never one step behind). Kept for the Analysis view, whose
    run no longer draws the widgets."""
    saved = st.session_state.setdefault("overrides", {}).setdefault(pharmacy.key, {})
    overrides: dict[str, dict[str, str]] = {}
    for source in sources:
        for internal in (*REQUIRED[source], *OPTIONAL[source]):
            key = map_key(pharmacy, source, internal)
            value = st.session_state.get(key, saved.get(source, {}).get(internal, AUTOMATIC))
            if value and value != AUTOMATIC:
                overrides.setdefault(source, {})[internal] = value
    saved.clear()
    saved.update(overrides)
    return overrides


def _use_header(key: str, header: str) -> None:
    st.session_state[key] = header


def render_check(pharmacy: Pharmacy, source: str, check: checks.UploadCheck) -> None:
    """The visible checklist for one upload: ready or not, found and missing columns, hints."""
    label = slot_label(pharmacy, source)
    with st.container(border=True):
        if not check.ready:
            st.error(f"**{label}**: cannot be analysed yet")
        elif check.missing_optional:
            st.warning(f"**{label}**: ready, but some useful columns are missing")
        else:
            st.success(f"**{label}**: ready")
        for message in check.blockers:
            st.error(message)
        if check.readable and check.columns:
            rows = [
                {
                    "": "✅" if c.found else ("❌" if c.required else "⚠️"),
                    "Column": c.internal,
                    "Found as": c.found_as or "not found",
                    "What it is for": c.guide,
                    "Needed": "required" if c.required else "optional",
                }
                for c in check.columns
            ]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        for column in [c for c in check.columns if not c.found and c.suggestions]:
            st.markdown(
                f"We couldn't find **{column.internal}** ({column.guide}). "
                "Did you mean one of these?"
            )
            for index, header in enumerate(column.suggestions):
                st.button(
                    f"Use '{header}' as {column.internal}",
                    key=f"{pharmacy.key}_use_{source}_{column.internal}_{index}",
                    on_click=_use_header,
                    args=(map_key(pharmacy, source, column.internal), header),
                )
        notes = []
        if check.rows_used:
            notes.append(f"{check.rows_used:,} usable row(s) of {check.rows_read:,} in the file")
        for why, count in check.removed.items():
            notes.append(f"{count:,} row(s) left out ({why})")
        if check.dropped_patient:
            notes.append(f"{len(check.dropped_patient)} patient column(s) dropped before analysis")
        for note in notes:
            st.caption(note)
        for warning in check.warnings:
            st.warning(warning)
        if check.ignored:
            with st.expander(f"Columns not used ({len(check.ignored)})"):
                st.write(", ".join(check.ignored))


def upload_checks(pharmacy: Pharmacy, overrides: dict) -> bool:
    """Show the check for every stored upload. True when none of them blocks the analysis."""
    ready = True
    for source, (_, content) in stored_files(pharmacy).items():
        check = checks.check_upload(source, content, overrides=overrides, pharmacy=pharmacy.key)
        render_check(pharmacy, source, check)
        ready &= check.ready
    pulls = stored_pulls(pharmacy)
    if pulls:
        check = checks.check_upload(
            ACTIVITY_SOURCE, pulls[0][1], overrides=overrides, pharmacy=pharmacy.key
        )
        render_check(pharmacy, ACTIVITY_SOURCE, check)
    return ready


def mapping_screen(pharmacy: Pharmacy, files: dict[str, bytes]) -> None:
    """Optional: pick which column of an upload feeds each internal column."""
    if not files:
        return
    column_map = config.load_column_map(pharmacy=pharmacy.key)
    saved = st.session_state.get("overrides", {}).get(pharmacy.key, {})
    with st.expander("Map your columns (optional)"):
        st.caption(
            "Leave on automatic unless a file's columns were not recognised. To make a mapping "
            f"permanent, a developer adds it under `pharmacies: {pharmacy.key}` in "
            "`column_map.yaml`."
        )
        for source, content in files.items():
            headers = pipeline.file_headers(content)
            status = pipeline.mapping_status(source, content, column_map)
            st.markdown(f"**{slot_label(pharmacy, source)}**")
            for internal in (*REQUIRED[source], *OPTIONAL[source]):
                required = internal in REQUIRED[source]
                key = map_key(pharmacy, source, internal)
                if key not in st.session_state and saved.get(source, {}).get(internal) in headers:
                    st.session_state[key] = saved[source][internal]  # restore after a view switch
                st.selectbox(
                    f"{internal}{' *' if required else ''}",
                    [AUTOMATIC, *headers],
                    help=f"Automatic: {status[internal] or 'not found'}",
                    key=key,
                )


def upload_page(pharmacy: Pharmacy) -> None:
    """The Upload view of one pharmacy: files, the check of each file, optional column mapping."""
    pharmacy_uploads(pharmacy)
    files = stored_files(pharmacy)
    pulls = stored_pulls(pharmacy)
    mappable = {s: c for s, (_, c) in files.items()}
    if pulls:
        mappable[ACTIVITY_SOURCE] = pulls[0][1]
    overrides = chosen_overrides(pharmacy, list(mappable))
    if not mappable:
        welcome(pharmacy)
        return
    st.subheader("Upload check")
    ready = upload_checks(pharmacy, overrides)
    mapping_screen(pharmacy, mappable)
    st.button(
        "Go to the analysis",
        key=f"{pharmacy.key}_go",
        type="primary",
        disabled=not (ready and (files or pulls)),
        on_click=_use_header,
        args=("view", ANALYSIS_VIEW),
        help="Fix the problems above first." if not ready else None,
    )


@st.cache_data(show_spinner="Analysing...", max_entries=4)
def run_cached(
    files: tuple[tuple[str, bytes], ...],
    settings: Settings,
    overrides: dict,
    use_code_match: bool,
    confirmed: pd.DataFrame,
    stock_is_packs: bool,
    activity_files: tuple[bytes, ...],
    pharmacy: str,
    include_latest_month: bool,
    catalogue_stamp: float = 0.0,  # file's modified time: refreshes the cache when it changes
):
    """Run the pipeline and pre-build both downloads. Held in memory only, never on disk."""
    result = pipeline.run_pipeline(
        dict(files),
        settings,
        overrides,
        use_code_match,
        confirmed,
        stock_is_packs=stock_is_packs,
        activity_files=list(activity_files),
        pharmacy=pharmacy,
        include_latest_month=include_latest_month,
        catalogue=read_catalogue(),
    )
    excel = export.to_excel_bytes(result.report) if result.report is not None else b""
    html = export.to_html(result.report) if result.report is not None else ""
    return result, excel, html


def read_catalogue():
    """The robot catalogue, or None when the file is missing or unreadable (no tags then)."""
    try:
        return eligible.load_catalogue(config.CATALOGUE_PATH)
    except (OSError, ValueError, pd.errors.ParserError):
        return None


def catalogue_stamp() -> float:
    try:
        return config.CATALOGUE_PATH.stat().st_mtime
    except OSError:
        return 0.0


def load_confirmed(pharmacy: Pharmacy) -> pd.DataFrame:
    try:
        return match.read_confirmed(config.confirmed_matches_path(pharmacy.key))
    except ValueError as problem:
        st.warning(f"Confirmed-match file ignored: {problem}")
        return match.empty_confirmed()


def is_dark() -> bool:
    """True when the app is showing its dark theme (charts pick their colours from it)."""
    try:
        return st.context.theme.type == "dark"
    except (AttributeError, RuntimeError):  # older Streamlit, or no browser (tests)
        return False


def show_chart(chart, caption: str = "") -> None:
    if chart is None:
        return
    st.altair_chart(chart, width="stretch", theme=None)
    if caption:
        st.caption(caption)


FILTER_MIN_ROWS = 6  # smaller tables are read at a glance
FILTER_PICK_LIMIT = 20  # up to this many distinct values a column gets a pick-list


def filter_frame(table: pd.DataFrame, choices: dict) -> pd.DataFrame:
    """Rows that meet every choice: {column: ("in", values) | ("range", (low, high)) |
    ("contains", text)}. Rows with no value in a range-filtered column are left out."""
    keep = pd.Series(True, index=table.index)
    for column, (kind, value) in choices.items():
        series = table[column]
        if kind == "in":
            keep &= series.astype(str).isin(value)
        elif kind == "range":
            keep &= series.between(value[0], value[1])
        else:
            keep &= series.astype(str).str.contains(value, case=False, regex=False, na=False)
    return table[keep]


def column_filters(table: pd.DataFrame, key: str) -> dict:
    """One control per column; returns only the choices that narrow the table."""
    choices: dict = {}
    boxes = st.columns(3)
    for i, column in enumerate(table.columns):
        series, box, wkey = table[column], boxes[i % 3], f"{key}_{column}"
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            if series.notna().any() and series.min() < series.max():
                convert = int if pd.api.types.is_integer_dtype(series) else float
                full = (convert(series.min()), convert(series.max()))
                picked = box.slider(column, *full, value=full, key=wkey)
                if tuple(picked) != full:
                    choices[column] = ("range", tuple(picked))
            continue
        values = sorted(series.dropna().astype(str).unique())
        if 1 < len(values) <= FILTER_PICK_LIMIT:
            picked = box.multiselect(column, values, key=wkey)
            if picked:
                choices[column] = ("in", picked)
        elif len(values) > FILTER_PICK_LIMIT:
            text = box.text_input(f"{column} contains", key=wkey)
            if text:
                choices[column] = ("contains", text)
    return choices


def next_widget_key(prefix: str) -> str:
    """A key for the next table-level widget. Tables are drawn in the same order every run, so
    the position keeps the widgets of different tables (and pharmacy tabs) apart."""
    seq = st.session_state["_table_seq"] = st.session_state.get("_table_seq", 0) + 1
    return f"{prefix}{seq}"


def print_tab(result) -> None:
    """One page to print any of the report's tables, whichever tab they appear on."""
    report = result.report
    held = [
        (key, title, note)
        for key, title, note in export.present_tables(report)
        if not report.tables[key].empty
    ]
    titles = {key: title for key, title, _ in held}
    st.caption(
        "Pick the tables to print, then press Print in the preview and choose Save as PDF or a "
        "printer. Tables are printed in full, not as filtered on screen."
    )
    chosen = st.multiselect(
        "Tables to print",
        list(titles),
        format_func=lambda k: f"{titles[k]} ({len(report.tables[k]):,})",
        key=next_widget_key("print"),
    )
    if not chosen:
        st.info("No table picked yet.")
        return
    sections = [(title, note, report.tables[key]) for key, title, note in held if key in chosen]
    rows = sum(len(table) for _, _, table in sections)
    if rows > 500:
        st.warning(f"{rows:,} rows selected: that is a long printout (about {rows // 45} pages).")
    note = f"{report.pharmacy}, printed {date.today():%d %b %Y}."
    if result.suggestions and "catalogue_products" in result.suggestions.meta:
        note += " Suggestions only cover products in the robot catalogue."
    page = export.printable_html("Robot stock audit", note, sections)
    components.html(page, height=520, scrolling=True)


def show_table(table: pd.DataFrame, empty: str = "None.") -> None:
    if table.empty:
        st.caption(empty)
        return
    if len(table) >= FILTER_MIN_ROWS:
        # Tables are drawn in the same order every run, so the position keeps their filters apart.
        with st.expander("Filter this table"):
            choices = column_filters(table, next_widget_key("tbl"))
        if choices:
            table = filter_frame(table, choices)
            st.caption(f"Showing {len(table):,} rows after filtering.")
    column_config = {k: v for k, v in COLUMN_CONFIG.items() if k in table.columns}
    st.dataframe(table, hide_index=True, width="stretch", column_config=column_config)


def show_issues(report: export.Report) -> None:
    table = report.validation
    for row in table[table["Severity"] == "error"].to_dict("records"):
        st.error(f"{row['Message']} ({row['Source'] or 'all files'})")
    for row in table[table["Severity"] == "warning"].to_dict("records"):
        st.warning(f"{row['Message']} ({row['Source'] or 'all files'})")
    info = table[table["Severity"] == "info"]
    if not info.empty:
        with st.expander("Details"):
            st.dataframe(info, hide_index=True, width="stretch")


def summary_tab(pharmacy: Pharmacy, result, excel: bytes, html: str) -> None:
    report = result.report
    rate, matched, total = pipeline.branch_match_rate(result.matching, None)
    st.metric("Match rate", f"{rate:.1%}")
    st.caption(
        f"{matched} of {total} robot items matched to the PMR. Check this before relying on "
        "anything below."
    )
    if rate < 1:
        st.progress(rate)

    counts = report.counts
    tiles = [
        ("dead_stock", "Dead stock"),
        ("overstocked", "Overstocked"),
        ("understocked", "Understocked"),
        ("healthy", "Healthy"),
    ]
    for column, (key, title) in zip(st.columns(4), tiles):
        column.metric(title, counts[key])
    more = st.columns(3)
    more[0].metric("Review list", counts["review"])
    more[1].metric("Unclassified", counts["unclassified"])
    more[2].metric("Usage with no stock", counts["usage_no_stock"])

    st.subheader("Stock at a glance")
    dark = is_dark()
    show_chart(charts.class_counts(counts, dark))
    over = st.session_state.get("set_overstock_days", DEFAULT_SETTINGS.values()["overstock_days"])
    under = st.session_state.get(
        "set_understock_days", DEFAULT_SETTINGS.values()["understock_days"]
    )
    items = charts.classified_items(report.tables)
    show_chart(
        charts.cover_histogram(items, float(under), float(over), dark),
        "How many items hold how many days of cover. Items left of the first line risk running "
        "out; items right of the second line hold more than needed.",
    )

    if result.matching.usage["qty_basis"].eq("line_count").any():
        st.warning(
            "The PMR file has no quantity column, so each row counts as one dispensing line. "
            "Usage is a rough proxy, not units."
        )
    st.subheader("Validation messages")
    show_issues(report)
    st.subheader("Criteria in use")
    show_table(report.settings.astype(str), "")  # mixed number/text values cannot be tabled
    st.subheader("Download")
    left, right = st.columns(2)
    left.download_button(
        "Excel report",
        excel,
        file_name=f"{pharmacy.key}_robot_stock_audit.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{pharmacy.key}_download_xlsx",
    )
    right.download_button(
        "HTML report (opens without the app)",
        html,
        file_name=f"{pharmacy.key}_robot_stock_audit.html",
        mime="text/html",
        key=f"{pharmacy.key}_download_html",
    )


def class_tab(tables: dict[str, pd.DataFrame], key: str) -> None:
    title, note = CLASS_TABS[key]
    table = tables[key]
    st.caption(f"{note} {len(table)} item(s).")
    if key == "overstocked":
        show_chart(charts.top_cover(table, ascending=False, dark=is_dark()), "Most days of cover.")
    elif key == "understocked":
        show_chart(charts.top_cover(table, ascending=True, dark=is_dark()), "Fewest days of cover.")
    show_table(table, f"No {title.lower()} items.")


def confirm_form(pharmacy: Pharmacy, result) -> None:
    pending = result.matching.matches[result.matching.matches["match_status"] == match.REVIEW]
    if pending.empty:
        return
    path = config.confirmed_matches_path(pharmacy.key)
    st.subheader("Confirm a decision")
    st.caption("Saved decisions are remembered, so the same item is not asked about again.")
    labels = {i: f"{r.robot_id} · {r.item_name}" for i, r in pending.iterrows()}
    index = st.selectbox(
        "Item to decide", list(labels), format_func=labels.get, key=f"{pharmacy.key}_confirm_item"
    )
    row = pending.loc[index]
    usage = result.matching.usage
    products = usage[usage["pmr_key"] != ""].drop_duplicates("pmr_key").set_index("pmr_key")
    keys = list(products.index)
    if row["pmr_key"] in keys:  # put the closest candidate first
        keys.remove(row["pmr_key"])
        keys.insert(0, row["pmr_key"])
    choice = st.selectbox(
        "Same product in the PMR",
        ["(not in the PMR)", *keys],
        format_func=lambda k: k if k.startswith("(") else products.loc[k, "item_name"],
        key=f"{pharmacy.key}_confirm_choice",
    )
    if st.button(f"Save decision (writes {path.name})", key=f"{pharmacy.key}_confirm_save"):
        decision = "no_match" if choice.startswith("(") else "match"
        table = match.confirm(
            load_confirmed(pharmacy),
            row["stock_key"],
            "" if decision == "no_match" else choice,
            decision,
            confirmed_on=date.today().isoformat(),
        )
        match.write_confirmed(table, path)
        st.success("Saved. The audit has been re-run with this decision.")
        st.rerun()


def bulk_form(pharmacy: Pharmacy, result) -> None:
    """Mark every stock item that has no PMR candidate at all as 'not in the PMR'."""
    candidates = match.bulk_not_in_pmr_candidates(result.matching)
    if candidates.empty:
        return
    path = config.confirmed_matches_path(pharmacy.key)
    st.subheader("Bulk decision")
    st.caption(
        f"{len(candidates)} item(s) have no similar product in the PMR. Marking them as "
        "'not in the PMR' makes them dead stock. Items with a close PMR candidate are not "
        "included: confirm those one by one above."
    )
    with st.expander("Show these items"):
        show_table(candidates.drop(columns=["stock_key"]))
    blocked = pipeline.bulk_blocked_reason(result.analysis)
    if blocked:
        st.warning(blocked)
    if st.button(
        f"Mark {len(candidates)} item(s) as not in the PMR (writes {path.name})",
        disabled=blocked is not None,
        key=f"{pharmacy.key}_bulk_save",
    ):
        table = match.confirm_all_not_in_pmr(
            load_confirmed(pharmacy), candidates, confirmed_on=date.today().isoformat()
        )
        match.write_confirmed(table, path)
        st.success(f"Saved {len(candidates)} decision(s). The audit has been re-run.")
        st.rerun()


def review_tab(pharmacy: Pharmacy, result, tables: dict[str, pd.DataFrame]) -> None:
    st.caption("Items a person must check before they can be relied on.")
    show_table(tables["review"], "Nothing to review.")
    confirm_form(pharmacy, result)
    bulk_form(pharmacy, result)
    with st.expander(f"Stock that could not be classified ({len(tables['unclassified'])})"):
        show_table(tables["unclassified"])
    with st.expander(f"Dispensed but not in the robot ({len(tables['usage_no_stock'])})"):
        show_table(tables["usage_no_stock"])


def activity_tab(result) -> None:
    """Robot activity statistics: daily counts for the whole robot, no item detail."""
    activity = result.activity
    for message in result.activity_errors:
        st.error(message)
    for issue in result.issues:  # shown even when nothing loaded, so the reason is visible
        if issue.source == ACTIVITY_SOURCE and issue.severity != "info":
            (st.error if issue.severity == "error" else st.warning)(issue.message)
    if activity is None:
        return
    st.caption(
        "Daily counts for the whole robot. They say how busy it is, not which medicines are "
        "unused, so they do not feed the stock classes. Repeated pulls are merged; for a day "
        "held by several, the most recent pull is kept."
    )
    for message in activity.warnings:
        st.warning(message)
    tables = export.activity_tables(activity, result.comparison)
    summary = activity.summary
    if not summary.empty:
        total = summary[["total_dispensed", "total_stocked", "total_rearranged"]].sum()
        columns = st.columns(4)
        columns[0].metric("Dispensed", f"{total['total_dispensed']:,.0f}")
        columns[1].metric("Stocked", f"{total['total_stocked']:,.0f}")
        columns[2].metric("Rearranged", f"{total['total_rearranged']:,.0f}")
        columns[3].metric("Quiet days", int(summary["quiet_days"].sum()))
    daily = activity.daily
    if not daily.empty:
        st.subheader("Dispensed per day")
        show_chart(charts.activity_lines(daily, is_dark()))
    st.subheader("Summary")
    show_table(tables["activity_summary"])
    st.subheader("Average dispensed by weekday")
    weekday = activity.weekday.rename(
        columns={
            "system_name": "System",
            "weekday": "Weekday",
            "days": "Days",
            "avg_dispensed": "Average dispensed",
        }
    )
    show_table(weekday)
    if not tables["activity_changes"].empty:
        with st.expander(f"Days that changed between pulls ({len(tables['activity_changes'])})"):
            show_table(tables["activity_changes"])
    if result.comparison is not None:
        _, found = result.comparison
        unit = "month(s)" if found["granularity"] == "month" else "day(s)"
        st.subheader("Robot against the PMR")
        if found["overlap_days"]:
            st.caption(
                f"Over the {found['overlap_days']} {unit} both cover: robot dispensed "
                f"{found['robot_total']:,.0f}; PMR total {found['pmr_total']:,.0f} "
                f"({found['pmr_basis']}). The PMR can include dispensing that did not come from "
                "the robot, so this is a sanity check, not a reconciliation."
            )
            show_table(tables["activity_pmr"])
        elif found["granularity"] == "month":
            st.caption(
                "The PMR gives only months, and no whole month is covered by the activity data."
            )
        else:
            st.caption("The PMR and the activity data share no days.")


def usage_tab(result) -> None:
    """What the PMR says was dispensed: busiest products, trends, stopped products, by month."""
    usage = result.usage
    tables = export.usage_tables(usage)
    meta = usage.meta
    unit = "months" if meta["date_basis"] == "month" else "days"
    st.caption(
        f"Usage window {meta['period_start']:%Y-%m-%d} to {meta['period_end']:%Y-%m-%d} "
        f"({meta['rate_days']} days, counted in whole {unit}). Ranked by scripts because "
        "quantities are in different units for different products. A trend needs at least "
        f"{meta['trend_min_scripts']} scripts and a change of {meta['trend_threshold']:.0%}; "
        "both are assumptions."
    )
    for message in usage.warnings:
        st.warning(message)
    counts = usage.products["trend"].value_counts()
    columns = st.columns(5)
    for column, trend in zip(columns, ("rising", "falling", "steady", "new", "few scripts")):
        column.metric(trend.capitalize(), int(counts.get(trend, 0)))

    st.subheader("Busiest products")
    show_chart(
        charts.top_products(tables["usage_products"], dark=is_dark()),
        "Scripts in the window, top 15.",
    )
    show_table(tables["usage_products"], "No products were dispensed in the window.")
    with st.expander(f"Dispensed earlier but not in the window ({len(tables['usage_stopped'])})"):
        st.caption(
            "Products that used to be dispensed and no longer are, most regularly dispensed "
            "first. They are the nearest thing to dead stock the PMR can show, but only the "
            "stock listing says whether any is still in the robot."
        )
        show_table(tables["usage_stopped"])

    monthly = tables["usage_monthly"]
    if not monthly.empty:
        st.subheader("Scripts per month")
        show_chart(charts.monthly_scripts(monthly, is_dark()))
        with st.expander("Month by month"):
            show_table(monthly)
    robot = tables["usage_robot"]
    if not robot.empty:
        st.subheader("Robot against the PMR, per weekday")
        st.caption(
            "Robot dispensed per weekday against PMR scripts per weekday, for months the "
            "activity data covers. Both count about one item per script, so they should be of "
            "the same order. A sanity check, not a reconciliation."
        )
        show_chart(charts.robot_vs_pmr(robot, is_dark()))
        show_table(robot)


def suggestions_tab(result) -> None:
    """What to put in, take out of, or stock up in the robot, from how fast products sell."""
    sg = result.suggestions
    meta = sg.meta
    dark = is_dark()
    tables = export.suggest_tables(sg)
    st.caption(
        "Products are ranked by scripts in the usage window and grouped: **A** fast movers (the "
        f"first {meta['a_share']:.0%} of scripts), **B** medium (to {meta['b_share']:.0%}), "
        "**C** slow movers. These are suggestions for the people who arrange the robot: the PMR "
        "does not say which robot dispensed a line and there is no slot data, so how many "
        "products fit is their call. The group limits are assumptions."
    )
    for message in sg.warnings:
        st.warning(message)
    if "catalogue_products" in meta:
        st.caption(
            f"Robot catalogue: {meta['catalogue_products']:,} products. Everything here uses "
            f"only products in it; {meta['left_out_products']:,} PMR products "
            f"({meta['left_out_share']:.0%} of scripts) are not in it and are left out. "
            "The catalogue may be out of date."
        )
    else:
        st.error(
            "The robot catalogue was not applied: `reference/robot_catalogue.csv` is missing, "
            "unreadable, empty, or none of its products match this PMR (wrong file?). "
            "Suggestions are not limited to products the robot can hold. Do not act on them "
            "until this is fixed."
        )
    columns = st.columns(6)
    columns[0].metric("A products", meta["a_products"])
    columns[1].metric("B products", meta["b_products"])
    columns[2].metric("C products", meta["c_products"])
    columns[3].metric("To add", meta["add_count"])
    columns[4].metric("To review for removal", meta["remove_count"])
    columns[5].metric("To stock up", meta["increase_count"])

    left, right = st.columns(2)
    with left:
        st.subheader("Where the sales are")
        show_chart(charts.group_share(sg.abc, dark))
    with right:
        st.subheader("How concentrated")
        show_chart(charts.pareto_curve(sg.abc, meta["a_share"], meta["b_share"], dark))

    if sg.has_stock:
        st.subheader("Add to the robot")
        st.caption("A and B products the robot does not hold, fastest first.")
        show_chart(charts.add_list(tables["suggest_add"], dark=dark))
        show_table(
            tables["suggest_add"], "No A or B product from the catalogue is missing from the robot."
        )
        st.subheader("Review for removal")
        st.caption("Robot items that are dead stock or slow movers, slowest first.")
        show_table(tables["suggest_remove"], "No dead stock or slow movers in the robot.")
        st.subheader("Stock up")
        st.caption("Fast movers (A) the robot holds but that are understocked.")
        show_table(tables["suggest_increase"], "No fast mover is understocked.")
        st.subheader("Swap pairs")
        st.caption(
            "The slowest robot item beside the fastest missing product. With a robot catalogue "
            "loaded, only products in the catalogue are paired in. A starting point. "
            "0 scripts means the robot holds the item but none was dispensed in the window; "
            "the stock quantity shows how much is there."
        )
        show_table(tables["suggest_swaps"], "Nothing to pair.")
    with st.expander(f"All products by sales group ({len(sg.abc)})"):
        show_table(tables["suggest_abc"])


def usage_summary_tab(pharmacy: Pharmacy, result, excel: bytes, html: str) -> None:
    """The Summary tab when there is no stock file: usage figures instead of stock classes."""
    meta = result.usage.meta
    columns = st.columns(4)
    columns[0].metric("Scripts in the window", f"{meta['scripts']:,}")
    columns[1].metric("Products dispensed", f"{meta['products']:,}")
    columns[2].metric("Stopped products", f"{meta['products_stopped']:,}")
    columns[3].metric("Months in the PMR", meta["months_in_data"])
    st.caption(
        f"{meta['quantity']:,.0f} units over {meta['period_start']:%Y-%m-%d} to "
        f"{meta['period_end']:%Y-%m-%d}. Stock was not classified because no stock file was "
        "uploaded."
    )
    st.subheader("Validation messages")
    show_issues(result.report)
    st.subheader("Criteria in use")
    show_table(result.report.settings.astype(str), "")
    st.subheader("Download")
    left, right = st.columns(2)
    left.download_button(
        "Excel report",
        excel,
        file_name=f"{pharmacy.key}_usage_analysis.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{pharmacy.key}_download_xlsx",
    )
    right.download_button(
        "HTML report (opens without the app)",
        html,
        file_name=f"{pharmacy.key}_usage_analysis.html",
        mime="text/html",
        key=f"{pharmacy.key}_download_html",
    )


def welcome(pharmacy: Pharmacy) -> None:
    st.info(
        f"Upload {pharmacy.label}'s PMR usage export (and, for the full stock audit, its robot "
        "stock export) on the **1 · Upload and check** step. The analysis opens in step 2."
    )
    st.markdown(
        f"1. Upload {pharmacy.label}'s PMR usage export. The robot activity statistics are "
        "optional. Add the OmniCell stock export to classify stock too.\n"
        "2. With a PMR alone you get a **usage analysis**: busiest products, trends, products "
        "that stopped being dispensed, and a month-by-month view.\n"
        "3. With the stock export as well, check the **match rate**, then review dead stock, "
        "overstock and understock, and the **Review** tab for items that could not be matched.\n"
        "4. Download the Excel report, or the HTML report if the app is unavailable."
    )
    st.caption(
        f"If {pharmacy.label}'s files use different column names, use **Map your columns** "
        "after uploading."
    )


def pharmacy_page(
    pharmacy: Pharmacy, settings: Settings, use_code: bool, packs: bool, latest: bool
) -> None:
    files = {source: content for source, (_, content) in stored_files(pharmacy).items()}
    activity_files = [content for _, content in stored_pulls(pharmacy)]
    overrides = chosen_overrides(pharmacy, [*files, *([ACTIVITY_SOURCE] if activity_files else [])])
    if not files and not activity_files:
        welcome(pharmacy)
        return
    result, excel, html = run_cached(
        tuple(sorted(files.items())),
        settings,
        overrides,
        use_code,
        load_confirmed(pharmacy),
        packs,
        tuple(activity_files),
        pharmacy.key,
        latest,
        catalogue_stamp(),
    )
    if not files:  # robot activity statistics on their own
        st.info(
            "Only robot activity statistics were uploaded. Upload the robot stock and PMR "
            "exports to run the stock audit."
        )
        activity_tab(result)
        return
    if result.errors:
        for message in result.errors:
            st.error(message)
        for issue in result.issues:
            if issue.severity != "info" and issue.source != ACTIVITY_SOURCE:
                st.warning(f"{issue.message} ({issue.source or 'all files'})")
        if result.activity_ran:
            st.divider()
            st.subheader("Robot activity")
            activity_tab(result)
        return

    has_activity = result.activity_ran or bool(result.activity_errors)
    if result.usage_only:  # a PMR but no stock file
        st.info(
            "No stock file was uploaded, so stock is not classified (dead, over, under). This is "
            "what the PMR says was dispensed. Upload the robot stock export as well to run the "
            "full stock audit."
        )
        names = [
            "Summary",
            *(["Suggestions"] if result.suggestions else []),
            "Usage",
            *(["Robot activity"] if has_activity else []),
            "Print",
        ]
        tabs = dict(zip(names, st.tabs(names)))
        with tabs["Summary"]:
            usage_summary_tab(pharmacy, result, excel, html)
        if "Suggestions" in tabs:
            with tabs["Suggestions"]:
                suggestions_tab(result)
        with tabs["Usage"]:
            usage_tab(result)
        if has_activity:
            with tabs["Robot activity"]:
                activity_tab(result)
        with tabs["Print"]:
            print_tab(result)
        return

    tables = result.report.tables
    names = ["Summary", *(title for title, _ in CLASS_TABS.values()), "Review"]
    extras = [
        *(["Suggestions"] if result.suggestions else []),
        *(["Usage"] if result.usage_ran else []),
        *(["Robot activity"] if has_activity else []),
        "Print",
    ]
    tabs = st.tabs([*names, *extras])
    summary, class_tabs = tabs[0], tabs[1 : 1 + len(CLASS_TABS)]
    review, extra_tabs = tabs[1 + len(CLASS_TABS)], tabs[2 + len(CLASS_TABS) :]
    with summary:
        summary_tab(pharmacy, result, excel, html)
    for tab, key in zip(class_tabs, CLASS_TABS):
        with tab:
            class_tab(tables, key)
    with review:
        review_tab(pharmacy, result, tables)
    for tab, name in zip(extra_tabs, extras):
        with tab:
            {
                "Suggestions": suggestions_tab,
                "Usage": usage_tab,
                "Robot activity": activity_tab,
                "Print": print_tab,
            }[name](result)


def main() -> None:
    st.set_page_config(page_title="Robot stock audit", layout="wide")
    st.session_state["_table_seq"] = 0  # numbers the tables drawn in this run (filter keys)
    st.title("Robot stock audit")
    view = st.radio(
        "Step",
        [UPLOAD_VIEW, ANALYSIS_VIEW],
        horizontal=True,
        key="view",
        label_visibility="collapsed",
    )
    settings = sidebar_criteria()
    use_code, packs, latest = sidebar_options()
    tabs = st.tabs([pharmacy.label for pharmacy in PHARMACIES])
    for pharmacy, tab in zip(PHARMACIES, tabs):
        with tab:
            if view == UPLOAD_VIEW:
                upload_page(pharmacy)
            else:
                pharmacy_page(pharmacy, settings, use_code, packs, latest)


# Streamlit runs this file as __main__; importing it (for tests) does not draw the page.
if __name__ == "__main__":
    main()
