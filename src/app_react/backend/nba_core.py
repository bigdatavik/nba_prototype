"""
Scoring, orchestration, and per-member decision support (pure logic).

Copied (behavior-preserving) from src/app/app.py. Streamlit status messages
(st.info/st.warning) are replaced with module logging; the model payload,
retry/cold-start handling, ranking, reason codes, priority bands, and trajectory
heuristic are unchanged.
"""

import json
import time
import logging

import requests

from .config import MODEL_ENDPOINT_NAME, PRIORITY_THRESHOLD, get_databricks_host

log = logging.getLogger("nba.core")


# =============================================================================
# Model Scoring
# =============================================================================

def score_actions(member_features: dict, actions: list) -> list:
    """Call the scoring endpoint to score each action for this member."""
    host = get_databricks_host()
    from databricks.sdk.core import Config
    cfg = Config()
    auth_headers = cfg.authenticate()
    auth_headers["Content-Type"] = "application/json"

    # Member feature columns (must match model training)
    m_cols = [
        'age', 'is_male', 'is_dual_eligible_flag', 'total_claims_12m',
        'total_paid_amount_12m', 'avg_claim_amount', 'high_cost_claims_12m',
        'preventive_visits_12m', 'chronic_claims_12m', 'total_interactions_12m',
        'negative_interactions_12m', 'complaints_12m', 'escalations_12m',
        'avg_satisfaction_score', 'phone_interactions', 'digital_interactions',
        'campaigns_received_12m', 'response_rate', 'digital_sessions_12m',
        'churn_risk_score', 'engagement_score', 'escalation_likelihood',
        'care_outreach_likelihood', 'plan_switch_propensity', 'raf_score',
        'clinical_risk_score', 'predicted_cost_12m', 'has_preventive_gap',
        'is_care_mgmt_candidate', 'is_churn_risk',
    ]

    # Build payload: one row per action
    records = []
    for action in actions:
        row = {}
        for col in m_cols:
            val = member_features.get(col, 0)
            row[f"m_{col}"] = float(val) if val is not None else 0.0
        row["a_value_score"] = float(action.get("value_score", 0))
        row["a_strategic_priority"] = float(action.get("strategic_priority", 3))
        row["a_compliance_flag"] = 1.0 if action.get("compliance_flag") else 0.0
        row["a_min_spacing_days"] = float(action.get("min_spacing_days", 30))
        records.append(row)

    # Call endpoint (with retry for cold-start wake-up)
    endpoint_url = f"https://{host}/serving-endpoints/{MODEL_ENDPOINT_NAME}/invocations"
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = requests.post(
                endpoint_url,
                headers=auth_headers,
                json={"dataframe_records": records},
                timeout=120,  # 2 min to allow cold-start wake-up
            )
            if resp.status_code == 200:
                return resp.json().get("predictions", [])
            elif resp.status_code == 503:
                if attempt < max_attempts - 1:
                    log.info("Model endpoint waking up (scale-to-zero) retry %d/%d",
                             attempt + 2, max_attempts)
                    time.sleep(5)
                    continue
            else:
                log.warning("Model endpoint returned %s. Using value_score fallback.",
                            resp.status_code)
                return [a["value_score"] / 100.0 for a in actions]
        except requests.exceptions.ReadTimeout:
            if attempt < max_attempts - 1:
                log.info("Model endpoint waking from cold start (~60s) retry %d/%d",
                         attempt + 2, max_attempts)
                continue
            else:
                log.warning("Model endpoint timed out after retries. Using value_score fallback.")
                return [a["value_score"] / 100.0 for a in actions]

    log.warning("Model endpoint unavailable. Using value_score fallback.")
    return [a["value_score"] / 100.0 for a in actions]


# =============================================================================
# Orchestration Rules
# =============================================================================

def select_channel(member_features: dict, action: dict) -> str:
    """Select optimal channel based on member preference and action eligibility."""
    eligible = action.get("eligible_channels", [])
    if isinstance(eligible, str):
        eligible = json.loads(eligible)

    digital = float(member_features.get("digital_interactions", 0) or 0)
    phone = float(member_features.get("phone_interactions", 0) or 0)

    if digital > phone and "Digital" in eligible:
        return "Digital"
    elif phone > digital and "Call center" in eligible:
        return "Call center"
    elif "Provider" in eligible:
        return "Provider"
    elif eligible:
        return eligible[0]
    return "Digital"


