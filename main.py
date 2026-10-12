import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import cfbd
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

if not secret_access_token:
    raise RuntimeError("Missing access token. Set ACCESS_TOKEN (or access_token) as a GitHub Actions secret.")

if not secret_discord_webhook:
    raise RuntimeError("Missing Discord webhook. Set DISCORD_WEBHOOK (or discord_webhook) as a GitHub Actions secret.")

configuration = cfbd.Configuration(
    host=secret_host,
    access_token=secret_access_token,
)
webhook_url = secret_discord_webhook


@dataclass
class Drive:
    drive_id: str
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
    home_team: str
    away_team: str
    home_score: int
    away_score: int


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


scoring_drives = []
non_scoring_drives = []
punts = []
safeties = []
field_goals = []
spinach_teams = []
games_that_were_17_to_0 = []
scoops_and_scores = []
pick_sixes = []

fbs_games = []
fcs_games = []
d2_games = []
d3_games = []

today = time.strftime("%Y-%m-%d")

year_raw = first_env("YEAR", "year")
week_raw = first_env("WEEK", "week", "CFBD_WEEK")

if year_raw is None or week_raw is None:
    raise RuntimeError("Missing YEAR and WEEK environment variables. cron-job.org must send both values for each run.")

year = int(year_raw)
week = int(week_raw)


def parse_punt_text(play_text):
    pattern = r"^(?:\([^)]+\)\s*)?#?(\d+)\s+(.+?)\s+punt\s+(\d+)\s+yards\b"
    match = re.search(pattern, play_text)
    if match:
        return int(match.group(1)), match.group(2), int(match.group(3))
    return 0, "", 0


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


with cfbd.ApiClient(configuration) as api_client:
    api_instance = cfbd.DrivesApi(api_client)
    plays_api = cfbd.PlaysApi(api_client)
    games_api = cfbd.GamesApi(api_client)

    try:
        api_response = api_instance.get_drives(year, week=week)
        for drive in api_response:
            result = drive.drive_result
            scoring = result in ["TD", "FG"]
            drive_result = Drive(
                drive_id=drive.id,
                offense_team=drive.offense,
                defense_team=drive.defense,
                result=result,
                plays=drive.plays,
                yards=drive.yards,
                minutes=drive.elapsed.minutes,
                seconds=drive.elapsed.seconds,
                scoring=scoring,
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
        print(f"Exception when calling DrivesApi->get_drives: {e}\n")

    try:
        for punt in plays_api.get_plays(year, week=week, play_type="PUNT"):
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
                    play_text=punt.play_text,
                )
            )
    except Exception as e:
        print(f"Exception when calling PlaysApi->get_plays: {e}\n")

    try:
        for safety in plays_api.get_plays(year, week=week, play_type="SF"):
            safeties.append(
                Play(
                    offense=safety.offense,
                    defense=safety.defense,
                    quarter=safety.period,
                    time=f"{safety.clock.minutes}:{str(safety.clock.seconds).zfill(2)}",
                    play_text=safety.play_text,
                )
            )
    except Exception as e:
        print(f"Exception when calling PlaysApi for Safeties->get_plays: {e}\n")

    try:
        for field_goal in plays_api.get_plays(year, week=week, play_type="FG"):
            play = Play(
                offense=field_goal.offense,
                defense=field_goal.defense,
                quarter=field_goal.period,
                time=f"{field_goal.clock.minutes}:{str(field_goal.clock.seconds).zfill(2)}",
                play_text=field_goal.play_text,
            )
            play.yards_gained = field_goal.yards_gained
            field_goals.append(play)
    except Exception as e:
        print(f"Exception when calling PlaysApi for Field Goals->get_plays: {e}\n")

    for drive in scoops_and_scores + pick_sixes:
        try:
            plays_response = plays_api.get_plays(year, week=week, defense=drive.defense_team)
            for play in plays_response:
                if play.drive_id == drive.drive_id and play.scoring:
                    drive.final_play_text = play.play_text
        except Exception as e:
            label = "Scoop and Score" if drive in scoops_and_scores else "Pick Six"
            print(f"Exception when calling PlaysApi for {label} final play text->get_plays: {e}\n")

    try:
        for team_stat in games_api.get_game_team_stats(year, week=week, classification="fbs"):
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
                home_team.spinach_yards = share_of_yards / home_team.total_points if home_team.total_points > 0 else share_of_yards
                spinach_teams.append(home_team)
            elif away_team.team_yards > 350 and away_team.team_yards > home_team.team_yards:
                share_of_yards = away_team.team_yards / total_yards
                away_team.spinach_yards = share_of_yards / away_team.total_points if away_team.total_points > 0 else share_of_yards
                spinach_teams.append(away_team)
    except Exception as e:
        print(f"Exception when calling TeamsApi for Team Stats->get_team_stats: {e}\n")

    try:
        for team_stat in games_api.get_game_team_stats(year, week=week, classification="fcs"):
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
                home_team.spinach_yards = share_of_yards / home_team.total_points if home_team.total_points > 0 else share_of_yards
                spinach_teams.append(home_team)
            elif away_team.team_yards > 350 and away_team.team_yards > home_team.team_yards:
                share_of_yards = away_team.team_yards / total_yards
                away_team.spinach_yards = share_of_yards / away_team.total_points if away_team.total_points > 0 else share_of_yards
                spinach_teams.append(away_team)
    except Exception as e:
        print(f"Exception when calling TeamsApi for Team Stats->get_team_stats: {e}\n")

    for classification, result_list in (
        ("fbs", fbs_games),
        ("fcs", fcs_games),
        ("ii", d2_games),
        ("iii", d3_games),
    ):
        try:
            for game in games_api.get_games(year, week=week, classification=classification):
                game_record = Game(
                    home_team=game.home_team,
                    away_team=game.away_team,
                    home_score=game.home_points,
                    away_score=game.away_points,
                )
                if game_record.home_score is not None and game_record.away_score is not None:
                    result_list.append(game_record)
        except Exception as e:
            print(f"Exception when calling GamesApi for {classification} Games->get_games: {e}\n")

