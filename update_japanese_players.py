import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_BASE_URL = "https://v3.football.api-sports.io"
STATE_PATH = Path("api_football_state.json")
TEAMS_PATH = Path("teams.json")
OUTPUT_PATH = Path("japanese_players.json")
OVERRIDES_PATH = Path("japanese_players_overrides.json")

# Free plan: 100 requests/day. Keep a safety margin.
MAX_REQUESTS_PER_RUN = 90
REQUEST_INTERVAL_SECONDS = 0.8

# The free plan test confirmed season 2024 is available.
# These league IDs are stable API-Football identifiers.
BOOTSTRAP_LEAGUES = [
    {"code": "PL", "name": "Premier League", "id": 39, "season": 2024},
    {"code": "ELC", "name": "Championship", "id": 40, "season": 2024},
    {"code": "PD", "name": "La Liga", "id": 140, "season": 2024},
    {"code": "BL1", "name": "Bundesliga", "id": 78, "season": 2024},
    {"code": "SA", "name": "Serie A", "id": 135, "season": 2024},
    {"code": "FL1", "name": "Ligue 1", "id": 61, "season": 2024},
    {"code": "DED", "name": "Eredivisie", "id": 88, "season": 2024},
    {"code": "PPL", "name": "Primeira Liga", "id": 94, "season": 2024},
    {"code": "CL", "name": "UEFA Champions League", "id": 2, "season": 2024},
]

COMMON_TEAM_WORDS = {
    "fc",
    "afc",
    "cf",
    "ac",
    "sc",
    "club",
    "football",
    "futbol",
    "calcio",
    "de",
    "the",
}


class ApiClient:
    def __init__(self, api_key: str, max_requests: int) -> None:
        self.api_key = api_key
        self.max_requests = max_requests
        self.request_count = 0

    def can_request(self) -> bool:
        return self.request_count < self.max_requests

    def get(self, endpoint: str) -> dict:
        if not self.can_request():
            raise RuntimeError("Request budget exhausted.")

        url = f"{API_BASE_URL}{endpoint}"
        request = Request(
            url,
            headers={
                "x-apisports-key": self.api_key,
                "User-Agent": "soccer-data-updater/1.0",
            },
        )

        try:
            with urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {error.code}: {error_body}"
            ) from error
        except URLError as error:
            raise RuntimeError(f"Could not connect: {error}") from error

        self.request_count += 1
        time.sleep(REQUEST_INTERVAL_SECONDS)

        data = json.loads(body)

        if not isinstance(data, dict):
            raise RuntimeError("API response was not a JSON object.")

        errors = data.get("errors")

        if isinstance(errors, dict) and errors:
            raise RuntimeError(
                "API error: "
                + "; ".join(f"{key}: {value}" for key, value in errors.items())
            )

        if isinstance(errors, list) and errors:
            raise RuntimeError(
                "API error: " + "; ".join(str(value) for value in errors)
            )

        return data


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def default_state() -> dict:
    return {
        "version": 1,
        "phase": "bootstrap",
        "bootstrapLeagueIndex": 0,
        "bootstrapPage": 1,
        "bootstrapProgress": {},
        "playerProfiles": {},
        "apiTeams": {},
        "teamMap": {},
        "squads": {},
        "squadCursor": 0,
        "pendingProfileIds": [],
        "unresolvedTeams": {},
        "lastFullSquadCycleAt": None,
        "recentErrors": [],
        "updatedAt": None,
    }


def normalize_text(value) -> str:
    if value is None:
        return ""

    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [
        token
        for token in text.split()
        if token not in COMMON_TEAM_WORDS
    ]
    return " ".join(tokens).strip()


def compact_text(value) -> str:
    return normalize_text(value).replace(" ", "")


def team_match_score(football_team: dict, api_team: dict) -> int:
    football_names = {
        normalize_text(football_team.get("name")),
        normalize_text(football_team.get("shortName")),
    }
    football_names.discard("")

    api_name = normalize_text(api_team.get("name"))
    if not api_name:
        return 0

    if api_name in football_names:
        return 100

    api_compact = api_name.replace(" ", "")

    for name in football_names:
        if name.replace(" ", "") == api_compact:
            return 96

    for name in football_names:
        if len(name) >= 4 and (name in api_name or api_name in name):
            return 84

    api_tokens = set(api_name.split())
    best = 0

    for name in football_names:
        name_tokens = set(name.split())
        if not name_tokens or not api_tokens:
            continue

        overlap = len(name_tokens & api_tokens)
        union = len(name_tokens | api_tokens)
        score = int(70 * overlap / union)
        best = max(best, score)

    return best


