"""Oliver — the R/W Book Club's Discord agent.

A discord.py bot powered by Claude (anthropic SDK) with the Git corpus as
ground truth. Answers everything in #ask-oliver and replies in the main
channel when @mentioned / named / replied to. Exposes the `/oliver` slash
command group (review, add-book, schedule, feedback, …), runs a daily
proactive scheduler (meeting reminders, review nudges, milestones), and
logs 👍/👎 reactions to its own replies for follow-up analysis.

Run from the repo root as `python -m agent.bot`.
"""
