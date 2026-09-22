"""Depot-Vergleich: MSCI World SRI gegen den Offensiv-Plan, Papierdepots ab START mit je 100.000 EUR.

Laeuft taeglich (GitHub Actions) und rechnet beide Depots bei jedem Lauf vollstaendig ab START neu
(deterministisch, kein gespeicherter Zustand). Ergebnis: docs/data.json fuer docs/index.html.

Regeln des Offensiv-Plans (Signale aus US-Schlusskursen, um Dividenden bereinigt):
  Topf B (50 %): QQQ ueber 200-Tage-Schnitt (Einstieg ueber +5 %, Ausstieg unter -5 %) UND 20-Tage-Vola von QQQ < 30 %
                 UND QQQ-Rendite ueber 126 Handelstage > 0  ->  3x S&P 500, sonst US-Staatsanleihen 20+ Jahre
  Topf A (30 %): SPY über 200-Tage-Schnitt (Einstieg ueber +5 %, Ausstieg unter -5 %)  ->  2x Nasdaq-100,
                 sonst je zur Haelfte US-Staatsanleihen 7-10 Jahre und Gold
  Topf G (20 %): immer Gold
  Handel: zur Eroeffnung des naechsten europaeischen Handelstags nach dem US-Schluss.
  Rebalancing: am ersten Handelstag jedes Jahres zurueck auf 50/30/20.
Positionen werden je Topf gefuehrt (Schluessel "Topf:Produkt"), damit Gold in Topf A und G getrennt bleibt.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

START = date.fromisoformat(os.environ.get("DEPOT_START", "2026-09-23"))
CAPITAL = 100_000.0
OUT = Path(__file__).parent / "docs" / "data.json"

PRODUCTS = {
    "MSCI": {"ticker": "2B7K.DE", "name": "iShares MSCI World SRI UCITS ETF (Acc)", "isin": "IE00BYX2JD69", "venue": "Xetra",
             "costs": "0,20 % p.a. (im Kurs enthalten)", "half_spread": 0.0005, "custody_pa": 0.0, "teilfreistellung": 0.30},
    "SP3X": {"ticker": "3USL.MI", "name": "WisdomTree S&P 500 3x Daily Leveraged", "isin": "IE00B7Y34M31", "venue": "Borsa Italiana",
             "costs": "0,75 % p.a. (im Kurs enthalten)", "half_spread": 0.0015, "custody_pa": 0.0, "teilfreistellung": 0.0},
    "UST20": {"ticker": "IS04.DE", "name": "iShares $ Treasury Bond 20+yr UCITS ETF (Dist)", "isin": "IE00BSKRJZ44", "venue": "Xetra",
              "costs": "0,07 % p.a. (im Kurs enthalten)", "half_spread": 0.0010, "custody_pa": 0.0, "teilfreistellung": 0.0},
    "NDX2X": {"ticker": "LQQ.PA", "name": "Amundi Nasdaq-100 Daily (2x) Leveraged UCITS ETF", "isin": "FR0010342592", "venue": "Euronext Paris",
              "costs": "0,60 % p.a. (im Kurs enthalten)", "half_spread": 0.0010, "custody_pa": 0.0, "teilfreistellung": 0.0},
    "UST710": {"ticker": "IUSM.DE", "name": "iShares $ Treasury Bond 7-10yr UCITS ETF (Dist)", "isin": "IE00B1FZS798", "venue": "Xetra",
               "costs": "0,07 % p.a. (im Kurs enthalten)", "half_spread": 0.0015, "custody_pa": 0.0, "teilfreistellung": 0.0},
    "GOLD": {"ticker": "4GLD.DE", "name": "Xetra-Gold", "isin": "DE000A0S9GB0", "venue": "Xetra",
             "costs": "0,30 % p.a. Verwahrentgelt (taeglich abgezogen)", "half_spread": 0.0005, "custody_pa": 0.003, "teilfreistellung": 0.0},
}
FEE_PCT, FEE_MIN = 0.0005, 3.0          # Annahme Interactive Brokers (Europa): 0,05 % je Order, mindestens 3 EUR
TAX = 0.26375                            # Abgeltungsteuer + Soli, ohne Kirchensteuer, ohne Sparer-Pauschbetrag
WEIGHTS = {"B": 0.5, "A": 0.3, "G": 0.2}
RULE_TEXT = {"B": ("Regel B an: QQQ-Trend, Vola und Momentum erfüllt", "Regel B aus: QQQ-Trend, Vola oder Momentum verletzt"),
             "A": ("Regel A an: SPY über 200-Tage-Schnitt (+5 %)", "Regel A aus: SPY unter 200-Tage-Schnitt (-5 %)")}


def fee(value: float) -> float:
    return max(FEE_MIN, FEE_PCT * value) if value > 0 else 0.0


def download(ticker: str, start: str, tries: int = 4) -> pd.DataFrame:
    for attempt in range(tries):
        try:
            df = yf.Ticker(ticker).history(start=start, auto_adjust=True)
            if len(df):
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                return df[~df.index.duplicated(keep="last")]
        except Exception as error:  # noqa: BLE001
            print(f"{ticker}: Versuch {attempt + 1} fehlgeschlagen: {error}")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Keine Kurse fuer {ticker}")


# --- Signale ---------------------------------------------------------------------------------------

def hysteresis(close: pd.Series, n: int = 200, band: float = 0.05) -> pd.Series:
    ma = close.rolling(n).mean()
    state, cur = [], 0
    for x, m in zip(close.values, ma.values):
        if not math.isnan(m):
            if cur == 0 and x > m * (1 + band):
                cur = 1
            elif cur == 1 and x < m * (1 - band):
                cur = 0
        state.append(cur)
    return pd.Series(state, index=close.index)


def signals() -> tuple[pd.DataFrame, dict]:
    q = download("QQQ", "2012-01-01")["Close"].dropna()
    s = download("SPY", "2012-01-01")["Close"].dropna()
    idx = q.index.intersection(s.index)
    q, s = q.loc[idx], s.loc[idx]
    vol = q.pct_change().rolling(20).std() * np.sqrt(252)
    mom = q / q.shift(126) - 1
    q_trend = hysteresis(q)
    b = (q_trend * (vol < 0.30) * (mom > 0)).astype(int)
    a = hysteresis(s).astype(int)
    detail = {
        "us_date": idx[-1].date().isoformat(),
        "qqq_close": round(float(q.iloc[-1]), 2), "qqq_ma200": round(float(q.rolling(200).mean().iloc[-1]), 2),
        "qqq_vol20": round(float(vol.iloc[-1]), 4), "qqq_mom126": round(float(mom.iloc[-1]), 4), "qqq_trend_on": int(q_trend.iloc[-1]),
        "spy_close": round(float(s.iloc[-1]), 2), "spy_ma200": round(float(s.rolling(200).mean().iloc[-1]), 2),
        "B": int(b.iloc[-1]), "A": int(a.iloc[-1]),
    }
    return pd.DataFrame({"B": b, "A": a}), detail


def target_assets(b_on: int, a_on: int) -> dict:
    return {"B": {"SP3X": 1.0} if b_on else {"UST20": 1.0},
            "A": {"NDX2X": 1.0} if a_on else {"UST710": 0.5, "GOLD": 0.5},
            "G": {"GOLD": 1.0}}


# --- Depot-Buchhaltung mit Steuer -----------------------------------------------------------------

@dataclass
class Lot:
    units: float
    cost: float          # Anschaffungskosten gesamt (inkl. Gebuehr)
    bought: date


@dataclass
class Depot:
    name: str
    cash: float = CAPITAL
    lots: dict = field(default_factory=dict)       # "Topf:Produkt" -> list[Lot]
    loss_pot: float = 0.0
    taxes_paid: float = 0.0
    fees_paid: float = 0.0
    trades: list = field(default_factory=list)

    def units(self, key: str) -> float:
        return sum(l.units for l in self.lots.get(key, []))

    def keys(self, sleeve: str) -> list[str]:
        return [k for k in self.lots if k.startswith(sleeve + ":") and self.units(k) > 1e-12]

    def buy(self, key: str, amount: float, price: float, day: date, reason: str):
        """amount = eingesetzter Betrag inkl. Gebuehr."""
        if amount <= 1:
            return
        sleeve, prod = key.split(":")
        p = price * (1 + PRODUCTS[prod]["half_spread"])
        f = fee(amount)
        units = (amount - f) / p
        self.lots.setdefault(key, []).append(Lot(units, amount, day))
        self.cash -= amount
        self.fees_paid += f
        self.trades.append({"date": day.isoformat(), "depot": self.name, "action": "Kauf", "sleeve": sleeve, "product": prod,
                            "units": round(units, 4), "price": round(p, 4), "value": round(amount, 2), "fee": round(f, 2),
                            "tax": 0.0, "reason": reason})

    def sell(self, key: str, fraction: float, price: float, day: date, reason: str) -> float:
        """Verkauft den Anteil `fraction` der Position (FIFO), versteuert den Gewinn, gibt den Nettoerloes zurueck."""
        lots = self.lots.get(key, [])
        total_units = sum(l.units for l in lots)
        if total_units <= 0 or fraction <= 0:
            return 0.0
        sleeve, prod = key.split(":")
        to_sell = total_units * min(fraction, 1.0)
        p = price * (1 - PRODUCTS[prod]["half_spread"])
        gross = to_sell * p
        f = fee(gross)
        taxable, remaining, new_lots = 0.0, to_sell, []
        for lot in lots:
            if remaining <= 1e-12:
                new_lots.append(lot)
                continue
            take = min(lot.units, remaining)
            cost = lot.cost * take / lot.units
            gain = take * p - f * take / to_sell - cost
            if prod == "GOLD" and (day - lot.bought).days > 365:
                gain = 0.0                                   # Gold-ETC nach einem Jahr steuerfrei
            taxable += gain * (1 - PRODUCTS[prod]["teilfreistellung"])
            if take < lot.units - 1e-12:
                new_lots.append(Lot(lot.units - take, lot.cost - cost, lot.bought))
            remaining -= take
        self.lots[key] = new_lots
        tax = self._tax(taxable)
        net = gross - f - tax
        self.cash += net
        self.fees_paid += f
        self.trades.append({"date": day.isoformat(), "depot": self.name, "action": "Verkauf", "sleeve": sleeve, "product": prod,
                            "units": round(to_sell, 4), "price": round(p, 4), "value": round(gross, 2), "fee": round(f, 2),
                            "tax": round(tax, 2), "reason": reason})
        return net

    def _tax(self, gain: float) -> float:
        total = gain + self.loss_pot
        if total <= 0:
            self.loss_pot = total
            return 0.0
        self.loss_pot = 0.0
        tax = total * TAX
        self.taxes_paid += tax
        return tax

    def value(self, prices: dict) -> float:
        return self.cash + sum(self.units(k) * prices[k.split(":")[1]] for k in self.lots)

    def sleeve_value(self, sleeve: str, prices: dict) -> float:
        return sum(self.units(k) * prices[k.split(":")[1]] for k in self.keys(sleeve))

    def value_after_tax(self, prices: dict, day: date) -> float:
        """Wert nach gedachtem Verkauf aller Positionen heute (Steuer auf unrealisierte Gewinne, Verlusttopf verrechnet)."""
        gain = self.loss_pot
        for key, lots in self.lots.items():
            prod = key.split(":")[1]
            for lot in lots:
                g = lot.units * prices[prod] * (1 - PRODUCTS[prod]["half_spread"]) - lot.cost
                if prod == "GOLD" and (day - lot.bought).days > 365:
                    g = 0.0
                gain += g * (1 - PRODUCTS[prod]["teilfreistellung"])
        return self.value(prices) - max(gain, 0.0) * TAX

    def custody(self, prices: dict, days: int):
        for key, lots in self.lots.items():
            prod = key.split(":")[1]
            rate = PRODUCTS[prod]["custody_pa"]
            if rate and days > 0:
                charge = sum(l.units for l in lots) * prices[prod] * rate * days / 365
                self.cash -= charge
                self.fees_paid += charge


# --- Simulation ------------------------------------------------------------------------------------

def main():
    sig, detail = signals()
    first = (pd.Timestamp(START) - pd.Timedelta(days=10)).date().isoformat()
    raw = {k: download(p["ticker"], first) for k, p in PRODUCTS.items()}
    opens = pd.DataFrame({k: v["Open"].where(v["Open"] > 0) for k, v in raw.items()}).sort_index()
    closes = pd.DataFrame({k: v["Close"].where(v["Close"] > 0) for k, v in raw.items()}).sort_index().ffill()
    opens = opens.fillna(closes.shift(1)).fillna(closes)          # fehlende Eroeffnung: Vortagesschluss
    eu_days = [d for d in raw["MSCI"].dropna(subset=["Close"]).index if d.date() >= START]

    msci, plan = Depot("MSCI World SRI"), Depot("Offensiv-Plan")
    state, history, last_day = None, [], None
    for day_ts in eu_days:
        day = day_ts.date()
        prior = sig[sig.index < day_ts]
        if not len(prior):
            continue
        b_on, a_on = int(prior["B"].iloc[-1]), int(prior["A"].iloc[-1])
        po = {k: float(opens.loc[day_ts, k]) for k in PRODUCTS}
        if last_day is not None:
            plan.custody(po, (day - last_day).days)
        tgt = target_assets(b_on, a_on)

        if state is None:                                          # Start
            msci.buy("M:MSCI", msci.cash, po["MSCI"], day, "Start: 100 % MSCI World SRI")
            for sleeve, w in WEIGHTS.items():
                note = {"B": " – " + RULE_TEXT["B"][0 if b_on else 1], "A": " – " + RULE_TEXT["A"][0 if a_on else 1], "G": ""}[sleeve]
                for prod, share in tgt[sleeve].items():
                    plan.buy(f"{sleeve}:{prod}", CAPITAL * w * share, po[prod], day, f"Start: Topf {sleeve} ({int(w * 100)} %){note}")
            state = {"B": b_on, "A": a_on}
        else:
            for sleeve, now in (("B", b_on), ("A", a_on)):
                if now == state[sleeve]:
                    continue
                reason = RULE_TEXT[sleeve][0 if now else 1]
                before = plan.cash
                for key in plan.keys(sleeve):
                    plan.sell(key, 1.0, po[key.split(":")[1]], day, reason)
                proceeds = plan.cash - before
                for prod, share in tgt[sleeve].items():
                    plan.buy(f"{sleeve}:{prod}", proceeds * share, po[prod], day, reason)
                state[sleeve] = now
            if day.year != last_day.year:                          # jaehrliches Rebalancing auf 50/30/20
                total = plan.value(po)
                for sleeve, w in WEIGHTS.items():
                    v = plan.sleeve_value(sleeve, po)
                    if v - total * w > 0.005 * total:
                        for key in plan.keys(sleeve):
                            plan.sell(key, (v - total * w) / v, po[key.split(":")[1]], day,
                                      f"Jahres-Rebalancing: Topf {sleeve} zurück auf {int(w * 100)} %")
                under = {s: max(total * w - plan.sleeve_value(s, po), 0.0) for s, w in WEIGHTS.items()}
                su, avail = sum(under.values()), plan.cash
                for sleeve, u in under.items():
                    if su > 0 and u > 0.005 * total:
                        for prod, share in tgt[sleeve].items():
                            plan.buy(f"{sleeve}:{prod}", avail * u / su * share, po[prod], day,
                                     f"Jahres-Rebalancing: Topf {sleeve} zurück auf {int(WEIGHTS[sleeve] * 100)} %")

        pc = {k: float(closes.loc[day_ts, k]) for k in PRODUCTS}
        history.append({"date": day.isoformat(), "msci": round(msci.value(pc), 2), "plan": round(plan.value(pc), 2),
                        "msci_after_tax": round(msci.value_after_tax(pc, day), 2), "plan_after_tax": round(plan.value_after_tax(pc, day), 2),
                        "B": b_on, "A": a_on})
        last_day = day

    # Was ist am naechsten Morgen zu tun? (Signal des letzten US-Schlusses gegen den aktuellen Bestand)
    b_now, a_now = detail["B"], detail["A"]
    tgt_now = target_assets(b_now, a_now)
    tomorrow = []
    if state is None:
        tomorrow.append({"depot": "MSCI World SRI", "text": f"100.000 EUR in {PRODUCTS['MSCI']['name']} ({PRODUCTS['MSCI']['venue']}) kaufen"})
        for sleeve, w in WEIGHTS.items():
            for prod, share in tgt_now[sleeve].items():
                amount = f"{CAPITAL * w * share:,.0f}".replace(",", ".")
                tomorrow.append({"depot": "Offensiv-Plan", "text": f"Topf {sleeve}: {amount} EUR in {PRODUCTS[prod]['name']} "
                                 f"({PRODUCTS[prod]['venue']}, {PRODUCTS[prod]['isin']}) kaufen"})
    else:
        for sleeve, now in (("B", b_now), ("A", a_now)):
            if now != state[sleeve]:
                old = " + ".join(PRODUCTS[p]["name"] for p in target_assets(state["B"], state["A"])[sleeve])
                new = " + ".join(PRODUCTS[p]["name"] for p in tgt_now[sleeve])
                tomorrow.append({"depot": "Offensiv-Plan", "text": f"Topf {sleeve}: {old} verkaufen, Erlös in {new} ({RULE_TEXT[sleeve][0 if now else 1]})"})

    pc_last = {k: float(closes[k].iloc[-1]) for k in PRODUCTS}
    holdings = []
    for depot in (msci, plan):
        for key in depot.lots:
            u = depot.units(key)
            if u > 1e-9:
                sleeve, prod = key.split(":")
                holdings.append({"depot": depot.name, "sleeve": sleeve, "product": prod, "units": round(u, 4),
                                 "price": round(pc_last[prod], 4), "value": round(u * pc_last[prod], 2)})
        if abs(depot.cash) > 0.5:
            holdings.append({"depot": depot.name, "sleeve": "", "product": "CASH", "units": None, "price": None, "value": round(depot.cash, 2)})

    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "start": START.isoformat(), "capital": CAPITAL, "weights": WEIGHTS,
        "signal": detail, "tomorrow": tomorrow, "state": state,
        "history": history, "trades": msci.trades + plan.trades, "holdings": holdings,
        "totals": {d.name: {"fees": round(d.fees_paid, 2), "taxes": round(d.taxes_paid, 2), "loss_pot": round(d.loss_pot, 2)} for d in (msci, plan)},
        "products": PRODUCTS,
        "assumptions": {
            "fee": "0,05 % je Order, mindestens 3 EUR (Annahme Interactive Brokers, Europa)",
            "tax": "26,375 % Abgeltungsteuer und Soli auf jeden realisierten Gewinn, Verlustverrechnung, kein Sparer-Pauschbetrag, keine Kirchensteuer. "
                   "MSCI-Fonds 30 % Teilfreistellung, Plan-Produkte ohne. Xetra-Gold nach einem Jahr steuerfrei. "
                   "Vorabpauschale und Steuer auf Ausschuettungen nicht beruecksichtigt.",
            "prices": "Kurse von Yahoo Finance, um Ausschuettungen bereinigt. Handel zum Eroeffnungskurs plus/minus halbem Spread (je Produkt geschaetzt).",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(history)} Handelstage, {len(data['trades'])} Buchungen, morgen: {[t['text'] for t in tomorrow]}")


if __name__ == "__main__":
    main()
