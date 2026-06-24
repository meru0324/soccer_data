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

# football-data.org のアクセス制限を避けるための待機時間
REQUEST_INTERVAL_SECONDS = 10
TEAM_DETAIL_INTERVAL_SECONDS = 7

JAPANESE_NATIONALITIES = {
    "japan",
    "japanese",
}


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


def save_squad_if_present(
    squads_by_team_id: dict,
    team: dict,
) -> None:
    if not isinstance(team, dict):
        return

    team_id = team.get("id")
    squad = team.get("squad")

    if (
        isinstance(team_id, int)
        and isinstance(squad, list)
        and squad
    ):
        squads_by_team_id[team_id] = squad


def add_teams_from_matches(
    teams_by_key: dict,
) -> set[int]:
    matches_path = Path("matches.json")
    match_team_ids: set[int] = set()

    if not matches_path.exists():
        print(
            "matches.json was not found. "
            "Skipping match team import."
        )
        return match_team_ids

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
        return match_team_ids

    matches = data.get("matches", [])

    if not isinstance(matches, list):
        return match_team_ids

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

        for side in ["homeTeam", "awayTeam"]:
            team = match.get(side)

            merge_team(
                teams_by_key,
                team,
                competition_code,
            )

            if isinstance(team, dict):
                team_id = team.get("id")

                # WCの代表チームはクラブ所属判定から除外する
                if (
                    isinstance(team_id, int)
                    and competition_code != "WC"
                ):
                    match_team_ids.add(team_id)

    return match_team_ids


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


def fetch_team_details(
    token: str,
    team_id: int,
) -> dict:
    url = f"{API_BASE_URL}/teams/{team_id}"
    return request_json(url, token)


def is_japanese_player(player: dict) -> bool:
    nationality = clean_text(
        player.get("nationality")
    )

    if nationality is None:
        return False

    return (
        nationality.casefold()
        in JAPANESE_NATIONALITIES
    )


def normalize_player(player: dict) -> dict:
    return {
        "id": player.get("id"),
        "name": clean_text(player.get("name")),
        "position": clean_text(
            player.get("position")
        ),
        "dateOfBirth": clean_text(
            player.get("dateOfBirth")
        ),
        "nationality": clean_text(
            player.get("nationality")
        ),
    }


def load_existing_player_teams() -> dict:
    path = Path("japanese_players.json")

    if not path.exists():
        return {}

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}

    result = {}

    for item in data.get("teams", []):
        if not isinstance(item, dict):
            continue

        team_id = item.get("teamId")

        if isinstance(team_id, int):
            result[team_id] = item

    return result


def build_japanese_player_data(
    token: str,
    teams: list,
    squads_by_team_id: dict,
    match_team_ids: set[int],
) -> tuple[list, list]:
    existing_by_team_id = (
        load_existing_player_teams()
    )
    errors = []
    player_teams = []

    teams_by_id = {
        team.get("id"): team
        for team in teams
        if isinstance(team.get("id"), int)
    }

    candidate_ids = sorted(
        team_id
        for team_id in match_team_ids
        if team_id in teams_by_id
    )

    print(
        "Japanese player scan targets: "
        f"{len(candidate_ids)} teams."
    )

    detail_request_count = 0

    for team_id in candidate_ids:
        team = teams_by_id[team_id]
        squad = squads_by_team_id.get(team_id)

        if not squad:
            if detail_request_count > 0:
                time.sleep(
                    TEAM_DETAIL_INTERVAL_SECONDS
                )

            try:
                details = fetch_team_details(
                    token,
                    team_id,
                )
                squad = details.get("squad")

                if not isinstance(squad, list):
                    squad = []

                detail_request_count += 1

            except RuntimeError as error:
                message = (
                    f"team {team_id}: {error}"
                )
                errors.append(message)
                print(
                    f"Warning: {message}",
                    file=sys.stderr,
                )

                previous = (
                    existing_by_team_id.get(team_id)
                )

                if previous is not None:
                    player_teams.append(previous)

                continue

        japanese_players = [
            normalize_player(player)
            for player in squad
            if (
                isinstance(player, dict)
                and is_japanese_player(player)
            )
        ]

        japanese_players.sort(
            key=lambda player: (
                player.get("name") or ""
            ).casefold()
        )

        if not japanese_players:
            continue

        player_teams.append(
            {
                "teamId": team_id,
                "teamName": (
                    team.get("shortName")
                    or team.get("name")
                ),
                "competitionCodes": (
                    team.get("competitionCodes")
                    or []
                ),
                "players": japanese_players,
            }
        )

        print(
            f"Japanese players found: "
            f"{team.get('shortName') or team.get('name')} "
            f"({len(japanese_players)})"
        )

    player_teams.sort(
        key=lambda item: (
            item.get("teamName") or ""
        ).casefold()
    )

    return player_teams, errors


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
    squads_by_team_id = {}
    errors = []

    # 90日間の試合に登場するクラブIDを収集
    match_team_ids = add_teams_from_matches(
        teams_by_key
    )

    for index, competition_code in enumerate(
        COMPETITIONS
    ):
        try:
            competition_teams = (
                fetch_competition_teams(
                    token,
                    competition_code,
                )
            )

            for team in competition_teams:
                merge_team(
                    teams_by_key,
                    team,
                    competition_code,
                )
                save_squad_if_present(
                    squads_by_team_id,
                    team,
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
        ).casefold()
    )

    updated_at = datetime.now(
        timezone.utc
    ).isoformat()

    teams_output = {
        "updatedAt": updated_at,
        "competitions": COMPETITIONS,
        "count": len(teams),
        "errors": errors,
        "teams": teams,
    }

    Path("teams.json").write_text(
        json.dumps(
            teams_output,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    player_teams, player_errors = (
        build_japanese_player_data(
            token,
            teams,
            squads_by_team_id,
            match_team_ids,
        )
    )

    player_count = sum(
        len(item.get("players", []))
        for item in player_teams
    )

    players_output = {
        "updatedAt": updated_at,
        "teamCount": len(player_teams),
        "playerCount": player_count,
        "errors": player_errors,
        "teams": player_teams,
    }

    Path("japanese_players.json").write_text(
        json.dumps(
            players_output,
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
    print(
        "Created japanese_players.json with "
        f"{player_count} players across "
        f"{len(player_teams)} teams."
    )


if __name__ == "__main__":
    main()
