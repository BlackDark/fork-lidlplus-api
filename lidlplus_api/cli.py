"""Interactive login and receipt archive commands."""

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile

from .api import LidlPlusApi
from .archive import convert_receipts, download_receipts
from .dates import resolve_date_range


def save_auth(path, api):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "country": api._country,
                    "language": api._language,
                    "refresh_token": api.refresh_token,
                },
                stream,
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(
        description="Archive Lidl Plus receipts as JSON, HTML, JPG and PDF."
    )
    parser.add_argument("--auth-file", type=Path, default=Path(".lidlplus/auth.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "install-browser", help="Install Chromium locally for login and export"
    )
    login = commands.add_parser("login", help="Log in through a visible browser")
    login.add_argument("--country", required=True, help="Account country, e.g. DE")
    login.add_argument("--language", default="de", help="Login language, e.g. de")
    login.add_argument(
        "--diagnostics",
        action="store_true",
        help="Show login stage and HTTP status without credentials",
    )
    download = commands.add_parser(
        "download", help="Download every available receipt and export JPG/PDF"
    )
    download.add_argument("--output", type=Path, default=Path("receipts"))
    download.add_argument(
        "--overwrite", action="store_true", help="Fetch receipt details again"
    )
    convert = commands.add_parser(
        "convert", help="Rebuild JPG/PDF from previously downloaded JSON"
    )
    convert.add_argument("--output", type=Path, default=Path("receipts"))
    for command in (download, convert):
        command.add_argument(
            "--period",
            choices=["this-month", "last-month", "this-year"],
            help="Select a calendar period using the local date",
        )
        command.add_argument(
            "--since", help="First purchase date, inclusive (YYYY-MM-DD)"
        )
        command.add_argument(
            "--until", help="Last purchase date, inclusive (YYYY-MM-DD)"
        )
    args = parser.parse_args()
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".lidlplus/browsers").resolve())
    )
    try:
        filters = {}
        if args.command in {"download", "convert"}:
            dates = resolve_date_range(
                period=args.period, since=args.since, until=args.until
            )
            filters = {"since": dates.since, "until": dates.until}
        if args.command == "install-browser":
            return subprocess.call(
                [sys.executable, "-m", "playwright", "install", "chromium"]
            )
        if args.command == "convert":
            print(f"Converted {convert_receipts(args.output, **filters)} receipts.")
        elif args.command == "login":
            api = LidlPlusApi(args.language, args.country)
            print(
                "Complete login and any verification in the browser (5 minute timeout)."
            )
            api.login(diagnostics=args.diagnostics)
            save_auth(args.auth_file, api)
            print(f"Login saved to {args.auth_file}. Keep this file private.")
        else:
            if not args.auth_file.exists():
                raise ValueError(
                    "Run `uv run lidlplus login --country DE --language de` first (use your account country)."
                )
            auth = json.loads(args.auth_file.read_text(encoding="utf-8"))
            api = LidlPlusApi(auth["language"], auth["country"], auth["refresh_token"])
            try:
                count, failures = download_receipts(
                    api, args.output, overwrite=args.overwrite, **filters
                )
            finally:
                # Refresh tokens may rotate even if a later receipt fails.
                save_auth(args.auth_file, api)
            print(
                f"Exported {count} receipts to {args.output}; {len(failures)} failed."
            )
            if failures:
                return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