for game in games_that_were_17_to_0:
    try:
        game_data = games_api.get_games(year, week=week, id=game.game_id)
        for result in game_data:
            game.home_final_score = result.home_points
            game.away_final_score = result.away_points
            if game.home_score > game.away_score:
                game.seventeen_zero_team_lost = result.home_points < result.away_points
            elif game.away_score > game.home_score:
                game.seventeen_zero_team_lost = result.away_points < result.home_points
    except Exception as e:
        print(f"Exception when calling GamesApi for 17-0 Games->get_games: {e}\n")


###################### REPORTING ######################

longest_scoring_drives = sorted(scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)
longest_non_scoring_drives = sorted(non_scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)

try:
    for drive in longest_scoring_drives[:10]:
        max_play_number = 0
        plays_response = plays_api.get_plays(year, week=week, offense=drive.offense_team)
        for play in plays_response:
            if play.drive_id == drive.drive_id:
                if play.play_type == "Timeout":
                    continue
                if play.play_number > max_play_number:
                    max_play_number = play.play_number
                    drive.final_play_text = play.play_text

    for drive in longest_non_scoring_drives[:10]:
        max_play_number = 0
        plays_response = plays_api.get_plays(year, week=week, offense=drive.offense_team)
        for play in plays_response:
            if play.drive_id == drive.drive_id:
                if play.play_type == "Timeout":
                    continue
                if play.play_number > max_play_number:
                    max_play_number = play.play_number
                    drive.final_play_text = play.play_text
except Exception as e:
    print(f"Exception when calling PlaysApi for final play text->get_plays: {e}\n")


# Helper for writing repeated report files.
def write_report(path, header, rows):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{header}\n")
        for row in rows:
            handle.write(row)


write_report(
    f"scoring_drives_{today}.txt",
    "10 Longest Scoring Drives:",
    [
        f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}, Yards: {drive.yards}, Final Play: {drive.final_play_text}\n"
        for i, drive in enumerate(longest_scoring_drives[:10])
    ],
)

write_report(
    f"non_scoring_drives_{today}.txt",
    "10 Longest Non-Scoring Drives:",
    [
        f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}, Yards: {drive.yards}, Final Play: {drive.final_play_text}\n"
        for i, drive in enumerate(longest_non_scoring_drives[:10])
    ],
)

write_report(
    f"longest_punts_{today}.txt",
    "10 Longest Punts:",
    [
        f"{i+1}. Offense: {punt.offense}, Defense: {punt.defense}, Quarter: {punt.quarter}, Time: {punt.time}, Player: #{punt.punter_number} {punt.punter_name}, Punt Distance: {punt.punt_distance} yards\n"
        for i, punt in enumerate(sorted(punts, key=lambda x: x.punt_distance, reverse=True)[:10])
    ],
)

write_report(
    f"shortest_punts_{today}.txt",
    "10 Shortest Punts:",
    [
        f"{i+1}. Offense: {punt.offense}, Defense: {punt.defense}, Quarter: {punt.quarter}, Time: {punt.time}, Player: #{punt.punter_number} {punt.punter_name}, Punt Distance: {punt.punt_distance} yards, Play Text: {punt.play_text}\n"
        for i, punt in enumerate(sorted(punts, key=lambda x: x.punt_distance)[:10])
    ],
)

write_report(
    f"safeties_{today}.txt",
    "Safety Alert:",
    [
        f"{i+1}. Offense: {safety.offense}, Defense: {safety.defense}, Quarter: {safety.quarter}, Time: {safety.time}, Play Text: {safety.play_text}\n"
        for i, safety in enumerate(safeties)
    ],
)

