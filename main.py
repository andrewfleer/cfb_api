import os
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from cfbd.api.drives_api import DrivesApi
from cfbd.api.games_api import GamesApi
from cfbd.api.plays_api import PlaysApi
from cfbd.api.stats_api import StatsApi
from cfbd.api_client import ApiClient
from cfbd.configuration import Configuration
from cfbd.models.division_classification import DivisionClassification
import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / "secrets.env", override=False)


def first_env(*names):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


secret_host = first_env("host", "HOST", "CFBD_HOST") or "https://api.collegefootballdata.com"
secret_access_token = first_env("access_token", "ACCESS_TOKEN")
secret_discord_webhook = first_env("discord_webhook", "DISCORD_WEBHOOK")
secret_discord_user = first_env("discord_user", "DISCORD_USER")

if not secret_access_token:
    raise RuntimeError("Missing access token. Set ACCESS_TOKEN (or access_token) as a GitHub Actions secret.")

if not secret_discord_webhook:
    raise RuntimeError("Missing Discord webhook. Set DISCORD_WEBHOOK (or discord_webhook) as a GitHub Actions secret.")

if not secret_discord_user:
    raise RuntimeError("Missing Discord user. Set DISCORD_USER (or discord_user) as a Github Actions secret.")

configuration = Configuration(
    host=secret_host,
    access_token=secret_access_token,
)
webhook_url = secret_discord_webhook
discord_user = secret_discord_user


@dataclass
class Drive:
    game_id: int
    drive_id: str
    quarter: int
    clock_time: str
    offense_team: str
    defense_team: str
    result: str
    plays: int
    yards: int
    minutes: int
    seconds: int
    scoring: bool
    final_play_text: str = ""


@dataclass
class Punt:
    offense: str
    defense: str
    quarter: int
    time: str
    punter_name: str
    punter_number: int
    punt_distance: int
    play_text: str


@dataclass
class Play:
    offense: str
    defense: str
    quarter: int
    time: str
    play_text: str
    yards_gained: int = 0


@dataclass
class TeamStats:
    team: str = ""
    total_points: int = 0
    team_yards: int = 0
    total_yards: int = 0
    spinach_yards: float = 0.0


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


@dataclass
class SeventeenToZeroGame:
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    game_id: int
    seventeen_zero_team_lost: bool = False
    home_final_score: int = 0
    away_final_score: int = 0

@dataclass
class PunterStats:
    year: int
    team: str
    conference: str
    player_name: str
    position: str
    number_of_punts: int
    total_punt_yards: int
    yards_per_punt: float
    longest_punt: int
    punts_inside_20: int
    touchbacks: int

@dataclass
class PassingStats:
    year: int
    team: str
    conference: str
    player_name: str
    position: str
    passing_attempts: int
    passing_completions: int
    passing_yards: int
    passing_touchdowns: int
    passing_interceptions: int
    passing_percentage: float
    passing_yards_per_attempt: float

@dataclass
class RushingStats:
    year: int
    team: str
    conference: str
    player_name: str
    position: str
    rushing_carries: int
    rushing_long: int
    rushing_touchdowns: int
    rushing_yards: int
    rushing_yards_per_carry: float


scoring_drives = []
non_scoring_drives = []
punts = []
safeties = []
field_goals = []
spinach_teams = []
games_that_were_17_to_0 = []
scoops_and_scores = []
pick_sixes = []
qb_punts = []
other_punts = []
non_qb_passes = []
non_rusher_rushes = []

fbs_games = []
fcs_games = []
d2_games = []
d3_games = []

year_raw = first_env("YEAR", "year")
week_raw = first_env("WEEK", "week", "CFBD_WEEK")

if year_raw is None or week_raw is None:
    raise RuntimeError("Missing YEAR and WEEK environment variables. cron-job.org must send both values for each run.")

year = int(year_raw)
week = int(week_raw)

def discord_webhook_error_alert(error_message):
    print(error_message)
    data = {
        "content": f"""Hey <@{discord_user}>: **Tummy Hort**
        
        Error message:
        ```
        {error_message}
        ```
        """
    }

    response = requests.post(
        webhook_url,
        json=data
    )
    if response.status_code in (200, 204):
        print("Successfully sent error message in discord")
    else:
        print(f"Failed to send error message via webhook. Status code: {response.status_code}")


def parse_punt_text(play_text):
    pattern = r"^\s*(?:\([^)]+\)\s*)?(?:#?(\d+)\s+(.+?)\s+)?punt\s+(\d+)\s+yards?\b"
    match = re.search(pattern, play_text)
    if match:
        return int(match.group(1) or 0), match.group(2) or "", int(match.group(3))
    return 0, "", 0


def build_turnover_games(game_team_stats):
    games = []
    for game_stats in game_team_stats:
        teams_by_location = {team.home_away: team for team in game_stats.teams}
        if len(game_stats.teams) != 2 or set(teams_by_location) != {"home", "away"}:
            raise ValueError(
                f"Expected one home and one away team for game {game_stats.id}"
            )

        home_team = teams_by_location["home"]
        away_team = teams_by_location["away"]
        game = Game(
            game_id=game_stats.id,
            home_team=home_team.team,
            away_team=away_team.team,
            home_score=home_team.points,
            away_score=away_team.points,
        )
        for team in game_stats.teams:
            team_stats = {stat.category: stat.stat for stat in team.stats}
            required_categories = {"interceptions", "fumblesLost"}
            missing_categories = required_categories - team_stats.keys()
            if missing_categories:
                raise ValueError(
                    f"Missing turnover stats for {team.team} in game "
                    f"{game.game_id}: {', '.join(sorted(missing_categories))}"
                )
            game.interceptions += int(team_stats["interceptions"])
            game.fumbles += int(team_stats["fumblesLost"])

        games.append(game)

    return games


