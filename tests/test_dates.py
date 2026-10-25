from datetime import date
import json
from unittest.mock import MagicMock, Mock

import pytest

from lidlplus_api import LidlPlusApi
from lidlplus_api.archive import convert_receipts, download_receipts
from lidlplus_api.dates import DateRange, resolve_date_range


@pytest.mark.parametrize(
    "period,today,start,end",
    [
        ("this-month", date(2026, 10, 24), date(2026, 10, 1), date(2026, 10, 31)),
        ("last-month", date(2026, 1, 10), date(2025, 12, 1), date(2025, 12, 31)),
        ("last-month", date(2024, 3, 1), date(2024, 2, 1), date(2024, 2, 29)),
        ("this-year", date(2026, 10, 24), date(2026, 1, 1), date(2026, 12, 31)),
    ],
)
def test_calendar_presets(period, today, start, end):
    assert resolve_date_range(period=period, today=today) == DateRange(start, end)


@pytest.mark.parametrize(
    "options",
    [
        {"period": "last-month", "since": "2026-01-01"},
        {"period": "this-year", "until": "2026-12-31"},
        {"since": "2026-10-31", "until": "2026-10-01"},
        {"since": "2026-02-30"},
        {"until": "20261001"},
        {"period": "unknown"},
    ],
)
def test_invalid_ranges(options):
    with pytest.raises(ValueError):
        resolve_date_range(**options)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-30T23:59:59Z", False),
        ("2026-10-01T00:00:00+02:00", True),
        ("2026-10-31T23:59:59-05:00", True),
        ("2026-11-01", False),
    ],
)
def test_inclusive_receipt_calendar_dates(value, expected):
    dates = resolve_date_range(since="2026-10-01", until="2026-10-31")
    assert dates.matches({"date": value}) is expected


def test_open_ended_ranges_and_unknown_dates():
    assert resolve_date_range(since="2026-10-01").matches({"date": "2027-01-01"})
    assert resolve_date_range(until="2026-10-01").matches({"date": "2025-01-01"})
    assert resolve_date_range().matches({})
    with pytest.raises(ValueError, match="missing or invalid date"):
        resolve_date_range(since="2026-01-01").matches({"id": "missing-date"})


def test_filtered_pagination_counts_all_receipts_and_handles_unsorted_pages():
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(
        side_effect=[
            {"totalCount": 3, "tickets": [{"id": "old", "date": "2025-01-01"}]},
            {
                "totalCount": 3,
                "tickets": [
                    {"id": "match", "date": "2026-10-31T23:59:59Z"},
                    {"id": "future", "date": "2027-01-01"},
                ],
            },
        ]
    )
    assert [
        r["id"] for r in api.iter_receipts(since="2026-10-01", until="2026-10-31")
    ] == ["match"]
    assert api.receipts.call_count == 2


def test_download_passes_resolved_dates_before_fetching_details(tmp_path, monkeypatch):
    api = Mock()
    api.iter_receipts.return_value = []
    monkeypatch.setattr("lidlplus_api.archive.renderer", MagicMock())
    assert download_receipts(api, tmp_path, since="2026-10-01", until="2026-10-31") == (
        0,
        [],
    )
    api.iter_receipts.assert_called_once_with(
        since=date(2026, 10, 1), until=date(2026, 10, 31)
    )
    api.receipt.assert_not_called()


def test_convert_filters_saved_summaries_and_falls_back_to_detail(
    tmp_path, monkeypatch
):
    for name, purchased in [
        ("old", "2025-01-01"),
        ("chosen", "2026-10-31"),
        ("fallback", "2026-10-01"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "receipt.json").write_text(
            json.dumps(
                {"id": name, "date": purchased, "htmlPrintedReceipt": "<p>Receipt</p>"}
            )
        )
        if name != "fallback":
            (folder / "summary.json").write_text(
                json.dumps({"id": name, "date": purchased})
            )
    monkeypatch.setattr("lidlplus_api.archive.renderer", MagicMock())
    render = Mock()
    monkeypatch.setattr("lidlplus_api.archive.render_receipt", render)
    assert convert_receipts(tmp_path, since="2026-10-01", until="2026-10-31") == 2
    assert {call.args[2].name for call in render.call_args_list} == {
        "2026-10-31_chosen",
        "2026-10-01_fallback",
    }
    # Legacy and newly written files may coexist; each receipt is converted once.
    render.reset_mock()
    assert convert_receipts(tmp_path, since="2026-10-01", until="2026-10-31") == 2
    assert render.call_count == 2
    assert convert_receipts(tmp_path, since="2028-01-01") == 0


def test_cli_rejects_invalid_filters_before_login_or_network(monkeypatch, capsys):
    from lidlplus_api.cli import main

    monkeypatch.setattr(
        "sys.argv",
        ["lidlplus", "download", "--period", "this-month", "--since", "2026-01-01"],
    )
    assert main() == 1
    assert "either period or since/until" in capsys.readouterr().err


def test_ranged_download_fetches_and_exports_only_selected_receipts(
    tmp_path, monkeypatch
):
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(
        return_value={
            "totalCount": 2,
            "tickets": [
                {"id": "outside", "date": "2026-09-30"},
                {"id": "selected", "date": "2026-10-01"},
            ],
        }
    )
    api.receipt = Mock(return_value={"htmlPrintedReceipt": "<p>Receipt</p>"})
    monkeypatch.setattr("lidlplus_api.archive.renderer", MagicMock())
    render = Mock()
    monkeypatch.setattr("lidlplus_api.archive.render_receipt", render)
    assert download_receipts(
        api, tmp_path, since="2026-10-01", until="2026-10-31", progress=lambda _: None
    ) == (1, [])
    api.receipt.assert_called_once_with("selected")
    render.assert_called_once()
    assert len(list(tmp_path.glob("2026-10/2026-10-01/*_summary.json"))) == 1