def load_club_teams() -> list:
    data = load_json(TEAMS_PATH, {})
    teams = data.get("teams", [])

    if not isinstance(teams, list):
        raise RuntimeError("teams.json did not contain a teams list.")

    clubs = []

    for team in teams:
        if not isinstance(team, dict):
            continue

        team_id = team.get("id")
        codes = team.get("competitionCodes", [])

        if not isinstance(team_id, int):
            continue

        if not isinstance(codes, list):
            continue

        # Exclude World Cup national teams, but retain clubs also in CL.
        if not any(code != "WC" for code in codes):
            continue

        clubs.append(team)

    clubs.sort(
        key=lambda team: (
            str(team.get("shortName") or team.get("name") or "").casefold(),
            team.get("id"),
        )
    )

    return clubs


def remember_profile(state: dict, player: dict) -> None:
    if not isinstance(player, dict):
        return

    player_id = player.get("id")
    if not isinstance(player_id, int):
        return

    birth = player.get("birth")
    if not isinstance(birth, dict):
        birth = {}

    state["playerProfiles"][str(player_id)] = {
        "id": player_id,
        "name": player.get("name"),
        "firstname": player.get("firstname"),
        "lastname": player.get("lastname"),
        "nationality": player.get("nationality"),
        "birthDate": birth.get("date"),
        "photo": player.get("photo"),
        "updatedAt": utc_now_iso(),
    }


def remember_api_team(state: dict, team: dict) -> None:
    if not isinstance(team, dict):
        return

    team_id = team.get("id")
    name = team.get("name")

    if not isinstance(team_id, int) or not name:
        return

    saved = state["apiTeams"].get(str(team_id), {})
    saved.update(
        {
            "id": team_id,
            "name": name,
            "country": team.get("country") or saved.get("country"),
            "logo": team.get("logo") or saved.get("logo"),
        }
    )
    state["apiTeams"][str(team_id)] = saved


def reconcile_team_map(state: dict, clubs: list) -> None:
    api_teams = list(state["apiTeams"].values())

    for club in clubs:
        football_id = str(club["id"])

        if football_id in state["teamMap"]:
            continue

        scored = [
            (team_match_score(club, api_team), api_team)
            for api_team in api_teams
        ]
        scored.sort(key=lambda item: item[0], reverse=True)

        if not scored or scored[0][0] < 90:
            continue

        best_score, best_team = scored[0]

        if len(scored) > 1 and scored[1][0] == best_score:
            continue

        state["teamMap"][football_id] = best_team["id"]


def run_bootstrap(client: ApiClient, state: dict, clubs: list) -> None:
    while (
        client.can_request()
        and state["bootstrapLeagueIndex"] < len(BOOTSTRAP_LEAGUES)
    ):
        league = BOOTSTRAP_LEAGUES[state["bootstrapLeagueIndex"]]
        page = int(state.get("bootstrapPage", 1))

        endpoint = (
            f"/players?league={league['id']}"
            f"&season={league['season']}"
            f"&page={page}"
        )

        print(
            f"Bootstrap {league['name']}: "
            f"page {page}, request {client.request_count + 1}"
        )

        try:
            data = client.get(endpoint)
        except RuntimeError as error:
            state["recentErrors"].append(
                {
                    "at": utc_now_iso(),
                    "phase": "bootstrap",
                    "league": league["code"],
                    "page": page,
                    "message": str(error),
                }
            )
            print(f"Warning: {error}", file=sys.stderr)
            break

        response = data.get("response", [])
        if not isinstance(response, list):
            response = []

        for item in response:
            if not isinstance(item, dict):
                continue

            remember_profile(state, item.get("player"))

            statistics = item.get("statistics", [])
            if not isinstance(statistics, list):
                continue

            for statistic in statistics:
                if not isinstance(statistic, dict):
                    continue
                remember_api_team(state, statistic.get("team"))

        paging = data.get("paging", {})
        total_pages = int(paging.get("total") or 1)

        state["bootstrapProgress"][league["code"]] = {
            "currentPage": page,
            "totalPages": total_pages,
            "completed": page >= total_pages,
        }

        if page >= total_pages:
            state["bootstrapLeagueIndex"] += 1
            state["bootstrapPage"] = 1
        else:
            state["bootstrapPage"] = page + 1

        reconcile_team_map(state, clubs)

    if state["bootstrapLeagueIndex"] >= len(BOOTSTRAP_LEAGUES):
        state["phase"] = "squads"
        state["bootstrapCompletedAt"] = utc_now_iso()
        state["squadCursor"] = 0
        print("Bootstrap completed. Squad scanning will begin.")