def build_most_punts_game_rows(games, plays):
    games_by_id = {game.id: game for game in games}
    punt_counts = {game.id: {"home": 0, "away": 0} for game in games}
    for play in plays:
        game = games_by_id.get(play.game_id)
        if game is None or play.play_type != "Punt":
            continue
        if play.offense == game.home_team:
            punt_counts[game.id]["home"] += 1
        elif play.offense == game.away_team:
            punt_counts[game.id]["away"] += 1

    ranked_games = sorted(
        games,
        key=lambda game: sum(punt_counts[game.id].values()),
        reverse=True,
    )[:10]
    return [
        (
            rank,
            f"{game.home_team} vs {game.away_team}",
            (
                f"{game.home_points}-{game.away_points}"
                if game.home_points is not None and game.away_points is not None
                else "N/A"
            ),
            punt_counts[game.id]["home"],
            punt_counts[game.id]["away"],
            sum(punt_counts[game.id].values()),
        )
        for rank, game in enumerate(ranked_games, start=1)
    ]


def build_fewest_total_yard_game_rows(game_team_stats, games_by_id):
    game_rows = []
    for game_stats in game_team_stats:
        game = games_by_id.get(game_stats.id)
        if game is None:
            continue

        teams_by_location = {team.home_away: team for team in game_stats.teams}
        if len(game_stats.teams) != 2 or set(teams_by_location) != {"home", "away"}:
            raise ValueError(
                f"Expected one home and one away team for game {game_stats.id}"
            )

        total_yards_by_team = {}
        for location in ("home", "away"):
            team = teams_by_location[location]
            team_stats = {stat.category: stat.stat for stat in team.stats}
            if "totalYards" not in team_stats:
                raise ValueError(
                    f"Missing totalYards stat for {team.team} in game {game_stats.id}"
                )
            total_yards_by_team[location] = int(team_stats["totalYards"])

        total_yards = sum(total_yards_by_team.values())
        game_rows.append(
            (
                total_yards,
                f"{game.home_team} vs {game.away_team}",
                (
                    f"{game.home_points}-{game.away_points}"
                    if game.home_points is not None and game.away_points is not None
                    else "N/A"
                ),
                total_yards_by_team["home"],
                total_yards_by_team["away"],
            )
        )

    ranked_games = sorted(game_rows, key=lambda row: row[0])[:10]
    return [
        (rank, game, score, home_yards, away_yards, total_yards)
        for rank, (
            total_yards,
            game,
            score,
            home_yards,
            away_yards,
        ) in enumerate(ranked_games, start=1)
    ]


def build_safety_leaderboard_rows(safety_plays):
    team_safety_counts = {}
    game_safety_counts = {}
    for week_number, play in safety_plays:
        if play.play_type != "Safety":
            continue
        if not play.defense:
            raise ValueError(f"Safety play {play.id} is missing its defense team")

        team_safety_counts[play.defense] = team_safety_counts.get(play.defense, 0) + 1
        game_counts = game_safety_counts.setdefault(
            play.game_id,
            {
                "home": play.home,
                "away": play.away,
                "home_count": 0,
                "away_count": 0,
                "count": 0,
                "week": week_number,
            },
        )
        game_counts["count"] += 1
        if play.defense == play.home:
            game_counts["home_count"] += 1
        elif play.defense == play.away:
            game_counts["away_count"] += 1
        else:
            raise ValueError(
                f"Safety play {play.id} defense does not match a game team"
            )

    def top_rows(counts, label_for, count_for):
        ranked = sorted(
            counts,
            key=lambda item: (-count_for(item), label_for(item)),
        )
        selected = ranked[:10]
        if len(ranked) > 10 and selected:
            cutoff_count = count_for(selected[-1])
            selected.extend(
                item for item in ranked[10:] if count_for(item) == cutoff_count
            )

        rows = []
        rank = 0
        previous_count = None
        for item in selected:
            count = count_for(item)
            if count != previous_count:
                rank += 1
                previous_count = count
            rows.append((rank, item, label_for(item), count))
        return rows

    team_ranked_rows = top_rows(
        list(team_safety_counts),
        label_for=lambda team: team,
        count_for=lambda team: team_safety_counts[team],
    )
    team_rows = [
        (rank, team, count)
        for rank, team, _, count in team_ranked_rows
    ]
    game_ranked_rows = top_rows(
        list(game_safety_counts),
        label_for=lambda game_id: (
            f"{game_safety_counts[game_id]['home']} vs "
            f"{game_safety_counts[game_id]['away']}"
        ),
        count_for=lambda game_id: game_safety_counts[game_id]["count"],
    )
    game_rows = [
        (
            rank,
            game_safety_counts[game_id]["week"],
            label,
            game_safety_counts[game_id]["home_count"],
            game_safety_counts[game_id]["away_count"],
            count,
        )
        for rank, game_id, label, count in game_ranked_rows
    ]
    return team_rows, game_rows


def build_spinach_teams(game_team_stats):
    teams_with_high_yardage_share = []
    for team_stat in game_team_stats:
        home_team = TeamStats()
        away_team = TeamStats()

        for team in team_stat.teams:
            total_yards = next(
                (stat.stat for stat in team.stats if stat.category == "totalYards"), 0
            )
            if team.home_away == "home":
                home_team.team = team.team
                home_team.total_points = team.points
                home_team.team_yards = int(total_yards)
            elif team.home_away == "away":
                away_team.team = team.team
                away_team.total_points = team.points
                away_team.team_yards = int(total_yards)

        total_yards = home_team.team_yards + away_team.team_yards
        home_team.total_yards = total_yards
        away_team.total_yards = total_yards

        if home_team.team_yards > 350 and home_team.team_yards > away_team.team_yards:
            share_of_yards = home_team.team_yards / total_yards
            home_team.spinach_yards = (
                share_of_yards / home_team.total_points
                if home_team.total_points > 0
                else share_of_yards
            )
            teams_with_high_yardage_share.append(home_team)
        elif away_team.team_yards > 350 and away_team.team_yards > home_team.team_yards:
            share_of_yards = away_team.team_yards / total_yards
            away_team.spinach_yards = (
                share_of_yards / away_team.total_points
                if away_team.total_points > 0
                else share_of_yards
            )
            teams_with_high_yardage_share.append(away_team)

    return teams_with_high_yardage_share


