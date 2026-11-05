# Market Agent V4.5

A personal US-equity assistant: a dashboard (`Market_Agent.html`) over a daily Python pipeline that prices the portfolio, screens a 300-stock universe, asks Claude for picks and reviews, enforces the strategy's rules in code, and sends one Telegram digest.

Code only — no data, no secrets. `watchlist.csv`, `.env`, `.trigger_token`, `logs/` and `data/` live in the install folder `~/market-agent`.

## Run it (laptop, manual runs)
See **[local/README.md](local/README.md)**: `bash ~/market-agent/local/start.sh`, then use the dashboard's Run buttons. Nothing runs on a schedule.

## Pipeline (`run_agent.sh`)
fetch_data → drawdown_monitor → stop_tracker → regime_detector → earnings_brief → market_context → agent_brain (thesis, outcome, delta, bear-case, correlation and reallocation steps) → portfolio_analysis → record_snapshot → send_alerts → simulator

Pre-market: `premarket_run.sh` → premarket_scanner → premarket_brief.

## Rules and settings
Every threshold lives in `strategy.json` (gates, sizing, risk per trade, stops, regime exposure, grading, Claude budget). Stops: −15% hard exit; the trailing stop sits the wider of 15% and 3× ATR below the high (simulator ledgers from +20%, real holdings once the target is confirmed). The evidence behind the rules is in `review/rules_review/Picking_Rules_Review.pdf`.

## Notes
- `trade_history.json` and `simulator_state.json` are write-protected via `READ_ONLY_FILES` in `trigger.py`.
- Any script reading `ANTHROPIC_API_KEY` must call `load_dotenv()`.
- `review/` holds the 2026-11 review and the sandbox / walk-forward tools; it is never copied into the install.