def choose_search_result(club: dict, response: list):
    candidates = []

    for item in response:
        if not isinstance(item, dict):
            continue

        team = item.get("team")
        if not isinstance(team, dict):
            continue

        score = team_match_score(club, team)

        area = club.get("area")
        if isinstance(area, dict):
            area_name = normalize_text(area.get("name"))
            country_name = normalize_text(team.get("country"))
            if area_name and country_name and area_name == country_name:
                score += 8

        candidates.append((score, team))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates or candidates[0][0] < 75:
        return None

    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None

    return candidates[0][1]


def resolve_team_id(
    client: ApiClient,
    state: dict,
    club: dict,
):
    football_id = str(club["id"])

    mapped = state["teamMap"].get(football_id)
    if isinstance(mapped, int):
        return mapped

    search_name = club.get("shortName") or club.get("name")
    if not search_name or not client.can_request():
        return None

    endpoint = f"/teams?search={quote(str(search_name))}"

    try:
        data = client.get(endpoint)
    except RuntimeError as error:
        state["unresolvedTeams"][football_id] = {
            "name": search_name,
            "message": str(error),
            "updatedAt": utc_now_iso(),
        }
        return None

    response = data.get("response", [])
    if not isinstance(response, list):
        response = []

    selected = choose_search_result(club, response)

    if selected is None:
        state["unresolvedTeams"][football_id] = {
            "name": search_name,
            "message": "No unambiguous API-Football team match.",
            "updatedAt": utc_now_iso(),
        }
        return None

    remember_api_team(state, selected)
    state["teamMap"][football_id] = selected["id"]
    state["unresolvedTeams"].pop(football_id, None)
    return selected["id"]


def queue_unknown_profiles(state: dict, players: list) -> None:
    pending = set(str(value) for value in state["pendingProfileIds"])
    known = state["playerProfiles"]

    for player in players:
        if not isinstance(player, dict):
            continue

        player_id = player.get("id")
        if not isinstance(player_id, int):
            continue

        if str(player_id) not in known:
            pending.add(str(player_id))

    state["pendingProfileIds"] = sorted(
        pending,
        key=lambda value: int(value),
    )


def fetch_squad(
    client: ApiClient,
    state: dict,
    club: dict,
    api_team_id: int,
) -> bool:
    endpoint = f"/players/squads?team={api_team_id}"

    try:
        data = client.get(endpoint)
    except RuntimeError as error:
        state["recentErrors"].append(
            {
                "at": utc_now_iso(),
                "phase": "squads",
                "footballDataTeamId": club["id"],
                "apiFootballTeamId": api_team_id,
                "message": str(error),
            }
        )
        return False

    response = data.get("response", [])
    if not isinstance(response, list) or not response:
        state["recentErrors"].append(
            {
                "at": utc_now_iso(),
                "phase": "squads",
                "footballDataTeamId": club["id"],
                "apiFootballTeamId": api_team_id,
                "message": "Squad response was empty.",
            }
        )
        return False

    item = response[0]
    if not isinstance(item, dict):
        return False

    api_team = item.get("team")
    players = item.get("players", [])

    if not isinstance(players, list):
        players = []

    remember_api_team(state, api_team)
    queue_unknown_profiles(state, players)

    state["squads"][str(club["id"])] = {
        "footballDataTeamId": club["id"],
        "footballDataTeamName": club.get("shortName") or club.get("name"),
        "apiFootballTeamId": api_team_id,
        "apiFootballTeamName": (
            api_team.get("name")
            if isinstance(api_team, dict)
            else None
        ),
        "competitionCodes": club.get("competitionCodes", []),
        "checkedAt": utc_now_iso(),
        "players": [
            {
                "id": player.get("id"),
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("position"),
                "photo": player.get("photo"),
            }
            for player in players
            if isinstance(player, dict)
            and isinstance(player.get("id"), int)
        ],
    }
    return True


