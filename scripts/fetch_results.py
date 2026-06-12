#!/usr/bin/env python3
"""Sync World Cup scores from the worldcup26.ir feed into results.json.

Runs hourly via GitHub Actions. Syncs finished group-stage games into
results["groups"] and finished knockout games (Round of 32 through the Final)
into results["ko"]. Scores recorded are regulation plus extra-time goals only;
the feed carries no penalty-shootout data, so a knockout game that is level
after extra time cannot have its advancing team determined and fails the run
rather than guessing. The third-place playoff is intentionally not scored and
is skipped.

Fail-loud contract: any finished game that cannot be fully and confidently
recorded exits non-zero, so the workflow fails and the repo owner is alerted
instead of the leaderboard silently freezing or storing a partial result. A
partial or silent skip of a finished game is itself a failure. The run exits
non-zero, naming the offending raw data, when a finished game has a team name
that does not resolve to a canonical name, a knockout game whose stage cannot
be identified, a knockout game level after extra time, or any missing or
non-numeric score.
"""
import datetime
import json
import re
import sys
import unicodedata
import urllib.request

API = "https://worldcup26.ir/get/games"
RESULTS = "results.json"

# Stage discriminator is the feed's lowercase "type" field. These five values
# are the knockout rounds the pool scores. "group" is handled separately;
# "third" (third-place playoff) is intentionally not scored and skipped.
STAGE = {"r32": "R32", "r16": "R16", "qf": "QF", "sf": "SF", "final": "Final"}

# Feed spellings that accent/punctuation-insensitive matching alone cannot
# reconcile to the pool's canonical names, applied as an explicit override on
# top of that matching. Seeded with plausible variants the feed might switch
# to so a spelling change does not silently break the sync.
ALIAS = {
    "Czech Republic": "Czechia",
    "South Korea": "Korea Republic",
    "Republic of Korea": "Korea Republic",
    "Democratic Republic of the Congo": "DR Congo",
    "Turkey": "Turkiye",
    "Curaçao": "Curacao",
    "USA": "United States",
    "US": "United States",
    "United States of America": "United States",
    "IR Iran": "Iran",
    "Islamic Republic of Iran": "Iran",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Cote d'Ivoire": "Ivory Coast",
    "Côte d'Ivoire": "Ivory Coast",
    "Cabo Verde": "Cape Verde",
}


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def norm(s):
    """Accent/punctuation-insensitive key: drop diacritics, lowercase, keep
    only alphanumerics. 'Curaçao' and 'CURACAO' both become 'curacao'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def build_resolver(canonical):
    canon_by_norm = {norm(c): c for c in canonical}
    alias_by_norm = {norm(k): v for k, v in ALIAS.items()}

    def resolve(raw):
        if not raw or not raw.strip():
            return None
        key = norm(raw)
        if key in alias_by_norm:
            return alias_by_norm[key]
        return canon_by_norm.get(key)

    return resolve


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
    resolve = build_resolver(our_teams)

    group_scores = {}   # frozenset(pair) -> {team: score}
    group_labels = {}   # frozenset(pair) -> "Home vs Away"
    ko_games = []       # [(stage, home, away, home_score, away_score)]
    group_finished = 0

    for game in games:
        if str(game.get("finished", "")).upper() != "TRUE":
            continue
        gtype = str(game.get("type", "group")).lower()
        home_raw = game.get("home_team_name_en")
        away_raw = game.get("away_team_name_en")

        if gtype == "third":
            continue  # third-place playoff intentionally not scored

        is_group = gtype == "group"
        if not is_group and gtype not in STAGE:
            fail(
                f"Finished knockout game with unrecognized stage type {gtype!r} "
                f"(group={game.get('group')!r}, id={game.get('id')!r}); "
                f"cannot map to a scoring stage"
            )
        stage = "group" if is_group else STAGE[gtype]

        home = resolve(home_raw)
        away = resolve(away_raw)
        if home is None or away is None:
            bad = [r for r, c in ((home_raw, home), (away_raw, away)) if c is None]
            fail(
                f"Finished {stage} game with unresolvable team name(s) {bad!r} "
                f"(home={home_raw!r} away={away_raw!r}); add to ALIAS"
            )

        hs, as_ = to_int(game.get("home_score")), to_int(game.get("away_score"))
        if hs is None or as_ is None:
            fail(
                f"Finished {stage} game without numeric score: {home} vs {away} "
                f"(home_score={game.get('home_score')!r} away_score={game.get('away_score')!r})"
            )

        if is_group:
            group_finished += 1
            key = frozenset((home, away))
            group_scores[key] = {home: hs, away: as_}
            group_labels[key] = f"{home} vs {away}"
        else:
            if hs == as_:
                fail(
                    f"Finished {stage} game level after extra time "
                    f"({home} {hs}-{as_} {away}); feed carries no penalty-shootout "
                    f"result, cannot determine who advanced"
                )
            ko_games.append((stage, home, away, hs, as_))

    changed = False

    # Group stage: match each finished game to its scheduled fixture by the
    # unordered team pair. A finished group game absent from the bracket is a
    # finished game we could not record, so it fails loud.
    matched_pairs = set()
    matched = 0
    for matches in results["groups"].values():
        for m in matches:
            key = frozenset((m["a"], m["b"]))
            sc = group_scores.get(key)
            if not sc:
                continue
            matched_pairs.add(key)
            matched += 1
            sa, sb = sc[m["a"]], sc[m["b"]]
            if m.get("sa") != sa or m.get("sb") != sb:
                m["sa"], m["sb"] = sa, sb
                changed = True

    unrecorded = [group_labels[k] for k in group_scores if k not in matched_pairs]
    if unrecorded:
        fail(f"Finished group game(s) not present in the bracket, cannot record: {unrecorded}")

    # Knockout stage: idempotent upsert into results["ko"] keyed on the
    # unordered team pair (a pair meets at most once in single elimination).
    ko = results.setdefault("ko", [])
    ko_synced = 0
    for stage, home, away, hs, as_ in ko_games:
        ko_synced += 1
        entry = {"stage": stage, "a": home, "b": away, "sa": hs, "sb": as_}
        existing = next(
            (e for e in ko if frozenset((e.get("a"), e.get("b"))) == frozenset((home, away))),
            None,
        )
        if existing is None:
            ko.append(entry)
            changed = True
        elif {k: existing.get(k) for k in entry} != entry:
            existing.clear()
            existing.update(entry)
            changed = True

    print(f"Finished group games in feed: {group_finished} | matched into bracket: {matched}")
    print(f"Knockout games synced: {ko_synced}")

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
