# ============================================================
# MARKET AGENT V4.5 — Structured Claude replies
# claude_json.py
#
# Every Claude call that expects JSON goes through ask_json().
# Instead of asking for "ONLY this JSON" as free text and then
# hand-parsing it (which broke the adversarial critique on the
# first V4.5 laptop run with "Expecting ',' delimiter"), Claude is
# made to answer through a forced tool call. The API returns the
# tool input already parsed, so a stray quote or brace in Claude's
# prose can no longer break a run.
#
# schema=None  — any JSON object; the prompt itself describes the
#                structure (used for the big main-analysis and
#                pre-market replies)
# schema={...} — exact JSON Schema, for small fixed replies
#                (verdicts, grades, statuses) so values are
#                limited to the allowed set
#
# On failure raises StructuredReplyError and saves the full API
# response to logs/claude_reply_errors/<label>.json.
#
# Spend: every call is logged to logs/api_usage.jsonl (tokens + cost)
# and refused BEFORE it is sent if its worst-case cost would take this
# run or this month past strategy.json's api_budget. run_agent.sh sets
# RUN_ID so per-run spend can be summed.
# ============================================================

import json
import os
import re
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ERROR_DIR = os.path.join(BASE_DIR, "logs", "claude_reply_errors")
USAGE_LOG = os.path.join(BASE_DIR, "logs", "api_usage.jsonl")
# USD per million tokens (input, output) — Anthropic list prices
PRICES    = {"claude-sonnet-4-6": (3.00, 15.00)}
DEFAULT_BUDGET = {"per_run_usd": 3.00, "monthly_usd": 50.00}
MODEL     = "claude-sonnet-4-6"
TOOL_NAME = "submit_json"

SUBMIT_NOTE = (
    f"\n\nSubmit your answer by calling the {TOOL_NAME} tool. Its input is exactly "
    "the JSON described above — same keys, nesting and value formats."
)


class StructuredReplyError(ValueError):
    """Claude's reply had no usable structured result."""


class BudgetExceededError(StructuredReplyError):
    """The call was not sent: it would exceed the per-run or monthly budget.
    Subclasses StructuredReplyError so every caller's existing fallback applies."""


def _price(model):
    return PRICES.get(model, (3.00, 15.00))


def _budget():
    try:
        with open(os.path.join(BASE_DIR, "strategy.json")) as f:
            b = json.load(f).get("api_budget", {})
        return {k: float(b.get(k, v)) for k, v in DEFAULT_BUDGET.items()}
    except Exception:
        return dict(DEFAULT_BUDGET)


def usage_summary():
    """Spend this run (RUN_ID) and this calendar month, from api_usage.jsonl."""
    run_id, month = os.getenv("RUN_ID"), datetime.now().strftime("%Y-%m")
    run = mon = 0.0
    calls_run = calls_mon = 0
    try:
        with open(USAGE_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("month") == month:
                    mon += r.get("cost_usd", 0); calls_mon += 1
                if run_id and r.get("run_id") == run_id:
                    run += r.get("cost_usd", 0); calls_run += 1
    except FileNotFoundError:
        pass
    b = _budget()
    return {"run_usd": round(run, 4), "month_usd": round(mon, 4), "calls_run": calls_run, "calls_month": calls_mon,
            "per_run_cap_usd": b["per_run_usd"], "monthly_cap_usd": b["monthly_usd"], "month": month, "run_id": run_id}


def _check_budget(prompt, max_tokens, model, label):
    pin, pout = _price(model)
    worst = (len(prompt) / 3.5) * pin / 1e6 + max_tokens * pout / 1e6  # ~3.5 chars/token, conservative
    u = usage_summary()
    if u["month_usd"] + worst > u["monthly_cap_usd"]:
        raise BudgetExceededError(f"{label}: not sent — monthly Claude budget ${u['monthly_cap_usd']:.2f} would be exceeded "
                                  f"(spent ${u['month_usd']:.2f} this month; this call up to ${worst:.2f})")
    if u["run_id"] and u["run_usd"] + worst > u["per_run_cap_usd"]:
        raise BudgetExceededError(f"{label}: not sent — per-run Claude budget ${u['per_run_cap_usd']:.2f} would be exceeded "
                                  f"(spent ${u['run_usd']:.2f} this run; this call up to ${worst:.2f})")


AUDIT_DIR = os.path.join(BASE_DIR, "logs", "audit")


def _audit(prompt, response, model, label):
    """X6: keep every prompt + reply (gzip, one file per call, grouped by
    day) so any claim in latest.json can be traced to the data Claude was
    actually given. Roughly 20-60 KB per run."""
    try:
        import gzip
        day = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(os.path.join(AUDIT_DIR, day), exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)
        name = f"{datetime.now().strftime('%H%M%S')}_{os.getenv('RUN_ID') or 'manual'}_{safe}.json.gz"
        with gzip.open(os.path.join(AUDIT_DIR, day, name), "wt") as f:
            json.dump({"at": datetime.now().isoformat(), "run_id": os.getenv("RUN_ID"), "label": label,
                       "model": model, "prompt": prompt, "response": response.model_dump()}, f, default=str)
    except Exception:
        pass  # auditing must never break a run


def _log_usage(response, model, label):
    try:
        u = response.usage
        pin, pout = _price(model)
        cache_w = getattr(u, "cache_creation_input_tokens", 0) or 0
        cache_r = getattr(u, "cache_read_input_tokens", 0) or 0
        cost = (u.input_tokens * pin + cache_w * pin * 1.25 + cache_r * pin * 0.1 + u.output_tokens * pout) / 1e6
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "month": datetime.now().strftime("%Y-%m"),
               "run_id": os.getenv("RUN_ID"), "label": label, "model": model,
               "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
               "cache_write_tokens": cache_w, "cache_read_tokens": cache_r, "cost_usd": round(cost, 5)}
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # accounting must never break a run


def _dump(label, response):
    try:
        os.makedirs(ERROR_DIR, exist_ok=True)
        safe  = re.sub(r"[^A-Za-z0-9_-]+", "_", label) or "claude"
        path  = os.path.join(ERROR_DIR, f"{safe}.json")
        with open(path, "w") as f:
            json.dump({"saved_at": datetime.now().isoformat(), "response": response.model_dump()},
                      f, indent=2, default=str)
        return path
    except Exception:
        return None


def ask_json(client, prompt, max_tokens, label, schema=None, model=MODEL):
    """Send one prompt, return Claude's answer as a parsed dict.

    API errors (overloaded, auth, network) propagate unchanged so callers
    keep their existing retry logic. Raises StructuredReplyError when the
    reply has no usable structured result.
    """
    _check_budget(prompt, max_tokens, model, label)
    tool = {
        "name":         TOOL_NAME,
        "description":  "Submit the JSON answer requested in the user's message.",
        "input_schema": schema or {"type": "object"},
    }
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": prompt + SUBMIT_NOTE}],
    )
    _log_usage(response, model, label)
    _audit(prompt, response, model, label)
    block  = next((b for b in response.content if b.type == "tool_use"), None)
    result = block.input if block else None
    if response.stop_reason == "max_tokens" or not isinstance(result, dict) or not result:
        path = _dump(label, response)
        where = f" — full response saved to {path}" if path else ""
        raise StructuredReplyError(f"no usable structured reply (stop_reason={response.stop_reason}){where}")
    return result


def keyed_schema(value_properties, required):
    """Schema for {"KEY": {...}, ...} replies keyed by ticker / grade key."""
    return {
        "type": "object",
        "additionalProperties": {
            "type":       "object",
            "properties": value_properties,
            "required":   required,
        },
    }
