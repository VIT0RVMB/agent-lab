#!/usr/bin/env python3
"""Plays a Talishar game by picking legal actions at random.

The point is to exercise the plumbing - read state, enumerate actions, send one,
notice when it was rejected - not to play well.

Usage:
    python3 random_agent.py <gameName> <playerID> <authKey> [options]

Options:
    --base-url URL        backend root (default http://localhost:8080/game)
    --interval SECONDS    poll interval (default 0.3)
    --stall-timeout SEC   stop if nothing changes while we hold priority (default 30)
    --confirm-timeout SEC how long to wait for lastUpdate to advance (default 4)
    --max-failures N      consecutive rejected inputs in one state before stopping (default 5)
    --on-unsupported X    stop (default) or pass, for prompts that need ProcessInputAPI.php
    --allow-cancel        last resort only: send the prompt's Cancel/Undo button when it
                          is the only thing left (off by default - mode 10000 is forbidden)
    --policy X            affordable (default) or random; "affordable" skips plays whose
                          cost the seat cannot pay, which is what keeps the agent out of
                          the terminal pitch prompt
    --think-delay SEC     wait this long before each decision that goes through the policy,
                          simulating an external model call. Automatic passes stay instant.
    --seed N              seed the random policy
    --runs-dir PATH       where to write decisions.jsonl (default ./runs)
    --quiet               only print the outcome

Exit code 0 means the game reached turnPhase OVER. Any other stop is a non-zero
exit with a reason, and the offending state is written to the run directory.
"""

import argparse
import json
import os
import random
import sys
import time

from card_info import CardInfo
from legal_actions import (
    PASS_MODE,
    describe,
    is_pass_only,
    legal_actions,
    turn_phase,
    unsupported_prompt,
)
import policies
from talishar_client import LOBBY, UNCHANGED, TalisharClient, TalisharError


# ------------------------------------------------------------------------ runner
class StopReason:
    OVER = "game_over"
    TURN_LIMIT = "turn_limit"
    REJECTED = "too_many_rejected_inputs"
    STALLED = "no_change_while_holding_priority"
    UNSUPPORTED = "unsupported_prompt"
    NO_ACTIONS = "no_legal_actions_while_holding_priority"
    BACKEND_ERROR = "backend_error"
    INTERRUPTED = "interrupted"


