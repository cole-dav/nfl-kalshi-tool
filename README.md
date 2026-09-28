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
- `team_tendencies.py` -- NFL Savant-style team tendencies from pbp (3-and-out,
  red-zone TD%, 1Q/1H/2H scoring, neutral pass rate/PROE, pass rate when up or
  down 8+, pace, where each defense forces targets/runs) with league ranks, plus
  a rule-based game-script read per matchup using Kalshi spread/total. Shown on
  the game page under TENDENCIES & SCRIPT. Coverage shells/man-zone come from
  nflverse participation, which is only published for completed seasons, so
  they're last season's.
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

## Bet recommendation engine

A fair-value engine behind the Bet Builder drawer. It has four backend modules:

- `fair_value.py` fits one margin distribution and one total distribution per game.
  - Shape: a normal curve reweighted by "key numbers" (how often a final margin of 3, 7, 10 or 14 actually happens), learned from nflverse results against the closing line for 2019-2025.
  - Center: a weighted least-squares fit to the Kalshi moneyline and every spread (or total) strike, blended 15% toward the nflverse line.
  - Team totals come from `(T ± M) / 2`.
  - It also flags hard ladder arbs: nested strikes (e.g. -3.5 bid above the ML ask), disjoint spreads whose bids sum past $1, and both moneylines asking under $1.
- `prop_model.py` projects each player stat.
  - Projection: a recency-weighted baseline multiplied by adjustments for opponent defense, implied team points, opponent pace, game script (`team_tendencies`), wind and injury status.
  - Distributions: lognormal for yards (plus a zero mass), negative binomial for counts, and Poisson for TDs (TD share × implied team TDs).
  - The final location is a blend: at most 35% model and at least 65% the location fitted to the Kalshi ladder. The model weight shrinks when the player has few games.
- `scenario_sim.py` runs a seeded Monte Carlo of 20k sims per game.
  - It draws margin and total, then team points, then player stats through a Gaussian copula. Pass volume rises when a team trails; rush volume rises when it leads.
  - Uses: correlated parlay prices, "what if" scenarios, and same-team ladders with P&L by margin bucket.
- `engine_agent.py` serves slate-wide edges, and the Claude chat that drives the tools above.

The chat needs `ANTHROPIC_API_KEY` (see `.env.example`). Without a key, `/api/engine/chat` returns 503 and everything else keeps working.

| Route | Purpose |
|---|---|
| `GET /api/engine/status` | `{chat_enabled, model}` |
| `GET /api/engine/edges?event=&min_edge=0.03&kind=game\|prop\|all` | ranked edges plus arbs; `min_edge=-1` lists every market |
| `POST /api/engine/price` | `{legs, scenario?}` → joint (correlated) vs independent fair, parlay cost |
| `POST /api/engine/scenario` | `{event, constraints}` → every market re-priced under the view |
| `POST /api/engine/ladder` | `{event, team, family, strikes, stakes?}` → stack of singles with an outcome table; strike 0 = ML |
| `POST /api/engine/chat` | `{conversation_id?, message, slip}` → reply, proposals, tool trace |

Run the tests with `python -m pytest tests`. They are offline, using synthetic markets and a mocked Anthropic client.

Caveats:
- Edges exclude Kalshi fees (~0.07·p·(1−p) per contract).
- Game lines are mostly market consensus, so game "edges" are small by design.
- Prop edges are model opinions: shrunk toward the market, but uncalibrated.
- Games already in progress are skipped (the model is pregame only).

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
