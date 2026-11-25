# Robot stock audit

Compares what an OmniCell robot holds (stock export) with what the pharmacy dispenses (PMR
usage export) and sorts each medicine into **dead stock**, **overstocked**, **understocked** or
**healthy**. It is a Streamlit app that runs locally, with **one tab per pharmacy** (Lowood,
McGregor): each pharmacy uploads its own files and gets its own analysis, because the files differ
between pharmacies. Uploaded files are processed in memory and never stored.

> **Status: working, but running on placeholders.** Column headers, thresholds, branches and
> several rules are assumptions until the real exports are mapped and the pharmacy confirms the
> criteria. Everything that is a guess is labelled in the app and listed in
> [`OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md). Do not present results as final until those are
> answered.

## How it works

```
upload -> load.py -> clean.py -> match.py -> analyse.py -> export.py
          read,       standard    link robot   usage rate,   Excel +
          validate,   tables      items to     days of       HTML
          drop patient            PMR items,   cover,        report
          columns                 review list  4 classes
```

With a **PMR alone** (no stock file) the app gives a **usage analysis** instead (`usage.py`): busiest
products, trends, products that stopped being dispensed, and a month-by-month view. Stock cannot
be classified without a stock quantity, and the app says so.

Robot activity statistics (daily counts for the whole robot) are handled on the side by
`activity.py`: they cannot feed the stock classes, but they show how busy the robot is and give
an independent total to compare the PMR with.

`pipeline.py` chains the stages; `app.py` is only the page. Settings and word lists live in
`config.py`; column names live in `column_map.yaml`; the internal schema is in `schema.py`.

## Setup

Developed and tested on Python 3.11. Python 3.11 or newer is expected to work.

Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

`requirements.lock` holds the exact versions of the last passing run. To reproduce that exact
environment instead, run `pip install -r requirements.lock`.

## Run the app

```bash
streamlit run app.py
```

It opens in the browser (normally <http://localhost:8501>). Use it on one computer; it has no
login, so do not expose it on a shared network until hosting is decided (open question 8).

## Running an audit (for pharmacy staff)

1. Download the **PMR usage export** and, for the full stock audit, the **OmniCell stock export**
   for the robot. The systems cannot send data automatically, so this is done by hand.
2. Open your **pharmacy's tab** and upload its files in the labelled slots. The audit runs by
   itself. The pharmacy's name is used as the branch, so no file needs a branch column. The robot
   activity statistics slot is optional and takes several files (repeated pulls are merged; the
   most recent pull wins for any day they share). It also works on its own. Criteria and options
   in the sidebar apply to every pharmacy.
3. Look at **Summary** first. With a stock export, the **match rate** says how many robot items
   were linked to the PMR; if it is low, do not rely on the lists below it. With a PMR alone,
   Summary shows scripts, products and stopped products, and the **Usage** tab holds the
   analysis. The usage tables rank products by scripts (quantities are in different units for
   different products) and only call a trend with at least 5 scripts and a 25% change; both are
   assumptions.
4. Read the validation messages. Errors and warnings explain missing columns, odd quantities,
   short date ranges and wrong files (for example an activity-statistics file in a stock slot).
5. Open **Dead stock**, **Overstock**, **Understock** and **Healthy**. If you uploaded activity statistics, the **Robot activity**
   tab shows daily counts, quiet days, weekday averages and a check against the PMR.
6. Open **Review** for items that could not be matched. For each, choose the same product in
   the PMR or "not in the PMR" and press **Save decision**. The decision is remembered next time
   in that pharmacy's own file (`confirmed_matches_lowood.csv`, `confirmed_matches_mcgregor.csv`).
   For many items with no similar product in the PMR at all, use
   **Bulk decision** to mark them all "not in the PMR" in one go. It is switched off whenever the
   PMR covers fewer days than the usage window, because an item missing from a short PMR is not
   necessarily unused.
7. Change the criteria in the sidebar if the pharmacy's rules differ. They are labelled
   "Assumption, to be confirmed" until someone confirms them.
8. On **Summary**, download the **Excel report** and, if you want a copy that opens without the
   app, the **HTML report**.

If a file's columns are not recognised, open **Map your columns (optional)** in that pharmacy's
tab and pick the right column for each field. Ask a developer to add it to `column_map.yaml`
under `pharmacies:` afterwards so it works automatically.

**PMR reports that give only a month.** Lowood's *Script Report Generator* gives the month of
dispensing, not the day. The usage window is then counted in whole calendar months (90 days is
3 months) and the rate is divided by the real length of those months. The latest month is left
out by default because a report run part-way through a month is incomplete; tick **Count the
latest month** in the sidebar to include it.

## If the app is unavailable (fallback)

The HTML report is a single self-contained file. It needs no app, no internet and no install:
open it in any browser. It shows the match rate, the counts, every table and the settings used,
with a branch filter, exactly as the app showed them on the day it was made. Each pharmacy
has its own report.

1. Use the **latest HTML report** that was downloaded from the app. Check the generated time at
   the top. It is a snapshot and cannot re-run an audit.
2. Share it by file or email within the pharmacy. It holds stock and dispensing quantities only
   and no patient data, but treat it as internal.
3. When the app is back, **re-run the audit** with fresh exports and download a new report.

Always download the Excel and HTML reports at the end of a run; that is what makes the fallback
possible.

## Troubleshooting

- **`No module named 'streamlit.proto'` (or other missing files) after `pip install` on
  Windows:** the project folder path is too long (Windows stops at 260 characters inside
  `.venv`). Clone or move the project to a short path such as `C:udit` and set up again.
- **A file is rejected as the wrong type:** a stock export went in the PMR slot, or an
  activity-statistics file went in a stock slot. Upload it in the right slot.
- **Match rate is low:** names differ between the systems. Use the **Review** tab, and tell a
  developer which spellings to add to `config.py` (see "Adapting to the real files").

## Tests and checks

```bash
pytest
ruff check .
```

`pytest` includes a coverage gate (80% minimum, currently about 98%). All test data is synthetic.
Regenerate it, byte for byte, with `python tests/make_fixtures.py`; `tests/fixtures/README.md`
lists every planted problem and the check that should catch it. Real exports must never be
committed (`*.csv` is git-ignored apart from the fixtures).

## Adapting to the real files

When real exports arrive, most changes are one-line edits:

| Change | Where |
|---|---|
| A column is named differently | `column_map.yaml` (add the header; case, spacing and `_` are ignored) |
| A new required or optional column | `schema.py`, then `column_map.yaml` |
| A pharmacy's own file format | `pharmacies:` section of `column_map.yaml` (its headers are tried first) |
| Add or rename a pharmacy, or give it more robots | `PHARMACIES` in `config.py` |
| Branch of each robot (used outside the pharmacy tabs) | `ROBOT_REGISTRY` in `config.py` |
| Thresholds, usage window, classification level | `DEFAULT_SETTINGS` in `config.py` (or the sidebar) |
| Abbreviations, salt names, unit spellings, spellings like `P/R` | `NAME_ABBREVIATIONS`, `NAME_IGNORED_WORDS`, `NAME_PHRASES`, `UNIT_ALIASES` in `config.py` |
| Stock count is packs, not units | Tick **Stock count is a number of packs** in the sidebar (default in `STOCK_QTY_IS_PACK_COUNT`, `config.py`) |
| Which robot an activity `SYSTEMNAME` is | `ACTIVITY_SYSTEM_ROBOT` in `config.py` |
| Patient-field name patterns | `PATIENT_FIELD_PATTERNS` in `config.py` |
| What counts as a rising or falling product | `TREND_THRESHOLD`, `TREND_MIN_SCRIPTS` in `config.py` |
| How strict fuzzy matching is | `FUZZY_THRESHOLD`, `AMBIGUITY_MARGIN` in `config.py` |
| Shared product code confirmed | Tick **Match on product code** in the sidebar |

A test fails if a source header is ever hard-coded in a `.py` file. After any change, run
`pytest` and `ruff check .`.

## Project layout

| File | Job |
|---|---|
| `app.py` | Streamlit page: one tab per pharmacy, criteria, results |
| `pipeline.py` | Chains the stages in memory and returns problems as messages |
| `config.py` | Settings, registries, word lists, column-map loader |
| `schema.py` | Internal schema (required and optional columns per source) |
| `column_map.yaml` | Source headers for each internal column (placeholders) |
| `load.py` | Read CSV (encoding, delimiter), drop patient columns, validate |
| `clean.py` | Standard stock and usage tables, strength and name normalisation |
| `match.py` | Matching levels 1 to 4, review list, confirmed-match lookup |
| `analyse.py` | Usage rate, days of cover, four classes |
| `activity.py` | Robot activity statistics: merge pulls, summarise, compare with the PMR |
| `usage.py` | PMR usage analysis: products, trends, stopped products, months, robot check |
| `export.py` | Excel report and static HTML report from one `Report` |
| `tests/` | One test file per module, plus synthetic fixtures |

## Data protection

- Uploaded files are used during the run and not kept. The only things written are the
  confirmed-match lookup (item keys and decisions, no patient data) and reports you download.
- Patient-like columns are detected by name and dropped before anything else happens. They never
  reach a table, a report or a log. The Excel and HTML builders refuse to run if one slips
  through.
- Quantities of stock and dispensing are the only data the audit needs.

## Known limits

- Every file seen so far is **Lowood's**. McGregor has no file yet, so its tab starts empty and
  its headers are unknown (`pharmacies: mcgregor` in `column_map.yaml` is a placeholder).
- Without a stock file nothing is classified as dead, over or understocked. The "stopped"
  list (dispensed earlier, nothing in the window) is only a pointer: whether such a product is
  still in the robot needs the stock listing.
- Lowood's *Script Report Generator* PMR gives quantities but only a month, no product code, and
  cannot say whether an item was dispensed from the robot. Its older 4-column sample has day
  dates but no quantity (each row then counts as one dispensing line).
- Matching works on name and strength. Brand names versus generics always need a person.
- The real OmniCell stock listing (`BestandArtikel`, semicolon-separated, 61 columns) is mapped
  from its header names, but the pharmacy has not confirmed what the columns mean. In
  particular the stock count (`ISTANZAHL`) may be a number of packs rather than units
  (`STOCK_QTY_IS_PACK_COUNT` in `config.py`; against the real Lowood PMR, counting it as units
  gives a median of 1 day of cover, counting it as packs gives 42, so it is almost certainly
  packs), and the file has no usable expiry date and does
  not say which robot it came from. The activity statistics are mapped from their real headers
  and shown on their own tab; which robot a `SYSTEMNAME` is, is unconfirmed. All are tracked in
  `OPEN_QUESTIONS.md`.
- A stock item that is absent from the PMR waits on the Review tab until a person confirms it is
  not in the PMR (one by one, or with the bulk decision). Against a 90-day PMR this will be a
  long list (open question 27).
