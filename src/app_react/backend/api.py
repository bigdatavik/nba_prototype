"""
FastAPI router — all /api/* routes. Each is a thin wrapper over the copied core
modules (db / nba_core / llm / genie / config). Behaviour matches the Streamlit
app; pandas is never returned across the boundary (JSON records only).
"""

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from . import config, db, nba_core, llm, genie

router = APIRouter(prefix="/api")


# =============================================================================
# Helpers
# =============================================================================

def _enrich_ranked(member: dict, actions: list, ranked: list) -> list:
    """Attach per-action explain / priority band / trajectory inline so the
    drawer needs no second call."""
    by_id = {a["action_id"]: a for a in actions}
    enriched = []
    for r in ranked:
        raw = by_id.get(r["action_id"], {})
        score = float(r["score"])
        item = dict(r)
        item["band"] = nba_core.priority_band(score)
        item["explain"] = nba_core.explain_recommendation(member, raw)
        item["trajectory"] = nba_core.project_trajectory(member, raw, score)
        enriched.append(item)
    return enriched


# =============================================================================
# Config + reference
# =============================================================================

@router.get("/config")
def get_config(request: Request):
    """Branches, project, schema, model, genie id, llm, user — feeds the shell +
    footer. Keeps nothing hardcoded on the frontend."""
    return {
        "lakebase_project": config.LAKEBASE_PROJECT,
        "lakebase_database": config.LAKEBASE_DATABASE,
        "lakebase_schema": config.LAKEBASE_SCHEMA,
        "branch_production": config.LAKEBASE_BRANCH_PRODUCTION,
        "branch_app_writes": config.LAKEBASE_BRANCH_APP_WRITES,
        "model_endpoint_name": config.MODEL_ENDPOINT_NAME,
        "genie_space_id": config.GENIE_SPACE_ID,
        "genie_enabled": bool(config.GENIE_SPACE_ID),
        "llm_endpoint_name": config.LLM_ENDPOINT_NAME,
        "llm_enabled": bool(config.LLM_ENDPOINT_NAME),
        "user": config.get_app_user(dict(request.headers)),
    }


@router.get("/reference")
def get_reference():
    return {
        "categories": config.CATEGORIES,
        "teams": config.TEAMS,
        "channels": config.CHANNELS,
        "priority_threshold": config.PRIORITY_THRESHOLD,
        "ask_nba_suggestions": config.ASK_NBA_SUGGESTIONS,
    }


# =============================================================================
# Members
# =============================================================================

@router.get("/members")
def list_members():
    return {"members": db.get_all_members()}


