"""Explainable proactive admission: explicit goals, evidence, urgency and cost.

This filter proposes investigation. It never grants permissions or turns a
document's text into an instruction. Final evidence-backed actions still require
the existing human approval flow.
"""
from __future__ import annotations

from .common import JST, canonical, now_utc, stamp


def assess(store, cfg, candidate, now):
    value = {"meeting": .9, "deadline": .85, "briefing": .5, "transcript": .45, "project_change": .6}[candidate["kind"]]
    urgency = {"meeting": 1., "deadline": .9, "briefing": .5, "transcript": .3, "project_change": .4}[candidate["kind"]]
    with store.connection() as cx:
        day = now.astimezone(JST).date().isoformat()
        budget = cx.execute("SELECT calls,tokens FROM budget WHERE day=?", (day,)).fetchone()
        notifications = cx.execute("SELECT count(*) FROM notifications WHERE created_at>=?", (stamp(now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)),)).fetchone()[0]
    if budget and (budget[0] >= cfg.bedrock.daily_calls or budget[1] >= cfg.bedrock.daily_tokens):
        return {"decision": "defer", "reason": "daily_budget", "score": 0.0}
    load = min(1., notifications / cfg.notification_daily_limit)
    # Only bounded read/propose capabilities are considered. Anything else must
    # pass explicit task/profile authorization, regardless of a high score.
    estimated_cost = .15
    risk = .1 if candidate["kind"] in ("transcript", "project_change") else .05
    score = .45 * value + .4 * urgency - .1 * estimated_cost - .1 * risk - .15 * load
    return {"decision": "investigate" if score >= .25 else "defer", "reason": candidate["kind"],
            "score": round(score, 4), "value": value, "urgency": urgency,
            "cost_estimate": estimated_cost, "risk_estimate": risk, "notification_load": load,
            "estimation": "host_rule_not_learned_utility"}
