# Depot-Duell

Papierdepot-Vergleich ab 22.09.2026, je 100.000 EUR:

- **MSCI World SRI** – iShares MSCI World SRI UCITS ETF (IE00BYX2JD69), kaufen und halten.
- **Offensiv-Plan** – 50 % Regel B (3x S&P 500 oder US-Staatsanleihen 20+ J.), 30 % Regel A (2x Nasdaq-100 oder halb
  US-Staatsanleihen 7-10 J., halb Gold), 20 % Gold. Regeln und Produkte stehen oben in `update.py`.

`update.py` holt werktags nach US-Boersenschluss die Kurse (Yahoo Finance), rechnet beide Depots ab Start komplett neu
(Ordergebuehren, Spreads, Verwahrentgelt, Abgeltungsteuer) und schreibt `docs/data.json`. Die Seite `docs/index.html`
zeigt Verlauf, Bestaende, alle Umschichtungen und was am naechsten Handelstag zu tun ist. Automatik: `.github/workflows/update.yml`.

Lokal testen: `pip install -r requirements.txt`, dann `python update.py` (mit `DEPOT_START=2025-10-01` fuer einen Rueckblick)
und `python -m http.server -d docs`.

Papierdepots zur Beobachtung, keine Anlageberatung.