@router.get("/members/{member_id}")
def member_features(member_id: str):
    member = db.get_member_features(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    return member


@router.get("/members/{member_id}/score")
def score_member(member_id: str):
    member = db.get_member_features(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    actions = db.get_action_catalog()
    scores = nba_core.score_actions(member, actions)
    ranked = nba_core.apply_orchestration(member, actions, scores)
    return {
        "member": member,
        "ranked": _enrich_ranked(member, actions, ranked),
        "priority_threshold": config.PRIORITY_THRESHOLD,
    }


class WhatIfBody(BaseModel):
    churn_risk_score: Optional[float] = None
    engagement_score: Optional[float] = None
    clinical_risk_score: Optional[float] = None
    has_preventive_gap: Optional[bool] = None


@router.post("/members/{member_id}/whatif")
def whatif(member_id: str, body: WhatIfBody):
    member = db.get_member_features(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    actions = db.get_action_catalog()

    m2 = dict(member)
    if body.churn_risk_score is not None:
        m2["churn_risk_score"] = body.churn_risk_score
        m2["is_churn_risk"] = 1 if body.churn_risk_score > 0.5 else 0
    if body.engagement_score is not None:
        m2["engagement_score"] = body.engagement_score
    if body.clinical_risk_score is not None:
        m2["clinical_risk_score"] = body.clinical_risk_score
        m2["is_care_mgmt_candidate"] = 1 if body.clinical_risk_score > 0.7 else 0
    if body.has_preventive_gap is not None:
        m2["has_preventive_gap"] = 1 if body.has_preventive_gap else 0

    ranked = nba_core.apply_orchestration(m2, actions, nba_core.score_actions(m2, actions))
    return {
        "ranked": _enrich_ranked(m2, actions, ranked),
        "priority_threshold": config.PRIORITY_THRESHOLD,
    }


# =============================================================================
# Draft outreach (LLM)
# =============================================================================

class DraftBody(BaseModel):
    member_id: str
    action_id: str
    channel: str


@router.post("/draft-outreach")
def draft(body: DraftBody):
    if not config.LLM_ENDPOINT_NAME:
        raise HTTPException(status_code=503, detail="LLM_ENDPOINT_NAME not set — drafting disabled.")
    member = db.get_member_features(body.member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member {body.member_id} not found.")
    action = next((a for a in db.get_action_catalog() if a["action_id"] == body.action_id), None)
    if not action:
        raise HTTPException(status_code=404, detail=f"Action {body.action_id} not found.")
    msg = llm.draft_outreach(member, action, body.channel)
    if not msg:
        raise HTTPException(status_code=502, detail="LLM endpoint returned no draft.")
    return {"message": msg}


# =============================================================================
# Actions (catalog CRUD)
# =============================================================================

@router.get("/actions")
def actions_production():
    return {"actions": db.get_action_catalog()}


@router.get("/actions/staged")
def actions_staged():
    return {"actions": db.get_action_catalog_staged()}


@router.post("/actions")
def create_action(action_data: dict = Body(...)):
    if not action_data.get("action_id") or not action_data.get("action_name"):
        raise HTTPException(status_code=400, detail="action_id and action_name are required.")
    ok = db.add_action(action_data)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to add action (cannot reach app-writes branch).")
    return {"ok": True}


@router.put("/actions/{action_id}")
def edit_action(action_id: str, updates: dict = Body(...)):
    ok = db.update_action(action_id, updates)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to update action.")
    return {"ok": True}


@router.delete("/actions/{action_id}")
def remove_action(action_id: str):
    ok = db.delete_action(action_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to delete action.")
    return {"ok": True}


# =============================================================================
# Change log
# =============================================================================

@router.get("/change-log")
def change_log():
    return {"rows": db.get_change_log()}


# =============================================================================
# Decisions — Approve & Act
# =============================================================================

@router.get("/decisions")
def decisions(member_id: Optional[str] = None):
    return {"decisions": db.get_decisions(member_id)}


class DecisionBody(BaseModel):
    member_id: str
    action_id: Optional[str] = None
    action_name: Optional[str] = None
    channel: Optional[str] = None
    score: Optional[float] = 0.0
    status: str = "Approved"
    disposition: Optional[str] = None
    note: Optional[str] = None
    approver: Optional[str] = None


@router.post("/decisions")
def create_decision(body: DecisionBody):
    ok = db.record_decision(
        body.member_id, body.action_id, body.action_name, body.channel, body.score,
        body.status, body.disposition, body.note, body.approver)
    if not ok:
        raise HTTPException(status_code=502,
                            detail="Could not write decision (app SP may lack CREATE/INSERT).")
    return {"ok": True}


class OutcomeBody(BaseModel):
    outcome: str


@router.patch("/decisions/{decision_id}/outcome")
def decision_outcome(decision_id: int, body: OutcomeBody):
    ok = db.update_decision_outcome(decision_id, body.outcome)
    if not ok:
        raise HTTPException(status_code=502, detail="Could not update the outcome.")
    return {"ok": True}


# =============================================================================
# Ask NBA (Genie)
# =============================================================================

class AskBody(BaseModel):
    question: str
    conversation_id: Optional[str] = None


@router.post("/ask-nba")
def ask_nba(body: AskBody):
    if not config.GENIE_SPACE_ID:
        raise HTTPException(status_code=503, detail="GENIE_SPACE_ID not set — Ask NBA disabled.")
    return genie.genie_ask(body.question, body.conversation_id)
