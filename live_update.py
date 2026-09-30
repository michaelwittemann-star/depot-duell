"""Stuendlicher Zwischenstand: aktuelle Werte aller drei Vergleichsdepots in docs/data.json, dazu der Kurs fuer Parqet.

Die Tagesreihe (Schlusskurse, Signale, Mail) schreibt weiterhin update.py nach Boersenschluss. Hier geht es nur um
den aktuellen Wert waehrend des Tages: Stueckzahlen aus data.json mal dem aktuellen Boersenpreis.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import parqet_sync as ps
from update import CAPITAL, PRODUCTS, SOLD_FUNDS

DATA = Path(__file__).parent / "docs" / "data.json"


def preis(ticker: str) -> float | None:
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"range": "1d", "interval": "1d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        return (r.json()["chart"]["result"][0].get("meta") or {}).get("regularMarketPrice")
    except Exception as error:  # noqa: BLE001
        print(f"{ticker}: kein aktueller Kurs ({error})")
        return None


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if not data.get("history"):
        print("Noch keine Bewertung - uebersprungen")
        return
    bestand = [h for h in data["holdings"] if h["product"] != "CASH"]
    kurse = {p: preis(PRODUCTS[p]["ticker"]) for p in {h["product"] for h in bestand}}
    alt = {h["product"]: h["price"] for h in bestand}

    def wert(depot: str) -> float:
        summe = sum(h["units"] * (kurse.get(h["product"]) or alt[h["product"]]) for h in bestand if h["depot"] == depot)
        return summe + next((h["value"] for h in data["holdings"] if h["depot"] == depot and h["product"] == "CASH"), 0.0)

    plan, msci = wert("Masterplan"), wert("MSCI World SRI")
    fonds = 0.0
    for tick, f in SOLD_FUNDS.items():
        kurs = preis(tick) or (data.get("funds", {}).get(tick) or {}).get("price")
        if kurs:
            fonds += f["units"] * kurs
    jetzt = datetime.now(timezone.utc)
    data["live"] = {"time": jetzt.isoformat(timespec="minutes"), "plan": round(plan, 2), "msci": round(msci, 2),
                    "funds": round(fonds, 2)}
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Zwischenstand {jetzt:%H:%M} UTC - Masterplan {plan:,.2f} | MSCI {msci:,.2f} | behaltene Fonds {fonds:,.2f}")

    if os.environ.get("PARQET_CLIENT_ID") and os.environ.get("PARQET_REFRESH_TOKEN"):
        tok = ps.token()
        pid = os.environ.get("PARQET_PORTFOLIO_ID") or sorted(
            ps.api("GET", "/portfolios", tok)["items"], key=lambda p: p.get("createdAt", ""))[0]["id"]
        hid = os.environ.get("PARQET_HOLDING_ID")
        if hid:
            stand = (jetzt - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:00.000Z")
            ps.api("POST", f"/portfolios/{pid}/quotes/user-managed", tok,
                   json={"identifier": {"type": "holdingId", "value": hid},
                         "quotes": [{"currency": "EUR", "datetime": stand, "price": round(plan / CAPITAL * 100, 4)}]})
            print(f"Parqet aktualisiert: Anteilswert {plan / CAPITAL * 100:.4f}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(0)
