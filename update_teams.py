import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE_URL = "https://api.football-data.org/v4"

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

# football-data.orgのアクセス制限を避けるため、
# 各大会の取得間隔を空ける
REQUEST_INTERVAL_SECONDS = 10


def request_json(url: str, token: str) -> dict:
    request = Request(
        url,
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
            f"HTTP {error.code}: {error_body}"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Could not connect: {error}"
        ) from error

    data = json.loads(body)

    if not isinstance(data, dict):
        raise RuntimeError(
            "API response was not a JSON object."
        )

    return data


def clean_text(value):
    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() == "null":
        return None

    return text


def merge_team(
    teams_by_key: dict,
    team: dict,
    competition_code: str | None,
) -> None:
    if not isinstance(team, dict):
        return

    team_id = team.get("id")
    name = clean_text(team.get("name"))
    short_name = clean_text(team.get("shortName"))
    tla = clean_text(team.get("tla"))

    if team_id is not None:
        key = f"id:{team_id}"
    elif short_name:
        key = f"shortName:{short_name}"
    elif name:
        key = f"name:{name}"
    else:
        return

    if key not in teams_by_key:
        teams_by_key[key] = {
            "id": team_id,
            "name": name,
            "shortName": short_name,
            "tla": tla,
            "crest": clean_text(team.get("crest")),
            "area": team.get("area"),
            "competitionCodes": [],
        }

    saved_team = teams_by_key[key]

    for field in [
        "id",
        "name",
        "shortName",
        "tla",
        "crest",
        "area",
    ]:
        current_value = saved_team.get(field)
        new_value = team.get(field)

        if current_value in [None, ""] and new_value not in [None, ""]:
            saved_team[field] = new_value

    if (
        competition_code
        and competition_code
        not in saved_team["competitionCodes"]
    ):
        saved_team["competitionCodes"].append(
            competition_code
        )


def add_teams_from_matches(
    teams_by_key: dict,
) -> None:
    matches_path = Path("matches.json")

    if not matches_path.exists():
        print(
            "matches.json was not found. "
            "Skipping match team import."
        )
        return

    try:
        data = json.loads(
            matches_path.read_text(
                encoding="utf-8",
            )
        )
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read matches.json: {error}"
        )
        return

    matches = data.get("matches", [])

    if not isinstance(matches, list):
        return

    for match in matches:
        if not isinstance(match, dict):
            continue

        competition = match.get("competition")

        if isinstance(competition, dict):
            competition_code = clean_text(
                competition.get("code")
            )
        else:
            competition_code = None

        merge_team(
            teams_by_key,
            match.get("homeTeam"),
            competition_code,
        )
        merge_team(
            teams_by_key,
            match.get("awayTeam"),
            competition_code,
        )


def fetch_competition_teams(
    token: str,
    competition_code: str,
) -> list:
    url = (
        f"{API_BASE_URL}/competitions/"
        f"{competition_code}/teams"
    )

    data = request_json(url, token)
    teams = data.get("teams")

    if not isinstance(teams, list):
        raise RuntimeError(
            "API response did not contain "
            "a teams list."
        )

    print(
        f"{competition_code}: "
        f"Fetched {len(teams)} teams."
    )

    return teams


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

    teams_by_key = {}
    errors = []

    # 先に現在の試合データからチームを登録
    add_teams_from_matches(teams_by_key)

    for index, competition_code in enumerate(
        COMPETITIONS
    ):
        try:
            teams = fetch_competition_teams(
                token,
                competition_code,
            )

            for team in teams:
                merge_team(
                    teams_by_key,
                    team,
                    competition_code,
                )

        except RuntimeError as error:
            message = (
                f"{competition_code}: {error}"
            )
            errors.append(message)
            print(
                f"Warning: {message}",
                file=sys.stderr,
            )

        if index < len(COMPETITIONS) - 1:
            time.sleep(
                REQUEST_INTERVAL_SECONDS
            )

    teams = list(teams_by_key.values())

    for team in teams:
        team["competitionCodes"].sort()

    teams.sort(
        key=lambda team: (
            team.get("shortName")
            or team.get("name")
            or ""
        ).lower()
    )

    output = {
        "updatedAt": datetime.now(
            timezone.utc
        ).isoformat(),
        "competitions": COMPETITIONS,
        "count": len(teams),
        "errors": errors,
        "teams": teams,
    }

    Path("teams.json").write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"Created teams.json with "
        f"{len(teams)} teams."
    )


if __name__ == "__main__":
    main()