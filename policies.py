#!/usr/bin/env python3
"""Policies for the random agent.

`random_policy` picks uniformly among legal actions. `AffordablePolicy` does the
same, minus the actions it can prove the seat cannot pay for.

Why the filter exists: a card goes on the stack before its cost is paid, so
playing a 1-cost card with an empty hand lands the engine in a pitch prompt with
nothing to pitch, no pass available, and only a Cancel button (mode 10000) as a
way out. With Cancel forbidden, that state is terminal. Checking affordability
first avoids reaching it at all.
"""

import json
import os
import re

# Modes that put a card from a zone onto the stack, where a resource cost is due.
PAID_PLAY_MODES = frozenset({27, 5, 14, 36})

# Equipment and aura ability activations. Their cost is computed by the engine at
# activation time (CardDictionary.php:682 AbilityCost, which branches on game
# state and per-card classes), so it is not in cardDictionary and cannot be read
# from the API. We gate them behind a resource reserve instead of a real cost.
ABILITY_MODES = frozenset({3, 22})
DEFAULT_ABILITY_RESERVE = 4

# The pitch prompt states how much is still owed: "Choose a card to pitch (4 of 7)".
PITCH_PROGRESS = re.compile(r"\((\d+) of (\d+)\)")
DEFAULT_COST_MEMORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ability_costs.json")


class AbilityCostMemory:
    """Learns ability costs by reading them off the pitch prompt.

    Ability costs are computed by the engine and are not in cardDictionary, and
    some are dynamic: grasp_of_the_arknight costs "2 + NumRunechants"
    (CardDictionaries/ArcaneRising/ARCShared.php:17), which in a Viserai deck
    climbs past any fixed reserve - that is what stalled runs 60, 87 and 94.

    The pitch prompt names the total it wants and turnPhase.layer names the card
    that asked, so every pitch the agent sees teaches it one cost. We keep the
    highest value seen per card, which is the safe side for a cost that grows.
    """

    def __init__(self, path=DEFAULT_COST_MEMORY, margin=1):
        self.path = path
        self.margin = margin
        self.costs = {}
        self.learned_this_run = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    self.costs = {k: int(v) for k, v in json.load(handle).items()}
            except (OSError, ValueError, TypeError):
                self.costs = {}

    def observe(self, state):
        phase = ((state.get("turnPhase") or {}).get("turnPhase")) or ""
        if phase != "P":
            return
        source = (state.get("turnPhase") or {}).get("layer") or ""
        # "PRETRIGGER" and friends are stack bookkeeping, not a card we can bill.
        if not source or source.isupper():
            return
        match = PITCH_PROGRESS.search((state.get("playerPrompt") or {}).get("helpText") or "")
        if not match:
            return
        total = int(match.group(2))
        if total > self.costs.get(source, -1):
            self.costs[source] = total
            self.learned_this_run[source] = total
            self.save()

    def save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.costs, handle, indent=2, sort_keys=True)
        except OSError:
            pass

    def required(self, card_id, floor):
        """Resources to insist on before activating this card's ability."""
        seen = self.costs.get(card_id)
        if seen is None:
            return floor
        return max(floor, seen + self.margin)

# Phases where mode 27 means "pitch this card" or "block with this card" rather
# than "play it", so no cost is due.
FREE_PHASES = frozenset({"P", "PDECK", "B", "ARS"})


def random_policy(state, actions, rng):
    return rng.choice(actions)


