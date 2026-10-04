# Viz Lab: Big Data Bowl ideas, built and not built

Source: *NFL Big Data Bowl: Viz Ideas Worth Stealing* (developer report, Oct 2026).
The Lab is the flask icon on the rail. It has five tabs. `BUILD STATUS` in the app mirrors this file.

## The constraint behind every "not yet"

Player tracking (x, y per player at 10 Hz) is **not public week to week**. It
only reaches the public as Big Data Bowl sample seasons. Those sit behind a
Kaggle login, except one: the 2019 contest's sample in the NFL's own GitHub
repo (`nfl-football-ops/Big-Data-Bowl`). That sample has games, plays, and
players for 2017 Weeks 1–6, but tracking for **one game**: 2017090700,
KC @ NE, all 177 plays. So:

- **Tracking views** run on that historical game.
- **Current-season views** use what the NFL publishes from tracking: Next Gen
  Stats aggregates. They also use nflverse charting (FTN, participation, PFR).

## What's built

| Report idea | Status | Where | Notes |
|---|---|---|---|
| All-22 play animator (#1) | Built, sample data | ALL-22 REPLAY | Every player and the ball, interpolated between frames. Includes trails, full routes, scrubbing with event ticks, 0.5/1/2× speed, space/←/→ keys, and hover readouts (speed, separation). The target or ball carrier comes from the play text. |
| Pocket pressure heat map (#2) | Approximation, sample data | ALL-22 REPLAY → "Pocket pressure" + chart | Each defender within 7 yd of the QB adds `1 − d/7`, up to doubled by closing speed. It's a heuristic in the spirit of the 2023 winners' CPP, **not** their trained model. There are no OL/DL leaderboards: one game is too small a sample. |
| Contested space / field control (#7) | Approximation, sample data | ALL-22 REPLAY → "Space control" | Each 1-yd cell goes to the player whose position, projected 0.5 s ahead, is nearest. That's a velocity-projected Voronoi diagram, not the physics time-to-reach model. |
| Receiver separation | Built, sample data | ALL-22 REPLAY chart 2 | Per-receiver distance to the nearest defender. Click the chart to seek. |
| Leaderboards from computed metrics (#10) | Built (current season) | NGS LEADERS | CPOE, time to throw, aggressiveness, RYOE/att, 8+ box rate, separation, YAC over expected, air-yard share, missed-tackle rate, pressures, and yards per target allowed. Bars are drawn relative to the volume-weighted league average. Clicking a name opens the research ticket. These replace SLIME / RYAE / tackle conversion / OPLE, which need raw tracking. |
| Pre-snap tell explorer (#8) | Built (charting version) | PRE-SNAP TELLS | This season (FTN): QB alignment, backfield count, motion, and tempo → pass rate; box count → blitz rate. Last season (participation): personnel → pass rate; box count → man rate; coverage mix with EPA allowed. Each panel calls out its biggest gap from league average. |
| Route-combination chart | Approximation | ROUTE TREES | Targeted-route share, catch %, yards/target, EPA, and aDOT per receiver, plus a man vs zone split. It covers last season only, because nflverse publishes participation after the season ends. It shows the targeted receiver's route, not full combinations. |

## What can't be built yet, and what would unlock it

| Report idea | Why not | Unlock |
|---|---|---|
| Completion-probability ticker (#3) | Needs a model trained on thousands of tracked passes (Palepu: GAT + BiLSTM). One game isn't enough to train it, and a heuristic would misrepresent the idea. | Download the BDB 2026 data from Kaggle, train offline, and serve a per-play JSON time series. The chart slot already exists. |
| Optimal-path overlay (#4) | Not blocked on data. Running A* over the space-control grid is feasible on the sample. It wasn't built because doing it honestly needs a movement model (speed/accel limits). | Next candidate on the sample game. |
| Tackle-probability timeline (#5) | Needs a tackle model trained on the 2024 BDB set (2022 Weeks 1–9, Kaggle). | Kaggle data + offline training. |
| Ghost-defender map / SLIME (#6) | Needs conditional-density models trained on 2026 BDB post-throw tracking. | Kaggle data + high-effort modeling. |
| Block-or-route dials, TEndencIQ (#9) | Needs pre-snap tracking frames and a trained classifier (2025 BDB). | Kaggle data + training. |
| Any tracking view for **this week's** games | Raw tracking isn't public in-season. | A licensed NGS feed. Only the NGS aggregates (used in NGS LEADERS) are free. |

## Files

- `bdb_tracking.py` — downloads the sample into `data/bdb/` (gitignored, ~35 MB) on first use. Serves the game and play lists and per-play payloads: positions, events, LOS/first-down line, target, pressure, and separation.
- `viz_lab.py` — `leaderboards()`, `tells(team)`, `routes(team)` from nflverse.
- `nflverse_data.load_nextgen(stat_type)` — NGS passing/rushing/receiving, cached like the other game datasets.
- `server.py` — `GET /api/lab/{bdb/games, bdb/plays?game=, bdb/play?game=&play=, leaders, tells?team=, routes?team=}` (snapshot-cached) and `/static/lab.js`.
- `static/lab.js` — the Lab module. It's self-contained and uses `index.html` globals (`state`, `escapeHtml`, `TEAM_COLORS`).

## Adding more tracking games

Drop more 2019-format `tracking_gameId_<gameId>.csv` files into `data/bdb/`. The original
contest shipped 91 games on Kaggle, and `games.csv` / `plays.csv` already cover all of
them. Only the first game is listed in the UI today; a game picker is the obvious next step.
Later contests (2021+) use different file layouts (`week*.csv`, `playDirection`,
`club`), and the loader would need a column normalizer for them.
