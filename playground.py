from dataclasses import dataclass
import os
from pathlib import Path

from cfbd.api.games_api import GamesApi
from cfbd.api_client import ApiClient
from cfbd.configuration import Configuration
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / "secrets.env", override=False)

YEAR = 2026
WEEK = 7


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@dataclass
class Game:
    game_id: int
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    interceptions: int = 0
    fumbles: int = 0

    @property
    def total_turnovers(self) -> int:
        return self.interceptions + self.fumbles


def build_games_list(game_team_stats: list[dict]) -> list[Game]:
    games = []
    for game_stats in game_team_stats:
        teams = game_stats["teams"]
        teams_by_location = {team["homeAway"]: team for team in teams}
        if len(teams) != 2 or set(teams_by_location) != {"home", "away"}:
            raise ValueError(
                f"Expected one home and one away team for game {game_stats['id']}"
            )

        game = Game(
            game_id=game_stats["id"],
            home_team=teams_by_location["home"]["team"],
            away_team=teams_by_location["away"]["team"],
            home_score=teams_by_location["home"]["points"],
            away_score=teams_by_location["away"]["points"],
        )
        for team in teams:
            team_stats = {
                stat["category"]: stat["stat"] for stat in team["stats"]
            }
            required_categories = {"interceptions", "fumblesLost"}
            missing_categories = required_categories - team_stats.keys()
            if missing_categories:
                raise ValueError(
                    f"Missing turnover stats for {team['team']} in game "
                    f"{game.game_id}: {', '.join(sorted(missing_categories))}"
                )
            game.interceptions += int(team_stats["interceptions"])
            game.fumbles += int(team_stats["fumblesLost"])

        games.append(game)

    return games


def write_top_turnover_games(games: list[Game]) -> None:
    ranked_games = sorted(
        games,
        key=lambda game: game.total_turnovers,
        reverse=True,
    )
    top_games = ranked_games[:10]
    if len(ranked_games) > 10 and top_games:
        cutoff_turnovers = top_games[-1].total_turnovers
        top_games.extend(
            game
            for game in ranked_games[10:]
            if game.total_turnovers == cutoff_turnovers
        )

    lines = [f"Top turnover games - Week {WEEK}, {YEAR}", ""]
    rank = 0
    previous_turnovers = None
    for game in top_games:
        if game.total_turnovers != previous_turnovers:
            rank += 1
            previous_turnovers = game.total_turnovers
        home_score = game.home_score if game.home_score is not None else "N/A"
        away_score = game.away_score if game.away_score is not None else "N/A"
        lines.extend(
            [
                f"{rank}. {game.home_team} vs "
                f"{game.away_team} | {home_score} - {away_score}",
                f"   Interceptions: {game.interceptions}",
                f"   Fumbles: {game.fumbles}",
                f"   Total turnovers: {game.total_turnovers}",
                "",
            ]
        )

    output_path = BASE_DIR / f"top_turnover_games_week_{WEEK}.txt"
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    access_token = first_env("access_token", "ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError(
            "Missing access token. Set ACCESS_TOKEN (or access_token) as a GitHub Actions secret."
        )

    configuration = Configuration(
        host=first_env("host", "HOST", "CFBD_HOST")
        or "https://api.collegefootballdata.com",
        access_token=access_token,
    )
    with ApiClient(configuration) as api_client:
        game_team_stats = [
            game_stats.to_dict()
            for game_stats in GamesApi(api_client).get_game_team_stats(
                year=YEAR,
                week=WEEK,
            )
        ]

    games = build_games_list(game_team_stats)
    write_top_turnover_games(games)


if __name__ == "__main__":
    main()
