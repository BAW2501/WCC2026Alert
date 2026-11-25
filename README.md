# Chess alert

Fails the GitHub Action (so GitHub emails / pushes you) when a live game in the FIDE World
Championship broadcast on Lichess has |eval| >= EVAL_THRESHOLD **or** |material| >= MATERIAL_THRESHOLD.
No SMTP, no passwords, no secrets.

## Setup
1. Push this folder to a new **public** GitHub repo.
2. Enable notifications: github.com/settings/notifications -> *System* -> **Actions**:
   tick Email (and/or GitHub Mobile), and choose **"Send notifications for failed workflows only"**.
3. Scheduled-run failure emails go to whoever last edited the `cron:` lines in the workflow file,
   so make that commit with the account you want notified.
4. Optional repo Variables: `EVAL_THRESHOLD` (default 1.5), `MATERIAL_THRESHOLD` (default 2).
5. Actions tab -> chess-alert -> Run workflow, to test during a game. Set EVAL_THRESHOLD=0.01 to force a failure.

## Local test (never fails, never saves state)
    uv run --with chess scripts/chess_alert.py --dry-run
    uv run --with chess scripts/chess_alert.py --pgn-file test.pgn --dry-run

## Notes
- One alert per event: it fires when a threshold is crossed or the advantage flips sides, and re-arms after the eval falls 0.5 below it.
- Network errors only produce a warning, not a failure, so you aren't alerted for outages.
- If cloud-eval signs look flipped in the logs, set env `CLOUD_EVAL_POV=stm`.
- `ROUND_ID` env forces a specific Lichess round id.
