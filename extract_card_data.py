#!/usr/bin/env python3
"""Extracts card stats from the Talishar backend's generated dictionaries.

`GetLobbyInfo.php` serves a cardDictionary for the submitted deck, but it covers
only cards in the deck list and omits equipment entirely, and it has no go-again
flag. Both gaps are filled by the backend's own generated tables, which are
plain `match($cardID) { "id" => value, ... }` expressions:

    GeneratedBlockValue.php      "grasp_of_the_arknight" => 2
    GeneratedHasBattleworn.php   "grasp_of_the_arknight" => true
    GeneratedGoAgain.php         "grasp_of_the_arknight" => true

This reads those files (read-only - nothing in ../Talishar is modified) and
writes card_data.json next to this script.

Battleworn matters for blocking: CoreLogic.php:1678 adds a -1 block counter each
time the equipment blocks, and destroys it when the effective block reaches 0.
A 2-block battleworn piece therefore has exactly two blocks in it.

Usage:
    python3 extract_card_data.py [--talishar ../Talishar] [--out card_data.json]
"""

import argparse
import json
import os
import re
import sys

# "card_id" => value,   with value a number, true/false, or a quoted string.
ENTRY = re.compile(r'"([a-z0-9_]+)"\s*=>\s*(-?\d+|true|false|"[^"]*")\s*,')

# Generated file -> key in the output record.
SOURCES = {
    "GeneratedBlockValue.php": "blockValue",
    "GeneratedCardCost.php": "cost",
    "GeneratedPitchValue.php": "pitch",
    "GeneratedPowerValue.php": "power",
    "GeneratedCardType.php": "type",
    "GeneratedCardSubtype.php": "subtype",
    "GeneratedCardClass.php": "cardClass",
    "GeneratedCardTalent.php": "talent",
    "GeneratedGoAgain.php": "hasGoAgain",
    "GeneratedHasBattleworn.php": "hasBattleworn",
    "GeneratedHasBladeBreak.php": "hasBladeBreak",
    "GeneratedHasGuardwell.php": "hasGuardwell",
    "GeneratedHasBloodDebt.php": "hasBloodDebt",
    "GeneratedHasDominate.php": "hasDominate",
    "GeneratedHasOverpower.php": "hasOverpower",
    "GeneratedHasPiercing.php": "hasPiercing",
}


def parse_value(raw):
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith('"'):
        return raw[1:-1]
    return int(raw)


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return {card: parse_value(value) for card, value in ENTRY.findall(text)}


def extract(talishar_root):
    base = os.path.join(talishar_root, "GeneratedCode", "GeneratedFunctions")
    if not os.path.isdir(base):
        raise SystemExit(f"not found: {base}")

    cards = {}
    used, missing = [], []
    for filename, key in SOURCES.items():
        path = os.path.join(base, filename)
        if not os.path.exists(path):
            missing.append(filename)
            continue
        used.append(filename)
        for card, value in parse_file(path).items():
            cards.setdefault(card, {})[key] = value
    return cards, used, missing


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Extract card stats from Talishar's generated code.")
    parser.add_argument("--talishar", default=os.path.join(here, "..", "Talishar"))
    parser.add_argument("--out", default=os.path.join(here, "card_data.json"))
    args = parser.parse_args(argv)

    cards, used, missing = extract(args.talishar)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(cards, handle, indent=0, sort_keys=True)

    print(f"{len(cards)} cards from {len(used)} tables -> {args.out}")
    if missing:
        print("tables not present (skipped): " + ", ".join(sorted(missing)))
    sample = cards.get("grasp_of_the_arknight")
    if sample:
        print("grasp_of_the_arknight:", json.dumps(sample, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
