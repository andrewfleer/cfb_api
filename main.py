import re
import time
import cfbd
from cfbd.exceptions import ApiException
from pprint import pprint
import os
from pathlib import Path
from dotenv import load_dotenv
import requests

# Load environment variables from a local file when present, but also allow
# GitHub Actions / CI secrets to override or provide values at runtime.
base_dir = Path(__file__).resolve().parent
load_dotenv(dotenv_path=base_dir / 'secrets.env', override=False)

secret_host = (
    os.getenv('host')
    or os.getenv('HOST')
    or os.getenv('CFBD_HOST')
    or 'https://api.collegefootballdata.com'
)
secret_access_token = os.getenv('access_token') or os.getenv('ACCESS_TOKEN')
secret_discord_webhook = os.getenv('discord_webhook') or os.getenv('DISCORD_WEBHOOK')

if not secret_access_token:
    raise RuntimeError('Missing access token. Set ACCESS_TOKEN (or access_token) as a GitHub Actions secret.')

if not secret_discord_webhook:
    raise RuntimeError('Missing Discord webhook. Set DISCORD_WEBHOOK (or discord_webhook) as a GitHub Actions secret.')

# Defining the host is optional and defaults to https://api.collegefootballdata.com
# See configuration.py for a list of all supported configuration parameters.
configuration = cfbd.Configuration(
    host = secret_host
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure Bearer authorization: apiKey
configuration = cfbd.Configuration(
    access_token = secret_access_token
)

webhook_url = secret_discord_webhook

class Drive:
    final_play_text = ""
    def __init__(self, drive_id, offense_team, defense_team, result, plays, yards, minutes, seconds, scoring):
        self.drive_id = drive_id
        self.offense_team = offense_team
        self.defense_team = defense_team
        self.result = result
        self.plays = plays
        self.yards = yards
        self.minutes = minutes
        self.seconds = seconds
        self.scoring = scoring

class Punt:
    def __init__(self, offense, defense, quarter, time, punter_name, punter_number, punt_distance, play_text):
        self.offense = offense
        self.defense = defense
        self.quarter = quarter
        self.time = time
        self.punter_name = punter_name
        self.punter_number = punter_number
        self.punt_distance = punt_distance
        self.play_text = play_text

class Play:
    def __init__(self, offense, defense, quarter, time, play_text):
        self.offense = offense
        self.defense = defense
        self.quarter = quarter
        self.time = time
        self.play_text = play_text
        self.yards_gained = 0

class TeamStats:
    team = ""
    total_points = 0
    team_yards = 0
    yards_per_point = 0
    total_yards = 0
    spinach_yards = 0.0

class Game:
    def __init__(self, home_team, away_team, home_score, away_score):
        self.home_team = home_team
        self.away_team = away_team
        self.home_score = home_score
        self.away_score = away_score

class SevenTeenToZeroGame:
    def __init__(self, home_team, away_team, home_score, away_score, game_id):
        self.home_team = home_team
        self.away_team = away_team
        self.home_score = home_score
        self.away_score = away_score
        self.game_id = game_id

    seventeen_zero_team_lost = False
    home_final_score = 0
    away_final_score = 0

scoring_drives = []
non_scoring_drives = []
punts = []
safeties = []
field_goals = []
spinach_teams = []
games_that_were_17_to_0 = []

fbs_games = []
fcs_games = []
d2_games = []
d3_games = []

# get today's date in the format YYYY-MM-DD
today = time.strftime("%Y-%m-%d")

# Require an explicit year/week value from the scheduler or workflow. Do not
# silently fall back so failed scheduler calls fail loudly.
year_raw = os.getenv('YEAR') or os.getenv('year')
week_raw = os.getenv('WEEK') or os.getenv('week') or os.getenv('CFBD_WEEK')

if year_raw is None or week_raw is None:
    raise RuntimeError('Missing YEAR and WEEK environment variables. cron-job.org must send both values for each run.')

year = int(year_raw)
week = int(week_raw)

# Enter a context with an instance of the API client
with cfbd.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = cfbd.DrivesApi(api_client)


    try:
        api_response = api_instance.get_drives(year, week=week)
        #print("The response of DrivesApi -> get_drives:\n")
        #pprint(api_response)
        for drive in api_response:
            result = drive.drive_result
            scoring = False

            if result in ["TD", "FG"]:
                scoring = True
            drive_results = Drive(
                drive_id=drive.id,
                offense_team=drive.offense,
                defense_team=drive.defense,
                result=result,
                plays=drive.plays,
                yards=drive.yards,
                minutes=drive.elapsed.minutes,
                seconds=drive.elapsed.seconds,
                scoring = scoring
            )
            if scoring:
                scoring_drives.append(drive_results)
            else:
                non_scoring_drives.append(drive_results)
            if drive.end_offense_score == 17 and drive.end_defense_score == 0:
                if drive.is_home_offense:
                    home_team = drive.offense
                    away_team = drive.defense
                else:
                    home_team = drive.defense
                    away_team = drive.offense
                seven_teen_to_zero_game = SevenTeenToZeroGame(
                    home_team=home_team,
                    away_team=away_team,
                    home_score=drive.end_offense_score,
                    away_score=drive.end_defense_score,
                    game_id=drive.game_id
                )
                game_already_in_table = False
                for game in games_that_were_17_to_0:
                    if game.game_id == seven_teen_to_zero_game.game_id:
                        game_already_in_table = True
                        break
                    
                if not game_already_in_table:
                    games_that_were_17_to_0.append(seven_teen_to_zero_game)
           # print(f"Drive ID: {drive.drive_id}, Team: {drive.offense_team}, Result: {drive.result}, Plays: {len(drive.plays)}")
    except Exception as e:
        print("Exception when calling DrivesApi->get_drives: %s\n" % e)

    plays_api = cfbd.PlaysApi(api_client)

    try:
        punts_response = plays_api.get_plays(year, week=week, play_type='PUNT')
    
        #print("The response of PlaysApi -> get_plays:\n")
        #pprint(plays_response)
        for punt in punts_response:

            punt_distance = 0
            punter_name = ""
            punter_number = 0
            # Breakdown of the pattern:
            # ^(\d+)       -> Matches the jersey number at the start of the string
            # \s+          -> Matches spaces
            # (.+?)\s+punt -> Captures the punter's name right up until the word 'punt'
            # (\d+)\s+yard -> Captures the punt distance right before the word 'yards'
            pattern = r"^(?:\([^)]+\)\s*)?#?(\d+)\s+(.+?)\s+punt\s+(\d+)\s+yards\b"

            match = re.search(pattern, punt.play_text)

            if match:
                punter_number = int(match.group(1))
                punter_name = match.group(2)
                punt_distance = int(match.group(3))
                
                #print(f"Number: {punter_number}")
                #print(f"Punter: {punter_name}")
                #print(f"Distance: {punt_distance} yards")
            else:
                print("Could not parse play-by-play text. %s\n" % punt.play_text)

            punt = Punt(
                offense=punt.offense,
                defense=punt.defense,
                quarter=punt.period,
                time=str(punt.clock.minutes) + ":" + str(punt.clock.seconds).zfill(2),
                punter_name=punter_name,
                punter_number=punter_number,
                punt_distance=punt_distance,
                play_text=punt.play_text
            )
            punts.append(punt)
    except Exception as e:
        print("Exception when calling PlaysApi->get_plays: %s\n" % e)

    try:
        safeties_response = plays_api.get_plays(year, week=week, play_type='SF')
    
        #print("The response of PlaysApi -> get_plays:\n")
        #pprint(plays_response)
        for safety in safeties_response:
            safety_play = Play(
                offense=safety.offense,
                defense=safety.defense,
                quarter=safety.period,
                time=str(safety.clock.minutes) + ":" + str(safety.clock.seconds).zfill(2),
                play_text=safety.play_text
            )
            safeties.append(safety_play)
    except Exception as e:
        print("Exception when calling PlaysApi for Safeties->get_plays: %s\n" % e)

    try:
        field_goals_response = plays_api.get_plays(year, week=week, play_type='FG')
    
        #print("The response of PlaysApi -> get_plays:\n")
        #pprint(plays_response)
        for field_goal in field_goals_response:
            field_goal_play = Play(
                offense=field_goal.offense,
                defense=field_goal.defense,
                quarter=field_goal.period,
                time=str(field_goal.clock.minutes) + ":" + str(field_goal.clock.seconds).zfill(2),
                play_text=field_goal.play_text
            )
            field_goal_play.yards_gained = field_goal.yards_gained
            field_goals.append(field_goal_play)
    except Exception as e:
        print("Exception when calling PlaysApi for Field Goals->get_plays: %s\n" % e)


    #Spinach Teams of the Week
    games_api = cfbd.GamesApi(api_client)

    # FBS
    try:
        fbs_stats_response = games_api.get_game_team_stats(year, week=week, classification='fbs')
    
        #print("The response of TeamsApi -> get_team_stats:\n")
        #pprint(team_stats_response)
        for team_stat in fbs_stats_response:
            homeTeam = TeamStats()
            awayTeam = TeamStats()
            for team in team_stat.teams:
                for stat in team.stats:
                    if stat.category == "totalYards":
                        yards = stat.stat
                        break

                if team.home_away == "home":
                    homeTeam.team = team.team
                    homeTeam.total_points = team.points
                    homeTeam.team_yards = int(yards)
                elif team.home_away == "away":
                    awayTeam.team = team.team
                    awayTeam.total_points = team.points
                    awayTeam.team_yards = int(yards)

            total_yards = homeTeam.team_yards + awayTeam.team_yards

            homeTeam.total_yards = total_yards
            awayTeam.total_yards = total_yards

            if homeTeam.team_yards > 350 and homeTeam.team_yards > awayTeam.team_yards:
                share_of_yards = homeTeam.team_yards / total_yards
                homeTeam.spinach_yards = share_of_yards / homeTeam.total_points if homeTeam.total_points > 0 else share_of_yards
                spinach_teams.append(homeTeam)
            elif awayTeam.team_yards > 350 and awayTeam.team_yards > homeTeam.team_yards:
                share_of_yards = awayTeam.team_yards / total_yards
                awayTeam.spinach_yards = share_of_yards / awayTeam.total_points if awayTeam.total_points > 0 else share_of_yards
                spinach_teams.append(awayTeam)

    except Exception as e:
        print("Exception when calling TeamsApi for Team Stats->get_team_stats: %s\n" % e)

    # FCS
    try:
        fcs_stats_response = games_api.get_game_team_stats(year, week=week, classification='fcs')

        for team_stat in fcs_stats_response:
            homeTeam = TeamStats()
            awayTeam = TeamStats()
            for team in team_stat.teams:
                for stat in team.stats:
                    if stat.category == "totalYards":
                        yards = stat.stat
                        break

                if team.home_away == "home":
                    homeTeam.team = team.team
                    homeTeam.total_points = team.points
                    homeTeam.team_yards = int(yards)
                elif team.home_away == "away":
                    awayTeam.team = team.team
                    awayTeam.total_points = team.points
                    awayTeam.team_yards = int(yards)

            total_yards = homeTeam.team_yards + awayTeam.team_yards

            homeTeam.total_yards = total_yards
            awayTeam.total_yards = total_yards

            if homeTeam.team_yards > 350 and homeTeam.team_yards > awayTeam.team_yards:
                share_of_yards = homeTeam.team_yards / total_yards
                homeTeam.spinach_yards = share_of_yards / homeTeam.total_points if homeTeam.total_points > 0 else share_of_yards
                spinach_teams.append(homeTeam)
            elif awayTeam.team_yards > 350 and awayTeam.team_yards > homeTeam.team_yards:
                share_of_yards = awayTeam.team_yards / total_yards
                awayTeam.spinach_yards = share_of_yards / awayTeam.total_points if awayTeam.total_points > 0 else share_of_yards
                spinach_teams.append(awayTeam)

    except Exception as e:
        print("Exception when calling TeamsApi for Team Stats->get_team_stats: %s\n" % e)


    # Get All FBS Games
    try:
        fbs_games_response = games_api.get_games(year, week=week, classification='fbs')

        for game in fbs_games_response:
            fbs_game = Game(
                home_team=game.home_team,
                away_team=game.away_team,
                home_score=game.home_points,
                away_score=game.away_points
            )
            if fbs_game.home_score is not None and fbs_game.away_score is not None:
                fbs_games.append(fbs_game)
    except Exception as e:
        print("Exception when calling GamesApi for FBS Games->get_games: %s\n" % e)

    # Get All FCS Games
    try:
        fcs_games_response = games_api.get_games(year, week=week, classification='fcs')

        for game in fcs_games_response:
            fcs_game = Game(
                home_team=game.home_team,
                away_team=game.away_team,
                home_score=game.home_points,
                away_score=game.away_points
            )
            if fcs_game.home_score is not None and fcs_game.away_score is not None:
                fcs_games.append(fcs_game)
    except Exception as e:
        print("Exception when calling GamesApi for FCS Games->get_games: %s\n" % e)

    # Get All D2 Games
    try:
        d2_games_response = games_api.get_games(year, week=week, classification='ii')

        for game in d2_games_response:
            d2_game = Game(
                home_team=game.home_team,
                away_team=game.away_team,
                home_score=game.home_points,
                away_score=game.away_points
            )
            if d2_game.home_score is not None and d2_game.away_score is not None:
                d2_games.append(d2_game)
    except Exception as e:
        print("Exception when calling GamesApi for D2 Games->get_games: %s\n" % e)

    # Get All D3 Games
    try:
        d3_games_response = games_api.get_games(year, week=week, classification='iii')

        for game in d3_games_response:
            d3_game = Game(
                home_team=game.home_team,
                away_team=game.away_team,
                home_score=game.home_points,
                away_score=game.away_points
            )
            if d3_game.home_score is not None and d3_game.away_score is not None:
                d3_games.append(d3_game)
    except Exception as e:
        print("Exception when calling GamesApi for D3 Games->get_games: %s\n" % e)

for seventeen_to_zero_game in games_that_were_17_to_0:
    try:
        game_data = games_api.get_games(year, week=week, id = seventeen_to_zero_game.game_id)
        for game in game_data:
            seventeen_to_zero_game.home_final_score = game.home_points
            seventeen_to_zero_game.away_final_score = game.away_points
            if seventeen_to_zero_game.home_score > seventeen_to_zero_game.away_score:
                if game.home_points > game.away_points:
                    seventeen_to_zero_game.seventeen_zero_team_lost = False
                else:
                    seventeen_to_zero_game.seventeen_zero_team_lost = True
            elif seventeen_to_zero_game.away_score > seventeen_to_zero_game.home_score:
                if game.away_points > game.home_points:
                    seventeen_to_zero_game.seventeen_zero_team_lost = False
                else:
                    seventeen_to_zero_game.seventeen_zero_team_lost = True

    except Exception as e:
        print("Exception when calling GamesApi for 17-0 Games->get_games: %s\n" % e)


###################### REPORTING ######################

longest_scoring_drives = sorted(scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)
longest_non_scoring_drives = sorted(non_scoring_drives, key=lambda x: (x.minutes, x.seconds), reverse=True)

# Populate the final play for the 10 longest scoring drives and the 10 longest non-scoring drives
try:
    for drive in longest_scoring_drives[:10]:
        max_play_number = 0
        plays_response = plays_api.get_plays(year, week=week, offense=drive.offense_team)
        for play in plays_response:
            if play.drive_id == drive.drive_id:
                if play.play_type == "Timeout": 
                    continue  # Skip timeout plays
                if play.play_number > max_play_number:
                    max_play_number = play.play_number
                    drive.final_play_text = play.play_text

    for drive in longest_non_scoring_drives[:10]:
        max_play_number = 0
        plays_response = plays_api.get_plays(year, week=week, offense=drive.offense_team)
        for play in plays_response:
            if play.drive_id == drive.drive_id:
                if play.play_type == "Timeout": 
                    continue  # Skip timeout plays
                if play.play_number > max_play_number:
                    max_play_number = play.play_number
                    drive.final_play_text = play.play_text
except Exception as e:
    print("Exception when calling PlaysApi for final play text->get_plays: %s\n" % e)

## Print 10 longest scoring drives
#print("5 Longest Scoring Drives:")
with open("scoring_drives_{}.txt".format(today), "w") as f: 
    f.write("10 Longest Scoring Drives:\n")
    for i, drive in enumerate(longest_scoring_drives[:10]):
        f.write(f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}, Yards: {drive.yards}, Final Play: {drive.final_play_text}\n")
#    print(f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}")

## Print 10 longest non-scoring drives
#print("\n5 Longest Non-Scoring Drives:")
with open("non_scoring_drives_{}.txt".format(today), "w") as f:
    f.write("10 Longest Non-Scoring Drives:\n")
    for i, drive in enumerate(longest_non_scoring_drives[:10]):
        f.write(f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}, Yards: {drive.yards}, Final Play: {drive.final_play_text}\n")
#    print(f"{i+1}. Offense: {drive.offense_team}, Defense: {drive.defense_team}, Result: {drive.result}, Plays: {drive.plays}, Time: {drive.minutes}:{drive.seconds:02d}")

#Print 10 longest punts
with open("longest_punts_{}.txt".format(today), "w") as f:
    f.write("10 Longest Punts:\n")
    for i, punt in enumerate(sorted(punts, key=lambda x: x.punt_distance, reverse=True)[:10]):
        f.write(f"{i+1}. Offense: {punt.offense}, Defense: {punt.defense}, Quarter: {punt.quarter}, Time: {punt.time}, Player: #{punt.punter_number} {punt.punter_name}, Punt Distance: {punt.punt_distance} yards\n")

#Print 10 shortest punts
with open("shortest_punts_{}.txt".format(today), "w") as f:
    f.write("10 Shortest Punts:\n")
    for i, punt in enumerate(sorted(punts, key=lambda x: x.punt_distance)[:10]):
        f.write(f"{i+1}. Offense: {punt.offense}, Defense: {punt.defense}, Quarter: {punt.quarter}, Time: {punt.time}, Player: #{punt.punter_number} {punt.punter_name}, Punt Distance: {punt.punt_distance} yards, Play Text: {punt.play_text}\n")

#Print all safeties to safeties.txt
with open("safeties_{}.txt".format(today), "w") as f:
    f.write("Safety Alert:\n")
    for i, safety in enumerate(safeties):
        f.write(f"{i+1}. Offense: {safety.offense}, Defense: {safety.defense}, Quarter: {safety.quarter}, Time: {safety.time}, Play Text: {safety.play_text}\n")

#Print 10 longest field goals to field_goals.txt
with open("field_goals_{}.txt".format(today), "w") as f:
    f.write("10 Longest Field Goals:\n")
    for i, field_goal in enumerate(sorted(field_goals, key=lambda x: x.yards_gained, reverse=True)[:10]):
        f.write(f"{i+1}. Offense: {field_goal.offense}, Defense: {field_goal.defense}, Quarter: {field_goal.quarter}, Time: {field_goal.time}, Play Text: {field_goal.play_text}, Yards Gained: {field_goal.yards_gained}\n")

#Print Spinach Teams of the Week:
with open("spinach_teams_of_the_week_{}.txt".format(today), "w") as f:
    f.write("Spinach Teams of the Week:\n")
    for i, team_stat in enumerate(sorted(spinach_teams, key=lambda x: x.spinach_yards, reverse=True)[:10]):
        f.write(f"{i+1}. Team: {team_stat.team}, Total Points: {team_stat.total_points}, Team Yards: {team_stat.team_yards}, Total Game Yards: {team_stat.total_yards}, Spinach Yards per Point: {team_stat.spinach_yards:.2f}\n")

# Print 10 highest scoring FBS games
with open("highest_scoring_fbs_games_{}.txt".format(today), "w") as f:
    f.write("10 Highest Scoring FBS Games:\n")
    for i, game in enumerate(sorted(fbs_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print 10 lowest scoring FBS games
with open("lowest_scoring_fbs_games_{}.txt".format(today), "w") as f:
    f.write("10 Lowest Scoring FBS Games:\n")
    for i, game in enumerate(sorted(fbs_games, key=lambda x: x.home_score + x.away_score)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n") 

# Print 10 highest scoring FCS games
with open("highest_scoring_fcs_games_{}.txt".format(today), "w") as f:
    f.write("10 Highest Scoring FCS Games:\n")
    for i, game in enumerate(sorted(fcs_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print 10 lowest scoring FCS games
with open("lowest_scoring_fcs_games_{}.txt".format(today), "w") as f:
    f.write("10 Lowest Scoring FCS Games:\n")
    for i, game in enumerate(sorted(fcs_games, key=lambda x: x.home_score + x.away_score)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print 10 highest scoring D2 games
with open("highest_scoring_d2_games_{}.txt".format(today), "w") as f:
    f.write("10 Highest Scoring D2 Games:\n")
    for i, game in enumerate(sorted(d2_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")
        
# Print 10 lowest scoring D2 games
with open("lowest_scoring_d2_games_{}.txt".format(today), "w") as f:
    f.write("10 Lowest Scoring D2 Games:\n")
    for i, game in enumerate(sorted(d2_games, key=lambda x: x.home_score + x.away_score)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print 10 highest scoring D3 games
with open("highest_scoring_d3_games_{}.txt".format(today), "w") as f:
    f.write("10 Highest Scoring D3 Games:\n")
    for i, game in enumerate(sorted(d3_games, key=lambda x: x.home_score + x.away_score, reverse=True)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print 10 lowest scoring D3 games
with open("lowest_scoring_d3_games_{}.txt".format(today), "w") as f:
    f.write("10 Lowest Scoring D3 Games:\n")
    for i, game in enumerate(sorted(d3_games, key=lambda x: x.home_score + x.away_score)[:10]):
        f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}\n")

# Print games where 17-0 team lost
with open("games_that_were_17_to_0_{}.txt".format(today), "w") as f:
    f.write ("*** The Most Dangerous Lead In College Football ***\n")
    f.write("Games that were 17-0:\n")
    for i, game in enumerate(games_that_were_17_to_0):
        if game.seventeen_zero_team_lost:
            if game.home_score > game.away_score:
                f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.home_team} lost {game.away_final_score} - {game.home_final_score}\n")
            else:
                f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.away_team} lost {game.home_final_score} - {game.away_final_score}\n")

    f.write("\n =================================================== \n\n")
    f.write ("Teams that survived:\n")
    for i, game in enumerate(games_that_were_17_to_0):
        if not game.seventeen_zero_team_lost:
            if game.home_score > game.away_score:
                f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.home_team} survived with a win of {game.home_final_score} - {game.away_final_score}\n")
            else:
                f.write(f"{i+1}. {game.home_team} vs {game.away_team}: {game.home_score}-{game.away_score}, {game.away_team} survived with a win of {game.away_final_score} - {game.home_final_score}\n")


# Send each file to discord via webhook
for filename in ["scoring_drives_{}.txt", "non_scoring_drives_{}.txt", "longest_punts_{}.txt", "safeties_{}.txt", "field_goals_{}.txt", "spinach_teams_of_the_week_{}.txt", "highest_scoring_fbs_games_{}.txt", "lowest_scoring_fbs_games_{}.txt", "highest_scoring_fcs_games_{}.txt", "lowest_scoring_fcs_games_{}.txt", "highest_scoring_d2_games_{}.txt", "lowest_scoring_d2_games_{}.txt", "highest_scoring_d3_games_{}.txt", "lowest_scoring_d3_games_{}.txt", "games_that_were_17_to_0_{}.txt"]:
    with open(filename.format(today), "rb") as f:
        file_data = f.read()
        response = requests.post(
            webhook_url,
            files={"file": (filename.format(today), file_data)},
        )
        if response.status_code == 204 or response.status_code == 200:
            print(f"Successfully sent {filename.format(today)} to Discord.")
        else:
            print(f"Failed to send {filename.format(today)} to Discord. Status code: {response.status_code}")