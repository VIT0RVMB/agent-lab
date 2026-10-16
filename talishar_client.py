#!/usr/bin/env python3
"""HTTP client for the Talishar game endpoints.

Covers the two calls an agent needs, and the quirks documented in
STATE_FORMAT.md section 1:

- GetNextTurn.php replies with the literal body "0" when nothing changed since
  the lastUpdate we passed, and with an empty body while the game is still in
  the lobby. Neither is an error.
- Errors arrive as JSON {"errorMessage": "..."}.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8080/game"
USER_AGENT = "talishar-agent-lab/1"


class TalisharError(RuntimeError):
    """The backend answered with an errorMessage."""


class StateUnchanged:
    """Sentinel: the poll returned "0" - no change since last_update."""

    __slots__ = ()

    def __repr__(self):
        return "<StateUnchanged>"


class LobbyWaiting:
    """Sentinel: the poll returned an empty body - the game has not started."""

    __slots__ = ()

    def __repr__(self):
        return "<LobbyWaiting>"


UNCHANGED = StateUnchanged()
LOBBY = LobbyWaiting()


class TalisharClient:
    def __init__(self, game_name, seat, auth_key, base_url=DEFAULT_BASE_URL, timeout=20):
        self.game_name = str(game_name)
        self.seat = int(seat)
        self.auth_key = auth_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ plumbing
    def _get(self, path, params):
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", "replace")

    def _base_params(self):
        return {
            "gameName": self.game_name,
            "playerID": self.seat,
            "authKey": self.auth_key,
        }

    # --------------------------------------------------------------------- state
    def get_state(self, last_update=0):
        """Returns a state dict, or the UNCHANGED / LOBBY sentinel."""
        params = self._base_params()
        params["lastUpdate"] = last_update
        body = self._get("GetNextTurn.php", params).strip()

        if body == "":
            return LOBBY
        if body == "0":
            return UNCHANGED

        try:
            state = json.loads(body)
        except json.JSONDecodeError as error:
            # A rejected input makes the engine print plain text; a state poll
            # should never do that, so surface it rather than swallow it.
            raise TalisharError(f"non-JSON state response: {body[:300]}") from error

        if isinstance(state, dict) and state.get("errorMessage"):
            raise TalisharError(state["errorMessage"])
        return state

    # ---------------------------------------------------------------------- input
    def send_input(self, mode, card_id=None, button_input=None):
        """Sends one input. The reply body is returned unparsed.

        The engine answers 200 even for a rejected input (often with a plain
        "... Invalid Input<BR>" body), so the caller must confirm the move by
        checking that lastUpdate advanced.
        """
        params = self._base_params()
        params["mode"] = mode
        if card_id is not None:
            params["cardID"] = card_id
        if button_input is not None:
            params["buttonInput"] = button_input
        return self._get("ProcessInput.php", params)
