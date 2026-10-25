import json
import stat
from unittest.mock import MagicMock, Mock

import pytest

from lidlplus_api import LidlPlusApi
from lidlplus_api.api import MissingLogin
from lidlplus_api.archive import download_receipts, receipt_destination, receipt_name
from lidlplus_api.cli import save_auth


def test_missing_login_has_useful_error():
    with pytest.raises(MissingLogin, match="login"):
        LidlPlusApi("de", "DE")._default_headers()


def test_pagination_and_overlapping_results():
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(
        side_effect=[
            {"tickets": [{"id": "a"}, {"id": "b"}], "totalCount": 3},
            {"tickets": [{"id": "b"}, {"id": "c"}], "totalCount": 3},
        ]
    )
    assert [ticket["id"] for ticket in api.iter_receipts()] == ["a", "b", "c"]
    assert api.receipts.call_args.kwargs == {"pageNumber": 2}


def test_repeated_page_does_not_loop_forever():
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(return_value={"tickets": [{"id": "a"}], "totalCount": 10})
    with pytest.raises(ValueError, match="repeated"):
        list(api.iter_receipts())


@pytest.mark.parametrize("payload", [{}, {"tickets": None}, {"tickets": [{}]}])
def test_invalid_listing_is_not_treated_as_empty(payload):
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(return_value=payload)
    with pytest.raises(ValueError):
        list(api.iter_receipts())


def test_truncated_listing_is_reported():
    api = LidlPlusApi("de", "DE")
    api.receipts = Mock(return_value={"tickets": [], "totalCount": 4})
    with pytest.raises(ValueError, match="totalCount"):
        list(api.iter_receipts())


def test_auth_file_is_private_and_replaced(tmp_path):
    api = LidlPlusApi("de", "DE", "first")
    path = tmp_path / "auth.json"
    save_auth(path, api)
    api._refresh_token = "rotated"
    save_auth(path, api)
    assert json.loads(path.read_text())["refresh_token"] == "rotated"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_originals_survive_render_failure_and_resume(tmp_path, monkeypatch):
    api = Mock()
    api.iter_receipts.return_value = [
        {"id": "../../escape", "date": "2026-10-24"},
        {"id": "good", "date": "2026-10-24"},
    ]
    api.receipt.return_value = {"htmlPrintedReceipt": "<p>Receipt</p>"}
    playwright = MagicMock()
    monkeypatch.setattr("lidlplus_api.archive.renderer", lambda: playwright)
    render = Mock(side_effect=[ValueError("bad render"), None])
    monkeypatch.setattr("lidlplus_api.archive.render_receipt", render)
    count, failures = download_receipts(api, tmp_path, progress=lambda _: None)
    assert count == 1
    assert failures[0]["id"] == "../../escape"
    assert (
        receipt_destination(tmp_path, {"id": "../../escape", "date": "2026-10-24"})
        .with_suffix(".json")
        .exists()
    )
    assert len(list(tmp_path.glob("2026-10-24/*_summary.json"))) == 2
    api.receipt.reset_mock()
    render.side_effect = None
    count, failures = download_receipts(api, tmp_path, progress=lambda _: None)
    assert (count, failures) == (2, [])
    api.receipt.assert_not_called()


def test_dated_export_reuses_legacy_cache_without_redownloading(tmp_path, monkeypatch):
    api = Mock()
    api.iter_receipts.return_value = [
        {"id": "first", "date": "2026-10-24T00:00:00+02:00"},
        {"id": "second", "date": "2026-10-24T23:59:59+02:00"},
    ]
    for ticket_id in ("first", "second"):
        folder = tmp_path / receipt_name(ticket_id)
        folder.mkdir()
        (folder / "receipt.json").write_text(
            json.dumps({"htmlPrintedReceipt": "<p>Cached</p>"})
        )
    monkeypatch.setattr("lidlplus_api.archive.renderer", MagicMock())
    render = Mock()
    monkeypatch.setattr("lidlplus_api.archive.render_receipt", render)
    assert download_receipts(api, tmp_path, progress=lambda _: None) == (2, [])
    api.receipt.assert_not_called()
    assert {call.args[2].name for call in render.call_args_list} == {
        "2026-10-24_first",
        "2026-10-24_second",
    }
    assert (tmp_path / "2026-10-24/2026-10-24_first.json").exists()
    assert (tmp_path / "2026-10-24/2026-10-24_second_summary.json").exists()


def test_refresh_http_errors_do_not_replace_token(monkeypatch):
    import requests

    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
    monkeypatch.setattr("lidlplus_api.api.requests.post", Mock(return_value=response))
    api = LidlPlusApi("de", "DE", "existing")
    with pytest.raises(requests.HTTPError):
        api._renew_token()
    assert api.refresh_token == "existing"


def test_oauth_callback_checks_state_and_decodes_code():
    from urllib.parse import parse_qs, urlparse

    api = LidlPlusApi("de", "DE")
    params = parse_qs(urlparse(api._register_link).query)
    assert params["code_challenge_method"] == ["S256"]
    state = params["state"][0]
    assert (
        api._callback_code(f"com.lidlplus.app://callback?state={state}&code=a%2Bb")
        == "a+b"
    )
    with pytest.raises(MissingLogin, match="state"):
        api._callback_code("com.lidlplus.app://callback?state=wrong&code=bad")
    assert (
        api._callback_code(f"https://elsewhere.example/?state={state}&code=bad") is None
    )


def test_rotated_token_is_saved_when_listing_fails(tmp_path, monkeypatch):
    from lidlplus_api import cli

    path = tmp_path / "auth.json"
    save_auth(path, LidlPlusApi("de", "DE", "original"))

    def failed_download(api, *args, **kwargs):
        api._refresh_token = "rotated"
        raise ValueError("listing failed")

    monkeypatch.setattr(cli, "download_receipts", failed_download)
    monkeypatch.setattr("sys.argv", ["lidlplus", "--auth-file", str(path), "download"])
    assert cli.main() == 1
    assert json.loads(path.read_text())["refresh_token"] == "rotated"


def test_browser_login_exchanges_redirect_code(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.launch.return_value
    page = browser.new_page.return_value
    api = LidlPlusApi("de", "DE")
    api._authorization_code = Mock()

    def navigate(url):
        state = parse_qs(urlparse(url).query)["state"][0]
        handler = page.on.call_args.args[1]
        handler(
            Mock(
                url="https://accounts.lidl.com/connect/authorize/callback",
                headers={
                    "location": f"com.lidlplus.app://callback?state={state}&code=accepted%2Bcode"
                },
            )
        )

    page.goto.side_effect = navigate
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: playwright)
    api.login()
    api._authorization_code.assert_called_once_with("accepted+code")
    browser.close.assert_called_once()


def test_generic_lidl_error_stops_login_instead_of_waiting(monkeypatch):
    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.launch.return_value
    page = browser.new_page.return_value
    page.is_closed.return_value = False
    page.get_by_text.return_value.count.return_value = 1
    api = LidlPlusApi("de", "DE")
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: playwright)
    with pytest.raises(MissingLogin, match="generic error page"):
        api.login()
    assert api.refresh_token == ""
    page.wait_for_timeout.assert_called_once()
