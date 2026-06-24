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


def fetch_matches(token: str) -> dict:
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

    output = fetch_matches(token)

    Path("matches.json").write_text(
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
        f"{output['count']} matches."
    )


if __name__ == "__main__":
    main()
