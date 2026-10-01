from __future__ import annotations

import pandas as pd

import video_index as vi


def test_game_titles():
    assert vi.parse_game_title("Philadelphia Eagles vs. Chicago Bears Game Highlights | 2026 NFL Season Week 3") == \
        {"away": "PHI", "home": "CHI", "week": 3}
    assert vi.parse_game_title("Baltimore Ravens vs Dallas Cowboys Game Highlights from Rio | 2026 NFL Season Week 3") == \
        {"away": "BAL", "home": "DAL", "week": 3}
    assert vi.parse_game_title("Arizona Cardinals vs. San Francisco 49ers Game Highlights | NFL 2026 Season Week 3") == \
        {"away": "ARI", "home": "SF", "week": 3}
    assert vi.parse_game_title("Los Angeles Rams vs Denver Broncos Game Highlights | 2026 NFL Week 1")["away"] == "LA"
    assert vi.parse_game_title("GAME OF THE WEEK! Baltimore Ravens vs. Dallas Cowboys FULL GAME | NFL 2026 Season Week 3") is None


def test_player_titles_curtain_call():
    assert vi.parse_player_title("Jeremiyah Love Week 3 Highlights vs 49ers | Every Play") == \
        {"name": "Jeremiyah Love", "week": 3, "opp": "SF", "team": None, "kind": "every_play", "season": None}
    assert vi.parse_player_title("Rashee Rice Week 3 Highlights vs Dolphins | Every Target and Catch")["kind"] == "every_target"
    assert vi.parse_player_title("Najee Harris Week 3 Highlights vs Titans | Every Run")["kind"] == "every_run"
    assert vi.parse_player_title("Devin Neal Week 14 Highlights | Every Run, Target, and Catch vs Buccaneers") == \
        {"name": "Devin Neal", "week": 14, "opp": "TB", "team": None, "kind": "every_touch", "season": None}
    assert vi.parse_player_title("Mike Washington Jr Week 3 Highlights vs Saints | Every Run")["name"] == "Mike Washington Jr"


def test_player_titles_nfl():
    assert vi.parse_player_title("Case Keenum's best plays from 3-TD game vs. Eagles | Week 3") == \
        {"name": "Case Keenum", "week": 3, "opp": "PHI", "team": None, "kind": "best_plays", "season": None}
    assert vi.parse_player_title("Every catch from Brock Bowers' 116-yard game vs. Saints | Week 3") == \
        {"name": "Brock Bowers", "week": 3, "opp": "NO", "team": None, "kind": "every_catch", "season": None}
    assert vi.parse_player_title("Ja'Marr Chase's best plays from 9 catch game vs. Steelers | Week 3")["name"] == "Ja'Marr Chase"
    assert vi.parse_player_title("Top 15 Plays of Week 3! | 2026 NFL Season") is None
    assert vi.parse_player_title("Jared Goff's best throws from 4-TD game vs. Bills | Week 2")["name"] == "Jared Goff"
    assert vi.parse_player_title("Caleb Williams best plays from 4-TD game vs. Panthers | Week 1")["name"] == "Caleb Williams"
    assert vi.parse_player_title("Davante Adams' best catches from 195-yard, 2-TD game | Week 2")["name"] == "Davante Adams"
    assert vi.parse_player_title("Every Kenyon Sadiq catch from 105-yard game | Week 3") == \
        {"name": "Kenyon Sadiq", "week": 3, "opp": None, "team": None, "kind": "every_catch", "season": None}
    # No week in the title: the playlist's week fills in.
    assert vi.parse_player_title("Bijan Robinson's best plays from 173-yard game vs. Steelers", 1)["week"] == 1
    assert vi.parse_player_title("Bijan Robinson's best plays from 173-yard game vs. Steelers") is None
    assert vi.parse_player_title("Vikings' defense best plays vs. Bears | Week 2") is None
    assert vi.parse_player_title("KIRK COUSINS TO TRE TUCKER 40 YARD TD PASS", 2) is None


def test_player_titles_stacked():
    assert vi.parse_player_title("Rome Odunze: Every Touch of 2026 Week 3 | Full Film Compilation") == \
        {"name": "Rome Odunze", "week": 3, "opp": None, "team": None, "kind": "every_touch", "season": 2026}
    assert vi.parse_player_title("Jalen Hurts: Every Dropback of 2026 Week 3 | Full Film Compilation")["kind"] == "every_dropback"
    assert vi.parse_player_title("D'Andre Swift: Every Touch of 2026 Week 3 | Full Film Compilation")["name"] == "D'Andre Swift"
    assert vi.classify_playlist("Week 3 2026", 2026) == ("player", 3)
    assert vi.classify_playlist("2025 Season - Film", 2026) is None


