"""Download original receipt data and render its printable HTML locally."""

import hashlib
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from .dates import receipt_date, resolve_date_range


def receipt_name(ticket_id):
    # Legacy cache name, also used for identifiers unsafe in filenames.
    return hashlib.sha256(ticket_id.encode()).hexdigest()


def receipt_destination(output, summary):
    purchased = receipt_date(summary).isoformat()
    ticket_id = summary.get("id")
    if not isinstance(ticket_id, str) or not ticket_id:
        raise ValueError("Receipt has no valid id for its filename.")
    identifier = (
        ticket_id
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ticket_id)
        else receipt_name(ticket_id)
    )
    return Path(output) / purchased / f"{purchased}_{identifier}"


def summary_path(stem):
    return stem.with_name(f"{stem.name}_summary.json")


def save_receipt(stem, detail, summary):
    write_json(stem.with_suffix(".json"), detail)
    write_json(summary_path(stem), summary)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def render_receipt(context, detail, destination):
    """Keep original HTML and render the purchase and any returned receipts."""
    documents = [detail] + (detail.get("returnedHtmlTickets") or [])
    for index, document in enumerate(documents):
        source = document.get("htmlPrintedReceipt")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Receipt has no printable HTML; original JSON was saved.")
        stem = (
            destination
            if index == 0
            else destination.with_name(f"{destination.name}-return-{index}")
        )
        stem.with_suffix(".html").write_text(source, encoding="utf-8")
        page = context.new_page()
        try:
            page.set_content(source, wait_until="networkidle", timeout=30000)
            page.emulate_media(media="print")
            page.screenshot(
                path=str(stem.with_suffix(".jpg")),
                type="jpeg",
                quality=95,
                full_page=True,
            )
            # Print the original HTML, keeping text selectable and paginating long receipts.
            temporary = stem.with_suffix(".pdf.tmp")
            page.pdf(
                path=str(temporary),
                format="A4",
                print_background=True,
                margin={
                    "top": "10mm",
                    "bottom": "10mm",
                    "left": "10mm",
                    "right": "10mm",
                },
            )
            temporary.replace(stem.with_suffix(".pdf"))
        finally:
            page.close()


def renderer():
    """Browser owner used by download and offline conversion."""
    return sync_playwright()


def create_context(browser):
    context = browser.new_context(
        viewport={"width": 480, "height": 800},
        device_scale_factor=2,
        java_script_enabled=False,
        service_workers="block",
    )

    def filter_request(route):
        # Allow receipt assets without sending Lidl API credentials to asset servers.
        if route.request.resource_type in {
            "image",
            "stylesheet",
            "font",
        } and route.request.url.startswith("https://"):
            route.continue_()
        else:
            route.abort()

    context.route("**/*", filter_request)
    return context


def download_receipts(
    api, output, *, overwrite=False, progress=print, period=None, since=None, until=None
):
    """Download and export receipts, optionally selected by inclusive purchase dates."""
    dates = resolve_date_range(period=period, since=since, until=until)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    failures = []
    count = 0
    with renderer() as p:
        browser = p.chromium.launch()
        try:
            context = create_context(browser)
            for summary in api.iter_receipts(since=dates.since, until=dates.until):
                ticket_id = summary["id"]
                try:
                    stem = receipt_destination(output, summary)
                    raw = stem.with_suffix(".json")
                    legacy = output / receipt_name(ticket_id) / "receipt.json"
                    cached = raw if raw.exists() else legacy
                    if cached.exists() and not overwrite:
                        detail = json.loads(cached.read_text(encoding="utf-8"))
                    else:
                        detail = api.receipt(ticket_id)
                        if not isinstance(detail, dict):
                            raise ValueError("Unexpected receipt detail response.")
                    save_receipt(stem, detail, summary)
                    # Re-render cached data to repair any interrupted export.
                    render_receipt(context, detail, stem)
                    count += 1
                    progress(f"Exported receipt {count}: {ticket_id}")
                except Exception as exc:
                    failures.append({"id": ticket_id, "error": str(exc)})
                    progress(f"Failed receipt {ticket_id}: {exc}")
        finally:
            browser.close()
            write_json(output / "failures.json", failures)
    return count, failures


def convert_receipts(output, *, period=None, since=None, until=None):
    """Rebuild JPGs and PDFs from downloaded JSON without authentication."""
    dates = resolve_date_range(period=period, since=since, until=until)
    output = Path(output)
    paths = sorted(
        path
        for path in output.glob("*/*.json")
        if path.name != "summary.json" and not path.name.endswith("_summary.json")
    )
    if not paths:
        raise ValueError("No downloaded receipt JSON files found.")
    count = 0
    seen = set()
    with renderer() as p:
        browser = p.chromium.launch()
        try:
            context = create_context(browser)
            for path in paths:
                detail = json.loads(path.read_text(encoding="utf-8"))
                metadata = (
                    path.with_name("summary.json")
                    if path.name == "receipt.json"
                    else summary_path(path.with_suffix(""))
                )
                summary = (
                    json.loads(metadata.read_text(encoding="utf-8"))
                    if metadata.exists()
                    else detail
                )
                if not dates.matches(summary):
                    continue
                stem = receipt_destination(output, summary)
                if summary["id"] in seen:
                    continue
                seen.add(summary["id"])
                save_receipt(stem, detail, summary)
                render_receipt(
                    context,
                    detail,
                    stem,
                )
                count += 1
        finally:
            browser.close()
    return count
