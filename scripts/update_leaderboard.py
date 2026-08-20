"""Refreshes index.html with live BMW Championship scores.

Reads the current index.html to recover the roster (12 teams, 5 players
each, in Tier 1-5 order) and the exact row markup, fetches the live
leaderboard from the Slash Golf API (live-golf-data on RapidAPI), sums
each team's players' score-to-par, re-ranks the teams, and rewrites just
the <tbody> and the "Last refreshed" line. Everything else in the file
(styles, header, footer) is left untouched.

Exits non-zero (without touching index.html) if the leaderboard can't be
fetched or a team's roster can't be recovered, so a bad/empty API
response never gets committed.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MOUNTAIN_TZ = ZoneInfo("America/Edmonton")

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"

LEADERBOARD_URL = (
    "https://live-golf-data.p.rapidapi.com/leaderboard"
    "?orgId=1&tournId=028&year=2026"
)


def fetch_leaderboard(api_key: str) -> dict:
    req = urllib.request.Request(
        LEADERBOARD_URL,
        headers={
            "x-rapidapi-host": "live-golf-data.p.rapidapi.com",
            "x-rapidapi-key": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def unwrap_bson(v):
    """Slash Golf wraps numbers as Mongo extended JSON, e.g. {"$numberInt": "4"}."""
    if isinstance(v, dict):
        for key in ("$numberInt", "$numberLong", "$numberDouble", "$numberDecimal"):
            if key in v:
                return v[key]
    return v


def parse_score(raw) -> int | None:
    v = unwrap_bson(raw)
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, str) and v.strip().upper() in ("E", "EVEN"):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def build_player_index(leaderboard: dict) -> dict[str, dict]:
    rows = leaderboard.get("leaderboardRows") or []
    index = {}
    for row in rows:
        first = str(row.get("firstName") or "").strip()
        last = str(row.get("lastName") or "").strip()
        full_name = f"{first} {last}".strip()
        if full_name:
            index[full_name.lower()] = row
    return index


def player_display(name: str, player_index: dict[str, dict]) -> dict:
    row = player_index.get(name.lower())
    if row is None:
        return {"score": None, "score_class": "", "score_display": "—", "status": "not started"}

    score = parse_score(row.get("total"))
    status_raw = str(row.get("status") or "").strip().lower()
    thru = unwrap_bson(row.get("thru"))
    thru = "" if thru is None else str(thru).strip()

    if score is None:
        status = "not started"
    elif status_raw == "cut":
        status = "cut"
    elif status_raw == "wd":
        status = "wd"
    elif thru in ("", "-", "0") and status_raw not in ("complete", "official"):
        status = "not started"
    elif thru.upper() == "F" or status_raw in ("complete", "official"):
        status = "F"
    else:
        status = f"thru {thru}" if thru else "thru"

    if score is None:
        score_display, score_class = "—", ""
    elif score == 0:
        score_display, score_class = "E", ""
    elif score > 0:
        score_display, score_class = f"+{score}", "over"
    else:
        score_display, score_class = str(score), "under"

    return {"score": score, "score_class": score_class, "score_display": score_display, "status": status}


TEAM_ROW_RE = re.compile(
    r'<tr class="team-row(?P<leader>(?: leader)?)">\s*'
    r'<td class="rank-cell">[^<]*</td>\s*'
    r'<td class="team-cell">(?P<team>[^<]+)</td>\s*'
    r'<td class="total-cell[^"]*">[^<]*</td>\s*'
    r'(?P<tiers>(?:<td class="tier-cell">.*?</td>\s*){5})'
    r'</tr>',
    re.DOTALL,
)
TIER_PLAYER_RE = re.compile(r'<div class="tier-player">([^<]+)</div>')


def parse_roster(html: str) -> list[dict]:
    teams = []
    for match in TEAM_ROW_RE.finditer(html):
        players = TIER_PLAYER_RE.findall(match.group("tiers"))
        if len(players) != 5:
            raise ValueError(f"team {match.group('team')!r} does not have exactly 5 tier players")
        teams.append({"name": match.group("team").strip(), "players": players})
    if len(teams) != 12:
        raise ValueError(f"expected 12 teams in index.html, found {len(teams)}")
    return teams


TIER_CELL_TEMPLATE = """            <td class="tier-cell">
              <div class="tier-player">{player}</div>
              <div class="tier-meta"><span class="player-score {score_class}">{score_display}</span><span class="player-status">{status}</span></div>
            </td>"""

TEAM_ROW_TEMPLATE = """          <tr class="team-row{leader_class}">
            <td class="rank-cell">{rank}</td>
            <td class="team-cell">{team}</td>
            <td class="total-cell {total_class}">{total_display}</td>
{tier_cells}
          </tr>"""


def format_total(total: int) -> tuple[str, str]:
    if total == 0:
        return "E", ""
    if total > 0:
        return f"+{total}", "over"
    return str(total), "under"


def assign_ranks(standings: list[dict]) -> None:
    rank_counter = 1
    i = 0
    while i < len(standings):
        j = i
        while j + 1 < len(standings) and standings[j + 1]["total"] == standings[i]["total"]:
            j += 1
        group_size = j - i + 1
        label = str(rank_counter) if group_size == 1 else f"T{rank_counter}"
        for k in range(i, j + 1):
            standings[k]["rank"] = label
        rank_counter += group_size
        i = j + 1


def render_tbody(teams: list[dict], player_index: dict[str, dict]) -> str:
    standings = []
    for team in teams:
        details = [player_display(p, player_index) for p in team["players"]]
        total = sum(d["score"] or 0 for d in details)
        standings.append({"name": team["name"], "players": team["players"], "details": details, "total": total})

    standings.sort(key=lambda t: t["total"])
    assign_ranks(standings)

    rows = []
    for idx, team in enumerate(standings):
        total_display, total_class = format_total(team["total"])
        tier_cells = "\n".join(
            TIER_CELL_TEMPLATE.format(
                player=player,
                score_class=detail["score_class"],
                score_display=detail["score_display"],
                status=detail["status"],
            )
            for player, detail in zip(team["players"], team["details"])
        )
        rows.append(
            TEAM_ROW_TEMPLATE.format(
                leader_class=" leader" if idx == 0 else "",
                rank=team["rank"],
                team=team["name"],
                total_class=total_class,
                total_display=total_display,
                tier_cells=tier_cells,
            )
        )
    return "\n\n" + "\n\n".join(rows) + "\n"


def main() -> int:
    api_key = None
    import os

    api_key = os.environ.get("SLASHGOLF_KEY")
    if not api_key:
        print("SLASHGOLF_KEY is not set", file=sys.stderr)
        return 1

    html = INDEX_HTML.read_text(encoding="utf-8")

    try:
        teams = parse_roster(html)
    except ValueError as exc:
        print(f"Failed to parse roster from index.html: {exc}", file=sys.stderr)
        return 1

    try:
        leaderboard = fetch_leaderboard(api_key)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to fetch leaderboard: {exc}", file=sys.stderr)
        return 1

    player_index = build_player_index(leaderboard)
    if not player_index:
        print("Leaderboard response had no leaderboardRows; refusing to update", file=sys.stderr)
        return 1

    new_tbody = render_tbody(teams, player_index)
    html = re.sub(r"(<tbody>).*?(</tbody>)", lambda m: m.group(1) + new_tbody + m.group(2), html, flags=re.DOTALL)

    now = datetime.now(timezone.utc).astimezone(MOUNTAIN_TZ)
    stamp = now.strftime("%a %b %-d, %-I:%M %p %Z")
    html = re.sub(
        r"(Last refreshed <strong>)[^<]*(</strong>)",
        lambda m: m.group(1) + stamp + m.group(2),
        html,
    )

    INDEX_HTML.write_text(html, encoding="utf-8")
    print(f"index.html updated ({stamp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