def apply_orchestration(member_features: dict, actions: list, scores: list) -> list:
    """Apply suppression, timing, spacing rules and rank actions."""
    results = []
    for i, action in enumerate(actions):
        score = scores[i] if i < len(scores) else 0
        channel = select_channel(member_features, action)

        results.append({
            "rank": 0,
            "action_id": action["action_id"],
            "action_name": action["action_name"],
            "category": action.get("action_category", ""),
            "team": action.get("team_owner", ""),
            "score": round(score, 4),
            "channel": channel,
            "value_score": action.get("value_score", 0),
            "compliance": action.get("compliance_flag", False),
            "description": action.get("description", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    return results


# =============================================================================
# Assist — per-member decision support (explain, band, trajectory)
# =============================================================================

def _f(member: dict, key: str, default=0.0) -> float:
    """Safe float accessor for member features."""
    val = member.get(key, default)
    try:
        return float(val) if val is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def explain_recommendation(member: dict, action: dict) -> list:
    """Deterministic, human-readable reasons this action was recommended."""
    reasons = []
    cat = (action.get("action_category") or "").upper()

    if _f(member, "has_preventive_gap") >= 1 and cat == "STARS":
        reasons.append("Open preventive / Stars care gap — this action targets it.")
    if (_f(member, "is_care_mgmt_candidate") >= 1 or _f(member, "clinical_risk_score") >= 0.7) \
            and cat in ("PCO", "HOME HEALTH", "MRA"):
        reasons.append(f"High clinical risk (score {_f(member,'clinical_risk_score'):.2f}) — care-management candidate.")
    if _f(member, "chronic_claims_12m") >= 5:
        reasons.append(f"Elevated chronic-condition claims ({int(_f(member,'chronic_claims_12m'))} in 12m).")
    if (_f(member, "is_churn_risk") >= 1 or _f(member, "churn_risk_score") >= 0.5) and cat == "PCO":
        reasons.append(f"Elevated churn risk (score {_f(member,'churn_risk_score'):.2f}) — retention-oriented.")
    if _f(member, "raf_score") >= 1.8 and cat == "MRA":
        reasons.append(f"RAF recapture opportunity (RAF {_f(member,'raf_score'):.2f}).")
    if action.get("compliance_flag"):
        reasons.append("Regulatory / compliance action — prioritized and not suppressible.")
    if _f(member, "value_score", action.get("value_score", 0)) and int(action.get("value_score", 0)) >= 85:
        reasons.append(f"High strategic value (value score {int(action.get('value_score',0))}).")
    if int(action.get("strategic_priority", 5)) == 1:
        reasons.append("Top strategic priority (P1).")

    digital = _f(member, "digital_interactions")
    phone = _f(member, "phone_interactions")
    if digital > phone:
        reasons.append("Member engages more via digital — matches the recommended channel.")
    elif phone > digital:
        reasons.append("Member engages more by phone — matches the recommended channel.")
    if _f(member, "engagement_score") <= 4:
        reasons.append(f"Low engagement ({_f(member,'engagement_score'):.1f}/10) — proactive outreach may re-activate.")

    if not reasons:
        reasons.append(f"Best model fit for this member among available actions "
                       f"(value score {int(action.get('value_score',0))}).")
    return reasons[:5]


def priority_band(score: float) -> str:
    """Map an NBA priority score (0-1) to a band label."""
    if score >= 0.66:
        return "High priority"
    if score >= 0.40:
        return "Medium priority"
    return "Watch"


def project_trajectory(member: dict, action: dict, score: float) -> dict:
    """Heuristic projection of the member's risk with vs. without this action."""
    cat = (action.get("action_category") or "").upper()
    churn = _f(member, "churn_risk_score")
    clinical = _f(member, "clinical_risk_score")
    if cat == "PCO":
        current, label = churn, "churn risk"
    elif cat in ("MRA", "HOME HEALTH"):
        current, label = clinical, "clinical risk"
    else:  # STARS / Pharmacy / other → clinical/care-gap proxy
        current, label = max(clinical, churn), "risk"
    value = float(action.get("value_score", 70) or 70)
    lift = min(0.45, 0.15 + value / 300.0)          # 15%–45% expected reduction
    without = min(0.99, round(current + 0.05, 2))    # drifts up without action
    with_new = round(current * (1 - lift), 2)
    return {"label": label, "current": round(current, 2), "without": without,
            "with_new": with_new, "lift_pct": round(lift * 100)}
