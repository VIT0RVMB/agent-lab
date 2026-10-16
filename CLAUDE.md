# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

agent-lab builds an external agent that plays Flesh and Blood on a local
Talishar instance. The game engine is a sibling checkout at `../Talishar` (PHP
backend, Docker) with its frontend at `../Talishar-FE`; this directory holds
only the agent, the harness that measures it, and the data captured from games.

**Nothing in `../Talishar` may be modified.** The agent speaks to the engine over
HTTP like any other client. The two exceptions already in place are a local
decklist loader (`Libraries/LocalDeckList.php`, added earlier with the owner's
agreement) and decklist files under `Assets/LocalDecks/`, which is the input
directory that loader reads. Card data is read out of `../Talishar` offline, never
written back.

## Running things

The backend must be up first (`cd ../Talishar && bash start.sh`), serving on
`localhost:8080`. Python is stdlib-only; there is nothing to install.

```bash
# Create a solo game against the built-in bot and play it
python3 new_game.py --deck local:iracc --bot-deck IraCC
python3 random_agent.py <gameName> 1 <authKey> --policy rules --on-unsupported pass

# Measure a policy over many games
python3 evaluate.py --policy rules --games 100 --deck local:iracc --bot-deck IraCC -- --on-unsupported pass

# Regenerate derived data
python3 extract_card_data.py            # card_data.json from the backend's generated tables
python3 make_mirror_deck.py --deck IraCC # Assets/LocalDecks/iracc.txt, a mirror of a bot deck
python3 make_mirror_deck.py --list       # the bot decks available locally

# Record a game played by a human in the browser
python3 capture.py <gameName> <playerID> <authKey>
```

Anything after a bare `--` in `evaluate.py` is forwarded to `random_agent.py`.

## Architecture

```
new_game.py        creates a solo game (CreateGame, ChooseFirstPlayer, GetLobbyInfo, SubmitSideboard)
      ↓
talishar_client.py GET GetNextTurn.php / ProcessInput.php, handles the "0" and empty-body replies
      ↓
legal_actions.py   turns a state into the list of legal actions
      ↓
policies.py        chooses one; registered by name in POLICIES
      ↓
random_agent.py    the loop: poll, choose, send, confirm, log to runs/<game>/
      ↓
evaluate.py        N games, aggregate, write evals/<policy>_<timestamp>.json
```

Supporting modules: `card_info.py` (card stats, lobby dictionary merged over
`card_data.json`), `extract_card_data.py` and `make_mirror_deck.py` (offline
extraction from `../Talishar`), `capture.py` (records a human's game).

- **`STATE_FORMAT.md`** is the reference for the state JSON, the action codes and
  the parameters each one takes. Read it before touching `legal_actions.py`.
- **`LATENCY.md`** covers how the engine treats a slow player.

## Writing a policy

A policy is anything callable as `policy(state, actions, rng)` returning one
action from `actions`. Register it in `POLICIES` at the bottom of `policies.py`:

```python
POLICIES["mine"] = {
    "factory": lambda card_info, args: MyPolicy(card_info),
    "needs_card_info": True,     # fetches the deck's stats before the first decision
    "description": "...",
}
```

`--policy` on both `random_agent.py` and `evaluate.py` reads this registry, so no
other file needs changing. A policy exposing `stats()` has that dict merged into
the run summary, and setting `self.last_reason` puts a justification on the
decision's line in `decisions.jsonl`.

Existing policies:

- **`random`** — uniform over legal actions. The plumbing test.
- **`affordable`** — random, minus plays it cannot pay for. The floor every other
  policy should build on; `RulesPolicy` uses it as a filter.
- **`rules`** — heuristics per phase (block, action, pitch, arsenal, reactions,
  prompts), with attack sequencing modelled on `BotBestAttackSequence`.

The engine's own bot is worth reading before inventing heuristics:
`../Talishar/AI/BotLogic.php`, particularly `BotScoreDefenseCandidate` (:597),
`BotEquipmentBlockAllowed` (:563), `BotLifeThresholdRisk` (:27) and
`BotBestAttackSequence` (:475).

## Output layout

| Path | Content |
|---|---|
| `runs/<gameName>/decisions.jsonl` | one line per decision: legal actions, choice, reason, accepted, response time |
| `runs/<gameName>/summary.json` | stop reason, final life, deck, policy stats |
| `runs/<gameName>/stopped_*.json` | the full state when a run stops for anything but a finished game |
| `evals/<policy>_<timestamp>.json` | aggregate over N games, plus a per-game table |
| `captures/<gameName>/` | states and moves recorded from a human's game |

`runs/` and `captures/` grow quickly (tens of MB); they are working data, not
deliverables.

## Things that cost time to rediscover

- **`cardID` is `actionDataOverride`, never the array index.** Zone arrays are
  strided, so the fifth equipment slot is `cardID=64`. The frontend does the same
  (`Talishar-FE/src/features/game/GameSlice.ts:242`).
- **A poll returns the body `0`** when nothing changed since `lastUpdate`, and an
  **empty body** while the game is still in the lobby. Neither is an error, and
  `GetNextTurn.php` returns nothing useful until the game has started.
- **A `200` does not mean the input landed.** The engine answers 200 for rejected
  input too; confirm by watching `lastUpdate` advance.
- **Ability costs are not in any API.** They are computed by the engine
  (`CardDictionary.php:682`) and some are dynamic: `grasp_of_the_arknight` costs
  `2 + NumRunechants`. `AbilityCostMemory` learns them from the pitch prompt
  (`"(4 of 7)"` plus `turnPhase.layer`) and caches them in `ability_costs.json`.
  Without that guard a policy can reach a pitch prompt it cannot pay, where the
  only legal action is Cancel (mode 10000) and the run is stuck.
- **Equipment is missing from the lobby dictionary**, and nothing there carries a
  go-again flag. Both come from `card_data.json`. Conditional go again — granted
  by card classes from game state, as Viserai's gloomblades do — is not in any
  table and is treated as absent.
- **Mirror matches can deck out into a stalemate**, passing forever with both
  heroes alive. `--max-turns` (150) and `--max-decisions` (5000) end those.
- **`ORDERTRIGGERS` and the `NEWOPT` popup** are resolved through
  `ProcessInputAPI.php` with a JSON body, which the agent does not speak.
  `--on-unsupported pass` accepts the engine's default order instead of stopping;
  every use is counted as a degraded prompt.
- **Modes never to send**: 0, 26, 102, 103, 104, 10000–10003 (settings, manual
  counters, undo). `FORBIDDEN_MODES` in `legal_actions.py`.
- **A long think is safe up to about 55 s per decision**, and the inactivity
  timer only exists in public games. See `LATENCY.md`.

## Conventions

- Standard library only, Python 3. No dependencies to install.
- Each script is runnable on its own with `--help` and sane defaults.
- Derived data (`card_data.json`, `ability_costs.json`, the mirror decklists) is
  regenerable; the script that writes it says where it came from.
- Measure before and after a change. A difference smaller than the 95% interval
  that `evaluate.py` prints is not a result.