def test_player_titles_hall():
    assert vi.parse_player_title("Chris Olave Week 3 Highlights (Every Target) | NFL 2026 - New Orleans Saints") == \
        {"name": "Chris Olave", "week": 3, "opp": None, "team": "NO", "kind": "every_target", "season": 2026}
    assert vi.parse_player_title("Keldric Faulk Week 2 Every Play | NFL 2026 - Tennesee Titans") == \
        {"name": "Keldric Faulk", "week": 2, "opp": None, "team": "TEN", "kind": "every_play", "season": 2026}
    assert vi.parse_player_title("C. J. Henderson Highlights | Week 3 NFL 2026 - Atlanta Falcons") == \
        {"name": "C. J. Henderson", "week": 3, "opp": None, "team": "ATL", "kind": "highlights", "season": 2026}
    assert vi.parse_player_title("Michael Penix Jr Debut Highlights | Week 3 NFL 2026 - Atlanta Falcons")["name"] == "Michael Penix Jr"
    assert vi.parse_player_title("Christian Watson Highlights (Every Target and Catch) | Week 3 NFL 2026 - Green Bay Packers")["kind"] == "every_target"
    assert vi.parse_player_title("Ashton Jeanty Week 3 Highlights | NFL 2026 - Las Vegas Raiders")["team"] == "LV"
    assert vi.parse_player_title("Derrick Henry Week 12 Highlights | NFL 2025 - Baltimore Ravens")["season"] == 2025


def test_continuation_tokens():
    page = {"a": [{"playlistVideoRenderer": {"videoId": "x1", "title": {"runs": [{"text": "T1"}]}}},
                  {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "TOK"}}}}]}
    assert vi._lockups(page, []) == [("x1", "T1")]
    assert vi._continuations(page, []) == ["TOK"]


def test_debut_titles_use_playlist_week():
    assert vi.parse_player_title("Kaleb Johnson Packers Debut Highlights | Every Run", 1) == \
        {"name": "Kaleb Johnson", "week": 1, "opp": None, "team": "GB", "kind": "every_run", "season": None}
    assert vi.parse_player_title("Mike Washington Jr NFL Debut Highlights | Every Run", 1)["name"] == "Mike Washington Jr"
    assert vi.parse_player_title("KC Concepcion NFL Debut Highlights | Every Play", 1)["name"] == "KC Concepcion"
    assert vi.parse_game_title("Cleveland Browns vs Tampa Bay Buccaneers Game Highlights", 2) == \
        {"away": "CLE", "home": "TB", "week": 2}


def test_playlist_classification():
    assert vi.classify_playlist("Game Recaps (Week 3) | 2026 NFL Season", 2026) == ("game", 3)
    assert vi.classify_playlist("Game Highlights (Week 1) | NFL 2026", 2026) == ("game", 1)
    assert vi.classify_playlist("Player Highlights (Week1) | NFL 2026", 2026) == ("player", 1)
    assert vi.classify_playlist("NFL | 2026 | Week 3", 2026) == ("player", 3)
    assert vi.classify_playlist("NFL | 2025 | Week 3", 2026) is None
    assert vi.classify_playlist("NFL | 2026 | Full Preseason", 2026) is None
    assert vi.classify_playlist("Best Of Week 3 | 2026 NFL Season", 2026) is None


def test_match_player():
    rosters = pd.DataFrame([
        {"full_name": "Mike Washington Jr.", "team": "LV", "gsis_id": "A"},
        {"full_name": "Marquise Brown", "team": "KC", "gsis_id": "B"},
        {"full_name": "Kalif Raymond", "team": "CHI", "gsis_id": "C"},
        {"full_name": "Jaylen Warren", "team": "PIT", "gsis_id": "D"},
    ])
    assert vi.match_player("Mike Washington Jr", "LV", rosters) == "A"
    assert vi.match_player("Hollywood Brown", "KC", rosters) == "B"   # last name on team
    assert vi.match_player("Kalif Raymond", "DET", rosters) == "C"   # traded: league-wide exact
    assert vi.match_player("Hollywood Brown", None, rosters) is None


def test_match_player_namesakes():
    rosters = pd.DataFrame([
        {"full_name": "DeVonta Smith", "team": "PHI", "gsis_id": "WR", "position": "WR", "status": "ACT"},
        {"full_name": "Devonta Smith", "team": "CAR", "gsis_id": "DB", "position": "DB", "status": "DEV"},
    ])
    assert vi.match_player("DeVonta Smith", None, rosters) == "WR"
    assert vi.match_player("Devonta Smith", "CAR", rosters) == "DB"   # team decides first
