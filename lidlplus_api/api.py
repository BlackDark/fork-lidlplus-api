import base64
from datetime import datetime, timedelta, timezone
import string
import requests
import hashlib
from secrets import choice, token_urlsafe
import uuid
from urllib.parse import parse_qs, urlencode, urlparse
import qrcode
from zoneinfo import ZoneInfo

from .dates import resolve_date_range


class LidlPlusApi:
    def __init__(self, language, country, refresh_token=""):
        self._login_url = ""
        self._code_verifier = ""
        self._code_challenge = ""
        self._refresh_token = refresh_token
        self._expires = None
        self._token = ""
        self._country = country.upper()
        self._language = language.lower()
        self._basech = string.ascii_letters + string.digits + "-._~"

    @property
    def refresh_token(self):
        """Lidl Plus api refresh token"""
        return self._refresh_token

    @property
    def token(self):
        """Current token to query api"""
        return self._token

    def add_code_challenge(self):
        code_verifier = "".join(choice(self._basech) for _ in range(64))
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, code_verifier

    def _register_oauth_client(self):
        if self._login_url:
            return self._login_url
        self._code_challenge, self._code_verifier = self.add_code_challenge()
        self._oauth_state = token_urlsafe(32)
        params = {
            "client_id": "LidlPlusNativeClient",
            "response_type": "code",
            "scope": "openid profile offline_access lpprofile lpapis",
            "redirect_uri": "com.lidlplus.app://callback",
            **self._code_challenge,
            "state": self._oauth_state,
        }
        self._login_url = (
            "https://accounts.lidl.com/connect/authorize/callback?" + urlencode(params)
        )
        return self._login_url

    def _auth(self, payload):
        default_secret = base64.b64encode(
            "LidlPlusNativeClient:secret".encode()
        ).decode()  # TGlkbFBsdXNOYXRpdmVDbGllbnQ6c2VjcmV0
        headers = {
            "Authorization": f"Basic {default_secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        kwargs = {"headers": headers, "data": payload, "timeout": 10}
        result = requests.post("https://accounts.lidl.com/connect/token", **kwargs)
        result.raise_for_status()
        response = result.json()
        self._expires = datetime.now(timezone.utc) + timedelta(
            seconds=response["expires_in"]
        )
        self._token = response["access_token"]
        self._refresh_token = response["refresh_token"]

    def _renew_token(self):
        payload = {"refresh_token": self._refresh_token, "grant_type": "refresh_token"}
        return self._auth(payload)

    def _authorization_code(self, code):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "com.lidlplus.app://callback",
            "code_verifier": self._code_verifier,
        }
        return self._auth(payload)

    @property
    def _register_link(self):
        args = {
            "Country": self._country,
            "language": f"{self._language}-{self._country}",
        }
        params = urlencode(args)
        return f"{self._register_oauth_client()}&{params}"

    def _api_link(self, requested):
        url = f"https://appgateway.lidlplus.com/app/v23/{self._country}/{requested}"
        return url

    def _callback_code(self, url):
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            "com.lidlplus.app",
            "callback",
            "",
        ):
            return None
        values = parse_qs(parsed.query)
        if values.get("state") != [self._oauth_state]:
            raise MissingLogin("Login callback state did not match. Run login again.")
        if values.get("error"):
            raise MissingLogin("Lidl rejected the login. Run login again.")
        return values.get("code", [None])[0]

    def login(self, email=None, password=None, *, timeout=300, diagnostics=False):
        """Open Lidl login; complete any verification in the browser yourself."""
        import re
        from playwright.sync_api import sync_playwright

        self._login_url = ""
        codes = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                page = browser.new_page()

                def capture_navigation(event):
                    code = self._callback_code(event.get("url", ""))
                    if code:
                        codes.append(code)

                # Native app URLs do not become HTTP requests, so request routing
                # and response headers alone miss JavaScript-driven callbacks.
                cdp = page.context.new_cdp_session(page)
                cdp.send("Page.enable")
                cdp.on("Page.frameRequestedNavigation", capture_navigation)

                def capture_blocked_app_launch(event):
                    # Chromium can reject an external protocol before emitting a
                    # navigation event. Its browser log still carries the callback.
                    message = event.get("entry", {}).get("text", "")
                    match = re.search(
                        r"Failed to launch '(com\.lidlplus\.app://callback\?[^']+)' because the scheme does not have a registered handler",
                        message,
                    )
                    if match:
                        code = self._callback_code(match[1])
                        if code:
                            codes.append(code)

                cdp.on("Log.entryAdded", capture_blocked_app_launch)
                cdp.send("Log.enable")

                def capture(route):
                    code = self._callback_code(route.request.url)
                    if code:
                        codes.append(code)
                    route.abort()

                page.route("com.lidlplus.app://**", capture)

                def capture_redirect(response):
                    parsed = urlparse(response.url)
                    if parsed.hostname != "accounts.lidl.com":
                        return
                    if diagnostics and (
                        response.request.resource_type == "document"
                        or response.status >= 400
                    ):
                        stage = (
                            "verification"
                            if "/Otp" in parsed.path
                            else "authorization"
                            if "/connect/" in parsed.path
                            else "login"
                        )
                        print(
                            f"Login diagnostic: {stage}, HTTP {response.status}",
                            flush=True,
                        )
                    location = response.headers.get("location", "")
                    code = self._callback_code(location)
                    if code:
                        codes.append(code)

                page.on("response", capture_redirect)
                page.goto(self._register_link)
                if email is not None:
                    page.get_by_test_id("input-email").fill(email)
                    page.get_by_test_id("login-or-register-submit-button").click()
                if password is not None:
                    page.get_by_test_id("login-input-password").fill(password)
                    page.get_by_test_id("button-primary").click()
                import time

                deadline = time.monotonic() + timeout
                while not codes:
                    if page.is_closed() or time.monotonic() >= deadline:
                        raise MissingLogin(
                            "Login was closed or timed out. Run login again."
                        )
                    page.wait_for_timeout(200)
                    if page.get_by_text(
                        "Hoppla! Es ist ein Fehler aufgetreten.", exact=False
                    ).count():
                        raise MissingLogin(
                            "Lidl displayed its generic error page before completing authorization. No token was saved."
                        )
                self._authorization_code(codes[0])
            finally:
                if browser.is_connected():
                    browser.close()

    def _default_headers(
        self,
        requiresCountryAndStoreID=False,
        store_id="",
        iscoupons=False,
        ishome=False,
        requiresStoreIDs=False,
    ):
        if (
            not self._token
            or self._expires is None
            or datetime.now(timezone.utc) >= self._expires
        ):
            if not self._refresh_token:
                raise MissingLogin("You need to login!")
            self._renew_token()
        if not self._token:
            raise MissingLogin("You need to login!")
        if requiresCountryAndStoreID:
            return {
                "Authorization": f"Bearer {self._token}",
                "App-Version": "16.43.4",
                "Operating-System": "Android",
                "App": "com.lidl.eci.lidlplus",
                "Accept-Language": self._language,
                "User-Agent": "okhttp/5.4.0",
                "OS-Version": "16",
                "Model": "sdk_gphone64_x86_64",
                "Brand": "Google",
                "deviceid": "f7a31c44276c0651",
                "country": self._country,
                "store-id": store_id,
            }
        elif iscoupons:
            return {
                "Authorization": f"Bearer {self._token}",
                "App-Version": "16.43.4",
                "Operating-System": "Android",
                "App": "com.lidl.eci.lidlplus",
                "Accept-Language": self._language,
                "User-Agent": "okhttp/5.4.0",
                "OS-Version": "16",
                "Model": "sdk_gphone64_x86_64",
                "Brand": "Google",
                "deviceid": "f7a31c44276c0651",
                "country": self._country,
                "action-location": "app_couponslist",
            }
        elif ishome:
            return {
                "Authorization": f"Bearer {self._token}",
                "App-Version": "16.43.4",
                "Operating-System": "Android",
                "App": "com.lidl.eci.lidlplus",
                "Accept-Language": self._language,
                "User-Agent": "okhttp/5.4.0",
                "OS-Version": "16",
                "Model": "sdk_gphone64_x86_64",
                "Brand": "Google",
                "deviceid": "f7a31c44276c0651",
                "homeId": "mainHome",
                "currentDate": datetime.now().isoformat(),
                "Host": "home.lidlplus.com",
                "isOptionalUpdate": "false",
                "IsPushEnabled": "false",
                "IsOneApp": "false",
                "SessionId": str(uuid.uuid4()),
            }
        elif requiresStoreIDs:
            return {
                "Authorization": f"Bearer {self._token}",
                "App-Version": "16.43.4",
                "Operating-System": "Android",
                "App": "com.lidl.eci.lidlplus",
                "Accept-Language": self._language,
                "User-Agent": "okhttp/5.4.0",
                "OS-Version": "16",
                "Model": "sdk_gphone64_x86_64",
                "Brand": "Google",
                "deviceid": "f7a31c44276c0651",
                "storesIds": store_id,
            }
        else:
            return {
                "Authorization": f"Bearer {self._token}",
                "App-Version": "16.43.4",
                "Operating-System": "Android",
                "App": "com.lidl.eci.lidlplus",
                "Accept-Language": self._language,
                "User-Agent": "okhttp/5.4.0",
                "OS-Version": "16",
                "Model": "sdk_gphone64_x86_64",
                "Brand": "Google",
                "deviceid": "f7a31c44276c0651",
                "Date": datetime.now(ZoneInfo("Europe/Budapest")).strftime(
                    "%a, %d %b %Y %H:%M:%S GMT"
                ),
            }

    def receipts(self, only_favorite=False, pageNumber=1):
        url = f"https://tickets.lidlplus.com/api/v2/{self._country}/tickets"
        response = requests.get(
            url,
            headers=self._default_headers(),
            timeout=30,
            params={
                "pageNumber": pageNumber,
                "onlyFavorite": str(only_favorite).lower(),
            },
        )
        response.raise_for_status()
        return response.json()

    def iter_receipts(self, *, period=None, since=None, until=None):
        """Yield receipt summaries in an inclusive date range, following every page."""
        dates = resolve_date_range(period=period, since=since, until=until)
        seen = set()
        page = 1
        while True:
            payload = self.receipts(pageNumber=page)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("tickets"), list
            ):
                raise ValueError("Unexpected receipt list response (expected tickets).")
            tickets = payload["tickets"]
            if not tickets:
                if len(seen) < payload.get("totalCount", 0):
                    raise ValueError(
                        "Receipt listing ended before totalCount was reached."
                    )
                return
            added = 0
            for ticket in tickets:
                if (
                    not isinstance(ticket, dict)
                    or not isinstance(ticket.get("id"), str)
                    or not ticket["id"]
                ):
                    raise ValueError("Receipt summary has no valid id.")
                if ticket["id"] not in seen:
                    seen.add(ticket["id"])
                    added += 1
                    if dates.matches(ticket):
                        yield ticket
            if not added:
                raise ValueError(
                    "Receipt API repeated a page; download stopped to avoid a loop."
                )
            if "totalCount" in payload and len(seen) >= payload["totalCount"]:
                return
            page += 1

    def receipt(self, ticket_id):
        from urllib.parse import quote

        url = f"https://tickets.lidlplus.com/api/v3/{self._country}/tickets/{quote(ticket_id, safe='')}"
        response = requests.get(url, headers=self._default_headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def coupons(self, store_id):
        url = "https://coupons.lidlplus.com/app/api/v4/promotionslist"

        kwargs = {
            "headers": self._default_headers(
                requiresCountryAndStoreID=True, store_id=store_id
            ),
            "timeout": 10,
        }

        # the lidl servers only returns personalised coupons if a "segment-ids" header is given
        segments = self.get_segments()
        if isinstance(segments, list):
            kwargs["headers"]["Segment-ids"] = ",".join(
                str(segment) for segment in segments
            )
        return requests.get(url, **kwargs).json()

    def get_segments(self):
        url = f"https://segments.lidlplus.com/api/v1/usersegments/{self._country}"
        return requests.get(url, headers=self._default_headers(), timeout=10).json()

    def activate_coupon(self, coupon_id):
        url = f"https://coupons.lidlplus.com/app/api/v2/promotions/{coupon_id}/activation"  # id in response
        kwargs = {"headers": self._default_headers(iscoupons=True), "timeout": 10}
        return (
            requests.post(url, json={"articleSelection": []}, **kwargs).status_code
            == 200
        )

    def deactivate_coupon(self, coupon_id):
        url = (
            f"https://coupons.lidlplus.com/app/api/v2/promotions/{coupon_id}/activation"
        )
        kwargs = {"headers": self._default_headers(iscoupons=True), "timeout": 10}
        return requests.delete(url, **kwargs).status_code == 200

    def start_couponplus(self, promotion_id="1870d40b-4249-42c0-a3b0-ea28c0df895e"):
        url = f"https://couponplus.lidlplus.com/api/v4/{self._country}/user/promotions/{promotion_id}/start"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.patch(url, **kwargs)

    def couponplus(self, store_id):
        url = "https://coupons.lidlplus.com/app/api/v2/promotions/titles?ids=4a770579-87b8-4e28-a8d0-22bc8d10b533&ids=38cff59c-72e8-43f6-9eee-b0b198946109&ids=05d8879d-b557-48f5-b028-330becd5df3e&ids=5ef13122-7427-47cf-bd75-ad1733eea001"
        kwargs = {
            "headers": self._default_headers(
                requiresCountryAndStoreID=True, store_id=store_id
            ),
            "timeout": 10,
        }
        return requests.get(url, **kwargs).text

    def purchaseLottery_details(self, coupon_id):
        url = f"https://purchaselottery.lidlplus.com/api/v1/{self._country}/lotteries/{coupon_id}"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.get(url, **kwargs).json()

    def redeem_purchaseLottery(self, coupon_id):
        url = f"https://purchaselottery.lidlplus.com/api/v1/{self._country}/lotteries/{coupon_id}/redeemed"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.patch(url, **kwargs).status_code == 202

    def purchaseLottery_status(self, coupon_id):
        url = f"https://purchaselottery.lidlplus.com/api/v1/{self._country}/lotteries/{coupon_id}/redeemed/status"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.get(url, **kwargs).text

    @property
    def loyalty_id(self, store_id=""):
        url = "https://profile.lidlplus.com/api/v1/upsertProfile"  # or https://profile.lidlplus.com/api/v1/updateCountryInfo
        kwargs = {
            "json": {
                "country_code": self._country,
                "language": self._country,
                "store_id": store_id,
            },
            "headers": self._default_headers(),
            "timeout": 10,
        }
        return requests.post(url, **kwargs).json()["loyaltyId"]

    def generate_loyalty_qr(self):
        img = qrcode.make(self.loyalty_id)
        img.save("loyalty_id.png")

    def get_stores(self):
        url = f"https://stores.lidlplus.com/api/v4/{self._country}"
        return requests.get(url).json()

    def offers(self, store_id):
        url = (
            f"https://offers.lidlplus.com/app/api/v4/{self._country}/{store_id}/offers"
        )
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.get(url, **kwargs).json()

    def brochures(self, store_id):
        url = f"https://brochures.lidlplus.com/api/v2/{self._country}/Brochures?store={store_id}"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.get(url, **kwargs).json()

    def translations(self):
        url = f"https://profile.lidl.com/api/translations?country={self._language}&language={self._language}-{self._country}"
        return requests.get(url).json()

    def activecoupons_count(self, store_id):
        url = "https://coupons.lidlplus.com/app/api/v1/promotionscount"
        kwargs = {
            "headers": self._default_headers(
                requiresCountryAndStoreID=True, store_id=store_id
            ),
            "timeout": 10,
        }
        return requests.get(url, **kwargs).json()

    def home(self, store_id):
        url = f"https://home.lidlplus.com/api/v2/{self._country}/home/logged"
        kwargs = {"headers": self._default_headers(ishome=True), "timeout": 10}
        return requests.post(
            url,
            json={
                "storeId": store_id,
                "modules": [
                    {"moduleName": "homeMessage", "aggregateVersion": 1},
                    {"moduleName": "usualStoreV2", "aggregateVersion": 1},
                    {"moduleName": "businessModels", "aggregateVersion": 1},
                    {"moduleName": "tipcards", "aggregateVersion": 1},
                    {"moduleName": "banners", "aggregateVersion": 1},
                    {"moduleName": "selfScanningSession", "aggregateVersion": 1},
                    {"moduleName": "clickAndPickOrderStatus", "aggregateVersion": 1},
                    {"moduleName": "clickAndPick", "aggregateVersion": 1},
                    {"moduleName": "stampCardLottery", "aggregateVersion": 1},
                    {"moduleName": "stampCardRewards", "aggregateVersion": 1},
                    {"moduleName": "stampCardBenefits", "aggregateVersion": 2},
                    {"moduleName": "purchaseLottery", "aggregateVersion": 1},
                    {"moduleName": "openGift", "aggregateVersion": 1},
                    {"moduleName": "couponPlus", "aggregateVersion": 3},
                    {"moduleName": "promotions", "aggregateVersion": 4},
                    {"moduleName": "bannersVideo", "aggregateVersion": 1},
                    {"moduleName": "offers", "aggregateVersion": 4},
                    {"moduleName": "WorldOfNeeds", "aggregateVersion": 1},
                    {"moduleName": "digitalLeafletv2_combined", "aggregateVersion": 1},
                    {"moduleName": "partnersBenefits", "aggregateVersion": 1},
                    {"moduleName": "featuredProducts", "aggregateVersion": 1},
                    {"moduleName": "recipes", "aggregateVersion": 1},
                    {"moduleName": "homeLegalDisclaimer", "aggregateVersion": 1},
                    {"moduleName": "usualStoreHeader", "aggregateVersion": 1},
                ],
            },
            **kwargs,
        ).json()

    def store_schedule(self, store_id):
        url = f"https://stores.lidlplus.com/api/v3/schedule/{store_id}?date={datetime.now().strftime('%Y-%m-%d')}"
        kwargs = {"headers": self._default_headers(), "timeout": 10}
        return requests.get(url, **kwargs).json()

    def store_details(self, store_id):
        url = f"https://stores.lidlplus.com/api/v5/{self._country}/details"
        kwargs = {
            "headers": self._default_headers(requiresStoreIDs=True, store_id=store_id),
            "timeout": 10,
        }
        return requests.get(url, **kwargs).json()

    def shoppinglists(self, store_id):
        url = "https://shopping-list.lidlplus.com/api/v4/lists"
        kwargs = {
            "headers": self._default_headers(
                requiresCountryAndStoreID=True, store_id=store_id
            ),
            "timeout": 10,
        }
        return requests.get(url, **kwargs).json()

    def shoppinglist(
        self,
        shoppinglist_id,
        store_id,
        body={"items": [], "list": {"itemIds": [], "deletedItems": []}},
    ):
        url = f"https://shopping-list.lidlplus.com/api/v4/lists/{shoppinglist_id}"
        kwargs = {
            "headers": self._default_headers(
                requiresCountryAndStoreID=True, store_id=store_id
            ),
            "timeout": 10,
        }
        return requests.patch(url, json=body, **kwargs).json()

    # def get_coupon_details(self, coupon_id):
    #    url = f"https://coupons.lidlplus.com/app/api/v4/promotionsdetails/{coupon_id}"
    #    kwargs = {"headers": self._default_headers(requiresCountryAndStoreID=True), "timeout": 10}
    #    return requests.get(url, **kwargs).text


class MissingLogin(RuntimeError):
    """Authentication is required before querying the API."""