def fetch_pending_profiles(client: ApiClient, state: dict) -> None:
    pending = list(state["pendingProfileIds"])
    remaining = []

    for player_id_text in pending:
        if not client.can_request():
            remaining.append(player_id_text)
            continue

        if player_id_text in state["playerProfiles"]:
            continue

        endpoint = f"/players/profiles?player={int(player_id_text)}"

        try:
            data = client.get(endpoint)
        except RuntimeError as error:
            state["recentErrors"].append(
                {
                    "at": utc_now_iso(),
                    "phase": "profiles",
                    "playerId": int(player_id_text),
                    "message": str(error),
                }
            )
            remaining.append(player_id_text)
            continue

        response = data.get("response", [])
        if not isinstance(response, list) or not response:
            remaining.append(player_id_text)
            continue

        item = response[0]
        if isinstance(item, dict):
            remember_profile(state, item.get("player"))

    state["pendingProfileIds"] = remaining


def run_squad_scan(client: ApiClient, state: dict, clubs: list) -> None:
    # Reserve part of the daily budget for newly discovered player profiles.
    max_squad_requests = min(60, max(0, client.max_requests - 25))
    squad_requests_at_start = client.request_count
    processed = 0

    while (
        client.can_request()
        and processed < len(clubs)
        and client.request_count - squad_requests_at_start < max_squad_requests
    ):
        cursor = int(state.get("squadCursor", 0))

        if cursor >= len(clubs):
            state["squadCursor"] = 0
            state["lastFullSquadCycleAt"] = utc_now_iso()
            cursor = 0
            print("Completed one full club squad cycle.")

        club = clubs[cursor]
        state["squadCursor"] = cursor + 1
        processed += 1

        api_team_id = resolve_team_id(client, state, club)

        if api_team_id is None:
            print(
                f"Unresolved team: "
                f"{club.get('shortName') or club.get('name')}"
            )
            continue

        if not client.can_request():
            break

        print(
            f"Squad: {club.get('shortName') or club.get('name')} "
            f"(API team {api_team_id})"
        )
        fetch_squad(client, state, club, api_team_id)

    fetch_pending_profiles(client, state)


def load_overrides() -> dict:
    data = load_json(
        OVERRIDES_PATH,
        {
            "add": [],
            "removePlayerIds": [],
            "japaneseNames": {},
        },
    )

    if not isinstance(data, dict):
        return {
            "add": [],
            "removePlayerIds": [],
            "japaneseNames": {},
        }

    return data


