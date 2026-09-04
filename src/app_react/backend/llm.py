"""
Foundation Model chat calls for the per-member Assist "Draft outreach".

Copied (behavior-preserving) from src/app/app.py.
"""

import logging
from typing import Optional

import requests

from .config import LLM_ENDPOINT_NAME, get_databricks_host
from .nba_core import _f

log = logging.getLogger("nba.llm")


def call_llm(messages: list, max_tokens: int = 300, temperature: float = 0.4) -> Optional[str]:
    """Call a Databricks Foundation Model chat endpoint. Returns text or None."""
    if not LLM_ENDPOINT_NAME:
        return None
    host = get_databricks_host()
    from databricks.sdk.core import Config
    cfg = Config()
    headers = cfg.authenticate()
    headers["Content-Type"] = "application/json"
    try:
        resp = requests.post(
            f"https://{host}/serving-endpoints/{LLM_ENDPOINT_NAME}/invocations",
            headers=headers,
            json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=60,
        )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def draft_outreach(member: dict, action: dict, channel: str) -> Optional[str]:
    """Draft a short, compliant member outreach message for this action/channel."""
    age = int(_f(member, "age"))
    context = (
        f"Member context (no PHI): age band ~{age}, engagement {_f(member,'engagement_score'):.1f}/10, "
        f"preferred channel {channel}."
    )
    system = (
        "You are a healthcare payer member-engagement assistant. Write a short, warm, "
        "plain-language outreach message. Be compliant: no PHI, no diagnosis claims, no "
        "guarantees; include a clear next step. Keep it under 80 words."
    )
    user = (
        f"Action: {action.get('action_name')} — {action.get('description','')}\n"
        f"Channel: {channel}\n{context}\n\n"
        f"Write the {channel} outreach message."
    )
    return call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}])
