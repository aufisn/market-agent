# ============================================================
# MARKET AGENT V4.5 — Adversarial Self-Critique
# adversarial_critique.py
#
# For every pick that passed ALL 7 quality gates (analysis["agent_picks"]),
# a SEPARATE Claude API call — fresh context, no prior turns, never the
# same call that generated the bull case — is instructed to argue the
# STRONGEST bear case against it. This is deliberate: a single call doing
# bull-then-bear reasoning risks anchoring on (and softening) the bear
# case it just argued against a moment earlier. A genuinely separate call
# has no such pull.
#
# This does NOT gate or filter agent_picks — a strong bear case does not
# remove a pick. It attaches bear_case metadata so Salim sees both sides
# before deciding, per his stated philosophy of flagging for judgment
# rather than auto-rejecting:
#   WEAK bear case     — thesis corroborated, no action needed
#   MODERATE bear case — worth noting, not disqualifying
#   STRONG bear case   — flagged for review — presented alongside the
#                        pick, not silently swapped in for a clean BUY
#
# All qualified picks this run are batched into ONE call (separate from
# the original bull-case call, but not one-call-per-ticker) for cost
# efficiency — matches the batching pattern already used by
# thesis_tracker.py and outcome_tracker.py.
#
# Reads:  analysis["agent_picks"]  — picks that passed all 7 gates
#
# Writes: analysis["agent_picks"][i]["bear_case"] — per-pick metadata:
#         {"strength": "WEAK|MODERATE|STRONG", "argument": "...",
#          "flagged_for_review": bool}
# ============================================================

import os
import time
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError
from common import GREEN, RED, YELLOW, RESET, safe_float

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


VALID_STRENGTHS = {"WEAK", "MODERATE", "STRONG"}


def _build_prompt(picks):
    blocks = []
    for p in picks:
        ticker = str(p.get("ticker", "")).upper()
        blocks.append(f"""
TICKER: {ticker}
Stated bull case: {p.get("reason", "")}
Signal: {p.get("signal","")}  |  Confidence: {p.get("confidence","")}  |  Risk: {p.get("risk_level","")}
Strategy score: {p.get("strategy_score","")}  |  Sector: {p.get("sector","")}
Current price: ${safe_float(p.get("price") or p.get("current_price")):.2f}  |  Suggested entry: {p.get("suggested_entry","")}
Upside potential: {p.get("upside_potential","")}
""")

    prompt = f"""You are playing devil's advocate against your own prior investment recommendation. You are NOT the analyst who wrote the bull case below — you are an independent, skeptical reviewer whose only job is to find the strongest possible reasons this pick could be WRONG.

Do not soften the critique to be polite or balanced. Argue the bear case as forcefully and specifically as the evidence allows — cite concrete risks (valuation, competitive threats, macro exposure, execution risk, what the bull case is ignoring or underweighting), not generic caveats like "markets can be volatile."

For each ticker below, construct the strongest bear case you can, then rate how strong that bear case actually is against the stated bull case:
- WEAK: the bear case is thin — mostly generic risk, nothing that seriously threatens the bull thesis
- MODERATE: the bear case raises a real, specific concern worth noting, but doesn't override the bull case
- STRONG: the bear case identifies a genuine, specific threat that a reasonable investor should weigh seriously before acting on this pick

{"".join(blocks)}
Return JSON with one entry per ticker listed above:
{{"critiques": [{{"ticker": "TICKER", "strength": "WEAK|MODERATE|STRONG", "argument": "2-4 sentences, specific and concrete, the strongest case you can honestly make"}}]}}"""
    return prompt


CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker":   {"type": "string"},
                    "strength": {"type": "string", "enum": sorted(VALID_STRENGTHS)},
                    "argument": {"type": "string"},
                },
                "required": ["ticker", "strength", "argument"],
            },
        },
    },
    "required": ["critiques"],
}


def critique_picks(picks):
    """Single separate API call — batched across all qualified picks this run."""
    prompt = _build_prompt(picks)

    for attempt in range(2):
        try:
            result = ask_json(client, prompt, max_tokens=4000,
                              label="adversarial_critique", schema=CRITIQUE_SCHEMA)
            return {
                str(c.get("ticker", "")).upper(): {"strength": c.get("strength"), "argument": c.get("argument", "")}
                for c in result.get("critiques", []) if isinstance(c, dict)
            }
        except StructuredReplyError as err:
            print(f"{RED}✗ Adversarial critique reply error: {err}{RESET}")
            return {}
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 1:
                print(f"{YELLOW}⚠ API overloaded — retrying adversarial critique in 20s...{RESET}")
                time.sleep(20)
                continue
            print(f"{RED}✗ Adversarial critique API error: {e}{RESET}")
            return {}
    return {}


# ── MAIN ENTRY POINT ──────────────────────────────────────────
def run(analysis, data):
    """
    Called from agent_brain.py's run() after rank_picks(), before the
    reallocation engine. Attaches bear_case metadata to every pick that
    passed all 7 gates. Does not filter or reorder agent_picks.
    """
    picks = analysis.get("agent_picks", [])
    if not picks:
        print(f"{YELLOW}⚠ No qualified picks to critique{RESET}")
        return analysis

    results = critique_picks(picks)
    if not results:
        return analysis

    flagged = []
    for p in picks:
        ticker = str(p.get("ticker", "")).upper()
        r = results.get(ticker)
        if not r or r.get("strength") not in VALID_STRENGTHS:
            continue
        strength = r.get("strength")
        p["bear_case"] = {
            "strength":           strength,
            "argument":           r.get("argument", ""),
            "flagged_for_review": strength == "STRONG",
        }
        icon = {"WEAK": GREEN, "MODERATE": YELLOW, "STRONG": RED}.get(strength, "")
        print(f"  {icon}{ticker} bear case: {strength}{RESET}")
        if strength == "STRONG":
            flagged.append(ticker)

    analysis["agent_picks"] = picks
    print(f"{GREEN}✓ Adversarial critique complete — "
          f"{len(flagged)} pick(s) flagged for review{RESET}")
    if flagged:
        print(f"{YELLOW}  ⚠ Strong bear case despite passing all gates: {', '.join(flagged)}{RESET}")

    return analysis
