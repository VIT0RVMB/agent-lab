# agent-lab

An external agent that plays **Flesh and Blood** against a local
[Talishar](https://github.com/Talishar/Talishar) server, plus the harness that
measures whether it is any good.

The agent talks to the game engine over plain HTTP, the same way the web client
does. It reads the game state, works out which actions are legal, picks one, and
checks that the engine accepted it. Every decision is logged with the reasoning
behind it, so a policy can be debugged after the fact instead of guessed at.

Python 3, standard library only. Nothing to install.

> Fan project, unaffiliated with Legend Story Studios or with the Talishar team.

---

## Why

Playing Flesh and Blood well is a planning problem under hidden information: a
card in hand is resources, defence, or damage, never all three, and the cost of
a choice only shows up turns later. Before any of that can be modelled, the
boring part has to be solid — reading the state correctly, enumerating the legal
moves, and knowing when a move was rejected.

This repository is that boring part, done carefully, with measurement attached.

## Quickstart

A local Talishar backend has to be running first, serving on `localhost:8080`:

```bash
git clone https://github.com/Talishar/Talishar          # sibling of this repo
cd Talishar && bash start.sh
```

Then, from here:

```bash
# Card stats, pulled out of the Talishar checkout
python3 extract_card_data.py

# A decklist mirroring the one the built-in bot plays
python3 make_mirror_deck.py --deck IraCC

# One game against the bot
python3 new_game.py --deck local:iracc --bot-deck IraCC
python3 random_agent.py <gameName> 1 <authKey> --policy rules --on-unsupported pass

# 100 games, with confidence intervals
python3 evaluate.py --policy rules --games 100 --deck local:iracc --bot-deck IraCC -- --on-unsupported pass
```

`new_game.py` prints the `gameName` and `authKey` the next two commands need.
To watch a game you are playing yourself in the browser, `capture.py` records
the states and the moves.

## How it fits together

```
new_game.py         creates a solo game against the built-in bot
      │
talishar_client.py  GET GetNextTurn.php / ProcessInput.php
      │
legal_actions.py    state  ->  list of legal actions
      │
policies.py         pick one
      │
random_agent.py     poll, choose, send, confirm, log
      │
evaluate.py         N games  ->  evals/<policy>_<timestamp>.json
```

| File | Role |
|---|---|
| `talishar_client.py` | the two endpoints, and their quirks |
| `legal_actions.py` | action extraction, both prompt regimes |
| `policies.py` | `random`, `affordable`, `rules`, and the registry |
| `card_info.py` | card stats: the lobby dictionary over the extracted tables |
| `random_agent.py` | the game loop, rejection handling, decision log |
| `evaluate.py` | batches, winrate and damage with 95% intervals |
| `capture.py` | records a human's game for offline study |
| [`STATE_FORMAT.md`](STATE_FORMAT.md) | the state JSON, every action code, worked examples |
| [`LATENCY.md`](LATENCY.md) | what the engine does about a slow player |

## Policies

A policy is anything callable as `policy(state, actions, rng)`. Register it by
name and both the agent and the harness pick it up:

```python
POLICIES["mine"] = {
    "factory": lambda card_info, args: MyPolicy(card_info),
    "needs_card_info": True,
    "description": "...",
}
```

- **`random`** — uniform over legal actions. Proves the plumbing.
- **`affordable`** — random, minus the plays it cannot pay for.
- **`rules`** — heuristics per phase: block only when the damage matters, attack
  along the best go-again chain, pitch the card it least wants to play.

Setting `self.last_reason` writes a justification onto the decision's log line:

```
block: incoming 4 vs life 6, crown_of_providence blocks 2 on turn 12
action: attack bittering_thorns_red power 4 cost 1 of 6 available;
        best line 8 damage via bittering_thorns_red -> soulbead_strike_red, go again
```

## Where it stands

Against the built-in bot, over 100 games each. The agent does not win yet —
damage dealt is the metric that moves in the meantime.

| Policy | Deck | Damage / game | Final turn | Winrate (95% CI) |
|---|---|---|---|---|
| `affordable` | Viserai | 6.6 | 9.3 | 0% [0, 7.1%] |
| `rules` | Viserai | 5.9 | 13.6 | 0% [0, 3.7%] |
| `affordable` | Ira mirror | 3.6 [2.8, 4.4] | 18.2 | 0% [0, 3.8%] |

The honest reading: the rules survive longer than random play and deal slightly
less damage. Blocking well is easy to encode; converting cards into damage is
not, and that is the open problem.

## Notes from the engine

Things that took real time to work out, kept here so they do not have to be
worked out twice:

- **`cardID` is the card's `actionDataOverride`, not its index.** Zone arrays are
  strided, so the fifth equipment slot is `cardID=64`.
- **A poll replies with the body `0`** when nothing changed, and with an empty
  body while the game is still in the lobby. Neither is an error.
- **HTTP 200 does not mean the move landed.** Rejected input also returns 200;
  confirm by watching `lastUpdate` advance.
- **Ability costs are in no API** and some are dynamic — one equipment ability
  costs `2 + the number of runechants in play`. The agent learns them by reading
  the pitch prompt (`"(4 of 7)"`) and caches them.
- **Some prompts need a different endpoint** (`ProcessInputAPI.php`, JSON body),
  which the agent does not speak; it accepts the engine's default and counts
  that as a degraded decision instead of inventing a payload.
- **A mirror match can deck out into a stalemate**, passing forever with both
  heroes alive, so runs are capped by turn count.

[`STATE_FORMAT.md`](STATE_FORMAT.md) has the full reference, with real examples
for every phase: action, pitch, block, reaction, and each prompt type.

## Repository conventions

- The Talishar checkout is read, never written. Card data is extracted offline.
- `runs/` and `captures/` are working data and are not committed; `evals/` holds
  the aggregate results, which are.
- Measure before and after. A difference smaller than the interval
  `evaluate.py` prints is not a result.
