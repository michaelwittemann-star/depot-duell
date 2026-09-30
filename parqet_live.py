"""Stuendlicher Zwischenstand fuer Parqet: aktueller Depotwert aus Live-Kursen, als neuer Kurs der Position.

Nutzt die Bestaende aus docs/data.json (Stueckzahlen des Masterplans) und die aktuellen Boersenpreise von Yahoo.
Waehrend der Handelszeit ist das der laufende Wert, ausserhalb der letzte Schlusskurs. Die Tagesreihe selbst
schreibt weiterhin update.py/parqet_sync.py nach Boersenschluss.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import parqet_sync as ps
from update import CAPITAL, PRODUCTS

DATA = Path(__file__).parent / "docs" / "data.json"


def live_preis(ticker: str) -> float | None:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"range": "1d", "interval": "1d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return (r.json()["chart"]["result"][0].get("meta") or {}).get("regularMarketPrice")


def main() -> None:
    if not os.environ.get("PARQET_CLIENT_ID") or not os.environ.get("PARQET_REFRESH_TOKEN"):
        print("Parqet: keine Zugangsdaten hinterlegt - uebersprungen")
        return
    data = json.loads(DATA.read_text(encoding="utf-8"))
    bestand = [h for h in data.get("holdings", []) if h["depot"] == "Masterplan" and h["product"] != "CASH"]
    if not bestand:
        print("Noch keine Positionen - uebersprungen")
        return
    wert, fehlend = 0.0, []
    for h in bestand:
        preis = live_preis(PRODUCTS[h["product"]]["ticker"])
        if preis is None:
            fehlend.append(h["product"])
            preis = h["price"]                       # Notfall: letzter bekannter Schlusskurs
        wert += h["units"] * preis
    wert += next((h["value"] for h in data["holdings"] if h["depot"] == "Masterplan" and h["product"] == "CASH"), 0.0)
    anteilswert = round(wert / CAPITAL * 100, 4)

    tok = ps.token()
    pid = os.environ.get("PARQET_PORTFOLIO_ID") or sorted(
        ps.api("GET", "/portfolios", tok)["items"], key=lambda p: p.get("createdAt", ""))[0]["id"]
    hid = os.environ.get("PARQET_HOLDING_ID")
    if not hid:
        holdings = ps.api("GET", f"/portfolios/{pid}/holdings", tok)["items"]
        treffer = [h for h in holdings if (h.get("asset") or {}).get("type") == "custom"]
        if not treffer:
            print("Position in Parqet nicht gefunden - erst parqet_sync.py laufen lassen")
            return
        hid = treffer[0]["id"]
    stand = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:00.000Z")
    ps.api("POST", f"/portfolios/{pid}/quotes/user-managed", tok,
           json={"identifier": {"type": "holdingId", "value": hid},
                 "quotes": [{"currency": "EUR", "datetime": stand, "price": anteilswert}]})
    hinweis = f" (Kurse fehlten fuer {', '.join(fehlend)})" if fehlend else ""
    print(f"Parqet-Zwischenstand {stand[11:16]} UTC: {wert:,.2f} EUR = Anteilswert {anteilswert}{hinweis}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(0)            # Parqet-Fehler duerfen nichts blockieren
