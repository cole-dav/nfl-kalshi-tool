# Reddit fan reports (planned, not built)

Decided 2026-11-12. Goal: give the post-game reviewer what play-by-play can't
show (no game film): who got beat up front, coverage busts, in-game injuries,
play-calling complaints, field/weather conditions, officiating.

## Access

- Free Reddit Data API (user approved). Create a "script" app at
  reddit.com/prefs/apps; put `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` in
  the server `.env` (never print them). App-only OAuth:
  `POST https://www.reddit.com/api/v1/access_token` with
  `grant_type=client_credentials` and basic auth, then read from
  `https://oauth.reddit.com`. Send a descriptive User-Agent with no personal
  details. Stay under the free tier's rate limit (100 requests/min).
- Unauthenticated requests are blocked from data-center IPs (the Hetzner VPS),
  so credentials are required. The feature must do nothing, without
  failing a review, when the keys are absent.
- Reddit is blocked from cloud Claude sessions, so build it against recorded
  fixtures and test it for real on the VPS or laptop.

## Rules

1. Sources: post-game threads only. Use the r/nfl "Post Game Thread" and each
   team subreddit's post-game thread, top-voted comments (about 150 per
   thread, depth 2). Skip in-game threads (mostly jokes and reactions).
   Match threads by title and team nickname
   (`madden_ratings.NICKNAME_TO_NFLVERSE`); needs a team -> subreddit map.
2. Fetch inside `game_review.review_game`, which runs 16h after kickoff, so
   votes have settled. Store the raw comments plus extraction in
   `data/feed/fan_reports/<game_id>.json` (host-only, backed up with
   `data/feed`). This is the source record notebook claims can point back to.
3. Extraction is a separate Claude call with **no tools**. Comments are
   wrapped as untrusted data. Output a short list of claims:
   `{team, category (line play | coverage | injury | play-calling | conditions |
   officiating | player), claim, commenters (distinct authors saying it),
   quotes (verbatim, max 2), checkable_in_pbp}`. Drop claims only one person
   made unless they are about injuries or conditions.
4. The reviewer gets the claims as `fan_observations`: biased, unverified,
   from people watching the broadcast. Check the checkable ones with
   `query_plays`. Notebook items from them are tagged, e.g. "(W10, fans, 14
   commenters)". Play-by-play wins any conflict. Fans don't create trends by
   themselves.
5. Raw Reddit text never reaches the preview writer. It only sees what the
   reviewer put in the notebooks. This contains fan bias and prompt injection.
6. Show the claims on the Week tab review card, labeled as fan reports.
