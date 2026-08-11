# Best game of the season

Which hitters and pitchers had the best game of their season against a given
MLB club — and who else plays over their head against that club?

`mlbdelta` pulls a season of box scores from the public MLB Stats API, rates
every individual game performance against league average, and serves a small
dashboard for reading the results: per-opponent deltas, a season-best-game
tally, and league-wide leaderboards.

No third-party packages. Python 3.10+ and the standard library.

```bash
python -m mlbdelta demo                 # fictional season, no network needed
python -m mlbdelta serve                # dashboard on http://127.0.0.1:8000
```

---

## Quick start

Pull this season (about 2,400 games; a few minutes on the first run, seconds
afterwards because every box score is cached on disk):

```bash
python -m mlbdelta ingest --season 2026
python -m mlbdelta serve
```

Prefer to look before you fetch the whole year:

```bash
python -m mlbdelta ingest --season 2026 --limit 200   # stop after 200 games
python -m mlbdelta report --team NYY --top 10         # straight to the terminal
```

No network, or just want to see the thing work? `python -m mlbdelta demo`
generates a complete fictional season — invented clubs, invented players,
simulated plate appearance by plate appearance — and writes it through the same
parser and the same tables the live ingest uses.

---

## How a game is rated

Both sides are scored in one unit: **runs above average (RAA)**. That is what
lets a hitter's night and a pitcher's night share a leaderboard.

**Batting.** Linear weights (wOBA-style event values) give the raw run value of
the game's events; the league's average run value per plate appearance is then
subtracted.

```
RAA = Σ (weight_event × count_event) − league_runs_per_PA × PA
```

A league-average hitter scores 0. Four-for-four with two homers scores far more
than one-for-one with a homer, which is what a fan means by a better game.

**Pitching.** Runs prevented, relative to what an average pitcher gives up over
the same number of outs.

```
RAA = league_runs_per_out × outs − runs_allowed
```

Seven shutout innings is roughly +3.5 runs. Two innings and five runs is deeply
negative. Tom Tango's **Game Score v2** is computed alongside it, for the more
familiar 0–100-ish dominance scale.

**The league constants are measured from the data you ingested**, not hardcoded
from some past season. Ingest 1968 and the baseline moves to 1968. Across the
whole league, the scores sum to exactly zero — there is a test for it.

---

## How a delta is computed

A delta is how a player performed against one club compared with how that same
player performed against *everybody else* that season:

```
delta = (average score vs opponent) − (average score vs the rest of the league)
```

The baseline deliberately **excludes** the games being measured. A hitter who
faces one division rival nineteen times would otherwise be graded partly against
himself, which flattens exactly the effect the dashboard is looking for.

Three ways of asking the question, switchable in the UI and on the CLI:

| Metric | Meaning | Good for |
|---|---|---|
| `total` (default) | total runs above the player's own norm, accumulated across every meeting | who has done the most damage to this club |
| `per_game` | the same, per game | who plays the biggest above themselves |
| `rate` | per plate appearance (batters) / per nine innings (pitchers) | comparing players with unequal playing time |

Two notes on `rate`. Batters are measured per PA and pitchers per nine innings,
so the two sides are *not* comparable to each other in this mode — the tiles
label the unit for that reason. And relievers dominate the pitching rate board:
ten scoreless one-inning outings really is a huge runs-per-nine figure, so the
number is right, but it is built on ten innings. `total` is the steadier read.

### Best game of the season

Separately from the deltas, each qualified player's single highest-scoring game
of the season is attributed to the opponent they faced. That gives the headline
counts — *N batters and M pitchers had their best game of the year against this
club* — and the per-team table at the bottom of the dashboard. Ties go to the
earlier game, so every player is counted exactly once and the per-team counts
add up.

### Sample-size thresholds

Defaults, all configurable:

| | Batters | Pitchers |
|---|---|---|
| Season minimum | 50 PA | 30 outs (10 IP) |
| Vs. the selected opponent | 3 games, 8 PA | 2 games, 15 outs |

The two sides need different bars. A regular sees a division rival a dozen-plus
times, but a starting pitcher may face a given club only twice all year — a
shared threshold either throws out every starter or lets noise onto the board.
Starters who faced a club exactly once still show up in the season-best-game
tally, which is the right home for a one-off gem.

---

## Dashboard

`python -m mlbdelta serve [--port 8000] [--db data/mlb.sqlite3]`

- Opponent, metric and top-N pickers across the top
- Headline tiles: how many batters and pitchers had their best game against
  this club
- A diverging bar chart of the biggest deltas, batters or pitchers, with the
  full table underneath
- Click any row for that player's full game log, with the games against the
  selected club highlighted
- League leaderboards (overall / batters / pitchers)
- A league table of season-best games surrendered, per club

Light and dark are both explicit palettes, and the whole thing is static files
plus a JSON API — no CDN, no build step.

### JSON API

| Endpoint | Returns |
|---|---|
| `GET /api/meta` | season, teams, league constants, thresholds, row counts |
| `GET /api/team?team_id=&metric=&limit=` | full report for one opponent |
| `GET /api/leaderboard?side=overall\|batting\|pitching&metric=&limit=` | league-wide deltas |
| `GET /api/best-game-counts` | season-best games attributed to each club |
| `GET /api/player?player_id=&side=&team_id=` | one player's game log |

---

## Commands

```
python -m mlbdelta [--db PATH] <command>

  ingest   --season 2026 [--game-types R] [--refresh] [--workers 4]
           [--sleep 0.1] [--limit N]
  demo     [--season 2026] [--days 162] [--seed N]
  report   [--team NYY|147|"New York Yankees"] [--metric total|per_game|rate]
           [--top 10] [--min-games 3]
  serve    [--host 127.0.0.1] [--port 8000]
```

`ingest` is incremental and resumable: games already stored are skipped, and
raw box scores are cached under `data/cache/`, so re-running it mid-season
fetches only what is new. Postseason games come in with
`--game-types R,F,D,L,W`.

---

## Layout

```
mlbdelta/
  statsapi.py   HTTP client for statsapi.mlb.com — retries, throttling, disk cache
  ingest.py     schedule → box scores → player game logs
  db.py         SQLite schema
  scoring.py    the game ratings and the league context they are measured against
  analytics.py  deltas, best-game attribution, leaderboards
  server.py     JSON API + static file serving
  demo.py       the fictional season
  static/       the dashboard
tests/          70 tests, standard-library unittest
```

```bash
python -m unittest discover -s tests
```

---

## What this does not do

- **No park or opponent adjustment.** That is deliberate: the dashboard exists
  to attribute performance *to the opponent*, so adjusting the opponent away
  would be circular. It does mean a hitter's Coors Field games flatter him.
- **No quality-of-pitcher control.** A hitter may look like he owns a club
  mostly because he keeps drawing their fifth starter.
- **Deltas are not evidence of a real effect.** With 500-odd qualified batters
  and 30 clubs, the top of the leaderboard is partly noise no matter how the
  season went. Read it as "who has been doing this", not "who will keep doing
  it".
- **Runs, not earned runs**, for pitchers — a game score should describe what
  happened on the field. Game Score v2 sits beside it if you want the
  dominance-flavoured view.
- The MLB Stats API is public and undocumented; it can change shape without
  notice. Parsing is defensive, but a schema change is a schema change.

Data comes from the MLB Stats API. This project is not affiliated with or
endorsed by MLB.