class AffordablePolicy:
    def __init__(self, card_info, ability_reserve=DEFAULT_ABILITY_RESERVE, rng_fallback=True,
                 cost_memory=None):
        self.card_info = card_info
        self.ability_reserve = ability_reserve
        self.rng_fallback = rng_fallback
        self.cost_memory = cost_memory if cost_memory is not None else AbilityCostMemory()
        self.filtered = 0
        self.filtered_abilities = 0
        self.unknown_costs = set()

    # ---------------------------------------------------------------- resources
    def available_resources(self, state, excluding_card_id):
        """Resources the seat could still produce for a cost.

        Floating resources already pitched, plus the pitch value of every other
        card in hand. Cards of unknown pitch value are assumed worthless, which
        keeps the estimate conservative.
        """
        try:
            floating = int(state.get("playerPitchCount") or 0)
        except (TypeError, ValueError):
            floating = 0

        from_hand = 0
        skipped = False
        for card in state.get("playerHand") or []:
            if not isinstance(card, dict):
                continue
            # The card being played is itself leaving the hand.
            if not skipped and str(card.get("actionDataOverride")) == str(excluding_card_id):
                skipped = True
                continue
            pitch = self.card_info.pitch(card.get("cardNumber"))
            if pitch:
                from_hand += pitch
        return floating + from_hand

    def affordable(self, state, action):
        if action["mode"] in ABILITY_MODES and action.get("phase") not in FREE_PHASES:
            # Cost is not in cardDictionary, so use the highest cost ever observed
            # for this card, falling back to the reserve for cards never seen
            # paying. Blocks some free abilities too, which is the price of not
            # reimplementing AbilityCost.
            needed = self.cost_memory.required(action.get("card"), self.ability_reserve)
            if self.available_resources(state, None) < needed:
                self.filtered_abilities += 1
                return False
            return True
        if action["mode"] not in PAID_PLAY_MODES:
            return True
        if action.get("phase") in FREE_PHASES:
            return True

        cost = self.card_info.cost(action.get("card"))
        if cost is None:
            # Unknown card. Allow it unless it would empty the hand, which is the
            # shape that produces the terminal pitch prompt.
            self.unknown_costs.add(action.get("card"))
            return len(state.get("playerHand") or []) > 1
        if cost == 0:
            return True
        return self.available_resources(state, action.get("cardID")) >= cost

    # ------------------------------------------------------------------- policy
    def __call__(self, state, actions, rng):
        self.cost_memory.observe(state)
        affordable = [action for action in actions if self.affordable(state, action)]
        self.filtered += len(actions) - len(affordable)
        if affordable:
            return rng.choice(affordable)
        # Everything looks unpayable. Passing is always in the list when it is
        # legal, so this only happens when it is not - fall back rather than
        # deadlock, and let the agent's rejection handling deal with it.
        return rng.choice(actions) if self.rng_fallback else None

    def stats(self):
        return {
            "actionsFilteredAsUnaffordable": self.filtered,
            "abilityActivationsHeldBack": self.filtered_abilities,
            "abilityReserve": self.ability_reserve,
            "cardsWithUnknownCost": sorted(c for c in self.unknown_costs if c),
            "abilityCostsLearned": dict(self.cost_memory.learned_this_run),
            "abilityCostsKnown": dict(self.cost_memory.costs),
        }


# ---------------------------------------------------------------------- registry
# Adding a policy: write the class here, then register its name below. Anything
# callable as policy(state, actions, rng) works; a `needs_card_info` entry gets
# the deck's cost/pitch table, and `stats()` is merged into the run summary.
POLICIES = {
    "random": {"factory": lambda card_info, args: random_policy,
               "needs_card_info": False,
               "description": "uniform over all legal actions"},
    "affordable": {"factory": lambda card_info, args: AffordablePolicy(
                       card_info, ability_reserve=getattr(args, "ability_reserve", DEFAULT_ABILITY_RESERVE)),
                   "needs_card_info": True,
                   "description": "uniform, minus plays whose cost the seat cannot pay"},
}


def policy_names():
    return sorted(POLICIES)


def build_policy(name, card_info, args):
    try:
        entry = POLICIES[name]
    except KeyError:
        raise ValueError(f"unknown policy {name!r}; known: {', '.join(policy_names())}") from None
    return entry["factory"](card_info, args)


def needs_card_info(name):
    return POLICIES.get(name, {}).get("needs_card_info", False)


# ------------------------------------------------------------------ rules policy
# Reference: ../Talishar/AI/BotLogic.php. Not a port - the shape of the
# heuristics is borrowed, the numbers are ours:
#   BotEquipmentBlockAllowed (:563)  block with equipment only when the hit is
#                                    lethal or crosses a life threshold
#   BotScoreDefenseCandidate (:597)  prevented damage, minus overblock, minus the
#                                    offence given up, minus an equipment cost
#   BotLifeThresholdRisk (:27)       life after damage drives the risk term
#   BotCardRolesFromStats (:88)      play/retain value from pitch, power, defense
#   BotBestAttackSequence (:475)     best damage reachable with the pitch pool

