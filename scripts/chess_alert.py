#!/usr/bin/env python3
"""
Fail the GitHub Action (-> GitHub emails you) when a live Lichess broadcast game gets "interesting".

Interesting = |engine eval| >= EVAL_THRESHOLD (pawns)  OR  |material diff| >= MATERIAL_THRESHOLD.

Data source: Lichess broadcast REST API (no websocket needed).
Eval:        Lichess cloud-eval REST API (same data the study page fetches over its websocket
             via evalGetMulti/evalHitMulti). Falls back to a PGN [%eval] annotation, then material only.
State:       small JSON file so you get ONE alert per event, not one every 5 minutes.
Alert:       exit code 1 + ::error annotation. GitHub's own failed-workflow email/push is the notification.
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import chess
import chess.pgn

API = "https://lichess.org/api"
UA = "chess-alert-github-action (personal use)"

# --- config (all overridable via env / repo variables) -----------------------
SEED_URL_PATH = os.getenv(
    "SEED_ROUND_PATH", "fide-world-chess-championship-2026/game-2/L3j03TZq"
)
ROUND_ID_OVERRIDE = os.getenv("ROUND_ID", "").strip()
EVAL_THRESHOLD = float(os.getenv("EVAL_THRESHOLD", "1.5"))       # pawns
MATERIAL_THRESHOLD = float(os.getenv("MATERIAL_THRESHOLD", "2"))  # points
HYSTERESIS = float(os.getenv("HYSTERESIS", "0.5"))  # re-arm margin, pawns
CLOUD_EVAL_POV = os.getenv("CLOUD_EVAL_POV", "white")  # "white" or "stm" (side to move) if cp signs look flipped
STATE_FILE = Path(os.getenv("STATE_FILE", "state/state.json"))

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


# --- Lichess helpers ----------------------------------------------------------
def http_get(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def find_live_round() -> str | None:
    """Return the id of the round being played right now (or None)."""
    if ROUND_ID_OVERRIDE:
        return ROUND_ID_OVERRIDE

    seed_id = SEED_URL_PATH.rstrip("/").split("/")[-1]
    try:
        seed = json.loads(http_get(f"{API}/broadcast/{SEED_URL_PATH}"))
        tour_id = seed["tour"]["id"]
        tour = json.loads(http_get(f"{API}/broadcast/{tour_id}"))
        rounds = tour.get("rounds", [])
    except Exception as e:  # fall back to the seed round only
        print(f"[warn] could not list rounds ({e}); falling back to seed round", file=sys.stderr)
        return seed_id

    now_ms = time.time() * 1000
    for r in rounds:
        if r.get("ongoing"):
            return r["id"]
    # Not flagged ongoing yet: accept a round that started within the last 9h or starts in 10 min.
    for r in rounds:
        s = r.get("startsAt")
        if s and not r.get("finished") and -10 * 60e3 <= now_ms - s <= 9 * 3600e3:
            return r["id"]
    return None


def fetch_games(round_id: str):
    pgn_text = http_get(f"{API}/broadcast/round/{round_id}.pgn")
    return read_games(pgn_text)


def read_games(pgn_text: str):
    stream = io.StringIO(pgn_text)
    while (g := chess.pgn.read_game(stream)) is not None:
        yield g


# --- chess logic ---------------------------------------------------------------
def material_diff(board: chess.Board) -> int:
    """White minus Black, in standard points (P1 N3 B3 R5 Q9)."""
    d = 0
    for pt, v in VALUES.items():
        d += v * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
    return d


def settled_material(board: chess.Board, pv: list[chess.Move]) -> int:
    """
    Material after any capture sequence in progress has played out, so we don't
    fire on a mid-exchange snapshot (e.g. right after Nxe5, before ...Nxe5).
    Follows the engine PV only while it keeps capturing.
    """
    b = board.copy()
    for mv in pv[:6]:
        if not b.is_capture(mv):
            break
        b.push(mv)
    return material_diff(b)


def cloud_eval(board: chess.Board):
    """
    Returns (pawns_from_white_POV, pv_moves, label) or None if Lichess has no cached eval.
    """
    url = f"{API}/cloud-eval?fen={urllib.parse.quote(board.fen())}&multiPv=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[warn] cloud-eval HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[warn] cloud-eval failed: {e}", file=sys.stderr)
        return None

    pv = (data.get("pvs") or [{}])[0]
    if "mate" in pv:
        pawns = 100.0 if pv["mate"] > 0 else -100.0
        label = f"mate {pv['mate']}"
    elif "cp" in pv:
        pawns = pv["cp"] / 100
        label = f"{pv['cp']}cp"
    else:
        return None
    if CLOUD_EVAL_POV == "stm" and board.turn == chess.BLACK:
        pawns = -pawns
    label += f" @ depth {data.get('depth', '?')}"

    moves = []
    b = board.copy()
    for uci in pv.get("moves", "").split():
        try:
            mv = chess.Move.from_uci(uci)
            if mv not in b.legal_moves:
                break
            moves.append(mv)
            b.push(mv)
        except ValueError:
            break
    return max(-100.0, min(100.0, pawns)), moves, label


def pgn_eval(node):
    """[%eval ...] annotation on the last move, if the broadcast provides one."""
    try:
        e = node.eval()
        if e is None:
            return None
        cp = e.white().score(mate_score=10000)
        return max(-100.0, min(100.0, cp / 100)), [], "PGN annotation"
    except Exception:
        return None


def evaluate(node):
    return cloud_eval(node.board()) or pgn_eval(node)


# --- state / alerting -----------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


def update_trigger(prev_sign: int, value: float, threshold: float, margin: float):
    """
    Returns (should_fire, new_sign).
    Fires when the value crosses the threshold, or flips sides.
    Re-arms only after it drops back below (threshold - margin).
    """
    if abs(value) >= threshold:
        s = sign(value)
        return (s != prev_sign), s
    if abs(value) < threshold - margin:
        return False, 0
    return False, prev_sign  # inside the hysteresis band: keep previous state


# --- main -----------------------------------------------------------------------
def process(games, state, round_id):
    """Returns (state_changed, list_of_alert_messages)."""
    changed = False
    alerts = []
    for idx, game in enumerate(games):
        h = game.headers
        if h.get("Result", "*") != "*":
            continue  # finished
        node = game.end()
        board = node.board()
        if board.move_stack == [] and node.parent is None:
            continue  # no moves yet
        white, black = h.get("White", "?"), h.get("Black", "?")

        res = evaluate(node)
        ev, pv, score = res if res else (None, [], "no eval available")

        # Material. With no engine line we can't tell if a capture sequence is mid-flight,
        # so if the last move was a capture, wait for the next poll rather than risk a false alarm.
        last_was_capture = node.parent is not None and node.parent.board().is_capture(node.move)
        mat = settled_material(board, pv)
        mat_reliable = bool(pv) or not last_was_capture

        key = f"{round_id}:{idx}"
        st = state.get(key, {"eval": 0, "mat": 0})
        fire_e = fire_m = False
        if ev is not None:
            fire_e, st["eval"] = update_trigger(st["eval"], ev, EVAL_THRESHOLD, HYSTERESIS)
        if mat_reliable:
            fire_m, st["mat"] = update_trigger(st["mat"], mat, MATERIAL_THRESHOLD, 0.0)
        state[key] = st
        changed = True

        ev_txt = "n/a" if ev is None else (score.split(" @")[0] if abs(ev) >= 100 else f"{ev:+.2f}")
        print(f"[{key}] {white} v {black} | move {board.fullmove_number} | "
              f"eval {ev_txt} ({score}) | material {mat:+d}{'' if mat_reliable else ' (deferred)'} | "
              f"fire eval={fire_e} mat={fire_m}")

        if not (fire_e or fire_m):
            continue

        reasons = []
        if fire_e:
            reasons.append(f"eval {ev_txt}")
        if fire_m:
            reasons.append(f"material {mat:+d}")
        subject = f"♟ {white} v {black}: " + ", ".join(reasons)

        try:
            best_line = board.variation_san(pv[:6]) if pv else "n/a"
        except Exception:
            best_line = "n/a"
        recent = []
        tmp = node
        for _ in range(6):
            if tmp.parent is None:
                break
            recent.append(tmp.san())
            tmp = tmp.parent
        body = (
            f"{white} vs {black}  (move {board.fullmove_number}, "
            f"{'White' if board.turn else 'Black'} to move)\n\n"
            f"Lichess cloud eval (White POV): {ev_txt}  [{score}]\n"
            f"Material (White - Black, settled): {mat:+d}\n"
            f"Best line (cloud PV): {best_line}\n"
            f"Last plies: {' '.join(reversed(recent))}\n\n"
            f"FEN: {board.fen()}\n"
            f"Watch: https://lichess.org/broadcast/-/-/{round_id}\n"
            f"Board: https://lichess.org/analysis/{board.fen().replace(' ', '_')}\n"
        )
        alerts.append((subject, body))
    return changed, alerts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="never fail the run or save state")
    ap.add_argument("--pgn-file", help="analyse a local PGN instead of calling Lichess (testing)")
    args = ap.parse_args()

    state = load_state()

    if args.pgn_file:
        round_id, games = "local", list(read_games(Path(args.pgn_file).read_text()))
    else:
        round_id = find_live_round()
        if not round_id:
            print("No live round right now; nothing to do.")
            return 0
        games = list(fetch_games(round_id))

    if not any(g.headers.get("Result", "*") == "*" for g in games):
        print("No unfinished games in this round; nothing to do.")
        return 0

    changed, alerts = process(games, state, round_id)
    if changed and not args.dry_run:
        save_state(state)

    for subject, body in alerts:
        print(f"\n==== ALERT ====\n{subject}\n{body}")
        print(f"::error title=Interesting chess position::{subject}")
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"### {subject}\n```\n{body}\n```\n")
    # Non-zero exit => GitHub sends its "workflow failed" notification. Dry runs never fail.
    return 1 if (alerts and not args.dry_run) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:  # network hiccups must not look like an alert
        print(f"::warning::{e}")
        sys.exit(0)
