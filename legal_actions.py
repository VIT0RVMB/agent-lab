#!/usr/bin/env python3
"""Extracts the legal actions from a Talishar game state.

Implements STATE_FORMAT.md sections 4.1 to 4.4. Two regimes:

- playerInputPopUp.active is false: actionable cards live in the player's zones,
  each carrying its own action code, plus playerPrompt.buttons and the pass.
- playerInputPopUp.active is true: zone cards drop to action 0 and the choices
  move into popup.cards and playerInputPopUp.buttons.

In both regimes the cardID to send is the card's actionDataOverride, never its
array index (section 4.2).
"""

# Modes an automated agent must never send: settings, manual counter fiddling,
# and the undo family, which would rewrite history instead of playing.
FORBIDDEN_MODES = frozenset({0, 26, 102, 103, 104, 10000, 10001, 10002, 10003})

PASS_MODE = 99

# Zones whose entries carry an action code for the viewing player.
ACTIONABLE_ZONES = (
    "playerHand",
    "playerArse",
    "playerEquipment",
    "playerBanish",
    "playerDiscard",
    "playerAuras",
    "playerItems",
    "playerPermanents",
    "playerAllies",
    "playerSoul",
    "playerDeckCard",
    "landmarks",
    "combatChainLinks",
    "opponentBanish",
)

# Prompts whose input goes to ProcessInputAPI.php with a JSON body. We cannot
# express them through ProcessInput.php, so the agent stops instead of guessing.
UNSUPPORTED_POPUP_IDS = frozenset({"NEWOPT"})
UNSUPPORTED_PHASES = frozenset({"ORDERTRIGGERS"})


def turn_phase(state):
    return ((state.get("turnPhase") or {}).get("turnPhase")) or ""


def popup(state):
    return state.get("playerInputPopUp") or {}


def popup_is_active(state):
    return bool(popup(state).get("active"))


def unsupported_prompt(state):
    """Returns a reason string when the current prompt needs ProcessInputAPI.php."""
    phase = turn_phase(state)
    if phase in UNSUPPORTED_PHASES:
        return f"phase {phase} is resolved through ProcessInputAPI.php (JSON body)"
    popup_id = (popup(state).get("popup") or {}).get("id")
    if popup_id in UNSUPPORTED_POPUP_IDS:
        return f"popup {popup_id} is resolved through ProcessInputAPI.php (JSON body)"
    return None


def _action_key(action):
    """Stable identity for an action, used to mark it bad for a given state."""
    return (action["mode"], action.get("cardID"), action.get("buttonInput"))


def _card_action(card, source):
    mode = card.get("action")
    if not mode or int(mode) in FORBIDDEN_MODES:
        return None
    return {
        "mode": int(mode),
        "cardID": str(card.get("actionDataOverride", "")),
        "buttonInput": None,
        "card": card.get("cardNumber"),
        "source": source,
    }


def _button_action(button, source):
    mode = button.get("mode")
    if mode is None or int(mode) in FORBIDDEN_MODES:
        return None
    button_input = button.get("buttonInput")
    card_id = button.get("cardID")
    return {
        "mode": int(mode),
        "cardID": None if card_id is None else str(card_id),
        # Button labels use underscores for spaces; send them verbatim.
        "buttonInput": None if button_input is None else str(button_input),
        "card": button.get("caption"),
        "source": source,
    }


def legal_actions(state):
    """Returns the list of legal actions, each a dict ready for send_input()."""
    phase = turn_phase(state)
    actions = []

    if popup_is_active(state):
        for card in (popup(state).get("popup") or {}).get("cards") or []:
            action = _card_action(card, "popup.cards")
            if action:
                actions.append(action)
        for button in popup(state).get("buttons") or []:
            action = _button_action(button, "popup.buttons")
            if action:
                actions.append(action)
    else:
        for zone in ACTIONABLE_ZONES:
            for card in state.get(zone) or []:
                if not isinstance(card, dict):
                    continue
                action = _card_action(card, zone)
                if action:
                    actions.append(action)
        for button in (state.get("playerPrompt") or {}).get("buttons") or []:
            action = _button_action(button, "playerPrompt.buttons")
            if action:
                actions.append(action)

    # playerPrompt.buttons is not a complete action list: in M and INSTANT it is
    # empty even when passing is legal, so the pass comes from canPassPhase.
    if state.get("canPassPhase") and not any(a["mode"] == PASS_MODE for a in actions):
        actions.append({
            "mode": PASS_MODE,
            "cardID": None,
            "buttonInput": None,
            "card": "PASS",
            "source": "canPassPhase",
        })

    # Deduplicate while keeping order - a button and a zone entry can coincide.
    seen = set()
    unique = []
    for action in actions:
        key = _action_key(action)
        if key in seen:
            continue
        seen.add(key)
        action["phase"] = phase
        unique.append(action)
    return unique


def is_pass_only(actions):
    return len(actions) == 1 and actions[0]["mode"] == PASS_MODE


def describe(action):
    parts = [f"mode={action['mode']}"]
    if action.get("cardID") is not None:
        parts.append(f"cardID={action['cardID']}")
    if action.get("buttonInput") is not None:
        parts.append(f"buttonInput={action['buttonInput']}")
    if action.get("card"):
        parts.append(f"({action['card']})")
    return " ".join(parts)
