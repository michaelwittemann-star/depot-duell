"""Einmalige Anmeldung bei Parqet Connect (OAuth 2.0 mit PKCE) - lokal ausfuehren, liefert den Refresh Token.

Vorher im Parqet Developer Hub (https://developer.parqet.com) eine private Integration anlegen:
  Name frei, Scope "portfolio:write" (und "portfolio:read"), Redirect-URI genau  http://localhost:8765/callback
Dann:  python parqet_auth.py <CLIENT_ID>
Der Browser oeffnet sich, du bestaetigst den Zugriff, danach steht der Refresh Token im Terminal.
Dieser Token gehoert als GitHub-Secret PARQET_REFRESH_TOKEN ins Repository (zusammen mit PARQET_CLIENT_ID).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests

AUTH = "https://connect.parqet.com/oauth2/authorize"
TOKEN = "https://connect.parqet.com/oauth2/token"
REDIRECT = "http://localhost:8765/callback"
SCOPE = "portfolio:read portfolio:write"


def main(client_id: str) -> None:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                        # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Fertig. Du kannst dieses Fenster schliessen.</h2>".encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):                            # Ausgabe unterdruecken
            return

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    print("Oeffne den Browser zur Anmeldung ...\n", url)
    webbrowser.open(url)
    server.serve_forever()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"Keine gueltige Antwort erhalten: {got}")
    r = requests.post(TOKEN, data={"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
                                   "client_id": client_id, "code_verifier": verifier}, timeout=30)
    r.raise_for_status()
    tok = r.json()
    print("\nRefresh Token (als GitHub-Secret PARQET_REFRESH_TOKEN hinterlegen):\n")
    print(tok.get("refresh_token"))
    print("\nAccess Token laeuft ab in", tok.get("expires_in"), "Sekunden; Scopes:", tok.get("scope"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Aufruf: python parqet_auth.py <CLIENT_ID>")
    main(sys.argv[1])