class Agent:
    def __init__(self, args, policy=None):
        self.args = args
        self.policy = policy or self.build_policy(args)
        self.rng = random.Random(args.seed)
        self.client = TalisharClient(args.game_name, args.player_id, args.auth_key,
                                     base_url=args.base_url)
        self.run_dir = os.path.join(args.runs_dir, str(args.game_name))
        os.makedirs(self.run_dir, exist_ok=True)
        self.decisions_path = os.path.join(self.run_dir, "decisions.jsonl")
        self.decision_count = 0
        self.rejected_by_phase = {}
        self.degraded_prompts = {}
        self.cancels = 0
        self.stop_reason = None
        self.stop_detail = ""
        self.deadline_observations = []
        # Life totals as first seen, so damage can be measured without assuming
        # a starting life (40 in CC, 20 in blitz/sage).
        self.starting_player_health = None
        self.starting_opponent_health = None

    @staticmethod
    def build_policy(args):
        card_info = None
        if policies.needs_card_info(args.policy):
            # Costs and pitch values come from GetLobbyInfo, which answers for a
            # running game.
            card_info = CardInfo.fetch(args.game_name, args.player_id,
                                       args.auth_key, base_url=args.base_url)
        return policies.build_policy(args.policy, card_info, args)

    # ------------------------------------------------------------------ logging
    @staticmethod
    def as_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def log(self, message):
        if not self.args.quiet:
            print(message, flush=True)

    def log_decision(self, record):
        with open(self.decisions_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def dump_state(self, state, tag):
        path = os.path.join(self.run_dir, f"stopped_{tag}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
        return path

    def note_rejection(self, phase):
        self.rejected_by_phase[phase] = self.rejected_by_phase.get(phase, 0) + 1

    def record_deadline(self, state, moment):
        """Snapshots the inactivity fields so a slow run can be audited."""
        deadline = state.get("inactivityDeadline")
        server_time = state.get("serverTime")
        self.deadline_observations.append({
            "moment": moment,
            "lastUpdate": state.get("lastUpdate"),
            "turnNo": state.get("turnNo"),
            "phase": turn_phase(state),
            "inactive": state.get("inactive"),
            "inactivityDeadline": deadline,
            "serverTime": server_time,
            "msToDeadline": (deadline - server_time) if (deadline and server_time) else None,
            "gameDeleteDeadline": state.get("gameDeleteDeadline"),
        })

    @staticmethod
    def cancel_button(state):
        """The prompt's Cancel/Undo button, if the state offers one."""
        for button in (state.get("playerPrompt") or {}).get("buttons") or []:
            if button.get("mode") in (10000, 10001):
                return {"mode": int(button["mode"]), "cardID": None, "buttonInput": None,
                        "card": button.get("caption"), "source": "cancel-last-resort"}
        return None

    # -------------------------------------------------------------------- state
    def wait_for_state(self, last_update, deadline):
        """Polls until a state arrives or the deadline passes.

        Returns (state, last_update) or (None, last_update) on timeout. The
        lobby and "nothing changed" replies are both normal waiting.
        """
        while time.monotonic() < deadline:
            result = self.client.get_state(last_update)
            if result is LOBBY or result is UNCHANGED:
                time.sleep(self.args.interval)
                continue
            if self.starting_opponent_health is None:
                # Life as first seen, so damage can be measured without assuming
                # a starting total (40 in CC, 20 in blitz and sage).
                self.starting_player_health = result.get("playerHealth")
                self.starting_opponent_health = result.get("opponentHealth")
            return result, result.get("lastUpdate", last_update)
        return None, last_update

    # --------------------------------------------------------------------- play
    def run(self):
        started = time.monotonic()
        state, last_update = self.wait_for_state(0, time.monotonic() + self.args.stall_timeout)
        if state is None:
            self.stop_reason = StopReason.STALLED
            self.stop_detail = "no state before the first timeout (is the game past the lobby?)"
            return self.finish(started, None)

        # Actions already rejected in the current state, so we do not retry them.
        bad_actions = set()
        bad_for_update = last_update
        consecutive_failures = 0
        last_change = time.monotonic()

        while True:
            phase = turn_phase(state)

            if phase == "OVER":
                self.stop_reason = StopReason.OVER
                return self.finish(started, state)

            # A mirror match can deck out into a stalemate: both heroes alive,
            # nothing playable, and passing is legal forever, so the turn counter
            # climbs without the game ever ending. Cap it rather than spin.
            turn_no = self.as_int(state.get("turnNo"))
            if self.args.max_turns and turn_no > self.args.max_turns:
                self.stop_reason = StopReason.TURN_LIMIT
                self.stop_detail = (f"turn {turn_no} exceeds --max-turns {self.args.max_turns}; "
                                    f"life {state.get('playerHealth')} vs "
                                    f"{state.get('opponentHealth')}")
                return self.finish(started, state)
            if self.args.max_decisions and self.decision_count >= self.args.max_decisions:
                self.stop_reason = StopReason.TURN_LIMIT
                self.stop_detail = (f"{self.decision_count} decisions exceeds --max-decisions "
                                    f"{self.args.max_decisions} at turn {turn_no}")
                return self.finish(started, state)

            if not state.get("havePriority"):
                # The opponent (or the bot) owes an input. Wait, but do not let
                # a silent backend hang us forever.
                new_state, last_update = self.wait_for_state(
                    last_update, time.monotonic() + self.args.stall_timeout)
                if new_state is None:
                    self.stop_reason = StopReason.STALLED
                    self.stop_detail = (f"no change for {self.args.stall_timeout}s while waiting "
                                        f"for the opponent in phase {phase}")
                    return self.finish(started, state)
                state, last_change = new_state, time.monotonic()
                continue

            reason = unsupported_prompt(state)
            if reason and (self.args.on_unsupported == "stop" or not state.get("canPassPhase")):
                self.stop_reason = StopReason.UNSUPPORTED
                self.stop_detail = reason
                return self.finish(started, state)

            if last_update != bad_for_update:
                bad_actions = set()
                bad_for_update = last_update

            actions = legal_actions(state)
            if not actions:
                # A random policy can reach adead end: playing a card whose cost
                # it cannot pay leaves a pitch prompt with an empty hand, no pass,
                # and only the Cancel button (mode 10000), which rule 1 forbids.
                cancel = self.cancel_button(state) if self.args.allow_cancel else None
                if cancel is None:
                    self.stop_reason = StopReason.NO_ACTIONS
                    self.stop_detail = f"phase {phase}, canPassPhase={state.get('canPassPhase')}"
                    return self.finish(started, state)
                actions = [cancel]
                self.cancels += 1

            # Rule 2: a lone pass is not a decision - send it without the policy.
            # A prompt we cannot express is the same situation when --on-unsupported
            # is "pass": the state itself offers the pass as "accept the default
            # order", so passing is a documented action and not an invented payload.
            # It still loses the choice, so it is counted separately.
            if reason:
                chosen = {"mode": PASS_MODE, "cardID": None, "buttonInput": None,
                          "card": "PASS", "source": "unsupported-prompt-fallback",
                          "phase": phase}
                via_policy = False
                self.degraded_prompts[reason] = self.degraded_prompts.get(reason, 0) + 1
            elif is_pass_only(actions):
                chosen = actions[0]
                via_policy = False
            else:
                candidates = [a for a in actions
                              if (a["mode"], a.get("cardID"), a.get("buttonInput")) not in bad_actions]
                if not candidates:
                    self.stop_reason = StopReason.REJECTED
                    self.stop_detail = (f"every legal action in phase {phase} was rejected "
                                        f"({len(actions)} tried)")
                    return self.finish(started, state)
                chosen = self.policy(state, candidates, self.rng)
                via_policy = True

            # Simulated model latency. Only decisions that actually went through
            # the policy pay it, matching how a real agent would behave: an
            # automatic pass needs no model call.
            if via_policy and self.args.think_delay > 0:
                self.record_deadline(state, "before-think")
                time.sleep(self.args.think_delay)
                self.record_deadline(state, "after-think")

            sent_at = time.monotonic()
            try:
                self.client.send_input(chosen["mode"], chosen.get("cardID"), chosen.get("buttonInput"))
            except TalisharError as error:
                self.stop_reason = StopReason.BACKEND_ERROR
                self.stop_detail = str(error)
                return self.finish(started, state)

            # Rule 3: confirm by lastUpdate, not by HTTP status.
            new_state, probed_update = self.wait_for_state(
                last_update, time.monotonic() + self.args.confirm_timeout)
            elapsed_ms = round((time.monotonic() - sent_at) * 1000)
            accepted = new_state is not None

            self.decision_count += 1
            self.log_decision({
                "decision": self.decision_count,
                "lastUpdate": last_update,
                "turnNo": state.get("turnNo"),
                "turnPlayer": state.get("turnPlayer"),
                "phase": phase,
                "layer": (state.get("turnPhase") or {}).get("layer"),
                "popupActive": bool((state.get("playerInputPopUp") or {}).get("active")),
                "legalActions": actions,
                "chosen": chosen,
                # Automatic passes never reach the policy, so its last reason
                # belongs to an earlier decision - do not attribute it here.
                "reason": (getattr(self.policy, "last_reason", "") or None) if via_policy
                          else "automatic pass (only legal action)",
                "degraded": reason,
                "viaPolicy": via_policy,
                "accepted": accepted,
                "responseMs": elapsed_ms,
            })

            if accepted:
                self.log(f"[{self.decision_count:04d}] turn={state.get('turnNo')} {phase:<18} "
                         f"{len(actions):2d} actions -> {describe(chosen)}  ({elapsed_ms} ms)")
                state, last_update = new_state, probed_update
                consecutive_failures = 0
                last_change = time.monotonic()
                continue

            # Rejected: remember it and try something else in this same state.
            self.note_rejection(phase)
            bad_actions.add((chosen["mode"], chosen.get("cardID"), chosen.get("buttonInput")))
            consecutive_failures += 1
            self.log(f"[{self.decision_count:04d}] turn={state.get('turnNo')} {phase:<18} "
                     f"REJECTED {describe(chosen)} ({consecutive_failures}/{self.args.max_failures})")

            if consecutive_failures >= self.args.max_failures:
                self.stop_reason = StopReason.REJECTED
                self.stop_detail = (f"{consecutive_failures} consecutive rejected inputs in "
                                    f"phase {phase}")
                return self.finish(started, state)

            # Rule 4: stall detection.
            if time.monotonic() - last_change > self.args.stall_timeout:
                self.stop_reason = StopReason.STALLED
                self.stop_detail = (f"no change for {self.args.stall_timeout}s while holding "
                                    f"priority in phase {phase}")
                return self.finish(started, state)

    # ------------------------------------------------------------------- finish
    def finish(self, started, state):
        summary = {
            "gameName": self.args.game_name,
            "seat": self.args.player_id,
            "stopReason": self.stop_reason,
            "detail": self.stop_detail,
            "decisions": self.decision_count,
            "rejectedByPhase": self.rejected_by_phase,
            "degradedPrompts": self.degraded_prompts,
            "cancelsUsed": self.cancels,
            "policy": self.args.policy,
            "deck": self.args.deck_label,
            "botDeck": self.args.bot_deck_label,
            "thinkDelay": self.args.think_delay,
            "startingPlayerHealth": self.starting_player_health,
            "startingOpponentHealth": self.starting_opponent_health,
            "elapsedSeconds": round(time.monotonic() - started, 1),
            "finalTurn": (state or {}).get("turnNo"),
            "finalPhase": turn_phase(state or {}),
            "playerHealth": (state or {}).get("playerHealth"),
            "opponentHealth": (state or {}).get("opponentHealth"),
        }
        if state is not None and self.stop_reason != StopReason.OVER:
            summary["stateDump"] = self.dump_state(state, self.stop_reason or "unknown")
        if hasattr(self.policy, "stats"):
            summary.update(self.policy.stats())
        if self.deadline_observations:
            with open(os.path.join(self.run_dir, "deadlines.jsonl"), "w", encoding="utf-8") as handle:
                for observation in self.deadline_observations:
                    handle.write(json.dumps(observation) + "\n")
            seen = [o["inactivityDeadline"] for o in self.deadline_observations]
            summary["inactivityDeadlineSeen"] = any(v is not None for v in seen)
            summary["inactiveFlagSeen"] = any(o["inactive"] for o in self.deadline_observations)
        with open(os.path.join(self.run_dir, "summary.json"), "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False)
        return summary


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Random-action Talishar agent.")
    parser.add_argument("game_name")
    parser.add_argument("player_id", type=int, choices=[1, 2])
    parser.add_argument("auth_key")
    parser.add_argument("--base-url", default="http://localhost:8080/game")
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--stall-timeout", type=float, default=30.0)
    parser.add_argument("--confirm-timeout", type=float, default=4.0)
    parser.add_argument("--max-failures", type=int, default=5)
    parser.add_argument("--max-turns", type=int, default=150,
                        help="stop if the turn counter passes this (deck-out stalemates)")
    parser.add_argument("--max-decisions", type=int, default=5000,
                        help="stop after this many decisions, whatever the turn")
    parser.add_argument("--allow-cancel", action="store_true",
                        help="last resort: send Cancel/Undo when nothing else is legal")
    parser.add_argument("--on-unsupported", choices=["stop", "pass"], default="stop",
                        help="prompts that need ProcessInputAPI.php: stop and dump the state "
                             "(default), or pass to accept the engine's default order")
    parser.add_argument("--policy", choices=policies.policy_names(), default="affordable")
    parser.add_argument("--deck-label", dest="deck_label", default=None,
                        help="recorded in the summary; the deck link used for this seat")
    parser.add_argument("--bot-deck-label", dest="bot_deck_label", default=None,
                        help="recorded in the summary; the bot's deck")
    parser.add_argument("--life-threshold", type=int, default=10,
                        help="rules policy: block when life after damage falls below this")
    parser.add_argument("--ability-reserve", type=int, default=4,
                        help="resources required before activating an equipment or aura "
                             "ability, whose real cost the API does not expose")
    parser.add_argument("--think-delay", type=float, default=0.0,
                        help="seconds to wait before each policy decision")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--runs-dir",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"))
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    agent = Agent(args)
    try:
        summary = agent.run()
    except KeyboardInterrupt:
        agent.stop_reason = StopReason.INTERRUPTED
        summary = agent.finish(time.monotonic(), None)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["stopReason"] == StopReason.OVER else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