write_report(
    f"field_goals_{today}.txt",
    "10 Longest Field Goals:",
    [
        f"{i+1}. Offense: {field_goal.offense}, Defense: {field_goal.defense}, Quarter: {field_goal.quarter}, Time: {field_goal.time}, Play Text: {field_goal.play_text}, Yards Gained: {field_goal.yards_gained}\n"
        for i, field_goal in enumerate(sorted(field_goals, key=lambda x: x.yards_gained, reverse=True)[:10])
    ],
)

write_report(
    f"spinach_teams_of_the_week_{today}.txt",
    "Spinach Teams of the Week:",
    [
        f"{i+1}. Team: {team_stat.team}, Total Points: {team_stat.total_points}, Team Yards: {team_stat.team_yards}, Total Game Yards: {team_stat.total_yards}, Spinach Yards per Point: {team_stat.spinach_yards:.2f}\n"
        for i, team_stat in enumerate(sorted(spinach_teams, key=lambda x: x.spinach_yards, reverse=True)[:10])
    ],
)

write_report(
    f"highest_scoring_fbs_games_{today}.txt",
    "10 Highest Scoring FBS Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(fbs_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10])
    ],
)

write_report(
    f"lowest_scoring_fbs_games_{today}.txt",
    "10 Lowest Scoring FBS Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(fbs_games, key=lambda x: x.home_score + x.away_score)[:10])
    ],
)

write_report(
    f"highest_scoring_fcs_games_{today}.txt",
    "10 Highest Scoring FCS Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(fcs_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10])
    ],
)

write_report(
    f"lowest_scoring_fcs_games_{today}.txt",
    "10 Lowest Scoring FCS Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(fcs_games, key=lambda x: x.home_score + x.away_score)[:10])
    ],
)

write_report(
    f"highest_scoring_d2_games_{today}.txt",
    "10 Highest Scoring D2 Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(d2_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10])
    ],
)

write_report(
    f"lowest_scoring_d2_games_{today}.txt",
    "10 Lowest Scoring D2 Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(d2_games, key=lambda x: x.home_score + x.away_score)[:10])
    ],
)

write_report(
    f"highest_scoring_d3_games_{today}.txt",
    "10 Highest Scoring D3 Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(d3_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10])
    ],
)

write_report(
    f"lowest_scoring_d3_games_{today}.txt",
    "10 Lowest Scoring D3 Games:",
    [
        f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n"
        for i, game in enumerate(sorted(d3_games, key=lambda x: x.home_score + x.away_score)[:10])
    ],
)

report_lines = [
    "*** The Most Dangerous Lead In College Football ***\n",
    f"Week {week} of {year} Season\n",
    "Teams that were 17-0 and lost:\n",
]
j = 0
for game in games_that_were_17_to_0:
    if game.seventeen_zero_team_lost:
        j += 1
        if game.home_score > game.away_score:
            report_lines.append(
                f"{j}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.home_team} lost {game.away_final_score} - {game.home_final_score}\n"
            )
        else:
            report_lines.append(
                f"{j}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.away_team} lost {game.home_final_score} - {game.away_final_score}\n"
            )

report_lines.append("\n =================================================== \n\n")
report_lines.append("Teams that survived:\n")
j = 0
for game in games_that_were_17_to_0:
    if not game.seventeen_zero_team_lost:
        j += 1
        if game.home_score > game.away_score:
            report_lines.append(
                f"{j}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.home_team} survived with a win of {game.home_final_score} - {game.away_final_score}\n"
            )
        else:
            report_lines.append(
                f"{j}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.away_team} survived with a win of {game.away_final_score} - {game.home_final_score}\n"
            )

write_report(f"games_that_were_17_to_0_{today}.txt", "", report_lines)

write_report(
    f"scoops_and_scores_{today}.txt",
    "Scoop and Score Plays:",
    [
        f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Time: {drive.minutes}:{drive.seconds:02d}, Final Play: {drive.final_play_text}\n"
        for i, drive in enumerate(scoops_and_scores)
    ],
)

write_report(
    f"pick_sixes_{today}.txt",
    "Pick Six Plays:",
    [
        f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Time: {drive.minutes}:{drive.seconds:02d}, Final Play: {drive.final_play_text}\n"
        for i, drive in enumerate(pick_sixes)
    ],
)

for filename in [
    f"scoring_drives_{today}.txt",
    f"non_scoring_drives_{today}.txt",
    f"longest_punts_{today}.txt",
    f"safeties_{today}.txt",
    f"field_goals_{today}.txt",
    f"spinach_teams_of_the_week_{today}.txt",
    f"highest_scoring_fbs_games_{today}.txt",
    f"lowest_scoring_fbs_games_{today}.txt",
    f"highest_scoring_fcs_games_{today}.txt",
    f"lowest_scoring_fcs_games_{today}.txt",
    f"highest_scoring_d2_games_{today}.txt",
    f"lowest_scoring_d2_games_{today}.txt",
    f"highest_scoring_d3_games_{today}.txt",
    f"lowest_scoring_d3_games_{today}.txt",
    f"games_that_were_17_to_0_{today}.txt",
    f"scoops_and_scores_{today}.txt",
    f"pick_sixes_{today}.txt",
]:
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