import os
from pathlib import Path

from dotenv import load_dotenv
import requests

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

def main() -> None:
    discord_webhook = first_env("discord_webhook","DISCORD_WEBHOOK")
    discord_user = first_env("discord_user", "DISCORD_USER")

    if not discord_webhook:
        raise RuntimeError("Missing webhook url!")
    if not discord_user:
        raise RuntimeError("Missing discord user!")

    webhook_url = discord_webhook
    user = discord_user
    data = {
        "content": f"Hey <@{discord_user}> this is a test error message!"
    }

    response = requests.post(
        webhook_url,
        json=data
    )
    if response.status_code in (200, 204):
        print("Successfully sent webhook!")
    else: 
        print(f"Failed to send webhook. Status code: {response.status_code}")


if __name__ == "__main__":
    main()
