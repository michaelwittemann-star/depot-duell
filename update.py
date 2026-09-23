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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

START = date.fromisoformat(os.environ.get("DEPOT_START", "2026-09-22"))
CAPITAL = 100_000.0
MY_DEPOT = float(os.environ.get("MY_DEPOT", "15000"))   # echtes Plan-Depot fuer E-Mail-Betraege (Seite: frei einstellbar)
WARN = 0.20                                              # Vorwarnung ab 20 % Wechselwahrscheinlichkeit in 5 Handelstagen
SITE = "https://michaelwittemann-star.github.io/depot-duell/"
OUT = Path(__file__).parent / "docs" / "data.json"
CACHE = Path(__file__).parent / "docs" / "prices.csv"
TRACK = Path(__file__).parent / "docs" / "masterfonds.csv"      # taeglicher Gesamtwert des Masterplans ("Masterfonds")
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
SIGNAL_TICKERS = ("QQQ", "SPY")
RULE_TEXT = {"B": ("Regel B an: QQQ-Trend, Vola und Momentum erfüllt", "Regel B aus: QQQ-Trend, Vola oder Momentum verletzt"),
             "A": ("Regel A an: SPY über 200-Tage-Schnitt (+5 %)", "Regel A aus: SPY unter 200-Tage-Schnitt (-5 %)")}


def fee(value: float, prod: str) -> float:
    if value <= 0:
        return 0.0
    return FEE_FLAT + (0.0 if PRODUCTS[prod]["venue"] == "Xetra" else FEE_FOREIGN)


def load_cache() -> pd.DataFrame:
    if CACHE.exists():
        df = pd.read_csv(CACHE, parse_dates=["date"])
        return df.set_index(["ticker", "date"]).sort_index()
    return pd.DataFrame(columns=["open", "close"], index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "date"]))


def save_cache(cache: pd.DataFrame) -> None:
    cache = cache.dropna(subset=["close"])                # unfertige/leere Tage nicht archivieren
    keep = cache[cache.index.get_level_values("date") >= pd.Timestamp(START) - pd.Timedelta(days=500)]
    keep.round(6).reset_index().sort_values(["ticker", "date"]).to_csv(CACHE, index=False)


