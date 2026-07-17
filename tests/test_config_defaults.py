"""Configuration contracts for consequential member-facing runtime features."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_without_dotenv() -> dict:
    """Import config in a clean interpreter without allowing the live root .env to participate."""
    script = r"""
import json
import os
import sys
import types

for key in (
    "WEBAPP_SECRET",
    "OLIVER_REVIEW_DRIVE_MEMBERS",
    "CLUB_EMAIL_CADENCE_ENABLED",
    "CLUB_POSTSCRIPT_ENABLED",
):
    os.environ.pop(key, None)
os.environ["DISCORD_BOT_TOKEN"] = "discord-token-must-not-sign-sessions"

dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: False
sys.modules["dotenv"] = dotenv

from agent import config

print(json.dumps({
    "webapp_secret": config.WEBAPP_SECRET,
    "webapp_dev_secret": config.WEBAPP_DEV_SECRET,
    "discord_token": config.TOKEN,
    "review_drive_members": config.REVIEW_DRIVE_MEMBERS,
    "club_email_cadence": config.CLUB_EMAIL_CADENCE_ENABLED,
    "club_postscript": config.CLUB_POSTSCRIPT_ENABLED,
}))
"""
    env = os.environ.copy()
    for key in (
        "WEBAPP_SECRET",
        "OLIVER_REVIEW_DRIVE_MEMBERS",
        "CLUB_EMAIL_CADENCE_ENABLED",
        "CLUB_POSTSCRIPT_ENABLED",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_member_facing_features_are_safe_by_default():
    values = _load_without_dotenv()
    assert values["webapp_secret"] == values["webapp_dev_secret"]
    assert values["webapp_secret"] != values["discord_token"]
    assert values["review_drive_members"] == ""
    assert values["club_email_cadence"] is False
    assert values["club_postscript"] is False


def test_env_example_keeps_member_communications_off():
    settings = {}
    for raw_line in (ROOT / ".env.example").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key] = value.split("#", 1)[0].strip()

    assert settings["CLUB_EMAIL_CADENCE_ENABLED"] == "0"
    assert settings["CLUB_POSTSCRIPT_ENABLED"] == "0"
    assert settings["OLIVER_REVIEW_DRIVE_MEMBERS"] == ""
    assert settings["WEBAPP_SECRET"] == ""