def filter_game_team_stats_by_classification(
    game_team_stats, games_by_id, classification
):
    return [
        team_stat
        for team_stat in game_team_stats
        if (game := games_by_id.get(team_stat.id)) is not None
        and classification in (game.home_classification, game.away_classification)
    ]


def add_17_to_0_game(drive):
    if drive.end_offense_score == 17 and drive.end_defense_score == 0:
        if drive.is_home_offense:
            home_team, away_team = drive.offense, drive.defense
            home_score, away_score = drive.end_offense_score, drive.end_defense_score
        else:
            home_team, away_team = drive.defense, drive.offense
            home_score, away_score = drive.end_defense_score, drive.end_offense_score
    elif drive.end_offense_score == 0 and drive.end_defense_score == 17:
        if drive.is_home_offense:
            home_team, away_team = drive.offense, drive.defense
            home_score, away_score = drive.end_offense_score, drive.end_defense_score
        else:
            home_team, away_team = drive.defense, drive.offense
            home_score, away_score = drive.end_defense_score, drive.end_offense_score
    else:
        return

    candidate = SeventeenToZeroGame(
        home_team=home_team,
        away_team=away_team,
        home_score=home_score,
        away_score=away_score,
        game_id=drive.game_id,
    )
    if not any(game.game_id == candidate.game_id for game in games_that_were_17_to_0):
        games_that_were_17_to_0.append(candidate)


