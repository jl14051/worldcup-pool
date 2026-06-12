#!/usr/bin/env python3
"""Sync group-stage scores from the worldcup26.ir feed into results.json.

Runs hourly via GitHub Actions. Any failure (network, bad shape, unmapped
team name, non-numeric score) exits non-zero so the workflow fails and the
repo owner gets alerted rather than the leaderboard silently freezing.
"""
import datetime
import json
import sys
import urllib.request

API = "https://worldcup26.ir/get/games"
RESULTS = "results.json"

# Feed spells a handful of teams differently than the pool app.
ALIAS = {
    "Czech Republic": "Czechia",
    "South Korea": "Korea Republic",
    "Democratic Republic of the Congo": "DR Congo",
    "Turkey": "Turkiye",
    "Curaçao": "Curacao",
}


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def canon(name):
    if not name:
        return None
    name = name.strip()
    return ALIAS.get(name, name)


def to_int(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def fetch_games():
    try:
        req = urllib.request.Request(API, headers={"User-Agent": "wc-pool-bot"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
    except Exception as e:
        fail(f"API request failed: {e}")
    try:
        data = json.loads(raw)
    except Exception as e:
        fail(f"API returned invalid JSON: {e}")
    games = data.get("games")
    if not isinstance(games, list) or len(games) < 104:
        n = len(games) if isinstance(games, list) else "n/a"
        fail(f"Unexpected API shape: games count = {n}")
    return games


def main():
    games = fetch_games()
    with open(RESULTS) as f:
        results = json.load(f)

    our_teams = set()
    for matches in results["groups"].values():
        for m in matches:
            our_teams.add(m["a"])
            our_teams.add(m["b"])

    # Index finished GROUP games by the unordered team pair -> {team: score}.
    # Knockout games carry a different bracket structure and are handled
    # separately once that stage begins, so they're skipped here.
    finished = {}
    unmapped = set()
    for game in games:
        if str(game.get("finished", "")).upper() != "TRUE":
            continue
        if str(game.get("type", "group")).lower() != "group":
            continue
        home = canon(game.get("home_team_name_en"))
        away = canon(game.get("away_team_name_en"))
        if not home or not away:
            continue
        for t in (home, away):
            if t not in our_teams:
                unmapped.add(t)
        hs, as_ = to_int(game.get("home_score")), to_int(game.get("away_score"))
        if hs is None or as_ is None:
            fail(f"Finished game without numeric score: {home} vs {away}")
        finished[frozenset((home, away))] = {home: hs, away: as_}

    if unmapped:
        fail(f"Unmapped team names from API (add to ALIAS): {sorted(unmapped)}")

    changed = False
    matched = 0
    for matches in results["groups"].values():
        for m in matches:
            sc = finished.get(frozenset((m["a"], m["b"])))
            if not sc:
                continue
            matched += 1
            sa, sb = sc[m["a"]], sc[m["b"]]
            if m.get("sa") != sa or m.get("sb") != sb:
                m["sa"], m["sb"] = sa, sb
                changed = True

    print(f"Finished group games in feed: {len(finished)} | matched into bracket: {matched}")

    if changed:
        results["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        with open(RESULTS, "w") as f:
            json.dump(results, f, separators=(",", ":"))
        print("results.json updated")
    else:
        print("no changes")


if __name__ == "__main__":
    main()
