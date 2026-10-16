#!/usr/bin/env python3
"""Capture Talishar game states and the moves played against them.

Polls GetNextTurn.php for one seat and writes every distinct state it sees to
captures/<gameName>/NNNN.json. In parallel it tails the web container's Apache
access log and records each ProcessInput.php request, pairing it with the state
that was current when the move was sent.

Usage:
    python3 capture.py <gameName> <playerID> <authKey> [options]

Options:
    --base-url URL      backend root (default http://localhost:8080/game)
    --container NAME    docker container running Apache (default talishar-web-server-1)
    --interval SECONDS  poll interval (default 0.25)
    --out-dir PATH      capture root (default ./captures)
    --no-log            skip access-log tailing (states only)

Stops on turnPhase "OVER", on a backend errorMessage, or on Ctrl+C.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATE_ENDPOINT = "GetNextTurn.php"
MOVE_PATH_MARKER = "ProcessInput"
# Apache combined log: host - - [10/Oct/2026:23:17:50 +0000] "GET /path?qs HTTP/1.1" 200 ...
LOG_LINE = re.compile(r'\[(?P<ts>[^\]]+)\]\s+"(?P<method>[A-Z]+)\s+(?P<target>\S+)\s+[^"]*"\s+(?P<status>\d{3})')
APACHE_TS = "%d/%b/%Y:%H:%M:%S %z"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Capture:
    def __init__(self, args):
        self.args = args
        self.game_dir = os.path.join(args.out_dir, args.game_name)
        os.makedirs(self.game_dir, exist_ok=True)
        self.state_count = 0
        self.last_update = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()
        # Index of the most recent state file written, and the states still
        # waiting to be named as the result of a move.
        self.latest_state = None
        self.move_count = 0
        self.pending_moves = []
        self.moves_path = os.path.join(self.game_dir, "moves.jsonl")
        self.states_log_path = os.path.join(self.game_dir, "states.jsonl")
        self.transitions_path = os.path.join(self.game_dir, "transitions.jsonl")

    # ---------------------------------------------------------------- states
    def state_url(self):
        params = {
            "gameName": self.args.game_name,
            "playerID": self.args.player_id,
            "authKey": self.args.auth_key,
            "lastUpdate": self.last_update,
        }
        return self.args.base_url.rstrip("/") + "/" + STATE_ENDPOINT + "?" + urllib.parse.urlencode(params)

    def fetch_state(self):
        request = urllib.request.Request(self.state_url(), headers={"User-Agent": "talishar-capture/1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", "replace")

    def write_state(self, state):
        with self.lock:
            index = self.state_count
            self.state_count += 1
            path = os.path.join(self.game_dir, f"{index:04d}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)

            turn_phase = (state.get("turnPhase") or {})
            summary = {
                "index": index,
                "captured_at": now_iso(),
                "lastUpdate": state.get("lastUpdate"),
                "turnNo": state.get("turnNo"),
                "turnPhase": turn_phase.get("turnPhase"),
                "layer": turn_phase.get("layer"),
                "havePriority": state.get("havePriority"),
                "amIActivePlayer": state.get("amIActivePlayer"),
                "playerHealth": (state.get("playerHealth") or {}).get("health")
                if isinstance(state.get("playerHealth"), dict) else state.get("playerHealth"),
            }
            with open(self.states_log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(summary, ensure_ascii=False) + "\n")

            self.latest_state = index
            # Every move logged since the previous state produced this one.
            for move in self.pending_moves:
                self.append_json(self.transitions_path, {
                    "move_id": move["move_id"],
                    "state_before": move["state_before"],
                    "state_after": index,
                    "params": move["params"],
                })
            self.pending_moves = []
            return index, summary

    def poll_states(self):
        backoff = self.args.interval
        while not self.stop.is_set():
            try:
                body = self.fetch_state()
                backoff = self.args.interval
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                print(f"[state] fetch failed: {error}", file=sys.stderr)
                self.stop.wait(min(backoff * 2, 5))
                backoff = min(backoff * 2, 5)
                continue

            stripped = body.strip()
            if stripped in ("", "0"):
                self.stop.wait(self.args.interval)
                continue

            try:
                state = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"[state] non-JSON response: {stripped[:200]}", file=sys.stderr)
                self.stop.wait(self.args.interval)
                continue

            if isinstance(state, dict) and state.get("errorMessage"):
                print(f"[state] backend error: {state['errorMessage']}", file=sys.stderr)
                self.stop.set()
                return

            new_last_update = state.get("lastUpdate")
            if isinstance(new_last_update, int):
                self.last_update = new_last_update

            index, summary = self.write_state(state)
            print(f"[state] {index:04d} turn={summary['turnNo']} phase={summary['turnPhase']}"
                  f" priority={summary['havePriority']} lastUpdate={summary['lastUpdate']}")

            if summary["turnPhase"] == "OVER":
                print("[state] turnPhase OVER - game finished.")
                self.stop.set()
                return

            self.stop.wait(self.args.interval)

    # ----------------------------------------------------------------- moves
    @staticmethod
    def append_json(path, record):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def record_move(self, target, status, log_ts, method="GET"):
        parsed = urllib.parse.urlparse(target)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        # ProcessInputAPI.php takes a JSON body (OPT reordering, layer reordering,
        # mode 106 ...). The access log carries no body, so the parameters of
        # those inputs cannot be recovered this way - record the gap explicitly.
        body_only = not params
        if not body_only and params.get("gameName") != str(self.args.game_name):
            return
        # authKey identifies the seat; keep only its presence, not the secret.
        auth = params.pop("authKey", None)
        with self.lock:
            move = {
                "move_id": self.move_count,
                "endpoint": endpoint,
                "method": method,
                "params_available": not body_only,
                "logged_at": log_ts,
                "recorded_at": now_iso(),
                "status": status,
                "params": params,
                "seat_matches_capture": auth == self.args.auth_key if auth else None,
                "state_before": self.latest_state,
            }
            self.move_count += 1
            # Persisted immediately so nothing is lost if the run is killed; the
            # resulting state is linked in transitions.jsonl once it arrives.
            self.append_json(self.moves_path, move)
            self.pending_moves.append(move)
        if body_only:
            print(f"[move]  id={move['move_id']} {method} {endpoint} (JSON body - parameters not in access log)"
                  f" state_before={move['state_before']}")
        else:
            print(f"[move]  id={move['move_id']} mode={params.get('mode')} cardID={params.get('cardID')}"
                  f" buttonInput={params.get('buttonInput')} state_before={move['state_before']}")

    def tail_access_log(self):
        if not shutil.which("docker"):
            print("[log] docker not on PATH - move capture disabled.", file=sys.stderr)
            return
        command = ["docker", "logs", "-f", "--since", "0s", self.args.container]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as error:
            print(f"[log] could not tail container: {error}", file=sys.stderr)
            return

        try:
            for line in process.stdout:
                if self.stop.is_set():
                    break
                if MOVE_PATH_MARKER not in line:
                    continue
                match = LOG_LINE.search(line)
                if not match:
                    continue
                try:
                    log_ts = datetime.strptime(match.group("ts"), APACHE_TS).isoformat()
                except ValueError:
                    log_ts = match.group("ts")
                self.record_move(match.group("target"), int(match.group("status")), log_ts,
                                 method=match.group("method"))
        finally:
            process.terminate()

    # ------------------------------------------------------------------ main
    def run(self):
        meta = {
            "started_at": now_iso(),
            "gameName": self.args.game_name,
            "playerID": self.args.player_id,
            "base_url": self.args.base_url,
            "container": self.args.container,
            "interval": self.args.interval,
        }
        with open(os.path.join(self.game_dir, "session.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)

        threads = []
        if not self.args.no_log:
            log_thread = threading.Thread(target=self.tail_access_log, daemon=True)
            log_thread.start()
            threads.append(log_thread)

        # SIGTERM should shut down as cleanly as Ctrl+C.
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())

        print(f"Capturing game {self.args.game_name} seat {self.args.player_id} into {self.game_dir}")
        print("Ctrl+C to stop.")
        try:
            self.poll_states()
        except KeyboardInterrupt:
            print("\n[main] interrupted.")
            self.stop.set()

        with self.lock:
            for move in self.pending_moves:
                self.append_json(self.transitions_path, {
                    "move_id": move["move_id"],
                    "state_before": move["state_before"],
                    "state_after": None,
                    "params": move["params"],
                })
            self.pending_moves = []

        print(f"[main] {self.state_count} states written to {self.game_dir}")
        if os.path.exists(self.moves_path):
            with open(self.moves_path, encoding="utf-8") as handle:
                print(f"[main] {sum(1 for _ in handle)} moves written to {self.moves_path}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Capture Talishar game states and moves.")
    parser.add_argument("game_name", help="game name / id, e.g. 2")
    parser.add_argument("player_id", type=int, choices=[1, 2, 3], help="seat to observe (3 = spectator)")
    parser.add_argument("auth_key", help="authKey for that seat")
    parser.add_argument("--base-url", default="http://localhost:8080/game")
    parser.add_argument("--container", default="talishar-web-server-1")
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures"))
    parser.add_argument("--no-log", action="store_true", help="do not tail the container access log")
    return parser.parse_args(argv)


if __name__ == "__main__":
    Capture(parse_args(sys.argv[1:])).run()
