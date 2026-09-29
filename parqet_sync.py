"""Schiebt den taeglichen Wert des Masterplans als eigene Position "Masterfonds" nach Parqet.

Der Masterplan wird dort wie ein Fonds gefuehrt: Anteilswert startet bei 100 EUR, die Stueckzahl ergibt sich
aus dem eingesetzten Kapital (16.500 EUR -> 165 Anteile). Eine einzige Kauf-Buchung am Starttag, danach nur noch
taegliche Kurse. So sieht man in Parqet eine geschlossene, langlaufende Position mit der Rendite des Plans.

Braucht die Umgebungsvariablen PARQET_CLIENT_ID und PARQET_REFRESH_TOKEN (GitHub-Secrets); ohne sie passiert nichts.
Anmeldung einmalig mit parqet_auth.py erzeugen.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import requests
from datetime import datetime, timedelta, timezone

BASE = "https://connect.parqet.com"
TOKEN_URL = f"{BASE}/oauth2/token"
TRACK = Path(__file__).parent / "docs" / "masterfonds.csv"
TOKENFILE = Path(__file__).parent / ".parqet_token"      # zuletzt gueltiger Refresh Token (nicht im Repo)
HOLDING_NAME = "Michael Wittemann Premium Fonds"
HOLDING_ID = os.environ.get("PARQET_HOLDING_ID", "")       # feste Kennung: bleibt gueltig, auch wenn du umbenennst
EXTERNAL_ID = "masterplan-depot-duell"
PORTFOLIO_ID = os.environ.get("PARQET_PORTFOLIO_ID", "")   # bestehendes Parqet-Depot (Basis-Tarif erlaubt nur eines)
BASE_QUOTE = 100.0                      # Anteilswert am Starttag


def zeitpunkt(tag: str) -> str:
    """Boersenschluss des Tages, aber nie in der Zukunft (Parqet ignoriert kuenftige Kurse)."""
    ts = datetime.fromisoformat(tag).replace(hour=20, tzinfo=timezone.utc)
    jetzt = datetime.now(timezone.utc) - timedelta(minutes=10)
    return min(ts, jetzt).strftime("%Y-%m-%dT%H:%M:00.000Z")


def token() -> str:
    """Access Token holen. Parqet gibt dabei jedes Mal einen neuen Refresh Token aus, der gesichert werden muss."""
    alt = TOKENFILE.read_text(encoding="utf-8").strip() if TOKENFILE.exists() else os.environ["PARQET_REFRESH_TOKEN"]
    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": os.environ["PARQET_CLIENT_ID"],
                                       "refresh_token": alt}, timeout=30)
    if not r.ok and alt != os.environ["PARQET_REFRESH_TOKEN"]:          # gemerkter Token abgelaufen -> Secret probieren
        alt = os.environ["PARQET_REFRESH_TOKEN"]
        r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": os.environ["PARQET_CLIENT_ID"],
                                           "refresh_token": alt}, timeout=30)
    if not r.ok:
        raise SystemExit(f"Anmeldung bei Parqet fehlgeschlagen ({r.status_code}): {r.text[:300]}")
    data = r.json()
    neu = data.get("refresh_token")
    if neu and neu != alt:
        TOKENFILE.write_text(neu, encoding="utf-8")
        save_secret(neu)
    return data["access_token"]


def save_secret(neu: str) -> None:
    """Neuen Refresh Token als GitHub-Secret sichern (braucht PARQET_TOKEN_PAT mit Secrets-Schreibrecht)."""
    pat, repo = os.environ.get("PARQET_TOKEN_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if not pat or not repo:
        print("Neuer Refresh Token liegt in .parqet_token; ohne PARQET_TOKEN_PAT bitte das GitHub-Secret von Hand setzen.")
        return
    res = subprocess.run(["gh", "secret", "set", "PARQET_REFRESH_TOKEN", "--repo", repo, "--body", neu],
                         env={**os.environ, "GH_TOKEN": pat}, capture_output=True, text=True)
    print("Refresh Token im Secret aktualisiert" if res.returncode == 0 else f"Secret-Update fehlgeschlagen: {res.stderr[:200]}")


def api(method: str, path: str, tok: str, **kw):
    r = requests.request(method, BASE + path, headers={"Authorization": f"Bearer {tok}"}, timeout=30, **kw)
    if not r.ok:
        raise SystemExit(f"{method} {path} fehlgeschlagen ({r.status_code}): {r.text[:400]}")
    return r.json() if r.content else {}


def read_track() -> list[dict]:
    with TRACK.open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def main() -> None:
    if not os.environ.get("PARQET_CLIENT_ID") or not os.environ.get("PARQET_REFRESH_TOKEN"):
        print("Parqet: keine Zugangsdaten hinterlegt - uebersprungen")
        return
    rows = read_track()
    if not rows:
        print("Parqet: noch keine Tageswerte")
        return
    tok = token()

    portfolios = api("GET", "/portfolios", tok).get("items", [])
    if PORTFOLIO_ID:
        pid = PORTFOLIO_ID
    elif portfolios:                                   # sonst das aelteste (Haupt-)Depot verwenden
        pid = sorted(portfolios, key=lambda p: p.get("createdAt", ""))[0]["id"]
    else:
        pid = api("POST", "/portfolios", tok, json={"name": HOLDING_NAME})["id"]
        print("Depot in Parqet angelegt:", pid)

    holdings = api("GET", f"/portfolios/{pid}/holdings", tok).get("items", [])
    hold = next((h for h in holdings if h.get("id") == HOLDING_ID), None) if HOLDING_ID else None
    if hold is None:
        hold = next((h for h in holdings if HOLDING_NAME in (h.get("nickname"), h.get("name"))
                     and (h.get("asset") or {}).get("type") == "custom"), None)
    if hold is None:
        hold = api("POST", f"/portfolios/{pid}/holdings/custom", tok,
                   json={"name": HOLDING_NAME, "assetProduct": "other", "externalId": EXTERNAL_ID,
                         "quotes": [{"currency": "EUR", "datetime": zeitpunkt(rows[0]["datum"]),
                                     "price": BASE_QUOTE}]})
        print("Position in Parqet angelegt:", hold.get("id"))
    hid = hold.get("id")

    antwort = api("GET", f"/portfolios/{pid}/activities", tok)
    activities = antwort.get("activities") or antwort.get("items") or []
    schon = any((a.get("holding") or a.get("holdingId") or a.get("holding_id")) == hid
                or a.get("externalId") == f"{EXTERNAL_ID}-start" for a in activities)
    if not schon:
        anteile = round(float(rows[0]["masterfonds_wert"]) / BASE_QUOTE, 6)
        api("POST", f"/portfolios/{pid}/activities", tok, json={"activities": [{
            "currency": "EUR", "datetime": zeitpunkt(rows[0]["datum"]), "shares": anteile,
            "price": BASE_QUOTE, "type": "buy", "assetIdentifierType": "custom_asset", "holding_id": hid,
            "description": "Start des Masterplans", "externalId": f"{EXTERNAL_ID}-start"}]})
        print(f"Kauf gebucht: {anteile} Anteile zu {BASE_QUOTE} EUR")

    quotes = [{"currency": "EUR", "datetime": zeitpunkt(r["datum"]),
               "price": round(float(r["masterfonds_anteilswert"]), 4)} for r in rows][-500:]
    api("POST", f"/portfolios/{pid}/quotes/user-managed", tok,
        json={"identifier": {"type": "holdingId", "value": hid}, "quotes": quotes})
    print(f"Parqet aktualisiert: {len(quotes)} Kurse, zuletzt {quotes[-1]['datetime'][:10]} = {quotes[-1]['price']} EUR "
          f"(Depotwert {rows[-1]['masterfonds_wert']} EUR)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(0)          # Parqet-Fehler duerfen den taeglichen Lauf nicht abbrechen
