from __future__ import annotations

import pandas as pd

import game_flags as gf

TEAMS = ["T%02d" % i for i in range(12)]


def _plays(gid, team, opp, runs, passes, cmp_every=2, ypa=7, qb="A.Qb", wp=0.5, ints=0, weather=None, roof="outdoors"):
    rows = []
    for i in range(runs):
        rows.append({"play_type": "run", "pass": 0, "pass_attempt": 0, "complete_pass": 0, "yards_gained": 4,
                     "passer_player_name": None})
    for i in range(passes):
        c = int(i % cmp_every == 0)
        rows.append({"play_type": "pass", "pass": 1, "pass_attempt": 1, "complete_pass": c, "yards_gained": ypa * 2 * c,
                     "passer_player_name": qb})
    for j, r in enumerate(rows):
        r.update(game_id=gid, posteam=team, defteam=opp, wp=wp, interception=int(j < ints), fumble_lost=0,
                 weather=weather, roof=roof)
    return rows


def season(rain_game=True):
    """6 weeks, 12 teams, every team-game ordinary except T00's week-6 rain game."""
    rows = []
    for wk in range(1, 7):
        for h in range(0, 12, 2):
            home, away = TEAMS[h], TEAMS[(h + wk * 2 + 1) % 12]
            gid = f"2026_{wk:02d}_{away}_{home}"
            wx = "Cloudy Temp: 60° F, Humidity: 50%, Wind: W 5 mph"
            for t, o in ((home, away), (away, home)):
                runs, passes = 28 + (wk % 3), 32 + (h % 4)
                if rain_game and wk == 6 and t == "T00":
                    runs, passes, wx = 54, 4, "Rain Temp: 78° F, Humidity: 81%, Wind: E 9 mph"
                rows += _plays(gid, t, o, runs, passes, weather=wx)
    return pd.DataFrame(rows)


def _gid_of(pbp, team, wk):
    return next(g for g in pbp["game_id"].unique() if g.startswith(f"2026_{wk:02d}_") and team in g.split("_")[2:])


def test_rain_game_is_major_and_names_the_stats():
    pbp = season()
    gid = _gid_of(pbp, "T00", 6)
    f = gf.flags_for_game(pbp, gid)
    assert f["severity"] == "major"
    kinds = {(x["kind"], x.get("team"), x.get("metric")) for x in f["flags"]}
    assert ("conditions", None, None) in kinds
    assert ("stat", "T00", "pass_att") in kinds and ("stat", "T00", "dropback_rate") in kinds
    assert f["conditions"]["precip"] == "rain" and f["conditions"]["wind_mph"] == 9


def test_ordinary_games_are_not_flagged():
    pbp = season()
    flagged = {g: v for g, v in gf.season_flags(pbp).items() if v["flags"]}
    assert list(flagged) == [_gid_of(pbp, "T00", 6)]


def test_dome_ignores_weather_text_and_wind_threshold():
    g = pd.DataFrame({"roof": ["dome"], "weather": ["Rain Wind: 30 mph"]})
    assert gf.conditions(g) == {"roof": "dome"}
    g = pd.DataFrame({"roof": ["outdoors"], "weather": ["Clear Temp: 15° F, Wind: NW 22 mph"]})
    c = gf.conditions(g)
    assert c["wind_mph"] == 22 and c["temp_f"] == 15 and c["precip"] is None


def test_qb_change_and_turnovers():
    pbp = season(rain_game=False)
    gid = _gid_of(pbp, "T03", 5)
    rows = pbp["game_id"].eq(gid) & pbp["posteam"].eq("T03") & pbp["play_type"].eq("pass")
    pbp.loc[rows, "passer_player_name"] = "B.Backup"
    pbp.loc[rows[rows].index[:5], "interception"] = 1
    f = gf.flags_for_game(pbp, gid)
    details = [x["detail"] for x in f["flags"] if x["team"] == "T03"]
    assert any("A.Qb threw 0%" in d and "B.Backup" in d for d in details)
    assert "5 turnovers" in details


def test_team_outliers_for_writer():
    pbp = season()
    sched = pd.DataFrame([{"game_id": g, "week": int(g.split("_")[1]), "away_team": g.split("_")[2],
                           "home_team": g.split("_")[3], "result": 3} for g in pbp["game_id"].unique()])
    out = gf.team_outliers(pbp, sched, "T00")
    assert len(out) == 1 and out[0]["week"] == 6 and out[0]["severity"] == "major"
    assert any(s.startswith("rain") for s in out[0]["flags"])
    assert all(not s.startswith("T") or s.startswith("T00:") for s in out[0]["flags"])
