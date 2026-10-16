#!/usr/bin/env python3
"""Card stats, from the lobby dictionary plus the extracted generated tables.

`GetLobbyInfo.php` answers during a running game and carries a
`deck.cardDictionary` with one entry per distinct card in the submitted deck,
including `cost` and `pitch`. Two things are missing from it:

- **equipment is absent entirely**, so no block value for the arena pieces;
- **there is no go-again flag** for anything.

card_data.json, written by extract_card_data.py from the backend's generated
dictionaries, fills both and covers every card in the game. The lobby dictionary
still wins where both have a value, since it is what this game is actually
using.

Cards in neither source return None so the caller can decide.
"""

import json
import os
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8080/game"
DEFAULT_CARD_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_data.json")


class CardInfo:
    def __init__(self, entries=None, card_data_path=DEFAULT_CARD_DATA):
        self.static = {}
        if card_data_path and os.path.exists(card_data_path):
            try:
                with open(card_data_path, encoding="utf-8") as handle:
                    self.static = json.load(handle)
            except (OSError, ValueError):
                self.static = {}

        self.by_id = {}
        for entry in entries or []:
            card_id = entry.get("id")
            if card_id:
                self.by_id[card_id] = entry

    def record(self, card_id):
        """Merged stats: the static table, overlaid with this game's dictionary."""
        if card_id is None:
            return None
        static = self.static.get(card_id)
        live = self.by_id.get(card_id)
        if static is None and live is None:
            return None
        merged = dict(static or {})
        for key, value in (live or {}).items():
            if value is not None:
                merged[key] = value
        return merged

    def field(self, card_id, key, default=None):
        record = self.record(card_id)
        if record is None:
            return default
        value = record.get(key)
        return default if value is None else value

    def block_value(self, card_id):
        """Printed block value, or None when unknown. -1 means "cannot block"."""
        value = self.field(card_id, "blockValue")
        if value is None:
            return None
        value = int(value)
        return None if value < 0 else value

    def has_go_again(self, card_id):
        return bool(self.field(card_id, "hasGoAgain", False))

    def has_battleworn(self, card_id):
        return bool(self.field(card_id, "hasBattleworn", False))

    def card_type(self, card_id):
        return self.field(card_id, "type", "") or ""

    @classmethod
    def fetch(cls, game_name, seat, auth_key, base_url=DEFAULT_BASE_URL, timeout=20):
        body = json.dumps({"gameName": str(game_name), "playerID": int(seat),
                           "authKey": auth_key}).encode()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/APIs/GetLobbyInfo.php", data=body,
            headers={"Content-Type": "application/json", "User-Agent": "talishar-agent-lab/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if payload.get("error"):
            raise RuntimeError(f"GetLobbyInfo failed: {payload['error']}")
        return cls((payload.get("deck") or {}).get("cardDictionary"))

    def __len__(self):
        return len(self.by_id)

    def __contains__(self, card_id):
        return self.record(card_id) is not None

    def cost(self, card_id):
        """Resource cost, or None when the card is unknown."""
        entry = self.record(card_id)
        if entry is None:
            return None
        value = entry.get("cost")
        # The dictionary uses -1 for "not applicable" (heroes, equipment).
        if value is None or value < 0:
            return 0
        return int(value)

    def pitch(self, card_id):
        """Resources produced when pitched, or None when the card is unknown."""
        entry = self.record(card_id)
        if entry is None:
            return None
        value = entry.get("pitch")
        if value is None or value < 0:
            return 0
        return int(value)
