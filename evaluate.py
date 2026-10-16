#!/usr/bin/env python3
"""Runs a policy over N fresh games and reports how it did.

Each game is created with new_game.py and played by random_agent.py in a
subprocess, so a crash in one game cannot take the batch down. The per-game
summary.json the agent writes is the source of truth for the aggregate.

Usage:
    python3 evaluate.py --policy affordable --games 50
    python3 evaluate.py --policy random --games 20 -- --ability-reserve 0

Anything after a bare -- is forwarded to random_agent.py untouched.

Outcome is read from the final health values in the summary:
    opponentHealth <= 0  -> win
    playerHealth   <= 0  -> loss
Anything else, or a stop before OVER, counts as unfinished.
"""

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def mean_interval(values, z=1.96):
    """Mean with a 95% normal-approximation confidence interval.

    While the winrate is pinned at 0%, damage dealt is the metric that moves, so
    it needs an interval of its own to tell a real gain from noise.
    """
    if not values:
        return None, (None, None)
    mean = statistics.mean(values)
    if len(values) < 2:
        return round(mean, 2), (round(mean, 2), round(mean, 2))
    margin = z * statistics.stdev(values) / math.sqrt(len(values))
    return round(mean, 2), (round(mean - margin, 2), round(mean + margin, 2))


def wilson_interval(successes, total, z=1.96):
    """95% confidence interval for a proportion (Wilson score interval).

    With 50 games the interval is wide, which is the point: it keeps a 4-point
    difference between two policies from being read as a real difference.
    """
    if total == 0:
        return (0.0, 0.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(summary):
    """Returns 'win', 'loss' or 'unfinished' plus a reason."""
    if summary.get("stopReason") != "game_over":
        return "unfinished", summary.get("stopReason") or "unknown"
    player = to_int(summary.get("playerHealth"))
    opponent = to_int(summary.get("opponentHealth"))
    if opponent is not None and opponent <= 0:
        return "win", ""
    if player is not None and player <= 0:
        return "loss", ""
    return "unfinished", "over_without_a_dead_hero"


def create_game(args):
    command = [sys.executable, os.path.join(HERE, "new_game.py"), "--base-url", args.base_url]
    if args.deck:
        command += ["--deck", args.deck]
    if args.bot_deck:
        command += ["--bot-deck", args.bot_deck]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"new_game.py failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


def play_game(args, game, seed, extra):
    command = [
        sys.executable, os.path.join(HERE, "random_agent.py"),
        str(game["gameName"]), str(game["playerID"]), game["authKey"],
        "--base-url", args.base_url,
        "--policy", args.policy,
        "--seed", str(seed),
        "--runs-dir", args.runs_dir,
        "--deck-label", str(game.get("deck") or args.deck or ""),
        "--bot-deck-label", str(game.get("botDeck") or args.bot_deck or ""),
        "--quiet",
    ] + extra
    subprocess.run(command, capture_output=True, text=True, timeout=args.game_timeout)
    summary_path = os.path.join(args.runs_dir, str(game["gameName"]), "summary.json")
    if not os.path.exists(summary_path):
        raise RuntimeError(f"no summary.json for game {game['gameName']}")
    with open(summary_path, encoding="utf-8") as handle:
        return json.load(handle)


def mean_decision_ms(runs_dir, game_name):
    path = os.path.join(runs_dir, str(game_name), "decisions.jsonl")
    if not os.path.exists(path):
        return None
    values = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                values.append(json.loads(line)["responseMs"])
            except (json.JSONDecodeError, KeyError):
                continue
    return statistics.mean(values) if values else None


def aggregate(args, results, elapsed):
    wins = sum(1 for r in results if r["outcome"] == "win")
    losses = sum(1 for r in results if r["outcome"] == "loss")
    finished = [r for r in results if r["outcome"] in ("win", "loss")]
    unfinished = [r for r in results if r["outcome"] == "unfinished"]

    low, high = wilson_interval(wins, len(finished)) if finished else (0.0, 0.0)

    def average(key, source=None):
        values = [to_int((source or r["summary"]).get(key)) for r in results
                  for source in [r["summary"]]]
        values = [v for v in values if v is not None]
        return round(statistics.mean(values), 2) if values else None

    stop_reasons = {}
    for result in unfinished:
        reason = result["reason"] or "unknown"
        stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

    degraded = {}
    for result in results:
        for key, value in (result["summary"].get("degradedPrompts") or {}).items():
            short = key.split(" is ")[0]
            degraded[short] = degraded.get(short, 0) + value

    decision_ms = [r["meanDecisionMs"] for r in results if r["meanDecisionMs"] is not None]

    # Damage dealt, measured against the life the opponent actually started with
    # rather than an assumed 40, since blitz and sage decks start at 20.
    damages, damage_rates = [], []
    for result in results:
        summary = result["summary"]
        start = to_int(summary.get("startingOpponentHealth"))
        end = to_int(summary.get("opponentHealth"))
        if start is None or end is None:
            continue
        damage = max(0, start - end)
        damages.append(damage)
        turns = to_int(summary.get("finalTurn")) or 0
        if turns > 0:
            damage_rates.append(damage / turns)
    damage_mean, damage_ci = mean_interval(damages)
    rate_mean, rate_ci = mean_interval(damage_rates)

    decks = {r["summary"].get("deck") for r in results if r["summary"].get("deck")}
    bot_decks = {r["summary"].get("botDeck") for r in results if r["summary"].get("botDeck")}

    return {
        "deck": sorted(d for d in decks if d) or [args.deck or "(new_game.py default)"],
        "botDeck": sorted(d for d in bot_decks if d) or [args.bot_deck or "(new_game.py default)"],
        "damagePerGame": damage_mean,
        "damagePerGameCI95": list(damage_ci),
        "damagePerTurn": rate_mean,
        "damagePerTurnCI95": list(rate_ci),
        "damageBasis": len(damages),
        "policy": args.policy,
        "games": len(results),
        "extraArgs": args.extra,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "wins": wins,
        "losses": losses,
        "unfinished": len(unfinished),
        "unfinishedReasons": stop_reasons,
        "winrate": round(wins / len(finished), 4) if finished else None,
        "winrateCI95": [round(low, 4), round(high, 4)],
        "winrateBasis": len(finished),
        "meanFinalTurn": average("finalTurn"),
        "meanPlayerHealth": average("playerHealth"),
        "meanOpponentHealth": average("opponentHealth"),
        "totalDecisions": sum(r["summary"].get("decisions", 0) for r in results),
        "meanDecisionMs": round(statistics.mean(decision_ms), 1) if decision_ms else None,
        "degradedPrompts": degraded,
        "actionsFilteredAsUnaffordable": sum(
            r["summary"].get("actionsFilteredAsUnaffordable", 0) for r in results),
        "abilityActivationsHeldBack": sum(
            r["summary"].get("abilityActivationsHeldBack", 0) for r in results),
        "cancelsUsed": sum(r["summary"].get("cancelsUsed", 0) for r in results),
        "elapsedSeconds": round(elapsed, 1),
        "perGame": [{
            "gameName": r["summary"].get("gameName"),
            "seed": r["seed"],
            "outcome": r["outcome"],
            "reason": r["reason"],
            "finalTurn": r["summary"].get("finalTurn"),
            "playerHealth": r["summary"].get("playerHealth"),
            "opponentHealth": r["summary"].get("opponentHealth"),
            "decisions": r["summary"].get("decisions"),
        } for r in results],
    }


def print_report(report):
    print()
    print(f"policy            {report['policy']}  {' '.join(report['extraArgs'])}".rstrip())
    print(f"deck              {', '.join(report['deck'])}  vs bot {', '.join(report['botDeck'])}")
    print(f"games             {report['games']}  ({report['elapsedSeconds']}s)")
    print(f"wins / losses     {report['wins']} / {report['losses']}")
    if report["winrate"] is not None:
        low, high = report["winrateCI95"]
        print(f"winrate           {report['winrate']:.1%}  "
              f"95% CI [{low:.1%}, {high:.1%}]  (n={report['winrateBasis']})")
    print(f"unfinished        {report['unfinished']}"
          + (f"  {report['unfinishedReasons']}" if report["unfinishedReasons"] else ""))
    if report["damagePerGame"] is not None:
        low, high = report["damagePerGameCI95"]
        print(f"damage per game   {report['damagePerGame']}  95% CI [{low}, {high}]  "
              f"(n={report['damageBasis']})")
        rate_low, rate_high = report["damagePerTurnCI95"]
        print(f"damage per turn   {report['damagePerTurn']}  95% CI [{rate_low}, {rate_high}]")
    print(f"mean final turn   {report['meanFinalTurn']}")
    print(f"mean health       own {report['meanPlayerHealth']}  "
          f"opponent {report['meanOpponentHealth']}")
    print(f"decisions         {report['totalDecisions']} total, "
          f"{report['meanDecisionMs']} ms mean round trip")
    print(f"filtered          {report['actionsFilteredAsUnaffordable']} unaffordable, "
          f"{report['abilityActivationsHeldBack']} abilities held back, "
          f"{report['cancelsUsed']} cancels")
    if report["degradedPrompts"]:
        print(f"degraded prompts  {report['degradedPrompts']}")


def parse_args(argv):
    extra = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1:]

    import policies  # imported here so --help works without a running backend
    parser = argparse.ArgumentParser(description="Evaluate a Talishar policy over N games.")
    parser.add_argument("--policy", default="affordable", choices=policies.policy_names())
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--seed-start", type=int, default=1)
    parser.add_argument("--base-url", default="http://localhost:8080/game")
    parser.add_argument("--deck", default=None, help="deck link, defaults to new_game.py's")
    parser.add_argument("--bot-deck", default=None)
    parser.add_argument("--game-timeout", type=float, default=900)
    parser.add_argument("--runs-dir", default=os.path.join(HERE, "runs"))
    parser.add_argument("--evals-dir", default=os.path.join(HERE, "evals"))
    args = parser.parse_args(argv)
    args.extra = extra
    return args


