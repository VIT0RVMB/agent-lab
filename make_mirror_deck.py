#!/usr/bin/env python3
"""Turns one of the bot's decks into a decklist the agent can submit.

The bot's decks are plain gamestate deck files in ../Talishar/Assets:

    line 1  hero, weapons and equipment
    line 2  the 60-card main deck
    line 3+ per-slot sideboards

The agent submits a deck through the deck-link field, which the local decklist
loader reads from Assets/LocalDecks/<name>.txt in the format
"<count>x <card>" under Deck / Arena / Inventory headings. That loader resolves
a card by slugifying the text, so a Talishar card ID passes through unchanged -
no name table needed, and the pitch colour is already part of the ID.

So a mirror match is just a format conversion. The only thing written into the
Talishar tree is a new decklist under Assets/LocalDecks, which is exactly the
input that directory exists for; no code there is touched.

Usage:
    python3 make_mirror_deck.py                 # IraCC -> local:iracc
    python3 make_mirror_deck.py --deck FaiCC
    python3 make_mirror_deck.py --list
"""

import argparse
import collections
import os
import sys

# Deck file -> the name the bot knows it by (APIs/CreateGame.php $soloDecks).
BOT_DECKS = {
    "Dummy": "Dummy.txt",
    "IraCC": "IraCC.txt",
    "FaiCC": "FaiCC.txt",
    "BriarSAGE": "BriarSAGE.txt",
    "BravoSAGE": "BravoSAGE.txt",
    "IraSAGE": "Ira.txt",
}


def read_deck_file(path):
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle.read().splitlines()]
    arena = lines[0].split() if lines else []
    main = lines[1].split() if len(lines) > 1 else []
    sideboard = []
    for line in lines[2:]:
        sideboard.extend(line.split())
    return arena, main, sideboard


def section(title, cards, hero=None):
    out = [title]
    if hero:
        out.append(hero)
    for card, count in collections.Counter(cards).items():
        out.append(f"{count}x {card}")
    out.append("")
    return out


def convert(talishar_root, deck_name, out_name):
    source = os.path.join(talishar_root, "Assets", BOT_DECKS[deck_name])
    if not os.path.exists(source):
        raise SystemExit(f"not found: {source}")

    arena, main, sideboard = read_deck_file(source)
    if not arena:
        raise SystemExit(f"{source} has no hero line")
    hero, equipment = arena[0], arena[1:]

    lines = [f"# Mirror of the bot's {deck_name} deck, converted from Assets/{BOT_DECKS[deck_name]}"]
    lines.append("")
    lines += section("Deck", main)
    lines += section("Arena", equipment, hero=hero)
    if sideboard:
        lines += section("Inventory", sideboard)

    target_dir = os.path.join(talishar_root, "Assets", "LocalDecks")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f"{out_name}.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    return {
        "source": source,
        "target": target,
        "link": f"local:{out_name}",
        "hero": hero,
        "equipment": equipment,
        "mainDeck": len(main),
        "distinct": len(set(main)),
        "sideboard": len(sideboard),
    }


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Convert a bot deck into an agent decklist.")
    parser.add_argument("--deck", default="IraCC", choices=sorted(BOT_DECKS))
    parser.add_argument("--name", default=None, help="output name (default: the deck, lowercased)")
    parser.add_argument("--talishar", default=os.path.join(here, "..", "Talishar"))
    parser.add_argument("--list", action="store_true", help="list the bot decks and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, filename in sorted(BOT_DECKS.items()):
            path = os.path.join(args.talishar, "Assets", filename)
            hero = "?"
            if os.path.exists(path):
                arena, main, _ = read_deck_file(path)
                hero = f"{arena[0] if arena else '?'} ({len(main)} cards)"
            print(f"  {name:12s} {filename:16s} {hero}")
        return 0

    result = convert(args.talishar, args.deck, args.name or args.deck.lower())
    print(f"{result['source']} -> {result['target']}")
    print(f"hero       {result['hero']}")
    print(f"equipment  {' '.join(result['equipment'])}")
    print(f"main deck  {result['mainDeck']} cards, {result['distinct']} distinct")
    print(f"use        --deck {result['link']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
