# lidlplus-api

A Python client and command-line receipt archive for Lidl Plus. Forked from
[Andre0512/lidl-plus](https://github.com/Andre0512/lidl-plus).

## Setup with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
these commands from this repository:

```sh
uv sync --locked
uv run lidlplus install-browser
```

uv installs the Python version in `.python-version` automatically. This project
uses Python 3.15.0, the newest stable build offered by the installed uv runtime
catalog when migrated. Python 3.15.1 was released but was not yet available in
that catalog. Dependencies are resolved in `uv.lock`; setup and publishing use
uv throughout. No system Python packages are needed. Chromium is stored locally in
`.lidlplus/browsers/` (or `PLAYWRIGHT_BROWSERS_PATH` if set).

On Linux, Chromium may need system libraries; use Playwright's documented
`install --with-deps chromium` setup if requested by its error message.

## Log in

Use the country of your Lidl Plus account and your preferred login language:

```sh
uv run lidlplus login --country DE --language de
```

A browser opens. Enter your email, password and any SMS/email verification there.
Complete the flow within five minutes. Credentials are never requested in the
terminal. The command stores a refresh token in `.lidlplus/auth.json` with owner-only
permissions. Keep this file private; it grants access to your account. Log in again
if Lidl expires or revokes the token. Close the browser to cancel login.

For troubleshooting, add `--diagnostics` to the login command. It reports only
the login stage and HTTP status. Native app callbacks are captured inside
Chromium; installing the Lidl mobile app on this computer is unnecessary.

## Download every available receipt

```sh
uv run lidlplus download
```

The command follows all receipt pages, downloads every receipt's full JSON and
renders the API's `htmlPrintedReceipt` using Chromium. Receipts are grouped by
purchase date, with date-based filenames and the receipt ID to distinguish
multiple purchases on the same day:

```text
receipts/
  2026-10-24/
    2026-10-24_<receipt-id>.json
    2026-10-24_<receipt-id>_summary.json
    2026-10-24_<receipt-id>.html
    2026-10-24_<receipt-id>.jpg
    2026-10-24_<receipt-id>.pdf
```

The plain JSON is the untouched full API response; `_summary.json` stores list
metadata. HTML preserves the original printable receipt, JPG is full length at
double resolution, and PDF is printable A4 with selectable text and pagination
for long receipts. Associated returns use the same filename prefix with
`-return-N` before the extension. Unsafe receipt identifiers are replaced by a
hash to keep filenames inside the date folder.

PDFs are generated directly from the HTML to preserve text quality. There is one
PDF per receipt (plus separate return PDFs). An empty account exports no files
except the failure report. Only receipts still made available by Lidl can be downloaded.

Run the same command again after an interruption. Already downloaded JSON is reused
and JPG/PDF files are rebuilt. Older hash-named folders are also recognized as a
cache: matching receipts are copied into the dated layout without refetching
full details. Existing legacy files are retained. `convert` also supports both
layouts and processes each matching receipt once.
Use `--overwrite` to fetch receipt details again:

```sh
uv run lidlplus download --output receipts --overwrite
uv run lidlplus convert --output receipts
```

`convert` rebuilds exports from local JSON without logging in. Rendering may fetch
HTTPS images, fonts and styles referenced by Lidl's HTML. Scripts are disabled.
If printable HTML is absent, raw JSON is retained and the receipt is reported as
failed rather than producing an incomplete reconstruction. Individual receipt
failures are recorded in `receipts/failures.json`; the download continues and exits
with status 1. Listing/network errors also exit with status 1. Successfully saved
receipts remain available. Refresh-token rotation is persisted after downloads.

The default token and receipt folders are gitignored. For a custom token location,
put the global option before the command:

```sh
uv run lidlplus --auth-file /private/path/auth.json download --output /private/path/receipts
```

Custom output paths may need their own gitignore entries. Separate account archives
by using a different auth file and output directory for each account.

## Export a month, year, or date range

The same filters work with `download` and `convert`:

```sh
uv run lidlplus download --period this-month
uv run lidlplus download --period last-month
uv run lidlplus download --period this-year
uv run lidlplus download --since 2026-10-01 --until 2026-10-31 --output receipts-october
uv run lidlplus convert --period last-month
uv run lidlplus convert --since 2026-10-24 --until 2026-10-24
```

Both endpoints are inclusive. Use only `--since` or only `--until` for an
open-ended range. Presets use your computer's local date and cover the full
calendar month or year; `last-month` also handles the January/December boundary.
Choose either `--period` or explicit endpoints. No filter means all receipts.

Filtering uses the purchase calendar date supplied by Lidl, preserving the date
in timestamps with timezone offsets. Downloads still inspect every listing page,
but fetch full details and generate files only for matching receipts. Associated
returns are exported with their selected purchase receipt. A missing or invalid
purchase date produces an error when filtering, so receipts are not silently omitted.

Existing files in the output folder are retained. To create a folder containing
only a chosen period, select a new `--output` directory. `convert` selects from
the saved summaries (or the full receipt's date if no summary is present).

## Python API

```python
from lidlplus_api import LidlPlusApi

api = LidlPlusApi(language="de", country="DE", refresh_token="YOUR_REFRESH_TOKEN")
for summary in api.iter_receipts():
    detail = api.receipt(summary["id"])
```

`api.login()` supports interactive browser login. Existing API methods for coupons,
stores, offers and loyalty remain available. `api.receipts(pageNumber=1)` returns
one page; `api.iter_receipts()` yields summaries from all pages. Use
`api.refresh_token` to persist the newest token when using the library directly.

Date filters are also available as Python functions:

```python
from lidlplus_api import download_receipts, convert_receipts

download_receipts(api, "receipts", period="last-month")
download_receipts(api, "receipts-october", since="2026-10-01", until="2026-10-31")
convert_receipts("receipts", period="this-year")
for summary in api.iter_receipts(since="2026-01-01", until="2026-12-31"):
    print(summary["id"])
```

`since` and `until` accept ISO date strings or `datetime.date` objects. When using
the Python functions directly, install Chromium and set `PLAYWRIGHT_BROWSERS_PATH`
to `.lidlplus/browsers` if using the CLI's local browser installation.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The automated tests use synthetic API responses and do not require an account.
Run the Chromium regression check as well with:

```sh
LIDLPLUS_RENDER_TESTS=1 uv run pytest
```

Install the local browser first with `uv run lidlplus install-browser`.
Live authentication and the account-specific receipt layout need verification
with a real account. To update libraries intentionally, run `uv lock --upgrade`
and repeat the checks. Release builds use `uv sync --locked` and `uv build`.

## Legal Notice

This application is an open source project written in Python, which uses the API of the Lidl Plus application, owned by **Lidl Stiftung & Co. KG**. The application was created solely for educational purposes and is not affiliated with Lidl Stiftung & Co. KG. The creator of the application is not affiliated with Lidl Stiftung & Co. KG. in any way and does not derive any financial benefits from this project. All trademarks, trade names, and logos are the property of their respective owners. Users use the application at their own risk.
