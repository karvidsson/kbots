#!/usr/bin/env python3
"""Re-authorize Google OAuth with updated scopes.

Generates a new authorization URL. After granting consent in browser,
paste the authorization code to get a new refresh token.

Usage:
    python scripts/google-reauth.py [--account NAME]

With --account NAME the consent is stored as a separate named identity
(vault key GOOGLE_REFRESH_TOKEN__NAME) using the same OAuth client — run
the browser consent logged in as that Google account. Agents opt into a
named identity via KBOTS_GOOGLE_ACCOUNT in their .mcp.json env.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost:8085"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="",
                    help="store consent as a named identity (GOOGLE_REFRESH_TOKEN__<name>)")
    account = ap.parse_args().account
    suffix = f"__{account}" if account else ""

    from src.core.base import resolve_vault_key_file
    from src.vault.fernet import FernetVault

    vault = FernetVault()
    key_file = resolve_vault_key_file()
    vault.unlock(key_file.read_text().strip())

    # Get existing credentials
    creds_json = vault.get("secrets/google-api-credentials.json")
    if not creds_json:
        print("ERROR: No Google credentials in vault")
        sys.exit(1)

    creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
    installed = creds.get("installed", creds)
    client_id = installed["client_id"]
    client_secret = installed["client_secret"]

    print("Current scopes requested:")
    for s in SCOPES:
        print(f"  - {s}")
    print("\nNEW scope: gmail.settings.sharing (for forwarding address management)\n")

    # Generate auth URL
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    if account:
        print(f"Storing consent as named identity: {account!r}")
        print("IMPORTANT: complete the consent logged in as THAT Google account.\n")
    print("Open this URL in your browser and grant access:\n")
    print(auth_url)
    print()
    print("After granting access, your browser will redirect to localhost:8085.")
    print("Waiting for the redirect...\n")

    # Start a local HTTP server to catch the OAuth redirect
    from http.server import BaseHTTPRequestHandler, HTTPServer
    auth_code = None

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if auth_code:
                self.wfile.write(b"<h1>Authorization successful!</h1><p>You can close this tab.</p>")
            else:
                error = qs.get("error", ["unknown"])[0]
                self.wfile.write(f"<h1>Authorization failed: {error}</h1>".encode())

        def log_message(self, format, *args):
            pass  # Suppress HTTP log noise

    server = HTTPServer(("0.0.0.0", 8085), OAuthHandler)
    server.timeout = 300  # 5 min timeout
    server.handle_request()

    code = auth_code
    if not code:
        print("No authorization code received, aborting.")
        sys.exit(1)

    print("Authorization code received.")

    # Exchange code for tokens
    token_data = urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()

    req = Request(GOOGLE_TOKEN_URL, data=token_data,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})

    with urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())

    if "refresh_token" not in result:
        print("ERROR: No refresh token in response. Try revoking access first at:")
        print("  https://myaccount.google.com/permissions")
        sys.exit(1)

    new_refresh_token = result["refresh_token"]
    print(f"\nNew refresh token obtained (starts with: {new_refresh_token[:10]}...)")

    # Update vault
    if account:
        vault.set(f"GOOGLE_REFRESH_TOKEN{suffix}", new_refresh_token)
        print(f"Vault updated: GOOGLE_REFRESH_TOKEN{suffix}")
        print(f"Point the agent at it with KBOTS_GOOGLE_ACCOUNT={account} in its .mcp.json env.")
    else:
        creds["refresh_token"] = new_refresh_token
        if "installed" in creds:
            creds["installed"]["refresh_token"] = new_refresh_token
        vault.set("secrets/google-api-credentials.json", json.dumps(creds))
        vault.set("GOOGLE_REFRESH_TOKEN", new_refresh_token)
        print("Vault updated with new refresh token.")
        print("Scopes now include gmail.settings.sharing — forwarding setup will work.")


if __name__ == "__main__":
    main()