with ApiClient(configuration) as api_client:
    drives_api = DrivesApi(api_client)
    plays_api = PlaysApi(api_client)
    games_api = GamesApi(api_client)
    stats_api = StatsApi(api_client)
    top_turnover_games = None
    weekly_game_team_stats = None
    most_punts_game_rows = []
    fewest_total_yards_game_rows = []
    season_safety_plays = []

    print(f"Getting game data for year {year} - week {week}...")
    try:
        week_games = games_api.get_games(year, week=week)
    except Exception as e:
        error_message = f"Exception when calling GamesApi->get_games: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    print(f"Getting drive data for year {year} - week {week}...")
    try:
        week_drives = drives_api.get_drives(year, week=week)
    except Exception as e:
        error_message = f"Exception when calling DrivesApi->get_drives: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    print(f"Getting play data for year {year} - week {week}...")
    try:
        week_plays = plays_api.get_plays(year, week=week)
    except Exception as e:
        error_message=f"Exception when calling PlaysApi->get_plays: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    season_safety_plays.extend(
        (week, play) for play in week_plays if play.play_type == "Safety"
    )
    for season_week in range(1, week):
        print(f"Getting safety plays for year {year} - week {season_week}...")
        try:
            season_week_plays = plays_api.get_plays(year, week=season_week)
            season_safety_plays.extend(
                (season_week, play)
                for play in season_week_plays
                if play.play_type == "Safety"
            )
        except Exception as e:
            error_message = (
                f"Exception when getting safety plays for week {season_week}: {e}\n"
            )
            discord_webhook_error_alert(error_message=error_message)
            sys.exit()

    try:
        season_safety_team_rows, season_safety_game_rows = (
            build_safety_leaderboard_rows(season_safety_plays)
        )
    except Exception as e:
        error_message = f"Exception when building season safety reports: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    print(f"Getting game stats data for year {year} - week {week}...")
    try:
        weekly_game_team_stats = games_api.get_game_team_stats(year, week=week)
    except Exception as e:
        error_message=f"Exception when calling GamesApi->get_game_team_stats: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    games_by_id = {game.id: game for game in week_games}
    if weekly_game_team_stats is not None:
        try:
            top_turnover_games = build_turnover_games(weekly_game_team_stats)
            fewest_total_yards_game_rows = build_fewest_total_yard_game_rows(
                weekly_game_team_stats,
                games_by_id,
            )
        except Exception as e:
            error_message=f"Exception when building game stats reports: {e}\n"
            discord_webhook_error_alert(error_message=error_message)
            sys.exit()

    try:
        most_punts_game_rows = build_most_punts_game_rows(week_games, week_plays)
    except Exception as e:
        error_message=f"Exception when building Most Punts by Game report: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    plays_by_drive = {}
    for play in week_plays:
        drive_key = (play.game_id, play.drive_id)
        plays_by_drive.setdefault(drive_key, []).append(play)

    print("processing drive data...")
    try:
        for drive in week_drives:
            result = drive.drive_result
            scoring = result in ["TD", "FG"]
            drive_result = Drive(
                game_id=drive.game_id,
                drive_id=drive.id,
                offense_team=drive.offense,
                defense_team=drive.defense,
                result=result,
                plays=drive.plays,
                yards=drive.yards,
                minutes=drive.elapsed.minutes or 0,
                seconds=drive.elapsed.seconds or 0,
                scoring=scoring,
                quarter=drive.start_period,
                clock_time=f"{drive.start_time.minutes}:{str(drive.start_time.seconds).zfill(2)}"
            )

            if result == "FUMBLE TD":
                scoops_and_scores.append(drive_result)
            elif result == "INT TD":
                pick_sixes.append(drive_result)

            if scoring:
                scoring_drives.append(drive_result)
            else:
                non_scoring_drives.append(drive_result)

            add_17_to_0_game(drive)
    except Exception as e:
        error_message=f"Exception when processing drive data: {e}\n"
        discord_webhook_error_alert(error_message=error_message)
        sys.exit()

    print("processing punts...")
    try:
        for punt in (play for play in week_plays if play.play_type == "Punt"):
            punter_number, punter_name, punt_distance = parse_punt_text(punt.play_text)
            punts.append(
                Punt(
                    offense=punt.offense,
                    defense=punt.defense,
                    quarter=punt.period,
                    time=f"{punt.clock.minutes}:{str(punt.clock.seconds).zfill(2)}",
                    punter_name=punter_name,
                    punter_number=punter_number,
                    punt_distance=punt_distance,
                    play_text=punt.play_text or "",
                )
            )
    except Exception as e:
        error_message=f"Exception when calling processing punts: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()

    print("processing safeties...")
    try:
        for safety in (play for play in week_plays if play.play_type == "Safety"):
            safeties.append(
                Play(
                    offense=safety.offense,
                    defense=safety.defense,
                    quarter=safety.period,
                    time=f"{safety.clock.minutes}:{str(safety.clock.seconds).zfill(2)}",
                    play_text=safety.play_text or "",
                )
            )
    except Exception as e:
        error_message=f"Exception when calling processing safeties: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()

    print("processing field goals...")
    try:
        field_goal_types = {
            "Field Goal Good",
        }
        for field_goal in (
            play for play in week_plays if play.play_type in field_goal_types
        ):
            play = Play(
                offense=field_goal.offense,
                defense=field_goal.defense,
                quarter=field_goal.period,
                time=f"{field_goal.clock.minutes}:{str(field_goal.clock.seconds).zfill(2)}",
                play_text=field_goal.play_text or "",
            )
            play.yards_gained = field_goal.yards_gained
            field_goals.append(play)
    except Exception as e:
        error_message=f"Exception when calling processing field goals: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()


    for drive in scoops_and_scores + pick_sixes:
        try:
            for play in plays_by_drive.get((drive.game_id, drive.drive_id), []):
                if play.scoring:
                    drive.final_play_text = play.play_text
                    drive.quarter = play.period
                    drive.clock_time = f"{play.clock.minutes}:{str(play.clock.seconds).zfill(2)}"
        except Exception as e:
            label = "Scoop and Score" if drive in scoops_and_scores else "Pick Six"
            error_message=f"Exception when turnover TDs for {label} final play text->get_plays: {e}\n"
            discord_webhook_error_alert(error_message)
            sys.exit()

    if weekly_game_team_stats is not None:
        for classification in (
            DivisionClassification("fbs"),
            DivisionClassification("fcs"),
        ):
            print(f"Getting {classification} spinach teams...")
            try:
                classified_game_stats = filter_game_team_stats_by_classification(
                    weekly_game_team_stats,
                    games_by_id,
                    classification,
                )
                spinach_teams.extend(build_spinach_teams(classified_game_stats))
            except Exception as e:
                error_message=f"Exception when building {classification} spinach teams: {e}\n"
                discord_webhook_error_alert(error_message)
                sys.exit()


    print("Working on high and low scoring games...")
    for classification, result_list in (
        (DivisionClassification("fbs"), fbs_games),
        (DivisionClassification("fcs"), fcs_games),
        (DivisionClassification("ii"), d2_games),
        (DivisionClassification("iii"), d3_games),
    ):
        try:
            for game in week_games:
                if classification not in (
                    game.home_classification,
                    game.away_classification,
                ):
                    continue
                if game.home_points is None or game.away_points is None:
                    continue

                game_record = Game(
                    game_id=game.id,
                    home_team=game.home_team,
                    away_team=game.away_team,
                    home_score=game.home_points,
                    away_score=game.away_points,
                )
                result_list.append(game_record)
        except Exception as e:
            error_message="Exception when processing high and low scoring games for {classification} Games: {e}\n"
            discord_webhook_error_alert(error_message)
            sys.exit() 

    print("Processing 17-0 games")
    for game in games_that_were_17_to_0:
        try:
            result = games_by_id.get(game.game_id)
            if result is None:
                continue
            if result.home_points is None or result.away_points is None:
                continue

            game.home_final_score = result.home_points
            game.away_final_score = result.away_points
            if game.home_score > game.away_score:
                game.seventeen_zero_team_lost = result.home_points < result.away_points
            elif game.away_score > game.home_score:
                game.seventeen_zero_team_lost = result.away_points < result.home_points
        except Exception as e:
            error_message=f"Exception when processing for 17-0 Games: {e}\n"
            discord_webhook_error_alert(error_message)
            sys.exit()


    print("getting non-punter punt stats")
    try:
        for punt_stat in stats_api.get_player_season_stats(year, category="punting"):
            if punt_stat.position in ["P", "PK"]:
                continue # Skip punters and kickers
            elif punt_stat.position == "QB":
                match = next(
                    (
                        stat
                        for stat in qb_punts
                        if stat.year == year
                        and stat.team == punt_stat.team
                        and stat.player_name == punt_stat.player
                        and stat.position == punt_stat.position
                    ),
                    None,
                )

                if match is not None:
                    if punt_stat.stat_type == "NO":
                        match.number_of_punts = int(punt_stat.stat)
                    elif punt_stat.stat_type == "In 20":
                        match.punts_inside_20 = int(punt_stat.stat)
                    elif punt_stat.stat_type == "LONG":
                        match.longest_punt = int(punt_stat.stat)
                    elif punt_stat.stat_type == "TB":
                        match.touchbacks = int(punt_stat.stat)
                    elif punt_stat.stat_type == "YDS":
                        match.total_punt_yards = int(punt_stat.stat)
                    elif punt_stat.stat_type == "YPP":
                        match.yards_per_punt = float(punt_stat.stat)
                else:
                    qb_punts.append(
                        PunterStats(
                            year=year,
                            team=punt_stat.team,
                            conference=punt_stat.conference,
                            player_name=punt_stat.player,
                            position=punt_stat.position,
                            number_of_punts=int(punt_stat.stat) if punt_stat.stat_type == "NO" else 0,
                            total_punt_yards=int(punt_stat.stat) if punt_stat.stat_type == "YDS" else 0,
                            yards_per_punt=float(punt_stat.stat) if punt_stat.stat_type == "YPP" else 0.0,
                            longest_punt=int(punt_stat.stat) if punt_stat.stat_type == "LONG" else 0,
                            punts_inside_20=int(punt_stat.stat) if punt_stat.stat_type == "In 20" else 0,
                            touchbacks=int(punt_stat.stat) if punt_stat.stat_type == "TB" else 0,
                        )
                    )
            else:
                match = next(
                    (
                        stat
                        for stat in other_punts
                        if stat.year == year
                        and stat.team == punt_stat.team
                        and stat.player_name == punt_stat.player
                        and stat.position == punt_stat.position
                    ),
                    None,
                )

                if match is not None:
                    if punt_stat.stat_type == "NO":
                        match.number_of_punts = int(punt_stat.stat)
                    elif punt_stat.stat_type == "In 20":
                        match.punts_inside_20 = int(punt_stat.stat)
                    elif punt_stat.stat_type == "LONG":
                        match.longest_punt = int(punt_stat.stat)
                    elif punt_stat.stat_type == "TB":
                        match.touchbacks = int(punt_stat.stat)
                    elif punt_stat.stat_type == "YDS":
                        match.total_punt_yards = int(punt_stat.stat)
                    elif punt_stat.stat_type == "YPP":
                        match.yards_per_punt = float(punt_stat.stat)
                else:
                    other_punts.append(
                        PunterStats(
                            year=year,
                            team=punt_stat.team,
                            conference=punt_stat.conference,
                            player_name=punt_stat.player,
                            position=punt_stat.position,
                            number_of_punts=int(punt_stat.stat) if punt_stat.stat_type == "NO" else 0,
                            total_punt_yards=int(punt_stat.stat) if punt_stat.stat_type == "YDS" else 0,
                            yards_per_punt=float(punt_stat.stat) if punt_stat.stat_type == "YPP" else 0.0,
                            longest_punt=int(punt_stat.stat) if punt_stat.stat_type == "LONG" else 0,
                            punts_inside_20=int(punt_stat.stat) if punt_stat.stat_type == "In 20" else 0,
                            touchbacks=int(punt_stat.stat) if punt_stat.stat_type == "TB" else 0,
                        )
                    )

    except Exception as e:
        error_message=f"Exception when calling StatsApi for Punting Stats->get_player_season_stats: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()

    print("getting non-QB passing stats")
    try:
        for pass_stat in stats_api.get_player_season_stats(year, category="passing"):
            if pass_stat.position != "QB":
                match = next(
                    (
                        player_stats
                        for player_stats in non_qb_passes
                        if player_stats.year == year
                        and player_stats.team == pass_stat.team
                        and player_stats.player_name == pass_stat.player
                        and player_stats.position == pass_stat.position
                    ),
                    None,
                )

                if match is not None:
                    if pass_stat.stat_type == "ATT":
                        match.passing_attempts = int(pass_stat.stat)
                    elif pass_stat.stat_type == "COMPLETIONS":
                        match.passing_completions = int(pass_stat.stat)
                    elif pass_stat.stat_type == "YDS":
                        match.passing_yards = int(pass_stat.stat)
                    elif pass_stat.stat_type == "TD":
                        match.passing_touchdowns = int(pass_stat.stat)
                    elif pass_stat.stat_type == "INT":
                        match.passing_interceptions = int(pass_stat.stat)
                    elif pass_stat.stat_type == "PCT":
                        match.passing_percentage = float(pass_stat.stat)
                    elif pass_stat.stat_type == "YPA":
                        match.passing_yards_per_attempt = float(pass_stat.stat)
                else:
                    non_qb_passes.append(
                        PassingStats(
                            year=year,
                            team=pass_stat.team,
                            conference=pass_stat.conference,
                            player_name=pass_stat.player,
                            position=pass_stat.position,
                            passing_attempts=int(pass_stat.stat) if pass_stat.stat_type == "ATT" else 0,
                            passing_completions=int(pass_stat.stat) if pass_stat.stat_type == "COMPLETIONS" else 0,
                            passing_yards=int(pass_stat.stat) if pass_stat.stat_type == "YDS" else 0,
                            passing_touchdowns=int(pass_stat.stat) if pass_stat.stat_type == "TD" else 0,
                            passing_interceptions=int(pass_stat.stat) if pass_stat.stat_type == "INT" else 0,
                            passing_percentage=float(pass_stat.stat) if pass_stat.stat_type == "PCT" else 0.0,
                            passing_yards_per_attempt=float(pass_stat.stat) if pass_stat.stat_type == "YPA" else 0.0,
                        )
                    )
    except Exception as e:
        error_message=f"Exception when calling StatsApi for Passing Stats->get_player_season_stats: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()

    print("getting non-rusher rushing stats")
    try:
        for rush_stat in stats_api.get_player_season_stats(year, category="rushing"):
            if rush_stat.position not in ["RB", "QB"]:
                match = next(
                    (
                        stat
                        for stat in non_rusher_rushes
                        if stat.year == year
                        and stat.team == rush_stat.team
                        and stat.player_name == rush_stat.player
                        and stat.position == rush_stat.position
                    ),
                    None,
                )

                if match is not None:
                    if rush_stat.stat_type == "CAR":
                        match.rushing_carries = int(rush_stat.stat)
                    elif rush_stat.stat_type == "LONG":
                        match.rushing_long = int(rush_stat.stat)
                    elif rush_stat.stat_type == "TD":
                        match.rushing_touchdowns = int(rush_stat.stat)
                    elif rush_stat.stat_type == "YDS":
                        match.rushing_yards = int(rush_stat.stat)
                    elif rush_stat.stat_type == "YPC":
                        match.rushing_yards_per_carry = float(rush_stat.stat)
                else:
                    non_rusher_rushes.append(
                        RushingStats(
                            year=year,
                            team=rush_stat.team,
                            conference=rush_stat.conference,
                            player_name=rush_stat.player,
                            position=rush_stat.position,
                            rushing_carries=int(rush_stat.stat) if rush_stat.stat_type == "CAR" else 0,
                            rushing_long=int(rush_stat.stat) if rush_stat.stat_type == "LONG" else 0,
                            rushing_touchdowns=int(rush_stat.stat) if rush_stat.stat_type == "TD" else 0,
                            rushing_yards=int(rush_stat.stat) if rush_stat.stat_type == "YDS" else 0,
                            rushing_yards_per_carry=float(rush_stat.stat) if rush_stat.stat_type == "YPC" else 0.0,
                        )
                    )
    except Exception as e:
        error_message=f"Exception when calling StatsApi for Rushing Stats->get_player_season_stats: {e}\n"
        discord_webhook_error_alert(error_message)
        sys.exit()

                

