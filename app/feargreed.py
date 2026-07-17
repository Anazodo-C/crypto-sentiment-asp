"""Fear & Greed Index client - alternative.me, free, keyless.

NOTE: not live-tested in the build sandbox (network there is allowlisted
and blocked api.alternative.me). This is a long-standing stable public
endpoint; verify once deployed.
"""
from __future__ import annotations

import httpx

FNG_URL = "https://api.alternative.me/fng/"


async def get_fear_greed(client: httpx.AsyncClient, limit: int = 8) -> dict | None:
    try:
        resp = await client.get(FNG_URL, params={"limit": limit})
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data:
            return None
        current = data[0]
        week_ago = data[7] if len(data) > 7 else data[-1]
        cur_val = int(current["value"])
        week_val = int(week_ago["value"])
        trend = "rising" if cur_val > week_val else "falling" if cur_val < week_val else "stable"
        return {
            "value": cur_val,
            "label": current["value_classification"],
            "trend_7d": trend,
        }
    except Exception:
        return None
