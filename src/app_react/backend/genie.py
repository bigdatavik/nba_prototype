"""
Genie Conversation API (Ask NBA).

Copied (behavior-preserving) from src/app/app.py, with one serialization change:
the SQL query result is returned as {columns, rows} JSON (columns = list[str],
rows = list[list]) instead of a pandas DataFrame, so it crosses the API boundary
cleanly. The auth path, start/continue-conversation flow, and polling loop are
unchanged.
"""

import json
import time
from typing import Optional

import requests

from .config import GENIE_SPACE_ID, get_databricks_host


def _genie_headers():
    from databricks.sdk.core import Config
    cfg = Config()
    h = cfg.authenticate()
    h["Content-Type"] = "application/json"
    return h


def genie_ask(question: str, conversation_id: Optional[str] = None) -> dict:
    """Ask the Genie space a question (start or continue a conversation) and wait
    for the answer. Returns {conversation_id, answer, sql, columns, rows, error}."""
    host = get_databricks_host()
    base = f"https://{host}/api/2.0/genie/spaces/{GENIE_SPACE_ID}"
    headers = _genie_headers()
    out = {"conversation_id": conversation_id, "answer": "", "sql": "",
           "columns": None, "rows": None, "error": None}
    try:
        # Start or continue the conversation
        if conversation_id:
            r = requests.post(f"{base}/conversations/{conversation_id}/messages",
                              headers=headers, json={"content": question}, timeout=30)
        else:
            r = requests.post(f"{base}/start-conversation",
                              headers=headers, json={"content": question}, timeout=30)
        if r.status_code != 200:
            out["error"] = f"Genie start error {r.status_code}: {r.text[:300]}"
            return out
        data = r.json()
        cid = data.get("conversation_id") or conversation_id
        mid = data.get("message_id") or (data.get("message") or {}).get("id")
        out["conversation_id"] = cid

        # Poll the message until it completes (Genie + warehouse can take a bit)
        msg = {}
        for _ in range(40):  # ~200s max
            m = requests.get(f"{base}/conversations/{cid}/messages/{mid}",
                             headers=headers, timeout=30)
            if m.status_code != 200:
                out["error"] = f"Genie poll error {m.status_code}"
                return out
            msg = m.json()
            status = msg.get("status")
            if status in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
                break
            time.sleep(5)

        if msg.get("status") != "COMPLETED":
            detail = ""
            err = msg.get("error")
            if isinstance(err, dict):
                detail = err.get("error") or err.get("message") or json.dumps(err)
            elif isinstance(err, str):
                detail = err
            if not detail:
                for att in msg.get("attachments", []):
                    if isinstance(att.get("error"), dict):
                        detail = att["error"].get("error") or att["error"].get("message", "")
                    if detail:
                        break
            if not detail:
                detail = json.dumps(msg)[:600]
            out["error"] = (f"Genie did not complete (status={msg.get('status')}): "
                            f"{detail}")
            return out

        # Extract text answer + SQL, and fetch the query result if present
        for att in msg.get("attachments", []):
            if att.get("text"):
                out["answer"] = att["text"].get("content", "")
            if att.get("query"):
                q = att["query"]
                out["sql"] = q.get("query", "")
                att_id = att.get("attachment_id")
                result = _genie_query_result(base, cid, mid, att_id, headers)
                if result:
                    out["columns"] = result["columns"]
                    out["rows"] = result["rows"]
        return out
    except Exception as e:
        out["error"] = f"Genie request failed: {e}"
        return out


def _genie_query_result(base, cid, mid, att_id, headers) -> Optional[dict]:
    """Fetch a Genie attachment's SQL result. Returns {columns, rows} or None."""
    urls = []
    if att_id:
        urls.append(f"{base}/conversations/{cid}/messages/{mid}/attachments/{att_id}/query-result")
    urls.append(f"{base}/conversations/{cid}/messages/{mid}/query-result")
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code != 200:
                continue
            sr = r.json().get("statement_response", {})
            cols = [c["name"] for c in sr.get("manifest", {}).get("schema", {}).get("columns", [])]
            rows = sr.get("result", {}).get("data_array")
            if cols and rows is not None:
                return {"columns": cols, "rows": rows}
        except Exception:
            continue
    return None
