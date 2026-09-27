# NFL Player Research Tool

Local tool for researching NFL players against your Kalshi positions: related
markets, opponent defense, team context, game environment, matchup history,
and correlation flags between props.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Put your Kalshi RSA private key at `keys/kalshi_private_key.pem` (gitignored,
already `chmod 600`).

## Run

```
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="./keys/kalshi_private_key.pem"
python3 server.py 8765
```

Open http://127.0.0.1:8765 and search a player.

## Files

- `kalshi_book.py` -- RSA-PSS request signing, balance/positions, scenario P&L.
- `kalshi_markets.py` -- NFL market discovery: weekly/season player-prop
  series, team/game series, ticker parsing, structured_targets (player/team
  UUID) resolution.
- `nflverse_data.py` -- cached nflreadpy loaders + derived metrics (defense
  EPA/dropback, pressure rates, pace, target share, injuries, matchup history).
  Local parquet cache in `cache/`, refreshed every 6h.
- `weather.py` -- Open-Meteo forecast lookup for the game's stadium.
- `player_research.py` -- ties it all together into one JSON payload per player.
- `combo.py` -- combo (parlay) builder backend: leg validation against Kalshi's
  multivariate collections, independent pricing, RFQ quotes.
- `server.py` + `static/index.html` -- stdlib HTTP server + single-page UI.
- `data/stadiums.json` -- stadium lat/lon/roof (hardcoded reference data).
- `data/coordinators.json` -- **manually maintained** DC names; nflverse and
  Kalshi have no structured feed for this, so it starts empty. Fill in `dc`
  per team yourself as staffs change.

## Combo builder (Correlation tab)

Book & Risk -> Correlation is an interactive parlay builder. Rows are the
searched player's lines, columns are the teammate / team-total lines the
correlation rules link them to (blue = QB <-> pass-catcher, amber = team
total). Click a row or column to add it to the slip; flip YES/NO and step
the line with the arrows. The slip shows running independent odds and units
per leg.

Every change is checked server-side (`POST /api/combo/validate`, public data
only) against Kalshi's combo rules:

- the stat must be offered in a combo collection (pass attempts/completions,
  rush attempts and longest-play props are not);
- game-level markets (moneyline, spread, totals, team total) allow 1 leg per
  game; player props are uncapped;
- at least 2 legs; no contradictory lines on the same player stat.

**GET KALSHI QUOTE** creates the combo market and sends an RFQ sized to your
stake; maker quotes stream in for ~45s. "vs INDEP." compares the quote to
multiplying the legs together (above 0 = Kalshi prices in correlation and/or
margin). Quotes are maker *bids*, so the displayed YES cost is `1 - no_bid`.

Accepting a quote places a real trade and is **off by default**. To enable
the ACCEPT button, start the server with `KALSHI_ENABLE_COMBO_ACCEPT=1`.
Verify the side semantics with a small stake first -- Kalshi's docs don't
spell out `accepted_side` precisely.

## Known data limitations

- **No man/zone coverage rate.** nflverse's free FTN charting release does
  not include the coverage-scheme field (that's in FTN's paid product). The
  opponent-defense section uses blitz rate + pass-rush count as the closest
  public proxy instead.
- **DC names are manual.** See `data/coordinators.json` above.
- Team code mapping: Kalshi uses `JAC`/`LAR`; nflverse uses `JAX`/`LA`.
  Handled in `nflverse_data.KALSHI_TO_NFLVERSE_TEAM`.

## v2 ideas (not built yet)

- Projection model (baseline x defense x pace x weather) shown against each
  line, per the original spec.
- Correlation flags currently only cover the searched player's own legs vs.
  their teammates/team total -- could extend to flag correlations on *any*
  held position, not just the player currently being viewed.