def build_output(state: dict, clubs: list) -> dict:
    profiles = state["playerProfiles"]
    overrides = load_overrides()
    remove_ids = {
        int(value)
        for value in overrides.get("removePlayerIds", [])
        if str(value).isdigit()
    }
    japanese_names = {
        str(key): value
        for key, value in overrides.get("japaneseNames", {}).items()
    }

    output_teams = []
    unknown_profile_ids = set()

    for club in clubs:
        squad = state["squads"].get(str(club["id"]))
        if not isinstance(squad, dict):
            continue

        japanese_players = []

        for squad_player in squad.get("players", []):
            if not isinstance(squad_player, dict):
                continue

            player_id = squad_player.get("id")
            if not isinstance(player_id, int):
                continue

            profile = profiles.get(str(player_id))

            if not isinstance(profile, dict):
                unknown_profile_ids.add(player_id)
                continue

            if player_id in remove_ids:
                continue

            nationality = str(profile.get("nationality") or "").casefold()
            if nationality != "japan":
                continue

            firstname = profile.get("firstname")
            lastname = profile.get("lastname")
            full_name = " ".join(
                value
                for value in [firstname, lastname]
                if isinstance(value, str) and value.strip()
            ).strip()

            japanese_players.append(
                {
                    "id": player_id,
                    "name": full_name or profile.get("name") or squad_player.get("name"),
                    "nameJa": japanese_names.get(str(player_id)),
                    "position": squad_player.get("position"),
                    "number": squad_player.get("number"),
                    "nationality": "Japan",
                    "photo": profile.get("photo") or squad_player.get("photo"),
                }
            )

        japanese_players.sort(
            key=lambda player: (
                player.get("nameJa")
                or player.get("name")
                or ""
            ).casefold()
        )

        if japanese_players:
            output_teams.append(
                {
                    "teamId": club["id"],
                    "teamName": club.get("shortName") or club.get("name"),
                    "apiFootballTeamId": squad.get("apiFootballTeamId"),
                    "competitionCodes": club.get("competitionCodes", []),
                    "players": japanese_players,
                    "checkedAt": squad.get("checkedAt"),
                }
            )

    for item in overrides.get("add", []):
        if not isinstance(item, dict):
            continue

        team_id = item.get("teamId")
        player = item.get("player")

        if not isinstance(team_id, int) or not isinstance(player, dict):
            continue

        target = next(
            (team for team in output_teams if team["teamId"] == team_id),
            None,
        )

        if target is None:
            club = next(
                (club for club in clubs if club["id"] == team_id),
                None,
            )
            if club is None:
                continue

            target = {
                "teamId": team_id,
                "teamName": club.get("shortName") or club.get("name"),
                "apiFootballTeamId": state["teamMap"].get(str(team_id)),
                "competitionCodes": club.get("competitionCodes", []),
                "players": [],
                "checkedAt": None,
            }
            output_teams.append(target)

        target["players"].append(player)

    output_teams.sort(
        key=lambda team: str(team.get("teamName") or "").casefold()
    )

    total_teams = len(clubs)
    mapped_teams = sum(
        1 for club in clubs if str(club["id"]) in state["teamMap"]
    )
    teams_with_squads = sum(
        1 for club in clubs if str(club["id"]) in state["squads"]
    )

    unresolved = [
        {
            "teamId": int(team_id),
            **details,
        }
        for team_id, details in state["unresolvedTeams"].items()
        if team_id.isdigit()
    ]

    unresolved.sort(key=lambda item: str(item.get("name") or "").casefold())

    coverage_complete = (
        state.get("phase") == "squads"
        and teams_with_squads == total_teams
        and not unresolved
        and not unknown_profile_ids
        and not state.get("pendingProfileIds")
    )

    return {
        "updatedAt": utc_now_iso(),
        "source": "API-Football",
        "phase": state.get("phase"),
        "teamCount": len(output_teams),
        "playerCount": sum(
            len(team.get("players", []))
            for team in output_teams
        ),
        "coverage": {
            "totalClubTeams": total_teams,
            "mappedTeams": mapped_teams,
            "teamsWithSquads": teams_with_squads,
            "unresolvedTeamCount": len(unresolved),
            "pendingProfileCount": len(state.get("pendingProfileIds", [])),
            "unknownProfileCount": len(unknown_profile_ids),
            "lastFullSquadCycleAt": state.get("lastFullSquadCycleAt"),
            "isComplete": coverage_complete,
        },
        "unresolvedTeams": unresolved,
        "teams": output_teams,
    }


def main() -> None:
    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()

    if not api_key:
        print("API_FOOTBALL_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    if not TEAMS_PATH.exists():
        print("teams.json was not found.", file=sys.stderr)
        sys.exit(1)

    state = load_json(STATE_PATH, default_state())
    if not isinstance(state, dict) or state.get("version") != 1:
        state = default_state()

    # Ensure keys added in later revisions exist.
    defaults = default_state()
    for key, value in defaults.items():
        state.setdefault(key, value)

    clubs = load_club_teams()
    client = ApiClient(api_key, MAX_REQUESTS_PER_RUN)

    if state["phase"] == "bootstrap":
        run_bootstrap(client, state, clubs)

    if state["phase"] == "squads" and client.can_request():
        run_squad_scan(client, state, clubs)

    state["recentErrors"] = state["recentErrors"][-100:]
    state["updatedAt"] = utc_now_iso()

    output = build_output(state, clubs)

    write_json(STATE_PATH, state)
    write_json(OUTPUT_PATH, output)

    print("")
    print(f"API requests used: {client.request_count}/{MAX_REQUESTS_PER_RUN}")
    print(f"Phase: {state['phase']}")
    print(
        "Coverage: "
        f"{output['coverage']['teamsWithSquads']}/"
        f"{output['coverage']['totalClubTeams']} teams"
    )
    print(
        f"Japanese players: {output['playerCount']} "
        f"across {output['teamCount']} teams"
    )
    print(f"Complete: {output['coverage']['isComplete']}")


if __name__ == "__main__":
    main()
