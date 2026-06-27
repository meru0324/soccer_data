import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://api.football-data.org/v4/matches"

COMPETITIONS = [
    "WC",
    "CL",
    "PL",
    "ELC",
    "PD",
    "BL1",
    "SA",
    "FL1",
    "DED",
    "PPL",
]

MAX_DAYS_PER_REQUEST = 10
FUTURE_DAYS = 90
REQUEST_DELAY_SECONDS = 7


PLACEHOLDER_TEAM_NAMES = {
    "",
    "tbd",
    "to be decided",
    "unknown",
    "未定",
    "null",
    "none",
}


def normalized_team_text(value: object) -> str:
    if value is None:
        return ""

    return str(value).strip().lower()


def is_resolved_team(team: object) -> bool:
    if not isinstance(team, dict):
        return False

    team_id = team.get("id")

    if isinstance(team_id, int) and team_id > 0:
        return True

    candidates = [
        team.get("name"),
        team.get("shortName"),
        team.get("tla"),
    ]

    for candidate in candidates:
        normalized = normalized_team_text(candidate)

        if not normalized:
            continue

        if normalized in PLACEHOLDER_TEAM_NAMES:
            continue

        if normalized.startswith("winner of "):
            continue

        if normalized.startswith("loser of "):
            continue

        if normalized.startswith("winner "):
            continue

        if normalized.startswith("loser "):
            continue

        return True

    return False


def load_previous_matches(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read previous {path.name}: {error}",
            file=sys.stderr,
        )
        return {}

    if not isinstance(raw, dict):
        return {}

    matches = raw.get("matches")

    if not isinstance(matches, list):
        return {}

    previous_by_id = {}

    for match in matches:
        if not isinstance(match, dict):
            continue

        match_id = match.get("id")

        if match_id is None:
            continue

        previous_by_id[match_id] = match

    return previous_by_id


def preserve_confirmed_teams(
    fresh_matches: list,
    previous_by_id: dict,
) -> int:
    preserved_slots = 0

    for fresh_match in fresh_matches:
        if not isinstance(fresh_match, dict):
            continue

        match_id = fresh_match.get("id")

        if match_id is None:
            continue

        previous_match = previous_by_id.get(match_id)

        if not isinstance(previous_match, dict):
            continue

        for side in ("homeTeam", "awayTeam"):
            fresh_team = fresh_match.get(side)
            previous_team = previous_match.get(side)

            if (
                not is_resolved_team(fresh_team)
                and is_resolved_team(previous_team)
            ):
                fresh_match[side] = previous_team
                preserved_slots += 1

                print(
                    "Preserved confirmed team: "
                    f"match={match_id}, side={side}, "
                    f"team={previous_team.get('name')}"
                )

    return preserved_slots


def fetch_period(
    token: str,
    date_from: date,
    date_to: date,
) -> list:
    query = urlencode(
        {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "competitions": ",".join(COMPETITIONS),
        }
    )

    request = Request(
        f"{API_URL}?{query}",
        headers={
            "X-Auth-Token": token,
            "User-Agent": "soccer-data-updater/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        error_body = error.read().decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            f"football-data.org returned "
            f"HTTP {error.code}: {error_body}"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Could not connect to "
            f"football-data.org: {error}"
        ) from error

    data = json.loads(body)

    if not isinstance(data, dict):
        raise RuntimeError(
            "API response was not a JSON object."
        )

    matches = data.get("matches")

    if not isinstance(matches, list):
        raise RuntimeError(
            "API response did not contain "
            "a matches list."
        )

    print(
        f"Fetched {len(matches)} matches: "
        f"{date_from.isoformat()} "
        f"to {date_to.isoformat()}"
    )

    return matches


def fetch_matches(
    token: str,
    previous_by_id: dict,
) -> dict:
    now_utc = datetime.now(timezone.utc)

    overall_start = (
        now_utc.date() - timedelta(days=1)
    )
    overall_end = (
        now_utc.date() + timedelta(days=FUTURE_DAYS)
    )

    all_matches = []
    seen_match_ids = set()
    current_start = overall_start

    while current_start <= overall_end:
        current_end = min(
            current_start
            + timedelta(
                days=MAX_DAYS_PER_REQUEST - 1
            ),
            overall_end,
        )

        period_matches = fetch_period(
            token,
            current_start,
            current_end,
        )

        for match in period_matches:
            if not isinstance(match, dict):
                continue

            match_id = match.get("id")

            if match_id is not None:
                if match_id in seen_match_ids:
                    continue

                seen_match_ids.add(match_id)

            all_matches.append(match)

        current_start = (
            current_end + timedelta(days=1)
        )

        if current_start <= overall_end:
            time.sleep(REQUEST_DELAY_SECONDS)

    if not all_matches:
        raise RuntimeError(
            "Fetched zero matches. "
            "Keeping the previous matches.json."
        )

    preserved_slots = preserve_confirmed_teams(
        all_matches,
        previous_by_id,
    )

    all_matches.sort(
        key=lambda match: (
            match.get("utcDate", "")
            if isinstance(match, dict)
            else ""
        )
    )

    return {
        "updatedAt": now_utc.isoformat(),
        "dateFrom": overall_start.isoformat(),
        "dateTo": overall_end.isoformat(),
        "competitions": COMPETITIONS,
        "count": len(all_matches),
        "preservedConfirmedTeamSlots": preserved_slots,
        "matches": all_matches,
    }


def main() -> None:
    token = os.environ.get(
        "FOOTBALL_DATA_TOKEN",
        "",
    ).strip()

    if not token:
        print(
            "FOOTBALL_DATA_TOKEN is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path = Path("matches.json")
    previous_by_id = load_previous_matches(output_path)

    print(
        f"Loaded {len(previous_by_id)} previous matches "
        "for regression protection."
    )

    output = fetch_matches(
        token,
        previous_by_id,
    )

    output_path.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"Created matches.json with "
        f"{output['count']} matches. "
        "Preserved confirmed team slots: "
        f"{output['preservedConfirmedTeamSlots']}."
    )


if __name__ == "__main__":
    main()