###################### REPORTING ######################

longest_scoring_drives = sorted(scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)
longest_non_scoring_drives = sorted(non_scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)
most_qb_punts = sorted(
    qb_punts,
    key=lambda punt: (punt.number_of_punts, punt.total_punt_yards),
    reverse=True,
)
most_other_punts = sorted(
    other_punts, 
    key=lambda punt: (punt.number_of_punts, punt.total_punt_yards),
    reverse=True
)
most_non_qb_passes = sorted(
    (passer for passer in non_qb_passes if passer.passing_attempts > 0),
    key=lambda passer: (passer.passing_attempts, passer.passing_yards),
    reverse=True,
)
most_non_rusher_rushes = sorted(
    (rusher for rusher in non_rusher_rushes if rusher.rushing_carries > 0),
    key=lambda rusher: (rusher.rushing_carries, rusher.rushing_yards),
    reverse=True
)

try:
    for drive in longest_scoring_drives[:10]:
        max_play_number = 0
        for play in plays_by_drive.get((drive.game_id, drive.drive_id), []):
            if play.play_type == "Timeout":
                continue
            if play.play_number is not None and play.play_number > max_play_number:
                max_play_number = play.play_number
                drive.final_play_text = play.play_text

    for drive in longest_non_scoring_drives[:10]:
        max_play_number = 0
        for play in plays_by_drive.get((drive.game_id, drive.drive_id), []):
            if play.play_type == "Timeout":
                continue
            if play.play_number is not None and play.play_number > max_play_number:
                max_play_number = play.play_number
                drive.final_play_text = play.play_text