def yahoo_chart(ticker: str, start: str) -> pd.DataFrame | None:
    """Tageskurse (dividendenbereinigt) von Yahoo. Fehlt der Schlusskurs des letzten Tages, wird der offizielle
    Boersenpreis aus dem Kopf der Antwort verwendet - aber nur, wenn die Boerse an diesem Tag schon geschlossen hat."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"period1": int(pd.Timestamp(start).timestamp()), "period2": int(time.time()) + 86400,
              "interval": "1d", "includeAdjustedClose": "true"}
    r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or quote["close"]
    rows = []
    for ts, o, c, a in zip(res["timestamp"], quote["open"], quote["close"], adj):
        day = pd.Timestamp(datetime.fromtimestamp(ts, timezone.utc).date())
        rows.append({"date": day, "open": o, "close": c, "adj": a})
    df = pd.DataFrame(rows).set_index("date")
    meta = res.get("meta", {})
    last_price, last_time = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if last_price and last_time:
        day = pd.Timestamp(datetime.fromtimestamp(last_time, timezone.utc).date())
        closed = datetime.now(timezone.utc) > datetime.fromtimestamp(last_time, timezone.utc) + pd.Timedelta(minutes=20).to_pytimedelta()
        if day in df.index and pd.isna(df.at[day, "close"]) and closed:
            df.at[day, "close"] = last_price
            df.at[day, "adj"] = last_price
            print(f"{ticker}: Schlusskurs {day.date()} aus dem Boersenpreis der Schnittstelle ergaenzt ({last_price})")
    factor = (df["adj"] / df["close"]).where(df["close"] > 0).ffill().fillna(1.0)
    out = pd.DataFrame({"open": df["open"] * factor, "close": df["adj"]})
    return out.dropna(subset=["close"])


SOURCES: dict[str, str] = {}


def download(ticker: str, start: str, cache: pd.DataFrame | None = None, tries: int = 5) -> pd.DataFrame:
    """Kurse von Yahoo, angereichert um das Archiv: neue Werte gewinnen, fehlende Tage kommen aus dem Archiv."""
    fetched = None
    for attempt in range(tries):
        try:
            df = yahoo_chart(ticker, start)
            if df is not None and len(df):
                fetched = df[~df.index.duplicated(keep="last")].sort_index()
                break
        except Exception as error:  # noqa: BLE001
            print(f"{ticker}: Versuch {attempt + 1} fehlgeschlagen: {error}")
        time.sleep(15 * (attempt + 1))
    stored = cache.loc[ticker] if (cache is not None and ticker in cache.index.get_level_values("ticker")) else None
    if fetched is None and stored is None:
        raise RuntimeError(f"Keine Kurse fuer {ticker} (weder von Yahoo noch im Archiv)")
    if fetched is None:
        print(f"{ticker}: Yahoo liefert nichts - Archiv wird verwendet")
        SOURCES[ticker] = "Archiv"
        return stored.sort_index()
    if stored is not None:
        missing = stored.index.difference(fetched.index)
        if len(missing):
            print(f"{ticker}: {len(missing)} Tag(e) fehlen bei Yahoo, aus dem Archiv ergaenzt: {[str(d.date()) for d in missing][-5:]}")
        fetched = fetched.combine_first(stored)             # geholte Werte gewinnen, Archiv fuellt Luecken
    SOURCES[ticker] = "Yahoo"
    return fetched.sort_index()


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


def signals(cache: pd.DataFrame) -> tuple[pd.DataFrame, dict, dict]:
    got = {t: download(t, "2012-01-01", cache) for t in SIGNAL_TICKERS}
    q = got["QQQ"]["close"].dropna()
    s = got["SPY"]["close"].dropna()
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
    return pd.DataFrame({"B": b, "A": a}), detail, got


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
    cache = load_cache()
    sig, detail, sig_raw = signals(cache)
    first = (pd.Timestamp(START) - pd.Timedelta(days=10)).date().isoformat()
    raw = {k: download(p["ticker"], first, cache) for k, p in PRODUCTS.items()}
    fresh = pd.concat({**{PRODUCTS[k]["ticker"]: v for k, v in raw.items()}, **sig_raw}, names=["ticker", "date"])
    save_cache(fresh.combine_first(cache))
    opens = pd.DataFrame({k: v["open"].where(v["open"] > 0) for k, v in raw.items()}).sort_index()
    closes = pd.DataFrame({k: v["close"].where(v["close"] > 0) for k, v in raw.items()}).sort_index().ffill()
    opens = opens.fillna(closes.shift(1)).fillna(closes)          # fehlende Eroeffnung: Vortagesschluss
    eu_days = [d for d in raw["MSCI"]["close"].dropna().index if d.date() >= START]

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
    orders = []                         # je Order: Betrag/Stück fuer das 100.000-EUR-Musterdepot (Seite skaliert)
    if state is None:
        orders.append({"depot": "MSCI World SRI", "action": "Kauf", "product": "MSCI", "sleeve": "M", "amount": CAPITAL,
                       "units": None, "price": round(pc_last["MSCI"], 4)})
        for sleeve, w in WEIGHTS.items():
            for prod, share in tgt_now[sleeve].items():
                orders.append({"depot": "Masterplan", "action": "Kauf", "product": prod, "sleeve": sleeve,
                               "amount": round(CAPITAL * w * share, 2), "units": None, "price": round(pc_last[prod], 4)})
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
                               "amount": round(value, 2), "units": round(u, 4), "all": prod != "GOLD",
                               "price": round(pc_last[prod], 4)})
            for prod, share in tgt_now[sleeve].items():
                orders.append({"depot": "Masterplan", "action": "Kauf", "product": prod, "sleeve": sleeve,
                               "amount": round(proceeds * share, 2), "units": None, "price": round(pc_last[prod], 4)})
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
        "waiting": bool(state is None and START <= date.today()),   # Start liegt an, Kurse fehlen noch
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
    write_tracking(history)
    stale = sorted(k for k, v in SOURCES.items() if v == "Archiv")
    data["stale"] = stale
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if old.get("start") == data["start"] and len(old.get("history", [])) > len(history):
            raise RuntimeError(
                f"Abbruch: nur {len(history)} statt bisher {len(old['history'])} Handelstage berechnet - "
                f"Kursquelle unvollstaendig ({SOURCES}). Die veroeffentlichten Daten bleiben unveraendert.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_notification(data, state)
    print(f"{len(history)} Handelstage, {len(data['trades'])} Buchungen, morgen: {[t['text'] for t in tomorrow]}")


def write_tracking(history: list) -> None:
    """Taeglicher Gesamtwert beider Depots als CSV - der Masterplan als 'Masterfonds' mit Anteilswert (Start 100)."""
    rows = ["datum;masterfonds_wert;masterfonds_anteilswert;masterfonds_tagesrendite;msci_wert;msci_anteilswert"]
    prev = None
    for h in history:
        idx_plan = h["plan"] / CAPITAL * 100
        day = "" if prev is None else f"{h['plan'] / prev - 1:.6f}"
        rows.append(f"{h['date']};{h['plan']:.2f};{idx_plan:.4f};{day};{h['msci']:.2f};{h['msci'] / CAPITAL * 100:.4f}")
        prev = h["plan"]
    TRACK.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")


def _eur(x: float) -> str:
    return f"{x:,.0f} EUR".replace(",", ".")


def last_us_session(now: datetime) -> date:
    """Datum der letzten abgeschlossenen US-Sitzung (Schluss 20:00 UTC im Sommer, 21:00 UTC im Winter; Puffer bis 21:30)."""
    d = now.date()
    if now.hour < 21 or (now.hour == 21 and now.minute < 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def write_notification(data: dict, state) -> None:
    """Schreibt notify/title.txt + notify/body.md: eine Meldung je Handelstag, kurz und in fester Reihenfolge."""
    for f in NOTIFY.glob("*"):
        f.unlink()
    sig, fc = data["signal"], data["signal"]["forecast"]
    now = datetime.now(timezone.utc)
    if now.hour >= 20 and date.fromisoformat(sig["us_date"]) < last_us_session(now):
        print("Abendlauf ohne frischen US-Schluss - keine Meldung, der Morgenlauf uebernimmt")
        return
    hist, orders = data["history"], data["orders"]
    last = hist[-1] if hist else None
    scale = MY_DEPOT / CAPITAL
    handeln = bool(orders)
    day = date.fromisoformat(sig["us_date"]) + timedelta(days=1)
    while day.weekday() >= 5:                                  # Samstag/Sonntag -> naechster Werktag
        day += timedelta(days=1)

    title = ("\U0001F534 Heute handeln" if handeln else "\U0001F7E2 Heute nichts tun") + f" - {day.strftime('%d.%m.%Y')}"
    lines = [f"# {'\U0001F534 Heute handeln' if handeln else '\U0001F7E2 Heute nichts tun'}", ""]

    if last:
        seit = last["plan"] / CAPITAL - 1
        wert = f"{last['plan']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        lines += [f"**Gesamtwert Masterplan: {wert} EUR** ({f'{seit * 100:+.1f}'.replace('.', ',')} % seit Start, Stand {date.fromisoformat(last['date']).strftime('%d.%m.%Y')})", ""]

    top_rule = max(("B", "A"), key=lambda k: fc[k]["p5"])
    lines += [f"Wahrscheinlichkeit, dass morgen gehandelt werden muss: **{fc[top_rule]['p1'] * 100:.0f} %**"
              f" (in 5 Handelstagen {fc[top_rule]['p5'] * 100:.0f} %, jeweils Regel {top_rule})", "",
              f"[Alle Details auf der Website]({SITE})", ""]

    if handeln:
        lines += ["## Das ist zu tun", ""]
        def _de(x, n=2):
            return f"{x:,.{n}f}".replace(",", "X").replace(".", ",").replace("X", ".")
        for o in orders:
            prod = PRODUCTS[o["product"]]
            kurs = o.get("price")
            if o["action"] == "Verkauf":
                menge = _de(o["units"] * scale, 3) + " Stück"
                zusatz = "alle Stücke" if o.get("all") else "nur den Anteil aus Topf A"
                limit = f", Limit ca. {_de(kurs * 0.995)} EUR" if kurs else ""
                lines += [f"- **Verkaufen: {prod['name']}**",
                          f"  - Menge: {menge} ({zusatz})",
                          f"  - Handelsplatz: {prod['venue']} · ISIN {prod['isin']}",
                          f"  - Modus: Limit-Verkauf{limit} (letzter Schluss {_de(kurs) if kurs else '-'} EUR), gültig für den Tag"]
            else:
                betrag = _de(o["amount"] * scale, 0)
                limit = f", Limit ca. {_de(kurs * 1.005)} EUR" if kurs else ""
                stk = f" (ca. {_de(o['amount'] * scale / kurs, 3)} Stück)" if kurs else ""
                lines += [f"- **Kaufen: {prod['name']}**",
                          f"  - Betrag: ca. {betrag} EUR{stk}",
                          f"  - Handelsplatz: {prod['venue']} · ISIN {prod['isin']}",
                          f"  - Modus: Limit-Kauf{limit} (letzter Schluss {_de(kurs) if kurs else '-'} EUR), gültig für den Tag"]
        lines += ["", "_Zuerst verkaufen, dann mit dem Erlös kaufen. Limit jeweils 0,5 % über bzw. unter dem letzten Schlusskurs - "
                  "bei ruhigem Markt reicht das meist für eine Ausführung in der Eröffnungsauktion. Beträge und Stückzahlen für "
                  + _eur(MY_DEPOT) + " Depotwert._", ""]

    letzte = [tr for tr in data["trades"] if tr["depot"] == "Masterplan" and last and tr["date"] == last["date"]]
    if letzte and not handeln:
        tag = date.fromisoformat(last["date"]).strftime("%d.%m.%Y")
        lines += [f"## Kontrolle: am {tag} war umzuschichten", "",
                  "Falls deine Order nicht ausgeführt wurde (Limit nicht erreicht, Teilausführung), bitte heute nachholen:"]
        for tr in letzte:
            art = "Verkauf" if tr["action"] == "Verkauf" else "Kauf"
            stk = f"{tr['units'] * scale:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")
            lines.append(f"- {art}: {PRODUCTS[tr['product']]['name']} ({PRODUCTS[tr['product']]['venue']}), rund {stk} Stück")
        lines += ["", "_Bei einem Verkauf, der zweimal nicht durchgeht: billigst bzw. bestens ausführen. "
                  "Ein paar Zehntelprozent Kurs kosten weniger als mehrere Tage in der falschen Position._", ""]
    soll = [h for h in data["holdings"] if h["depot"] == "Masterplan" and h["product"] != "CASH"]
    if soll and last:
        gesamt = sum(h["value"] for h in soll)
        lines += ["## Soll-Bestand (zur Kontrolle)", ""]
        for h in sorted(soll, key=lambda x: -x["value"]):
            stk = f"{h['units'] * scale:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")
            lines.append(f"- {PRODUCTS[h['product']]['name']}: {h['value'] / gesamt * 100:.0f} % ({stk} Stück)")
        lines.append("")
    if last and len(hist) > 1 and date.fromisoformat(last["date"]).month != date.fromisoformat(hist[-2]["date"]).month:
        wert = f"{last['plan']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        lines += ["## Monatsanfang: Parqet aktualisieren", "",
                  f"Wert des eigenen Vermögenswerts \u201eMasterfonds\u201c auf **{wert} EUR** setzen.", ""]

    if data.get("stale"):
        lines += [f"> Hinweis: Für {', '.join(data['stale'])} lagen keine frischen Kurse vor, es wurden archivierte Werte benutzt.", ""]
    if data.get("waiting"):
        lines += ["> Hinweis: Fuer den Starttag liegen noch keine Schlusskurse vor.", ""]

    lines.append(f"<!-- meldung:{sig['us_date']} -->")
    NOTIFY.mkdir(exist_ok=True)
    (NOTIFY / "title.txt").write_text(title, encoding="utf-8")
    (NOTIFY / "body.md").write_text(chr(10).join(lines), encoding="utf-8")
    (NOTIFY / "marker.txt").write_text(f"meldung:{sig['us_date']}", encoding="utf-8")
    print("Meldung geschrieben:", title.encode("ascii", "replace").decode())


if __name__ == "__main__":
    main()