ATTACK_TYPES = frozenset({"AA"})
REACTION_TYPES = frozenset({"AR", "DR"})
DEFENSE_REACTION = "DR"

# Phases where the policy has a rule of its own; anything else falls through to
# the generic prompt handling.
BLOCK_PHASES = frozenset({"B"})
REACTION_PHASES = frozenset({"A", "D", "INSTANT"})
PROMPT_PHASES = frozenset({"YESNO", "BUTTONINPUT", "CHOOSEMULTIZONE", "MAYCHOOSEMULTIZONE"})

DECLINE_TOKENS = ("SKIP", "NO", "CARDID-SKIP", "No_search", "PASS")

# Numeric BUTTONINPUT prompts ask for an amount, and the meaning of the number
# is card-specific: "Lose life to create runechants" trades life for board, while
# another card may be asking how many targets to hit. There is no generic reading
# of it, so the default is the most conservative value and anything smarter is
# keyed by popup.sourceCard. Only prompts that actually show up often enough to
# matter get an entry (see PROMPT_HANDLER_THRESHOLD in the notes).
#
#   handler(state, numeric_options, policy) -> chosen option, or None to default
NUMERIC_PROMPT_HANDLERS = {}


def numeric_options(actions):
    """The subset of actions whose buttonInput is a plain integer."""
    options = []
    for action in actions:
        raw = action.get("buttonInput")
        if raw is None:
            continue
        text = str(raw).strip()
        if text.lstrip("-").isdigit():
            options.append((int(text), action))
    return sorted(options, key=lambda pair: pair[0])


