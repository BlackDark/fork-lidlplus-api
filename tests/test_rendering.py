"""Opt-in browser check: LIDLPLUS_RENDER_TESTS=1 uv run pytest."""

import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest


@pytest.mark.skipif(
    os.environ.get("LIDLPLUS_RENDER_TESTS") != "1",
    reason="Set LIDLPLUS_RENDER_TESTS=1 after installing Chromium",
)
def test_long_receipt_and_return_render_without_hanging(tmp_path):
    env = os.environ.copy()
    env.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".lidlplus/browsers").resolve())
    )
    # A subprocess timeout also catches browser operations whose promises never settle.
    script = """
from pathlib import Path
import sys
from lidlplus_api.archive import renderer, create_context, render_receipt
rows = ''.join(f'<p>Article {i}: Äpfel, Café - 1,99 €</p>' for i in range(120))
detail = {
    'htmlPrintedReceipt': '<h1>Receipt</h1>' + rows + '<b>END</b>',
    'returnedHtmlTickets': [{'htmlPrintedReceipt': '<h1>Return</h1><p>1,99 €</p>'}],
}
with renderer() as p:
    browser = p.chromium.launch()
    try:
        render_receipt(create_context(browser), detail, Path(sys.argv[1]) / 'receipt')
    finally:
        browser.close()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], env=env, check=True, timeout=60
    )
    with Image.open(tmp_path / "receipt.jpg") as image:
        assert image.width == 960
        assert image.height > 5000
    for stem in ("receipt", "receipt-return-1"):
        assert (tmp_path / f"{stem}.pdf").read_bytes().startswith(b"%PDF")
        assert (tmp_path / f"{stem}.jpg").stat().st_size > 1000


@pytest.mark.skipif(
    os.environ.get("LIDLPLUS_RENDER_TESTS") != "1",
    reason="Set LIDLPLUS_RENDER_TESTS=1 after installing Chromium",
)
@pytest.mark.parametrize("clicked", ["timer", "button", "iframe", "log-only"])
def test_login_captures_javascript_native_app_navigation(clicked):
    env = os.environ.copy()
    env.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".lidlplus/browsers").resolve())
    )
    script = """
import sys
from urllib.parse import quote
from lidlplus_api import LidlPlusApi
from playwright.sync_api import Browser, CDPSession

original_on = CDPSession.on
if sys.argv[1] == 'log-only':
    def on(session, event, handler):
        if event != 'Page.frameRequestedNavigation':
            return original_on(session, event, handler)
    CDPSession.on = on

mode = sys.argv[1]
clicked = mode != 'timer'
original_new_page = Browser.new_page
def new_page(browser, *args, **kwargs):
    page = original_new_page(browser, *args, **kwargs)
    def serve(route):
        callback = f'com.lidlplus.app://callback?code=synthetic&state={api._oauth_state}#login/verifymfacode'
        html = f'<input data-testid="input-email"><button data-testid="login-or-register-submit-button" onclick="location.href=&quot;{callback}&quot;">Confirm</button>'
        if mode == 'iframe':
            html = f'<input data-testid="input-email"><button data-testid="login-or-register-submit-button" onclick="const f=document.createElement(&quot;iframe&quot;);f.src=&quot;{callback}&quot;;document.body.appendChild(f)">Confirm</button>'
        route.fulfill(body=html, content_type='text/html')
    page.route('https://accounts.lidl.com/**', serve)
    return page
if clicked:
    Browser.new_page = new_page

def synthetic_login(api):
    api._register_oauth_client()
    callback = f'com.lidlplus.app://callback?code=synthetic&state={api._oauth_state}#login/verifymfacode'
    html = '<script>setTimeout(() => {window.location.href="' + callback + '"}, 100)</script>'
    return 'data:text/html,' + quote(html)

if not clicked:
    LidlPlusApi._register_link = property(synthetic_login)
api = LidlPlusApi('de', 'DE')
received = []
api._authorization_code = received.append
api.login(email='synthetic' if clicked else None, timeout=3)
assert received == ['synthetic'], received
"""
    subprocess.run(
        [sys.executable, "-c", script, str(clicked)], env=env, check=True, timeout=20
    )