except Exception as e:
    error_message=f"Exception when processing longest drives: {e}\n"
    discord_webhook_error_alert(error_message)
    sys.exit()


# Helper for writing repeated report files.
report_files = []


def write_report(path, header, rows):
    with open(path, "w", encoding="utf-8") as handle:
        if header:
            handle.write(f"{header} - Week {week}, {year}\n")
        for row in rows:
            handle.write(row)
    report_files.append(path)


def format_table(headers, rows):
    rows = [[str(value) for value in row] for row in rows]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    header_line = " | ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    separator = "-+-".join("-" * width for width in widths)
    data_lines = [
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return ["\n", *(f"{line}\n" for line in (header_line, separator, *data_lines))]


def format_table_with_details(headers, entries):
    table_lines = format_table(headers, [row for row, _ in entries])
    output_lines = table_lines[:3]
    for table_line, (_, detail) in zip(table_lines[3:], entries):
        output_lines.append(table_line)
        wrapped_detail = textwrap.fill(
            detail,
            width=100,
            initial_indent="    ",
            subsequent_indent="        ",
        )
        output_lines.extend(f"{line}\n" for line in wrapped_detail.splitlines())
        output_lines.append("\n")
    return output_lines


if top_turnover_games is not None:
    ranked_turnover_games = sorted(
        top_turnover_games,
        key=lambda game: game.total_turnovers,
        reverse=True,
    )
    top_turnover_games = ranked_turnover_games[:10]
    if len(ranked_turnover_games) > 10 and top_turnover_games:
        cutoff_turnovers = top_turnover_games[-1].total_turnovers
        top_turnover_games.extend(
            game
            for game in ranked_turnover_games[10:]
            if game.total_turnovers == cutoff_turnovers
        )

    turnover_report_rows = []
    rank = 0
    previous_turnovers = None
    for game in top_turnover_games:
        if game.total_turnovers != previous_turnovers:
            rank += 1
            previous_turnovers = game.total_turnovers
        home_score = game.home_score if game.home_score is not None else "N/A"
        away_score = game.away_score if game.away_score is not None else "N/A"
        turnover_report_rows.append(
            (
                rank,
                f"{game.home_team} vs {game.away_team}",
                f"{home_score}-{away_score}",
                game.interceptions,
                game.fumbles,
                game.total_turnovers,
            )
        )

    write_report(
        f"top_turnover_games_week_{week}.txt",
        "Top Turnover Games",
        format_table(
            ["Rank", "Game", "Score", "Interceptions", "Fumbles", "Turnovers"],
            turnover_report_rows,
        ),
    )


write_report(
    f"most_punts_by_game_week_{week}.txt",
    "10 Games With the Most Punts",
    format_table(
        ["Rank", "Game", "Score", "Home Punts", "Away Punts", "Total Punts"],
        most_punts_game_rows,
    ),
)

write_report(
    f"fewest_total_yards_by_game_week_{week}.txt",
    "10 Games With the Fewest Total Yards",
    format_table(
        ["Rank", "Game", "Score", "Home Yards", "Away Yards", "Total Yards"],
        fewest_total_yards_game_rows,
    ),
)

write_report(
    f"most_safeties_by_team_year_{year}_through_week_{week}.txt",
    "Top 10 Teams With the Most Safeties This Season",
    format_table(
        ["Rank", "Team", "Safeties"],
        season_safety_team_rows,
    ),
)

write_report(
    f"most_safeties_by_game_year_{year}_through_week_{week}.txt",
    "10 Games With the Most Safeties This Season",
    format_table(
        ["Rank", "Week", "Game", "Home Safeties", "Away Safeties", "Total Safeties"],
        season_safety_game_rows,
    ),
)


write_report(
    f"scoring_drives_week_{week}.txt",
    "10 Longest Scoring Drives",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Result", "Plays", "Time", "Yards"],
        [
            (
                (
                    i + 1,
                    drive.offense_team,
                    drive.defense_team,
                    drive.result,
                    drive.plays,
                    f"{drive.minutes}:{drive.seconds:02d}",
                    drive.yards,
                ),
                f"Final play: {drive.final_play_text}",
            )
            for i, drive in enumerate(longest_scoring_drives[:10])
        ],
    ),
)