class RulesPolicy:
    """Phase-by-phase heuristics on top of the affordability filter."""

    def __init__(self, card_info, ability_reserve=DEFAULT_ABILITY_RESERVE,
                 life_threshold=10, equipment_block_min_turn=3, cost_memory=None,
                 equipment_cost=2.0, equipment_min_prevented=3, danger_life=4):
        self.base = AffordablePolicy(card_info, ability_reserve=ability_reserve,
                                     cost_memory=cost_memory)
        self.card_info = card_info
        self.life_threshold = life_threshold
        self.equipment_block_min_turn = equipment_block_min_turn
        # BotScoreDefenseCandidate charges a flat 2.0 for using equipment, and
        # BotEquipmentBlockAllowed only permits it when the hit is lethal or the
        # life left lands in BotLifeThresholdRisk's band (<= 4).
        self.equipment_cost = equipment_cost
        self.equipment_min_prevented = equipment_min_prevented
        self.danger_life = danger_life
        self.last_reason = ""
        self.prompt_log = []
        self.rule_counts = {}

    # ------------------------------------------------------------------ helpers
    def stat(self, card_id, key, default=0):
        entry = self.card_info.by_id.get(card_id) if self.card_info else None
        if entry is None:
            return default
        value = entry.get(key)
        if value is None or (isinstance(value, int) and value < 0 and key != "power"):
            return default
        return value

    def card_type(self, card_id):
        return self.stat(card_id, "type", "")

    def power(self, card_id):
        return max(0, int(self.stat(card_id, "power", 0) or 0))

    def block_value(self, card_id):
        """Printed block value. Equipment comes from card_data.json."""
        if self.card_info is not None:
            value = self.card_info.block_value(card_id)
            if value is not None:
                return value
        return max(0, int(self.stat(card_id, "blockValue", 0) or 0))

    def has_go_again(self, card_id):
        return bool(self.card_info and self.card_info.has_go_again(card_id))

    def has_battleworn(self, card_id):
        return bool(self.card_info and self.card_info.has_battleworn(card_id))

    @staticmethod
    def equipment_entry(state, card_id_param):
        """The playerEquipment entry an action refers to, by actionDataOverride."""
        for card in state.get("playerEquipment") or []:
            if isinstance(card, dict) and str(card.get("actionDataOverride")) == str(card_id_param):
                return card
        return None

    def equipment_block(self, state, action):
        """Effective block of an equipment piece: printed value plus its counters.

        Battleworn equipment accumulates -1 block counters as it blocks
        (CoreLogic.php:1678) and is destroyed when the total reaches 0, so the
        counters already in the state tell us what is left in it.
        """
        printed = self.block_value(action.get("card"))
        entry = self.equipment_entry(state, action.get("cardID"))
        counters = 0
        if entry is not None:
            counters = min(0, self.as_int(entry.get("defCounters"), 0))
        return max(0, printed + counters)

    def pitch_value(self, card_id):
        return max(0, int(self.stat(card_id, "pitch", 0) or 0))

    def cost(self, card_id):
        value = self.card_info.cost(card_id) if self.card_info else None
        return 0 if value is None else value

    def known(self, card_id):
        return bool(self.card_info and card_id in self.card_info.by_id)

    def offence_value(self, card_id):
        """How much we give up by not keeping this card to attack with.

        Mirrors BotCardRolesFromStats: an attack is worth its power plus a bonus
        for being cheap to play (low pitch), a reaction keeps a flat premium.
        """
        card_type = self.card_type(card_id)
        colour_bonus = max(0, 3 - self.pitch_value(card_id)) * 2
        if card_type in ATTACK_TYPES:
            return self.power(card_id) + colour_bonus
        if card_type in REACTION_TYPES:
            return self.power(card_id) + 6
        return self.pitch_value(card_id)

    @staticmethod
    def incoming_damage(state):
        link = state.get("activeChainLink") or {}
        try:
            return max(0, int(link.get("totalPower") or 0) - int(link.get("totalDefense") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def as_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def available_resources(self, state):
        return self.base.available_resources(state, None)

    def pending_cost(self, state):
        """Resources still owed in a pitch prompt, from "(paid of total)"."""
        match = PITCH_PROGRESS.search((state.get("playerPrompt") or {}).get("helpText") or "")
        if not match:
            return None
        paid, total = int(match.group(1)), int(match.group(2))
        return max(0, total - paid)

    def note(self, rule, reason):
        self.rule_counts[rule] = self.rule_counts.get(rule, 0) + 1
        self.last_reason = reason

    # -------------------------------------------------------------------- rules
    def choose_block(self, state, actions):
        incoming = self.incoming_damage(state)
        life = self.as_int(state.get("playerHealth"))
        turn = self.as_int(state.get("turnNo"))
        passes = [a for a in actions if a["mode"] in (99, 101)]

        if incoming <= 0:
            if passes:
                self.note("block-nothing-incoming", f"block: nothing incoming, pass")
                return passes[0]

        lethal = incoming >= life
        after = life - incoming
        below_threshold = after < self.life_threshold

        candidates = []
        for action in actions:
            if action["mode"] == 27:
                block = self.block_value(action.get("card"))
                if block <= 0:
                    continue
                candidates.append((action, block, self.offence_value(action.get("card")), False))
            elif action["mode"] == 3:
                block = self.equipment_block(state, action)
                if block <= 0:
                    continue
                prevented = min(incoming, block)
                # Spend equipment only when the hit actually matters: lethal, or
                # it leaves us in the danger band, or it stops a real chunk.
                worth_it = (lethal
                            or (life - incoming) <= self.danger_life
                            or prevented >= self.equipment_min_prevented)
                if not worth_it or turn < self.equipment_block_min_turn:
                    continue
                candidates.append((action, block, 0.0, True))

        if not candidates:
            if passes:
                self.note("block-no-blocker", f"block: incoming {incoming}, no usable blocker, pass")
                return passes[0]
            return None

        useless_to_attack = all(
            self.card_type(a.get("card")) not in ATTACK_TYPES or self.power(a.get("card")) == 0
            for a, _, _, is_equipment in candidates if not is_equipment
        ) if any(not c[3] for c in candidates) else False

        if not (lethal or below_threshold or useless_to_attack):
            if passes:
                self.note("block-not-worth",
                          f"block: incoming {incoming} leaves {after} >= {self.life_threshold}, "
                          f"keeping cards, pass")
                return passes[0]

        def score(entry):
            action, block, offence, is_equipment = entry
            prevented = min(incoming, block)
            overblock = max(0, block - incoming)
            value = prevented - overblock * 1.5
            if is_equipment:
                # A battleworn piece spends one of its uses per block, so the
                # fewer blocks it has left, the dearer each one is.
                value -= self.equipment_cost
                if self.has_battleworn(action.get("card")):
                    value -= 2.0 if block <= 1 else 1.0
            else:
                value -= offence * 0.3
            if lethal and prevented >= incoming:
                value += 100
            return value

        best = max(candidates, key=score)
        action, block, offence, is_equipment = best
        if is_equipment:
            self.note("block-equipment",
                      f"block: incoming {incoming} vs life {life}, "
                      f"{action.get('card')} blocks {block}"
                      + (" (battleworn, spends a use)" if self.has_battleworn(action.get("card")) else "")
                      + f" on turn {turn}")
        else:
            reason = (f"block: incoming {incoming} would leave {after} < {self.life_threshold}"
                      if below_threshold and not lethal else
                      f"block: incoming {incoming} >= life {life}, lethal" if lethal else
                      f"block: blocker has no offensive use")
            self.note("block-take", f"{reason}; block {block} with {action.get('card')}")
        return action

    def plan_attacks(self, state, actions):
        """Best damage reachable this turn, and the attack to start it with.

        Follows BotBestAttackSequence (AI/BotLogic.php:475): every attack not
        played is still available as pitch, so the resource pool is the floating
        resources plus the pitch value of the whole hand, and playing an attack
        both spends its cost and removes its pitch from the pool. Only an attack
        with go again lets the chain continue, so the search branches on that.

        Conditional go again - Viserai's gloomblades grant it from game state
        through DoesAttackHaveGoAgain() - is treated as absent here, since
        card_data.json only carries the printed keyword.
        """
        attacks = []
        for action in actions:
            if action["mode"] not in PAID_PLAY_MODES:
                continue
            card = action.get("card")
            if self.card_type(card) not in ATTACK_TYPES:
                continue
            attacks.append({
                "action": action,
                "card": card,
                "power": self.power(card),
                "cost": self.cost(card),
                "pitch": self.pitch_value(card),
                "goAgain": self.has_go_again(card),
            })
        if not attacks:
            return None, 0.0, []

        # Bound the search: 10 attacks is already 10! orderings in the worst case,
        # and a hand that size is rare. Prefer the strongest when trimming.
        attacks.sort(key=lambda a: (-a["power"], a["cost"]))
        attacks = attacks[:8]

        floating = self.as_int(state.get("playerPitchCount"))
        hand_pitch = sum(self.pitch_value(c.get("cardNumber"))
                         for c in (state.get("playerHand") or []) if isinstance(c, dict))
        total_pool = floating + hand_pitch

        best = {"damage": 0.0, "first": None, "sequence": []}

        def walk(used, spent_pitch, spent_cost, damage, sequence):
            if damage > best["damage"]:
                best.update({"damage": damage, "first": sequence[0] if sequence else None,
                             "sequence": list(sequence)})
            for index, attack in enumerate(attacks):
                if used & (1 << index):
                    continue
                pitch = spent_pitch + attack["pitch"]
                cost = spent_cost + attack["cost"]
                if total_pool - pitch - cost < 0:
                    continue
                reached = damage + attack["power"]
                sequence.append(attack)
                if attack["goAgain"]:
                    walk(used | (1 << index), pitch, cost, reached, sequence)
                elif reached > best["damage"]:
                    best.update({"damage": reached, "first": sequence[0],
                                 "sequence": list(sequence)})
                sequence.pop()

        walk(0, 0, 0, 0.0, [])
        return best["first"], best["damage"], best["sequence"]

    def choose_action(self, state, actions):
        available = self.available_resources(state)
        action_points = self.as_int(state.get("playerAP"))
        plays = [a for a in actions if a["mode"] in PAID_PLAY_MODES or a["mode"] in ABILITY_MODES]
        passes = [a for a in actions if a["mode"] == 99]

        if action_points > 0:
            first, damage, sequence = self.plan_attacks(state, actions)
            if first is not None and damage > 0:
                card = first["card"]
                chain = " -> ".join(a["card"] for a in sequence)
                self.note("action-attack",
                          f"action: attack {card} power {first['power']} cost {first['cost']} "
                          f"of {available} available; best line {damage:.0f} damage"
                          + (f" via {chain}" if len(sequence) > 1 else "")
                          + (", go again" if first["goAgain"] else ""))
                return first["action"]

        # No attack worth making: a non-attack play is still better than nothing
        # when it costs nothing, otherwise keep the cards.
        free_plays = [a for a in plays
                      if a["mode"] in PAID_PLAY_MODES and self.cost(a.get("card")) == 0
                      and self.known(a.get("card"))]
        if free_plays and action_points > 0:
            choice = max(free_plays, key=lambda a: self.power(a.get("card")))
            self.note("action-free-play",
                      f"action: no affordable attack, free play {choice.get('card')}")
            return choice

        if passes:
            self.note("action-pass",
                      f"action: no attack within {available} resources and {action_points} AP, pass")
            return passes[0]
        return None

    def choose_pitch(self, state, actions):
        pending = self.pending_cost(state)
        pitches = [a for a in actions if a["mode"] == 27]
        if not pitches:
            return None

        def entry(action):
            card = action.get("card")
            return {
                "action": action,
                "card": card,
                "pitch": self.pitch_value(card),
                "keep": self.offence_value(card),
            }

        options = [entry(a) for a in pitches]
        if pending:
            # Do not overpay: prefer the smallest pitch that still covers what is
            # owed, otherwise the largest that stays under it.
            enough = [o for o in options if o["pitch"] >= pending]
            if enough:
                choice = min(enough, key=lambda o: (o["pitch"], o["keep"]))
                self.note("pitch-exact",
                          f"pitch: {pending} owed, {choice['card']} gives {choice['pitch']}")
                return choice["action"]
            choice = max(options, key=lambda o: (o["pitch"], -o["keep"]))
            self.note("pitch-partial",
                      f"pitch: {pending} owed, no single card covers it, "
                      f"{choice['card']} gives {choice['pitch']}")
            return choice["action"]

        choice = min(options, key=lambda o: (o["keep"], -o["pitch"]))
        self.note("pitch-cheapest",
                  f"pitch: cost unknown, pitching least useful {choice['card']} "
                  f"(keep value {choice['keep']})")
        return choice["action"]

    def choose_arsenal(self, state, actions):
        candidates = [a for a in actions if a["mode"] == 4]
        if not candidates:
            return None
        attacks = [a for a in candidates if self.card_type(a.get("card")) in ATTACK_TYPES]
        pool = attacks or candidates
        choice = max(pool, key=lambda a: (self.power(a.get("card")), self.pitch_value(a.get("card"))))
        self.note("arsenal-best-attack",
                  f"arsenal: storing {choice.get('card')} power {self.power(choice.get('card'))}")
        return choice

    def choose_reaction(self, state, actions):
        incoming = self.incoming_damage(state)
        life = self.as_int(state.get("playerHealth"))
        passes = [a for a in actions if a["mode"] == 99]
        if incoming > 0 and (incoming >= life or life - incoming < self.life_threshold):
            defenses = [a for a in actions
                        if a["mode"] in PAID_PLAY_MODES
                        and self.card_type(a.get("card")) == DEFENSE_REACTION
                        and self.cost(a.get("card")) <= self.available_resources(state)]
            if defenses:
                choice = max(defenses, key=lambda a: self.block_value(a.get("card")))
                self.note("reaction-defend",
                          f"reaction: incoming {incoming} vs life {life}, "
                          f"defense reaction {choice.get('card')}")
                return choice
        if passes:
            self.note("reaction-pass", f"reaction: incoming {incoming}, life {life}, pass")
            return passes[0]
        return None

    def choose_prompt(self, state, actions, phase):
        """Prompt handling: a per-card table for numeric asks, else first non-decline."""
        popup = (state.get("playerInputPopUp") or {}).get("popup") or {}
        source = popup.get("sourceCard") or ""
        numbers = numeric_options(actions)

        if numbers:
            handler = NUMERIC_PROMPT_HANDLERS.get(source)
            choice = handler(state, numbers, self) if handler else None
            if choice is None:
                # Conservative default: the smallest amount on offer. For a
                # "lose life" prompt that is the cheapest, and for a "how many"
                # prompt it gives up value rather than risking the game.
                choice = numbers[0][1]
                self.note("prompt-numeric-default",
                          f"prompt {phase}: numeric ask from {source or 'unknown'}, "
                          f"taking smallest value {numbers[0][0]} (no handler)")
            else:
                self.note("prompt-numeric-handler",
                          f"prompt {phase}: numeric ask from {source}, handler chose "
                          f"{choice.get('buttonInput')}")
            self.prompt_log.append({
                "phase": phase, "turnNo": state.get("turnNo"), "sourceCard": source,
                "popupId": popup.get("id"),
                "helpText": (state.get("playerPrompt") or {}).get("helpText"),
                "numeric": True, "handled": handler is not None,
                "options": [value for value, _ in numbers],
                "chosen": choice.get("buttonInput"),
            })
            return choice

        non_decline = [a for a in actions
                       if not any(token in str(a.get("buttonInput") or a.get("cardID") or
                                               a.get("card") or "") for token in DECLINE_TOKENS)]
        choice = (non_decline or actions)[0]
        record = {
            "phase": phase,
            "turnNo": state.get("turnNo"),
            "sourceCard": source,
            "popupId": popup.get("id"),
            "helpText": (state.get("playerPrompt") or {}).get("helpText"),
            "options": [describe_action(a) for a in actions],
            "chosen": describe_action(choice),
        }
        self.prompt_log.append(record)
        self.note(f"prompt-{phase.lower()}",
                  f"prompt {phase}: taking first non-decline option "
                  f"{describe_action(choice)} (needs review)")
        return choice

    # ------------------------------------------------------------------- policy
    def __call__(self, state, actions, rng):
        self.base.cost_memory.observe(state)
        self.last_reason = ""
        phase = ((state.get("turnPhase") or {}).get("turnPhase")) or ""

        # The affordability filter is the floor: never offer the rules an action
        # that would stall the game in an unpayable pitch prompt.
        safe = [a for a in actions if self.base.affordable(state, a)]
        pool = safe or actions
        if not safe:
            self.note("fallback-unaffordable",
                      "no affordable action passed the filter, falling back to the full list")

        chosen = None
        if phase in BLOCK_PHASES:
            chosen = self.choose_block(state, pool)
        elif phase == "M":
            chosen = self.choose_action(state, pool)
        elif phase == "P":
            chosen = self.choose_pitch(state, pool)
        elif phase == "ARS":
            chosen = self.choose_arsenal(state, pool)
        elif phase in REACTION_PHASES:
            chosen = self.choose_reaction(state, pool)
        elif phase in PROMPT_PHASES:
            chosen = self.choose_prompt(state, pool, phase)

        if chosen is None:
            chosen = rng.choice(pool)
            if not self.last_reason:
                self.note(f"random-{phase.lower() or 'unknown'}",
                          f"{phase}: no rule applies, random among {len(pool)} actions")
        return chosen

    def stats(self):
        merged = dict(self.base.stats())
        merged["ruleCounts"] = dict(sorted(self.rule_counts.items()))
        merged["promptsNeedingReview"] = len(self.prompt_log)
        merged["promptSamples"] = self.prompt_log[:20]
        return merged


def describe_action(action):
    parts = [f"mode={action['mode']}"]
    if action.get("cardID") is not None:
        parts.append(str(action["cardID"]))
    if action.get("buttonInput") is not None:
        parts.append(str(action["buttonInput"]))
    if action.get("card"):
        parts.append(f"({action['card']})")
    return " ".join(parts)


POLICIES["rules"] = {
    "factory": lambda card_info, args: RulesPolicy(
        card_info,
        ability_reserve=getattr(args, "ability_reserve", DEFAULT_ABILITY_RESERVE),
        life_threshold=getattr(args, "life_threshold", 10),
    ),
    "needs_card_info": True,
    "description": "phase-by-phase heuristics over the affordability filter",
}
