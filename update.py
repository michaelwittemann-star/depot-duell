"""Depot-Vergleich: MSCI World SRI gegen den Masterplan, Papierdepots ab START mit je 100.000 EUR.

Laeuft taeglich (GitHub Actions) und rechnet beide Depots bei jedem Lauf vollstaendig ab START neu
(deterministisch, kein gespeicherter Zustand). Ergebnis: docs/data.json fuer docs/index.html.

Regeln des Masterplans (Signale aus US-Schlusskursen, um Dividenden bereinigt):
  Topf B (50 %): QQQ über 200-Tage-Schnitt (Einstieg über +5 %, Ausstieg unter -5 %) UND 20-Tage-Vola von QQQ < 30 %
                 UND QQQ-Rendite über 126 Handelstage > 0  ->  3x S&P 500, sonst US-Staatsanleihen 20+ Jahre
  Topf A (30 %): SPY über 200-Tage-Schnitt (Einstieg über +5 %, Ausstieg unter -5 %)  ->  2x Nasdaq-100,
                 sonst je zur Haelfte US-Staatsanleihen 7-10 Jahre und Gold
  Topf G (20 %): immer Gold
  Handel: zur Eroeffnung des nächsten europaeischen Handelstags nach dem US-Schluss.
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

START = date.fromisoformat(os.environ.get("DEPOT_START", "2026-09-22"))
CAPITAL = 100_000.0
MY_DEPOT = float(os.environ.get("MY_DEPOT", "15000"))   # echtes Plan-Depot fuer E-Mail-Betraege (Seite: frei einstellbar)
WARN = 0.20                                              # Vorwarnung ab 20 % Wechselwahrscheinlichkeit in 5 Handelstagen
SITE = "https://michaelwittemann-star.github.io/depot-duell/"
OUT = Path(__file__).parent / "docs" / "data.json"
NOTIFY = Path(__file__).parent / "notify"

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
FEE_FLAT, FEE_FOREIGN = 5.90, 2.00      # flatex: 5,90 EUR je Order, an Auslandsboersen (Paris, Mailand) zzgl. ca. 2 EUR Fremdkosten
TAX = 0.26375                            # Abgeltungsteuer + Soli, ohne Kirchensteuer, ohne Sparer-Pauschbetrag
WEIGHTS = {"B": 0.5, "A": 0.3, "G": 0.2}
RULE_TEXT = {"B": ("Regel B an: QQQ-Trend, Vola und Momentum erfüllt", "Regel B aus: QQQ-Trend, Vola oder Momentum verletzt"),
             "A": ("Regel A an: SPY über 200-Tage-Schnitt (+5 %)", "Regel A aus: SPY unter 200-Tage-Schnitt (-5 %)")}


def fee(value: float, prod: str) -> float:
    if value <= 0:
        return 0.0
    return FEE_FLAT + (0.0 if PRODUCTS[prod]["venue"] == "Xetra" else FEE_FOREIGN)


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
    fc_b = forecast(q, int(b.iloc[-1]), int(q_trend.iloc[-1]), True)
    fc_a = forecast(s, int(a.iloc[-1]), int(a.iloc[-1]), False)
    prev_b = forecast(q.iloc[:-1], int(b.iloc[-2]), int(q_trend.iloc[-2]), True)
    prev_a = forecast(s.iloc[:-1], int(a.iloc[-2]), int(a.iloc[-2]), False)
    detail = {
        "forecast": {"B": fc_b, "A": fc_a}, "forecast_prev": {"B": prev_b["p5"], "A": prev_a["p5"]},
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


# --- Prognose ---------------------------------------------------------------------------------------
# Exakt bekannt: welche Kurse in den nächsten Tagen aus dem 200-Tage-Schnitt und aus dem 126-Tage-Vergleich herausfallen.
# Unsicher: die Kursbewegung selbst -> Wahrscheinlichkeit per Normalverteilung mit der aktuellen Tagesschwankung.
# Historisch (2000-2026, Schwelle 20 %): 83-89 % der Wechsel mind. 1 Tag vorher angekuendigt, gut 4 von 10 Warnungen treffen.

def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ma_trigger(c: np.ndarray, state: int, ahead: int, n: int = 200, band: float = 0.05) -> float:
    """Kurs, bei dem der Trendzustand in `ahead` Tagen kippt, wenn der Kurs bis dahin konstant bei diesem Wert liegt."""
    keep = c[-(n - ahead):]
    k = (1 - band) if state else (1 + band)
    return k * keep.sum() / n / (1 - k * ahead / n)


def vol_breach(rets20: np.ndarray, limit: float = 0.30) -> float:
    """Kleinste Tagesbewegung (log), die morgen die 20-Tage-Schwankung über `limit` hebt."""
    r19 = rets20[1:]
    for x in np.linspace(0, 0.3, 601):
        if max(np.std(np.r_[r19, x], ddof=1), np.std(np.r_[r19, -x], ddof=1)) * math.sqrt(252) >= limit:
            return float(x)
    return 0.3


def forecast(c: pd.Series, rule_on: int, trend_on: int, with_filters: bool) -> dict:
    v = c.values.astype(float)
    x0 = v[-1]
    rets = np.diff(np.log(v[-21:]))
    sd = float(np.std(rets, ddof=1))
    vol_now = sd * math.sqrt(252)
    res = {"sigma_day": round(sd, 4), "conditions": []}
    for days in (1, 5):
        trig = ma_trigger(v, trend_on, days)
        need = math.log(trig / x0)
        s = sd * math.sqrt(days)
        p_ma = (_phi(need / s)) if trend_on else (1 - _phi(need / s))
        if not with_filters:
            p = p_ma
        else:
            ref = v[-127 + days]                            # Kurs, gegen den das Momentum in `days` Tagen gemessen wird
            need_m = math.log(ref / x0)
            vb = vol_breach(rets)
            p_vol = 1 - (1 - 2 * (1 - _phi(vb / sd))) ** days
            if rule_on:                                     # an -> aus: eine verletzte Bedingung reicht
                p_mom = _phi(need_m / s)
                p = 1 - ((1 - p_ma) if trend_on else 1.0) * (1 - p_mom) * (1 - p_vol)
            else:                                           # aus -> an: alle Bedingungen noetig
                p_ma_on = 1.0 if trend_on else p_ma
                p_mom_on = 1 - _phi(need_m / s)
                p_vol_on = 1.0 if vol_now < 0.30 else min(1.0, days / 20)
                p = p_ma_on * p_mom_on * p_vol_on
        res[f"p{days}"] = round(float(min(max(p, 0.0), 1.0)), 3)
        if days == 1:
            res["conditions"].append({"name": "200-Tage-Schnitt", "ok": bool(trend_on), "trigger_price": round(float(trig), 2),
                                      "move": round(float(trig / x0 - 1), 4),
                                      "text": ("kippt auf aus bei Schluss unter " if trend_on else "kippt auf an bei Schluss über ") + f"{trig:.2f} $"})
            if with_filters:
                ref = v[-126]
                res["conditions"].append({"name": "6-Monats-Rendite", "ok": bool(x0 > v[-127]), "trigger_price": round(float(ref), 2),
                                          "move": round(float(ref / x0 - 1), 4),
                                          "text": f"morgen positiv, solange Schluss über {ref:.2f} $"})
                res["conditions"].append({"name": "20-Tage-Schwankung", "ok": bool(vol_now < 0.30), "value": round(vol_now, 4),
                                          "move": round(float(math.expm1(vol_breach(rets))), 4),
                                          "text": f"jetzt {vol_now * 100:.0f} %, über 30 % bei einer Tagesbewegung von mehr als {math.expm1(vol_breach(rets)) * 100:.1f} %"})
    # Ausblick bei gleichbleibendem Kurs: kippt etwas allein durch herausfallende alte Kurse?
    flat = []
    for days in range(1, 6):
        trig = ma_trigger(v, trend_on, days)
        if (trend_on and x0 < trig) or (not trend_on and x0 > trig):
            flat.append(f"Trend kippt bei gleichbleibendem Kurs in {days} Handelstag(en)")
            break
    if with_filters:
        for days in range(1, 6):
            if (x0 > v[-127]) != (x0 > v[-127 + days]):
                flat.append(f"Momentum kippt bei gleichbleibendem Kurs in {days} Handelstag(en)")
                break
    res["flat"] = flat
    return res


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
        f = fee(amount, prod)
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
        f = fee(gross, prod)
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

    msci, plan = Depot("MSCI World SRI"), Depot("Masterplan")
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

    # Was ist am nächsten Morgen zu tun? (Signal des letzten US-Schlusses gegen den aktuellen Bestand)
    b_now, a_now = detail["B"], detail["A"]
    tgt_now = target_assets(b_now, a_now)
    tomorrow = []
    if state is None:
        tomorrow.append({"depot": "MSCI World SRI", "text": f"100.000 EUR in {PRODUCTS['MSCI']['name']} ({PRODUCTS['MSCI']['venue']}) kaufen"})
        for sleeve, w in WEIGHTS.items():
            for prod, share in tgt_now[sleeve].items():
                amount = f"{CAPITAL * w * share:,.0f}".replace(",", ".")
                tomorrow.append({"depot": "Masterplan", "text": f"Topf {sleeve}: {amount} EUR in {PRODUCTS[prod]['name']} "
                                 f"({PRODUCTS[prod]['venue']}, {PRODUCTS[prod]['isin']}) kaufen"})
    else:
        for sleeve, now in (("B", b_now), ("A", a_now)):
            if now != state[sleeve]:
                old = " + ".join(PRODUCTS[p]["name"] for p in target_assets(state["B"], state["A"])[sleeve])
                new = " + ".join(PRODUCTS[p]["name"] for p in tgt_now[sleeve])
                tomorrow.append({"depot": "Masterplan", "text": f"Topf {sleeve}: {old} verkaufen, Erlös in {new} ({RULE_TEXT[sleeve][0 if now else 1]})"})

    pc_last = {k: float(closes[k].iloc[-1]) for k in PRODUCTS}
    orders = []                         # je Order: Betrag/Stueck fuer das 100.000-EUR-Musterdepot (Seite skaliert)
    if state is None:
        orders.append({"depot": "MSCI World SRI", "action": "Kauf", "product": "MSCI", "sleeve": "M", "amount": CAPITAL, "units": None})
        for sleeve, w in WEIGHTS.items():
            for prod, share in tgt_now[sleeve].items():
                orders.append({"depot": "Masterplan", "action": "Kauf", "product": prod, "sleeve": sleeve,
                               "amount": round(CAPITAL * w * share, 2), "units": None})
    else:
        for sleeve, now in (("B", b_now), ("A", a_now)):
            if now == state[sleeve]:
                continue
            proceeds = 0.0
            for key in plan.keys(sleeve):
                prod = key.split(":")[1]
                u = plan.units(key)
                value = u * pc_last[prod]
                proceeds += value
                orders.append({"depot": "Masterplan", "action": "Verkauf", "product": prod, "sleeve": sleeve,
                               "amount": round(value, 2), "units": round(u, 4), "all": prod != "GOLD"})
            for prod, share in tgt_now[sleeve].items():
                orders.append({"depot": "Masterplan", "action": "Kauf", "product": prod, "sleeve": sleeve,
                               "amount": round(proceeds * share, 2), "units": None})
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
        "signal": detail, "tomorrow": tomorrow, "orders": orders, "state": state, "my_depot": MY_DEPOT, "warn": WARN,
        "history": history, "trades": msci.trades + plan.trades, "holdings": holdings,
        "totals": {d.name: {"fees": round(d.fees_paid, 2), "taxes": round(d.taxes_paid, 2), "loss_pot": round(d.loss_pot, 2)} for d in (msci, plan)},
        "products": PRODUCTS,
        "assumptions": {
            "fee": "flatex: 5,90 EUR je Order, an Euronext Paris und Borsa Italiana zzgl. ca. 2 EUR Fremdkosten",
            "tax": "26,375 % Abgeltungsteuer und Soli auf jeden realisierten Gewinn, Verlustverrechnung, kein Sparer-Pauschbetrag, keine Kirchensteuer. "
                   "MSCI-Fonds 30 % Teilfreistellung, Plan-Produkte ohne. Xetra-Gold nach einem Jahr steuerfrei. "
                   "Vorabpauschale und Steuer auf Ausschuettungen nicht beruecksichtigt.",
            "prices": "Kurse von Yahoo Finance, um Ausschuettungen bereinigt. Handel zum Eroeffnungskurs plus/minus halbem Spread (je Produkt geschaetzt).",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_notification(data, state)
    print(f"{len(history)} Handelstage, {len(data['trades'])} Buchungen, morgen: {[t['text'] for t in tomorrow]}")


def _eur(x: float) -> str:
    return f"{x:,.0f} EUR".replace(",", ".")


def write_notification(data: dict, state) -> None:
    """Legt notify/title.txt + notify/body.md an, wenn gehandelt werden muss oder eine Regel neu in den Warnbereich kommt."""
    for f in NOTIFY.glob("*"):
        f.unlink()
    sig, fc = data["signal"], data["signal"]["forecast"]
    scale = MY_DEPOT / CAPITAL
    names = {"B": "Regel B (Topf 50 %: 3x S&P 500 oder Staatsanleihen 20+)", "A": "Regel A (Topf 30 %: 2x Nasdaq-100 oder Anleihen 7-10 + Gold)"}
    lines_fc = ["| Regel | Zustand | Wechsel morgen | Wechsel in 5 Tagen |", "|---|---|---|---|"]
    for k in ("B", "A"):
        lines_fc.append(f"| {names[k]} | {'an' if sig[k] else 'aus'} | {fc[k]['p1']:.0%} | {fc[k]['p5']:.0%} |")
    detail = []
    for k in ("B", "A"):
        for c in fc[k]["conditions"]:
            detail.append(f"- {k}: {c['name']}: {c['text']} ({c['move'] * 100:+.1f} % vom letzten Schluss)" if c["name"] != "20-Tage-Schwankung"
                          else f"- {k}: {c['text']}")
        for f in fc[k]["flat"]:
            detail.append(f"- {k}: {f}")
    footer = ["", "### Ausblick", *lines_fc, "", *detail, "", f"Seite: {SITE}",
              "", f"_Grundlage: US-Schluss vom {sig['us_date']}. Beträge 'Mein Depot' für {_eur(MY_DEPOT)}, Kurse vom letzten Schluss (Schätzung)._"]
    title, body = None, []
    if data["orders"]:
        title = f"Handeln zur nächsten Eroeffnung (Signal vom {sig['us_date']})"
        body = ["Zur Eroeffnung des nächsten Handelstags bitte umschichten:", "",
                "| Depot | Aktion | Produkt | ISIN | Boerse | Musterdepot 100k | Mein Depot |", "|---|---|---|---|---|---|---|"]
        for o in data["orders"]:
            p = PRODUCTS[o["product"]]
            mine = _eur(o["amount"] * scale) + (f" ({o['units'] * scale:,.3f} Stk.)".replace(",", "X").replace(".", ",").replace("X", ".") if o.get("units") else "")
            if o["depot"] == "MSCI World SRI":
                mine = "-"
            what = "alle Stücke" if o.get("all") else ("nur Topf-A-Anteil" if o["action"] == "Verkauf" else "")
            body.append(f"| {o['depot']} | {o['action']} {what} | {p['name']} | {p['isin']} | {p['venue']} | {_eur(o['amount'])} | {mine} |")
    else:
        newly = [k for k in ("B", "A") if fc[k]["p5"] >= WARN > data["signal"]["forecast_prev"][k]]
        if newly:
            title = f"Vorwarnung: {' und '.join('Regel ' + k for k in newly)} könnte bald umschalten (Signal vom {sig['us_date']})"
            body = ["Noch nichts zu tun. " + " ".join(
                f"Regel {k} ist {'an' if sig[k] else 'aus'} und könnte mit {fc[k]['p5']:.0%} Wahrscheinlichkeit in den nächsten 5 Handelstagen umschalten."
                for k in newly),
                "Historisch wurden gut 8 von 10 Wechseln so vorher angekuendigt; etwa 4 von 10 Warnungen führen tatsaechlich zu einem Wechsel."]
    if not title and os.environ.get("TEST_NOTIFY") == "true":
        title = f"Test der Benachrichtigung (Signal vom {sig['us_date']})"
        body = ["Dies ist eine Test-Nachricht. So sehen künftige Hinweise aus; gehandelt werden muss gerade nichts."]
    if title:
        NOTIFY.mkdir(exist_ok=True)
        (NOTIFY / "title.txt").write_text(title, encoding="utf-8")
        (NOTIFY / "body.md").write_text("\n".join(body + footer), encoding="utf-8")
        print("Benachrichtigung:", title)


if __name__ == "__main__":
    main()