write_report(
    f"non_scoring_drives_week_{week}.txt",
    "10 Longest Non-Scoring Drives",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Result", "Plays", "Time", "Yards"],
        [
            (
                (
                    i + 1,
                    drive.offense_team,
                    drive.defense_team,
                    drive.result,
                    drive.plays,
                    f"{drive.minutes}:{drive.seconds:02d}",
                    drive.yards,
                ),
                f"Final play: {drive.final_play_text}",
            )
            for i, drive in enumerate(longest_non_scoring_drives[:10])
        ],
    ),
)

write_report(
    f"longest_punts_week_{week}.txt",
    "10 Longest Punts",
    format_table(
        ["Rank", "Game", "Distance", "Punter", "Quarter", "Time"],
        [
            (
                i + 1,
                f"{punt.offense} vs {punt.defense}",
                f"{punt.punt_distance} yd",
                f"#{punt.punter_number} {punt.punter_name}".strip(),
                punt.quarter,
                punt.time,
            )
        for i, punt in enumerate(sorted(punts, key=lambda x: x.punt_distance, reverse=True)[:10])
        ],
    ),
)

write_report(
    f"punts_no_touchbacks_week_{week}.txt",
    "10 Longest Punts Excluding Touchbacks",
    format_table(
        ["Rank", "Game", "Distance", "Punter", "Quarter", "Time"],
        [
            (
                i + 1,
                f"{punt.offense} vs {punt.defense}",
                f"{punt.punt_distance} yd",
                f"#{punt.punter_number} {punt.punter_name}".strip(),
                punt.quarter,
                punt.time,
            )
            for i, punt in enumerate(
                sorted(
                    (punt for punt in punts if "TOUCHBACK" not in punt.play_text.upper()),
                    key=lambda punt: punt.punt_distance,
                    reverse=True,
                )[:10]
            )
        ],
    ),
)

write_report(
    f"safeties_week_{week}.txt",
    "Safety Alert",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Quarter", "Time"],
        [
            (
                (
                    i + 1,
                    safety.offense,
                    safety.defense,
                    safety.quarter,
                    safety.time,
                ),
                f"Play: {safety.play_text}",
            )
            for i, safety in enumerate(safeties)
        ],
    ),
)

write_report(
    f"field_goals_week_{week}.txt",
    "10 Longest Field Goals",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Distance", "Quarter", "Time"],
        [
            (
                (
                    i + 1,
                    field_goal.offense,
                    field_goal.defense,
                    f"{field_goal.yards_gained} yd",
                    field_goal.quarter,
                    field_goal.time,
                ),
                f"Play: {field_goal.play_text}",
            )
            for i, field_goal in enumerate(
                sorted(field_goals, key=lambda x: x.yards_gained, reverse=True)[:10]
            )
        ],
    ),
)

write_report(
    f"spinach_teams_of_the_week_week_{week}.txt",
    "Spinach Teams of the Week",
    format_table(
        ["Rank", "Team", "Points", "Team Yards", "Game Yards", "Yards per Point"],
        [
            (
                i + 1,
                team_stat.team,
                team_stat.total_points,
                team_stat.team_yards,
                team_stat.total_yards,
                f"{team_stat.spinach_yards:.2f}",
            )
            for i, team_stat in enumerate(
                sorted(spinach_teams, key=lambda x: x.spinach_yards, reverse=True)[:10]
            )
        ],
    ),
)

for classification, games in (
    ("fbs", fbs_games),
    ("fcs", fcs_games),
    ("d2", d2_games),
    ("d3", d3_games),
):
    for direction, reverse in (("Highest", True), ("Lowest", False)):
        ranked_games = sorted(
            games,
            key=lambda game: game.home_score + game.away_score,
            reverse=reverse,
        )[:10]
        write_report(
            f"{direction.lower()}_scoring_{classification}_games_week_{week}.txt",
            f"10 {direction} Scoring {classification.upper()} Games",
            format_table(
                ["Rank", "Game", "Score", "Total Points"],
                [
                    (
                        i + 1,
                        f"{game.home_team} vs {game.away_team}",
                        f"{game.home_score}-{game.away_score}",
                        game.home_score + game.away_score,
                    )
                    for i, game in enumerate(ranked_games)
                ],
            ),
        )