def main(argv):
    args = parse_args(argv)
    os.makedirs(args.evals_dir, exist_ok=True)
    started = time.monotonic()
    results = []

    for index in range(args.games):
        seed = args.seed_start + index
        try:
            game = create_game(args)
            summary = play_game(args, game, seed, args.extra)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            print(f"[{index + 1}/{args.games}] harness failure: {error}", file=sys.stderr)
            results.append({"summary": {"stopReason": f"harness_failure: {error}"},
                            "seed": seed, "outcome": "unfinished",
                            "reason": "harness_failure", "meanDecisionMs": None})
            continue

        outcome, reason = classify(summary)
        results.append({
            "summary": summary,
            "seed": seed,
            "outcome": outcome,
            "reason": reason,
            "meanDecisionMs": mean_decision_ms(args.runs_dir, summary.get("gameName")),
        })
        print(f"[{index + 1}/{args.games}] game {summary.get('gameName')} {outcome}"
              f" turn {summary.get('finalTurn')}"
              f" ({summary.get('playerHealth')} vs {summary.get('opponentHealth')})"
              + (f" - {reason}" if reason else ""), flush=True)

    report = aggregate(args, results, time.monotonic() - started)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    path = os.path.join(args.evals_dir, f"{args.policy}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    print_report(report)
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
