import json
import os
import sys
from datetime import datetime, timedelta, timezone
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


def fetch_matches(token: str) -> dict:
    now_utc = datetime.now(timezone.utc)

    # 日本時間との日付差を取りこぼさないよう、前日から32日先まで取得する。
    date_from = now_utc.date() - timedelta(days=1)
    date_to = now_utc.date() + timedelta(days=32)

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
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"football-data.org returned HTTP {error.code}: {error_body}"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Could not connect to football-data.org: {error}"
        ) from error

    data = json.loads(body)

    if not isinstance(data, dict):
        raise RuntimeError("API response was not a JSON object.")

    matches = data.get("matches")

    if not isinstance(matches, list):
        raise RuntimeError("API response did not contain a matches list.")

    return {
        "updatedAt": now_utc.isoformat(),
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "competitions": COMPETITIONS,
        "count": len(matches),
        "matches": matches,
    }


def main() -> None:
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()

    if not token:
        print("FOOTBALL_DATA_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    output = fetch_matches(token)

    Path("matches.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Created matches.json with {output['count']} matches.")


if __name__ == "__main__":
    main()
