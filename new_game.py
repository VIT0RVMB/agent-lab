#!/usr/bin/env python3
"""Creates a solo game against the built-in bot without using the browser.

Only a test harness: it drives the same lobby APIs the frontend uses
(CreateGame, ChooseFirstPlayer, GetLobbyInfo, SubmitSideboard) so a run of the
random agent can be started from the shell. Playing a real game by hand through
the browser works exactly the same way.

Usage:
    python3 new_game.py [--deck local:viserai] [--bot-deck IraCC] [--format open]

Prints {"gameName": ..., "playerID": 1, "authKey": ...} on stdout.

Note on --format: "open" has no deck-size minimum, so the lobby can be submitted
as-is. In "cc" the backend requires 60 main-deck cards, which means choosing
which inventory cards to include - a decision this harness does not make.
"""

import argparse
import json
import sys
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8080/game"


def post(base_url, endpoint, body):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/APIs/{endpoint}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "talishar-agent-lab/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{endpoint} returned non-JSON: {raw[:300]}") from error


def first_or_empty(values):
    return values[0] if values else ""


def create_game(args):
    created = post(args.base_url, "CreateGame.php", {
        "deckTestMode": "1",
        "deckTestDeck": args.bot_deck,
        "format": args.format,
        "visibility": "private",
        "gameDescription": args.description,
        "fabdb": args.deck,
    })
    if created.get("error"):
        raise RuntimeError(f"CreateGame failed: {created['error']}")
    game_name, auth_key = created["gameName"], created["authKey"]

    # The human always wins the roll against a bot, so this choice is ours.
    post(args.base_url, "ChooseFirstPlayer.php", {
        "gameName": game_name, "playerID": 1,
        "action": "Go First" if args.go_first else "Go Second",
        "authKey": auth_key,
    })

    info = post(args.base_url, "GetLobbyInfo.php", {
        "gameName": game_name, "playerID": 1, "authKey": auth_key,
    })
    deck = info["deck"]
    submission = {
        "hero": deck["hero"],
        "hands": [weapon["id"] for weapon in deck.get("hands") or []],
        "head": first_or_empty(deck.get("head")),
        "chest": first_or_empty(deck.get("chest")),
        "arms": first_or_empty(deck.get("arms")),
        "legs": first_or_empty(deck.get("legs")),
        "deck": deck["cards"],
        "inventory": deck.get("cardsSB") or [],
    }
    submitted = post(args.base_url, "SubmitSideboard.php", {
        "gameName": game_name, "playerID": 1, "authKey": auth_key,
        "submission": json.dumps(submission), "phase": "deck",
    })
    if submitted.get("status") != "OK":
        raise RuntimeError(f"SubmitSideboard failed: {json.dumps(submitted)}")

    return {
        "gameName": game_name,
        "playerID": 1,
        "authKey": auth_key,
        "gameStarted": bool(submitted.get("gameStarted")),
        "deckSize": len(deck["cards"]),
        "deck": args.deck,
        "botDeck": args.bot_deck,
        "hero": deck.get("hero"),
        "format": args.format,
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Create a solo Talishar game against the bot.")
    parser.add_argument("--deck", default="local:viserai",
                        help="deck link for seat 1; make_mirror_deck.py writes local:<name> "
                             "decklists that mirror the bot's decks")
    parser.add_argument("--bot-deck", default="IraCC",
                        choices=["Dummy", "IraCC", "FaiCC", "BriarSAGE", "BravoSAGE", "IraSAGE"])
    parser.add_argument("--format", default="open")
    parser.add_argument("--description", default="agent-lab")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--go-second", dest="go_first", action="store_false")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        print(json.dumps(create_game(parse_args(sys.argv[1:])), indent=2))
    except (RuntimeError, urllib.error.URLError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