lost_17_to_0_games = [
    game for game in games_that_were_17_to_0 if game.seventeen_zero_team_lost
]
survived_17_to_0_games = [
    game for game in games_that_were_17_to_0 if not game.seventeen_zero_team_lost
]


def build_17_to_0_rows(games):
    rows = []
    for rank, game in enumerate(games, start=1):
        if game.home_score == 17:
            leading_team, opponent = game.home_team, game.away_team
            leading_team_final_score = game.home_final_score
            opponent_final_score = game.away_final_score
        else:
            leading_team, opponent = game.away_team, game.home_team
            leading_team_final_score = game.away_final_score
            opponent_final_score = game.home_final_score
        rows.append(
            (
                rank,
                leading_team,
                opponent,
                f"{leading_team_final_score}-{opponent_final_score}",
                "Lost" if game.seventeen_zero_team_lost else "Won",
            )
        )
    return rows


write_report(
    f"games_that_were_17_to_0_week_{week}.txt",
    "The Most Dangerous Lead In College Football",
    [
        "Teams that lost:\n",
        *format_table(
            ["Rank", "Team With 17-0 Lead", "Opponent", "Final Score", "Outcome"],
            build_17_to_0_rows(lost_17_to_0_games),
        ),
        "\nTeams that survived:\n",
        *format_table(
            ["Rank", "Team With 17-0 Lead", "Opponent", "Final Score", "Outcome"],
            build_17_to_0_rows(survived_17_to_0_games),
        ),
    ],
)

write_report(
    f"scoops_and_scores_week_{week}.txt",
    "Scoop and Score Plays",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Quarter", "Time"],
        [
            (
                (
                    i + 1,
                    drive.offense_team,
                    drive.defense_team,
                    drive.quarter,
                    drive.clock_time,
                ),
                f"Play: {drive.final_play_text}",
            )
            for i, drive in enumerate(scoops_and_scores)
        ],
    ),
)

write_report(
    f"pick_sixes_week_{week}.txt",
    "Pick Six Plays",
    format_table_with_details(
        ["Rank", "Offense", "Defense", "Quarter", "Time"],
        [
            (
                (
                    i + 1,
                    drive.offense_team,
                    drive.defense_team,
                    drive.quarter,
                    drive.clock_time,
                ),
                f"Play: {drive.final_play_text}",
            )
            for i, drive in enumerate(pick_sixes)
        ],
    ),
)

write_report(
    f"qb_punts_week_{week}.txt",
    "Quarterback Punting Stats",
    format_table(
        ["Rank", "Player", "Team", "Conference", "Punts", "Yards", "Avg", "Long", "Inside 20", "Touchbacks"],
        [
            (
                i + 1,
                f"{punt.player_name} ({punt.position})",
                punt.team,
                punt.conference,
                punt.number_of_punts,
                punt.total_punt_yards,
                f"{punt.yards_per_punt:.2f}",
                punt.longest_punt,
                punt.punts_inside_20,
                punt.touchbacks,
            )
            for i, punt in enumerate(most_qb_punts)
        ],
    ),
)

if len(most_other_punts) > 0:
    write_report(
        f"other_punts_week_{week}.txt",
        "Other Punting Stats",
        format_table(
            ["Rank", "Player", "Team", "Conference", "Punts", "Yards", "Avg", "Long", "Inside 20", "Touchbacks"],
            [
                (
                    i + 1,
                    f"{punt.player_name} ({punt.position})",
                    punt.team,
                    punt.conference,
                    punt.number_of_punts,
                    punt.total_punt_yards,
                    f"{punt.yards_per_punt:.2f}",
                    punt.longest_punt,
                    punt.punts_inside_20,
                    punt.touchbacks,
                )
                for i, punt in enumerate(most_other_punts)
            ],
        ),
    )

write_report(
    f"non_qb_passes_week_{week}.txt",
    "Non-QB Passing Stats",
    format_table(
        ["Rank", "Player", "Team", "Conference", "Comp/Att", "Yards", "TD", "INT", "Comp %", "YPA"],
        [
            (
                i + 1,
                f"{passer.player_name} ({passer.position})",
                passer.team,
                passer.conference,
                f"{passer.passing_completions}/{passer.passing_attempts}",
                passer.passing_yards,
                passer.passing_touchdowns,
                passer.passing_interceptions,
                f"{passer.passing_percentage:.2f}",
                f"{passer.passing_yards_per_attempt:.2f}",
            )
            for i, passer in enumerate(most_non_qb_passes)
        ],
    ),
)

write_report(
    f"non_rusher_rushes_week_{week}.txt",
    "Non-Rusher Rushing Stats",
    format_table(
        ["Rank", "Player", "Team", "Conference", "Carries", "Yards", "TD", "Long", "YPC"],
        [
            (
                i + 1,
                f"{rusher.player_name} ({rusher.position})",
                rusher.team,
                rusher.conference,
                rusher.rushing_carries,
                rusher.rushing_yards,
                rusher.rushing_touchdowns,
                rusher.rushing_long,
                f"{rusher.rushing_yards_per_carry:.2f}",
            )
            for i, rusher in enumerate(most_non_rusher_rushes)
        ],
    ),
)

for filename in report_files:
    with open(filename, "rb") as file_handle:
        file_data = file_handle.read()
        response = requests.post(
            webhook_url,
            files={"file": (filename, file_data)},
        )
        if response.status_code in (200, 204):
            print(f"Successfully sent {filename} to Discord.")
        else:
            print(f"Failed to send {filename} to Discord. Status code: {response.status_code}")