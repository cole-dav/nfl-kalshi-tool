# Player Research Terminal — redesign spec

Target: replace the long single-scroll `static/index.html` with a dense,
one-screen dashboard styled like a trading/risk terminal. The layout must scale
to many nested tabs and panels.

- Live design canvas: https://claude.ai/artifact/DaLqAKZxDEP5kCKWV9oxcs
  (private to the owner unless shared)
- Source of the mock: `Main.dc.html` in this folder. It's a Design-Component
  file, not production code. The markup is plain HTML with inline styles and
  the `<script data-dc-script>` block holds the sample data + derivations.
  Treat it as the visual reference and port it to plain HTML/CSS/JS in
  `static/index.html`. Don't try to run the `<x-dc>`/`<sc-for>` tags directly.
- Target viewport: 1440×900, no page scroll. Panels scroll internally if
  content overflows.

## Information architecture (nesting levels)

1. **Left rail (48px): modules.** Research (built), Book, Slate, Correlation
   map, Alerts, Settings. Each module owns the whole workspace.
2. **Top bar (36px): open player tickets.** One tab per player searched,
   closable, `+` opens a new one. Also: search box (`/` focuses), Kalshi
   connection state, nflverse cache age, clock.
3. **Context strip (60px): game-level KPIs** pinned for the active ticket.
   Player name/pos/number, then spread, total, team-total over, forecast,
   rest, surface, opp D EPA/dropback rank, correlation flag count.
4. **Workspace grid: panels.** Every panel has a 30px header with its title
   and its own tab row. Tabs are per-panel state.

| Panel | Tabs | Payload source |
|---|---|---|
| Markets | Player (32) · Teammates (92) · Team/Game (74) · Season (8) | `markets.player_props`, `teammate_props`, `team_game_markets`, `season_props` |
| Implied curve | one tab per stat family | `markets.player_props` grouped by `series` |
| Book & Risk | Positions · Correlation · Exposure | `my_position` on markets (from `build_my_positions`), `correlations.flags` |
| Matchup | Opp Def · Team Off · Game | `opponent_defense`, `team_context`, `game_environment` |
| Injuries | Opp · Own | `game_environment.injuries_home` / `injuries_away` |
| History vs opp | — | `history_vs_opponent` |

Layout: left column 760px (Markets flex-grow, Implied curve 230px); right
column fills the rest (Book & Risk 280px, Matchup + Injuries side-by-side
360px, History fills remaining).

## Panel details

**Markets > Player:** two-column price ladder. Left column holds passing
(yds, TDs, completions, attempts, INT), right column holds rushing (yds,
attempts), anytime TD and longest rush. Below the right column sit summary
tiles: props count, total volume, wide-spread count and ref-line count.
- Group header row: stat name, `MED` = implied median (linear interpolation
  of where implied prob crosses 50%), `LAST v OPP` if history has that stat.
- Row columns: line, bid¢ (blue), ask¢ (orange), spread¢ (orange if ≥10¢),
  implied % with an inline bar and a 50% tick, volume (dim if 0), position
  (`Y40` / `N50` in amber).
- `◆` marks the reference line, i.e. the threshold closest to 50% that
  `_build_correlation_section` picks. This is what the old UI outlined in
  dashed yellow (`tr.correlated`). It is **not** a position.
- Held rows get an amber tint (`rgba(242,165,65,0.10)`).

**Implied curve:** SVG with thresholds on x and implied % on y. Shows the
bid–ask band (shaded), mid line with dots, a diamond on the ref line, a dotted
vertical at the median, a dashed amber vertical at last-vs-opp and a dashed
50% line.

**Book & Risk:**
- Positions: summary tiles (contracts, cost, market value, unrealized P&L,
  max payout), then a blotter: contract, side, qty, avg¢, mark¢ (mid, or
  100−mid for NO), P&L, max win, P(win).
- Correlation: matrix. Rows = the player's ref line per stat family; columns
  = correlated legs (teammate ref lines + team-total over). Filled cell = a
  flag. Blue marks a pass-catcher leg, amber the team total. Σ column counts
  per row. This replaces the long `↪` list.
- Exposure: diverging bars per position, risk (orange, left) vs to-win
  (blue, right).

**Matchup:** rows of metric · value · rank bar · `#rank`. Bar length is
proportional to (33 − rank)/32. Colors are from the searched player's point
of view:
- opponent defense rank ≥21 = favorable (blue), ≤11 = against (orange)
- own team rank ≥21 = against, ≤11 = favorable
- otherwise neutral grey

Keep the man/zone coverage caveat as a footnote.

**Injuries:** sorted DNP → LP → FP. Status pill colors: DNP orange, LP amber,
FP blue. The tab label shows counts `DNP·LP·FP`.

**History:** rows are actual (per past game), market-implied median, and
actual − implied.

## Visual tokens

| Token | Value | Use |
|---|---|---|
| bg | `#07090B` | page |
| chrome | `#0B0E12` | top bar, rail, context strip |
| panel | `#101318` | panel body |
| panel-head | `#0D1014` | panel headers, group rows, tiles |
| border | `#1E242C` | panel borders; row rules `#151A20`, header rules `#262E37` |
| text | `#E3E7EC` / `#C9D1D9` | primary / labels |
| muted | `#9AA4AF` | secondary values |
| dim | `#7A8591` | column heads, captions |
| amber | `#F2A541` | active tab underline, held, last-vs-opp, brand |
| blue | `#5AA9FF` | bid, favorable, positive P&L, YES |
| orange | `#FF7A45` (`#FF9F6E` for ask text) | ask, wide spread, against, negative P&L, NO |
| bar | `#2F5F92` on `#161B22` track | implied % bars |

Blue/orange (not green/red) for good/bad keeps it colorblind-safe. No rounded
corners, 1px borders, 8px gutters.

Type: IBM Plex Sans Condensed (labels/text) + IBM Plex Mono with
`tabular-nums` (all numbers). Sizes: 11px table cells, 9.5–10px uppercase
column heads with 0.06–0.1em tracking, 12px tabs, 13–14px KPI values, 20px
player name. Row heights: 17px ladder, 20–24px other tables.

Active tab: text `#E3E7EC` with a 2px amber bottom border. Inactive: `#7A8591`.

## Notes / open items

- The mock's positions are **sample fills** (the source snapshot had none).
  Wire the Positions tab to real `my_position` data and show an empty state
  when there are none.
- The Teammates / Team-Game tabs in the mock only list the correlated legs.
  The real build should list every market, with a "linked to player" column.
- Other modules (Book, Slate, Correlation map, Alerts) and non-QB layouts
  (WR/TE should add a target-share/WOPR volatility panel from
  `team_context`) aren't designed yet.
- Implied medians are derived client-side from the ladder, not a new API
  field.
