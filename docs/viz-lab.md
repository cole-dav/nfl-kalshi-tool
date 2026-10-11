# Viz Lab: Big Data Bowl ideas, built and not built

Source: *NFL Big Data Bowl: Viz Ideas Worth Stealing* (developer report, Oct 2026).
The Lab is the flask icon on the rail. `BUILD STATUS` in the app mirrors this file.

## The rule: betting pages only see this season

Anything tied to betting (research tickets, the slate, the builder) reads **only the
current season**. That means Next Gen Stats and nflverse/PFR/FTN charting aggregates.
Everything built from Big Data Bowl tracking is a past season and lives **only in
the Viz Lab**.

- The research ticket's **NGS** tab (`/api/player_ngs`, `viz_lab.player_ngs`) always uses `nd.current_season()`.
- Historical tabs are labelled `HISTORICAL` in the Lab header.

## Data sources

| Contest | Season(s) | Who's tracked | Where | Used for |
|---|---|---|---|---|
| BDB 2019 (public GitHub) | 2017 Wk 1–6 | All 22 + ball, whole play | `data/bdb/` (one game on master, the other 90 fetched from repo history on first open) | ALL-22 REPLAY |
| BDB 2023 (Kaggle) | 2021 Wk 1–8, every dropback | All 22 + ball, **snap to throw/sack** + PFF scouting (rush/block roles, hits/hurries/sacks) | `data/bdb/2023/` | ALL-22 REPLAY, pressure validation |
| BDB 2026 Analytics (Kaggle) | 2023 Wk 1–18 pass plays | QB, route runners, coverage defenders before the throw; ~3 players in flight | `data/bdb/2026/` | Completion model training |
| BDB 2027 (Kaggle) | 2023–25 | 510 players from the 2023–25 draft classes (no QBs/RBs, many first-rounders missing), ~4 per play, no ball | `data/bdb/2027/` | DRAFT CLASSES |

The 2024 and 2025 contest datasets were removed from Kaggle at the host's request.

All of `data/bdb/` and `cache/` is gitignored. The trained model
`data/models/completion_v1.json` (coefficients only) is committed.

### Setting up on a new machine

```sh
# Kaggle token at ~/.kaggle/access_token (chmod 600); rules accepted for each contest
.venv/bin/pip install kaggle
.venv/bin/kaggle competitions download nfl-big-data-bowl-2023 -p data/bdb/2023 && (cd data/bdb/2023 && unzip -q *.zip && rm *.zip)
.venv/bin/kaggle competitions download nfl-big-data-bowl-2026-analytics -p data/bdb/2026 && (cd data/bdb/2026 && unzip -q *.zip && rm *.zip)
.venv/bin/kaggle competitions download nfl-big-data-bowl-2027 -p data/bdb/2027 && (cd data/bdb/2027 && unzip -q *.zip && rm *.zip)
.venv/bin/python tools/bdb_precompute.py      # 2021 per-game cache + pressure validation (~2 min)
.venv/bin/python tools/train_completion.py    # retrain the completion model (~15 s)
```

Without the Kaggle data, the Lab still works on the 2017 games. The 2021 source and
DRAFT CLASSES just don't appear, or show "data not found".

## What's built

| Report idea | Status | Where | Notes |
|---|---|---|---|
| All-22 play animator (#1) | Built | ALL-22 REPLAY | 2021 (122 games, every dropback) and 2017 (91 games, every play), with source and game pickers. Trails, full routes, scrub with event ticks, 0.5/1/2× speed, space/←/→, hover readouts. 2021 tracking stops at the release. |
| Completion-probability ticker (#3) | Built | Replay chart 2, "CATCH PROB · IF THROWN NOW" | Logistic regression (`completion_model.py`) on 14,105 thrown passes from BDB 2026, built on 12 geometry features at the release frame. Holdout (2023 Wk 15–18): **AUC 0.73**, log loss 0.548 vs 0.621 baseline, calibrated within a few points from 30–100%. It's applied per receiver per frame from snap to throw. Caveats: it learned only from passes actually thrown to that receiver (selection bias), and BDB 2026 tracks only coverage defenders, so pass rushers are excluded at inference. |
| Pocket pressure (#2) | Built, validated | Replay "Pocket pressure" + chart 1 | Each rusher within 7 yd of the QB adds `1 − d/7`, up to doubled by closing speed. On 2021 plays it uses PFF's charted rushers and shows PFF's hit/hurry/sack credits (dashed white ring). Validated across **8,556 dropbacks: AUC 0.71**. The top fifth of peaks drew PFF pressure 70% of the time, vs 19% for the bottom fifth. |
| Leaderboards (#10) | Built (this season) | NGS LEADERS + research-ticket **NGS** tab | Current-season NGS + PFR boards; the ticket tab shows the player's rank on each board he qualifies for. |
| Draft-class profiles (new, from BDB 2027) | Built (historical) | DRAFT CLASSES | Receivers, pass protection, pass rush and coverage per season (2023/24/25, regular season + playoffs, 100+ snap minimum). Sortable; the best 10% per metric is highlighted, respecting each metric's direction. |
| Contested space (#7) | Approximation | Replay "Space control" | Velocity-projected Voronoi diagram. |
| Pre-snap tells (#8) | Built (charting) | PRE-SNAP TELLS | This season (FTN) plus last season (participation). |
| Route trees | Approximation | ROUTE TREES | Last season's targeted routes. |

## Not built, and why

| Idea | Why |
|---|---|
| Optimal-path overlay (#4) | Deferred. Buildable on the 2017 whole-play games with a speed/acceleration movement model. |
| Ghost defenders / SLIME (#6) | BDB 2026 has post-throw data but only ~3 players in flight; needs conditional-density modeling. High effort. |
| Tackle-probability timeline (#5) | Needs the BDB 2024 data, which was removed from Kaggle. |
| Block-or-route dials (#9) | Needs the BDB 2025 data, also removed. |
| Any tracking view for this week's games | Raw tracking isn't public in-season. Betting pages use only published current-season aggregates. |

## Files

- `bdb_tracking.py` — sources, per-game normalized parquet cache (`cache/bdb/<source>/`), play lists, per-play payloads (positions, events, PFF credits, pressure, separation, catch probability).
- `completion_model.py` — feature builder, Newton logistic fit, prediction, model loader.
- `tools/train_completion.py`, `tools/bdb_precompute.py` — offline training and prep.
- `viz_lab.py` — `leaderboards()`, `player_ngs()` (current season), `tells()`, `routes()`, `draft_classes()`.
- `server.py` — `GET /api/lab/{bdb/sources, bdb/games?src=, bdb/plays?src=&game=, bdb/play?src=&game=&play=, leaders, tells?team=, routes?team=, draft?group=&season=}` and `GET /api/player_ngs?name=&gsis=`.
- `static/lab.js` — the Lab UI. The NGS ticket tab lives in `static/index.html` (`renderMatchup`).
