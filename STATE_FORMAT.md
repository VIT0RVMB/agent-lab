# Talishar game state format

Reference for an external agent that observes and plays through the HTTP API.
Everything here was derived from two sources:

- **Code**: `GetNextTurn.php`, `BuildGameState.php`, `Libraries/NetworkingLibraries.php`,
  `GameTerms.php` in the backend, plus `src/features/game/GameSlice.ts` in Talishar-FE.
- **Capture**: `captures/4/` — one complete game, Viserai vs. the built-in Ira bot,
  407 states and 400 logged inputs over 14 turns, ending in a win for seat 1.

Every claim marked "observed" is backed by a file in `captures/4/`. Items that the
capture could not settle are collected in [Open questions](#open-questions).

---

## 1. The two endpoints

### Reading state

```
GET /game/GetNextTurn.php?gameName=<id>&playerID=<1|2|3>&authKey=<hex>&lastUpdate=<n>
```

- Returns the full state as JSON, from the point of view of `playerID`.
- `lastUpdate` is a **change counter**, not a timestamp. Pass the `lastUpdate` from
  the previous response; if nothing changed the endpoint replies with the literal
  body `0` and no JSON. Pass `0` to force a full state.
- A game that is still in the lobby (choosing first player, sideboarding) returns an
  **empty body**, not an error. Polling only yields states once play has started.
- Errors come back as `{"errorMessage": "..."}`.
- `playerID=3` is a spectator and requires a logged-in session, so it is not useful
  for an agent driven by `authKey` alone.

Side effect worth knowing: each call writes player presence into the game cache
(`GetNextTurn.php:107`), so polling marks the seat as connected and can emit one
"🔌Player N has connected" line into the game log.

The frontend itself does **not** use this endpoint — it consumes
`GetUpdateSSE.php`. `GetNextTurn.php` is the polling fallback and is the simpler
surface for an agent.

### Sending a move

```
GET /game/ProcessInput.php?gameName=<id>&playerID=<n>&authKey=<hex>&mode=<code>&cardID=<ref>&buttonInput=<value>
```

`mode` selects the kind of input; `cardID` and `buttonInput` carry its argument.
Which one is used depends on the mode — see [section 6](#6-action-code-reference).

There is a **second input endpoint**: `ProcessInputAPI.php`, which takes a JSON
body instead of query parameters. The frontend uses it for inputs that carry
structured data, such as reordering cards for a top/bottom-of-deck choice
(`NewOptInput.tsx:70` posts `{mode, submission:{cardListTop, cardListBottom}}`).
In the captured game 12 inputs went through it. An agent that only speaks
`ProcessInput.php` cannot express those choices.

---

## 2. Top-level shape

The response is one flat object. Observed keys (74 in game 4, state `0169.json`):

### Status and turn

| Field | Example | Meaning |
|---|---|---|
| `lastUpdate` | `414` | change counter; feed back into the next poll |
| `turnNo` | `"4"` | turn number, as a string |
| `turnPlayer` | `"1"` | whose turn it is (`1` or `2`) |
| `turnPhase` | `{"turnPhase":"M","layer":"TRIGGER","caption":"Choose an action"}` | phase, current stack layer, human caption |
| `havePriority` | `true` | `currentPlayer == playerID`, i.e. the engine is waiting on this seat |
| `canPassPhase` | `true` | a pass (`mode=99`) is legal right now |
| `amIActivePlayer` | `true` | the decision queue is addressing this seat — **not** "is it my turn", and not a reliable priority flag; see [section 3](#3-is-it-the-agents-turn-and-in-what-phase) |
| `preventPassPrompt` | `""` | non-empty when the UI warns before passing |
| `playerAP` / `opponentAP` | `"1"` / `0` | action points |
| `firstPlayer`, `otherPlayer` | `"1"`, `2` | seat bookkeeping |
| `clock`, `serverTime`, `inactivityDeadline` | `289` | timers |

### Zones, own seat (`player*`)

| Field | Content |
|---|---|
| `playerHand` | array of cards, each with `action`, `actionDataOverride`, `uniqueID` (`h1`, `h2`, …) |
| `playerArse` | arsenal |
| `playerEquipment` | hero + weapons + equipment, each with a `slot` (`Hero`, `LWep`, `Head`, `Chest`, `Arms`, `Legs`, …) |
| `playerPitch`, `playerPitchCount` | pitched cards this turn, with `pitchValue` |
| `playerDiscard`, `playerBanish`, `playerSoul`, `playerSoulCount` | graveyard, banish, soul |
| `playerAuras`, `playerItems`, `playerPermanents`, `playerAllies` | permanents in play |
| `playerDeck`, `playerDeckCount`, `playerDeckCard`, `playerDeckPopup` | deck size and, for heroes that see the top card, its identity |
| `playerHealth` | `"23"` — a string, and it can go negative (`"-3"` in the final state) |
| `playerEffects` | active continuous effects |
| `playerInventory` | equipment/weapons available in the lobby, not in play |
| `myBloodDebtCount`, `myBlessingsCount`, `myEarthCount` | resource counters (only those the hero uses appear) |

### Zones, opponent (`opponent*`)

Same names with the `opponent` prefix. What is hidden:

- `opponentHand` is an array of `{"cardNumber":"CardBack"}` — **count only, no identities**.
  Observed: 4 entries while the bot held 4 cards.
- `opponentDeck` / `opponentDeckCount` give the size; the cards are not listed.
- `opponentArse` entries are face-down unless the card was turned up.
- `opponentEquipment`, `opponentAuras`, `opponentItems`, `opponentPermanents`,
  `opponentDiscard`, `opponentBanish`, `opponentPitch` are fully visible, as they are
  public zones in Flesh and Blood.
- Opponent cards carry no `action` field — you cannot act on them from these lists
  (acting on an opponent's zone happens through a multizone prompt instead).

### Combat

| Field | Content |
|---|---|
| `activeChainLink` | the live chain link: `attackingCard`, `totalPower`, `totalDefense`, `attackTarget`, `reactions[]`, plus keyword booleans (`dominate`, `overpower`, `goAgain`, `piercing`, `phantasm`, `veil`, `combo`, `fused`, …) |
| `combatChainLinks` | **summaries** of resolved links in this chain, e.g. `[{"result":"no-hit","isDraconic":false}]` — not the cards |
| `layerDisplay` | the stack: `layerContents[]` and `reorderableLayers[]` with `layerID` and `isReorderable` |
| `landmarks` | landmarks in play |
| `lastPlayedCard` | `{"cardNumber":"shadow_of_ursur_blue","controller":"1"}` |

### Prompts

| Field | Content |
|---|---|
| `playerPrompt` | `{"helpText": "...", "buttons": [...]}` |
| `playerInputPopUp` | `{"active": bool, "popup": {...}, "buttons": [...]}` |
| `newEvents` | `{"eventArray":[{"eventType":"...","eventValue":"..."}]}` — animation/notification hints |
| `chatLog`, `chatEnabled` | game log lines |

---

## 3. Is it the agent's turn, and in what phase?

Three questions, three different fields:

1. **Is the engine waiting on me?** → `havePriority`. This is `currentPlayer == playerID`
   in `BuildGameState.php:1702`. When false, the other seat (or the bot) owes an input.
2. **Whose turn is it?** → `turnPlayer`. Observed `"1"` for our own turns and `"2"`
   for the bot's turns, including the block and defense-reaction windows inside them.
3. **What is being asked for?** → `turnPhase.turnPhase`, with `turnPhase.layer` for the
   stack item being resolved and `turnPhase.caption` as a ready-made human sentence.

### Why not `amIActivePlayer`

`amIActivePlayer` is `$turn[1] == $playerID` (`BuildGameState.php:1724`), and it reads
like a priority flag — priority does pass to the non-turn player during reaction
windows, so the two coincide most of the time. They are not the same thing.

`$turn[1]` is written in exactly two places in the backend, both in the decision queue:

```php
// CardLogic.php:787 - DecisionQueueStaticEffect, asking a player for a decision
$turn[0] = $phase;
$turn[1] = $player;
$currentPlayer = $player;

// CardLogic.php:433 - CloseDecisionQueue, restoring the pre-queue state
$turn[0] = $dqState[1];
$turn[1] = $dqState[2];
```

So `$turn[1]` means "the seat the decision queue is currently addressing". Inside the
queue it is assigned alongside `$currentPlayer`, which is why it tracks priority there.
But priority also moves through paths that never touch it:

```php
// CoreLogic.php:2807 - chain link closing
$turn[0] = "M";
$currentPlayer = $mainPlayer;

// CoreLogic.php:3181 - pitch zone choice
$currentPlayer = $player;
$turn[0] = "PDECK";
```

In those, `$currentPlayer` moves and `$turn[1]` keeps its old value. The capture shows
the two diverging: `0406.json` has `havePriority: true` with `amIActivePlayer: false`.

Use `havePriority` to decide whether an input is owed — it derives from `currentPlayer`
and is maintained on every path. `amIActivePlayer` is useful for a different question:
whether a decision popup is addressed to this seat, which is how
`BuildPlayerInputPopup.php` uses it, in roughly twenty `if ($turn[1] == $playerID)`
branches.

Phase codes, from `GameTerms.php:5` and the capture (counts from game 4):

| Code | Meaning | States |
|---|---|---|
| `M` | Main phase — play actions, use equipment, attack | 74 |
| `P` | Pitch — choose a card to pitch for resources | 15 |
| `B` | Block — choose blockers (only while `turnPlayer` is the opponent) | 35 |
| `A` | Attack reaction window | 29 |
| `D` | Defense reaction window | 16 |
| `INSTANT` | Priority window to play an instant | 195 |
| `ARS` | Choose a card to put into arsenal | 2 |
| `PDECK` | Choose a card from the pitch zone | 5 |
| `OPT` | Arrange cards on top/bottom of deck | 5 |
| `YESNO` | Yes/no decision | 3 |
| `BUTTONINPUT` | Choose one of N labelled buttons | 5 |
| `CHOOSEMULTIZONE` | Choose a card across zones (mandatory) | 10 |
| `MAYCHOOSEMULTIZONE` | Same, but may decline | 5 |
| `ORDERTRIGGERS` | Order simultaneous triggers | 7 |
| `OVER` | Game finished | 1 |

`turnPhase.turnPhase == "OVER"` is the end-of-game signal the frontend uses
(`Talishar-FE/src/routes/game/play/Play.tsx:130`), and it is what `capture.py` stops on.

`INSTANT` dominates the state count (195 of 407) because the engine opens a priority
window around nearly every stack item. Most of those are answered with a pass: 193 of
the 278 passes in the capture came from an `INSTANT` state.

---

## 4. Extracting the legal actions

There are two regimes, and `playerInputPopUp.active` tells you which.

### 4.1 Popup inactive — act on zones

Every card object in a `player*` zone carries an `action` integer.
**`action == 0` means not actionable right now.** A non-zero `action` is both "this is
legal" and "this is the `mode` to send".

Observed mapping of zone → action code across the whole capture:

| Zone | Action codes seen |
|---|---|
| `playerHand` | `27` (play/pitch/block), `4` (put into arsenal) |
| `playerArse` | `5` |
| `playerEquipment` | `3` |
| `playerBanish` | `14` |
| `playerAuras` | `22` |

So the legal-action list is:

```python
actions = []
for zone in ("playerHand", "playerArse", "playerEquipment",
             "playerBanish", "playerAuras", "playerDiscard",
             "playerItems", "playerPermanents", "playerAllies"):
    for card in state.get(zone) or []:
        if card.get("action"):
            actions.append({"mode": card["action"],
                            "cardID": card["actionDataOverride"],
                            "card": card["cardNumber"]})
if state["canPassPhase"]:
    actions.append({"mode": 99})
actions += [{"mode": b["mode"], "buttonInput": b["buttonInput"]}
            for b in state["playerPrompt"]["buttons"]]
```

### 4.2 The `cardID` rule

**`cardID` is the card's `actionDataOverride`, not its position in the array.**
From `GameSlice.ts:242`:

```ts
const playNo = params.cardParams.actionDataOverride != ''
  ? params.cardParams.actionDataOverride
  : params.cardIndex;
```

This matters because `actionDataOverride` is the card's offset in the backend's raw
zone array, which is strided. Equipment in game 4:

```json
[{"cardNumber":"viserai_the_forsaken","actionDataOverride":"0","slot":"Hero"},
 {"cardNumber":"seven_sin_nebula",    "actionDataOverride":"16","slot":"LWep"},
 {"cardNumber":"face_purgatory",      "actionDataOverride":"32","slot":"Head"},
 {"cardNumber":"fyendals_spring_tunic","actionDataOverride":"48","slot":"Chest"},
 {"cardNumber":"grasp_of_the_arknight","actionDataOverride":"64","slot":"Arms"},
 {"cardNumber":"spellbound_creepers", "actionDataOverride":"80","slot":"Legs"}]
```

Stride 16, so the fifth equipment piece is `cardID=64`, not `cardID=4`. The hand
happens to be stride 1, which is why hand indices look like plain positions.

For some modes `actionDataOverride` is not a number at all: arsenal submission
(`mode=4`) and pitch-zone selection (`mode=6`) use the **card name**, and multizone
prompts use a `ZONE-offset` string.

This rule was verified mechanically against the capture: for all 85 zone-based inputs,
the `cardID` sent equals the `actionDataOverride` of a card whose `action` equals the
`mode` sent. Zero mismatches.

### 4.3 Popup active — act on the popup

When `playerInputPopUp.active` is `true`, zone cards drop to `action: 0` and the
choices move into the popup. Two lists matter:

- `playerInputPopUp.popup.cards[]` — selectable cards, each with its own `action` and
  `actionDataOverride`.
- `playerInputPopUp.buttons[]` — each button is a ready-made call:
  `{"mode": 20, "buttonInput": "YES", "caption": "Yes"}`.

`popup.id` names the prompt type (`YESNO`, `BUTTONINPUT`, `CHOOSEMULTIZONE`, `NEWOPT`, …)
and `popup.sourceCard` names the card that asked. `popup.topCards` / `popup.bottomCards`
are used by the deck-ordering prompt.

### 4.4 Passing

`playerPrompt.buttons` is **not** a complete action list. In the capture it was
populated only during `B` (3 buttons) and `P` (1 button); in `M` and `INSTANT` it was
always empty even though passing was legal. Use `canPassPhase` to decide whether
`mode=99` is available, rather than looking for a Pass button.

---

## 5. Pairing state with action

`capture.py` records the inputs by tailing the web container's Apache access log,
which logs the full request line including the query string:

```
192.168.65.1 - - [16/Oct/2026:00:34:34 +0000] "GET /game/ProcessInput.php?gameName=4&playerID=1&mode=27&cardID=2 HTTP/1.1" 200 ...
```

Output per game, in `captures/<gameName>/`:

| File | Content |
|---|---|
| `NNNN.json` | one full state per observed change |
| `states.jsonl` | index: turn, phase, layer, priority, health |
| `moves.jsonl` | one record per input, with `state_before` |
| `transitions.jsonl` | `state_before` → input → `state_after` |
| `session.json` | run parameters |

Caveats, both real:

1. Apache logs a request when it **completes**, so the pairing is by wall-clock order,
   not a transactional link. `state_before` is the newest state captured before the log
   line appeared. At a 250 ms poll interval this held up well, but a state that changes
   twice between two polls collapses into one capture.
2. Inputs sent to `ProcessInputAPI.php` as a JSON POST carry no parameters in the log.
   `capture.py` now records them with `"params_available": false` so the gap is visible
   instead of silent, but their content is unrecoverable from the log.

---

## 6. Action code reference

Codes are the `mode` values in `Libraries/NetworkingLibraries.php`. "Observed" means the
capture contains at least one real example.

### Playing cards and abilities

| mode | Meaning | `cardID` | Observed |
|---|---|---|---|
| `27` | Play a card from hand. Also how you **pitch** (in phase `P`) and how you **block** (in phase `B`) — same code, the phase decides. | hand index | 44× |
| `5` | Play a card from arsenal | arsenal offset | 2× |
| `3` | Use a hero or equipment ability; in phase `B` it is how equipment blocks | equipment offset | 17× |
| `14` | Play a card from your own banish | banish offset | 16× |
| `15` | Play a card from the opponent's banish | their banish offset | — |
| `22` | Use an aura ability | aura offset | 4× |
| `36` | Play a card from the graveyard | discard offset | — |
| `35` | Play a card from the deck (heroes that allow it) | deck offset | — |
| `21` | Use an ability of a card on the combat chain | chain offset | — |
| `10`, `24`, `25`, `34` | Item / ally / landmark / permanent ability | zone offset | — |
| `37`, `38`, `39` | Opponent's arsenal, past chain link, past defending card | offset | — |
| `4` | Put a card into arsenal (phase `ARS`) | **card name** | 2× |
| `6` | Choose a card in the pitch zone (phase `PDECK`) | **card name** | 5× |

### Answering prompts

| mode | Meaning | Argument | Observed |
|---|---|---|---|
| `99` | Pass priority / pass the phase. Gated by `canPassPhase`. | — | 278× |
| `20` | Yes/no answer (phase `YESNO`) | `buttonInput=YES\|NO` | 3× |
| `17` | Press one of the offered buttons (phase `BUTTONINPUT`, also `CHOOSEARCANE`, `CHOOSEFIRSTPLAYER`, `CHOOSETRIGGERS`, `ARSENALORHEAVE`) | `buttonInput=<label>` | 5× |
| `16` | Choose one option from a decision-queue prompt, including `CHOOSEMULTIZONE` / `MAYCHOOSEMULTIZONE` | `cardID=<multizone index>` | 15× |
| `19` | Multi-select variant of the above (`MINCOUNT`/`MAXCOUNT` limits) | `cardID=<comma list>` | — |
| `7` | Numeric input (`CHOOSENUMBER`, `NUMBERINPUT`, `DYNPITCH`) | `buttonInput=<n>` | — |
| `30` | String input | `buttonInput=<string>` | — |
| `23` | `CHOOSECARD` prompt | `cardID` | — |
| `8`, `9` | Deck top / bottom placement (`OPT`, `CHOOSETOP`, `CHOOSEBOTTOM`) | `cardID` | — |
| `11`, `12`, `13`, `29` | `CHOOSEDECK`, `HANDTOP`, `HANDBOTTOM`, `CHOOSETOPOPPONENT` | `cardID` | — |
| `31`, `32`, `33` | Move a layer deeper, move it up, fully reorder layers | `cardID`/body | — |

Button labels use underscores for spaces: the capture contains
`buttonInput=No_search` for the button captioned "No search". Send the
`buttonInput` string from the state verbatim; do not re-space it.

### Control and convenience

| mode | Meaning | Observed |
|---|---|---|
| `100` | Break chain — pass priority in the resolution step | — |
| `101` | Pass block **and** reactions (offered as a button in phase `B`) | — |
| `102`, `103`, `104` | Toggle equipment / own permanent / opponent permanent active | — |
| `105`, `113` | Skip all runechants / prevent the next X runechants | — |
| `106` | Use floating resources instead of the gold alternative payment; also the mode the deck-ordering popup posts to `ProcessInputAPI.php` | — |
| `10000` | Undo | 5× |
| `10001` | Revert to the pre-block backup (the "Undo Block" button in phase `B`) | 1× |
| `10002` | Manually add action points | — |
| `10003` | Revert to a prior turn | — |
| `40`, `41` | Manual counter fiddling (Valiant Dynamo, Valda) | — |
| `26` | Change a player setting | — |

The engine validates everything: an input that does not match the current phase and a
legal target is rejected (`IsPlayable`, and the explicit `$turn[2]` whitelist for modes
`16`, `17`, `19`), often with a plain-text `... Invalid Input<BR>` body and no state
change. An agent can therefore probe safely, but should not treat a `200` as proof
that the move landed — compare `lastUpdate` instead.

---

## 7. Real examples from `captures/4/`

### 7.1 Main phase — choose an action (`0002.json`)

```json
"turnPhase": {"turnPhase": "M", "caption": "Choose an action"},
"turnNo": "0", "turnPlayer": "1", "playerAP": "1",
"playerHealth": "40", "opponentHealth": "40",
"havePriority": true, "canPassPhase": true,
"playerHand": [
  {"cardNumber": "embrace_sin_yellow",            "action": 27, "actionDataOverride": "0", "uniqueID": "h1"},
  {"cardNumber": "eloquent_eulogy_red",           "action": 27, "actionDataOverride": "1", "uniqueID": "h2"},
  {"cardNumber": "open_the_gate_to_iarathael_red","action": 27, "actionDataOverride": "2", "uniqueID": "h3"}
]
```

The input that followed (`transitions.jsonl`, move 2): `mode=14&cardID=0` — playing
`shadow_of_ursur_blue` out of banish, which was listed in `playerBanish` with
`action: 14`.

### 7.2 Pitch (`0001.json`)

```json
"turnPhase": {"turnPhase": "P", "layer": "grasp_of_the_arknight",
              "caption": "Choose a card to pitch"},
"playerPrompt": {"helpText": "Choose a card to pitch (0 of 2)",
                 "buttons": [{"mode": 10000, "caption": "Cancel"}]},
"playerHand": [
  {"cardNumber": "embrace_sin_yellow",             "action": 27, "actionDataOverride": "0"},
  {"cardNumber": "eloquent_eulogy_red",            "action": 27, "actionDataOverride": "1"},
  {"cardNumber": "become_the_shadow_lord_blue",    "action": 27, "actionDataOverride": "2"},
  {"cardNumber": "open_the_gate_to_iarathael_red", "action": 27, "actionDataOverride": "3"}
]
```

Sent: `mode=27&cardID=2`. Note that pitching uses the same code as playing — the
distinguishing signal is `turnPhase == "P"`, and `helpText` even carries the progress
("0 of 2"). The pitched card then appears in `playerPitch` with its `pitchValue`.

### 7.3 Block (`0017.json`)

```json
"turnPhase": {"turnPhase": "B", "caption": "Choose a card to block"},
"turnPlayer": "2",
"activeChainLink": {
  "attackingCard": {"cardNumber": "scar_for_a_scar_red", "controller": "2"},
  "totalPower": 4, "totalDefense": 0,
  "attackTarget": "Viserai, the Forsaken",
  "dominate": false, "overpower": false, "goAgain": false
},
"playerHand": [
  {"cardNumber": "embrace_sin_yellow",      "action": 27, "actionDataOverride": "0"},
  {"cardNumber": "runechant_of_lust_yellow","action": 0,  "actionDataOverride": "1"}
],
"playerEquipment": [
  {"cardNumber": "face_purgatory",        "action": 3, "slot": "Head"},
  {"cardNumber": "grasp_of_the_arknight", "action": 3, "slot": "Arms"}
],
"playerPrompt": {"buttons": [
  {"mode": 10001, "caption": "Undo Block"},
  {"mode": 99,    "caption": "Pass"},
  {"mode": 101,   "caption": "Pass Block and Reactions"}
]}
```

This is the clearest example of `action` encoding legality: only one of the four hand
cards is blockable (the runechants are not), and the equipment that can block carries
`action: 3`. Across the capture, blocks were sent as 13 `mode=27` and 5 `mode=3`.

### 7.4 Reaction window (`0005.json`)

```json
"turnPhase": {"turnPhase": "INSTANT", "layer": "FINALIZECHAINLINK",
              "caption": "Choose an instant"},
"havePriority": true, "canPassPhase": true,
"layerDisplay": {"layerContents": [{"cardNumber": "FINALIZECHAINLINK", "controller": "1"}],
                 "reorderableLayers": [{"layerID": 0, "isReorderable": false}]},
"playerHand": [{"cardNumber": "embrace_sin_yellow", "action": 0}],
"playerPrompt": {"helpText": "Choose an instant", "buttons": []}
```

Nothing is playable (`action: 0` everywhere) and no buttons are offered, yet
`canPassPhase` is true. The only legal action is `mode=99`. This shape accounts for
most of the capture, and an agent that does not pass promptly here simply stalls.

### 7.5 Yes/no decision (`0169.json`)

```json
"turnPhase": {"turnPhase": "YESNO", "layer": "PRETRIGGER",
              "caption": "Choose if you want to forsake your throne"},
"playerPrompt": {"helpText": "Do you wish to forsake your throne?", "buttons": []},
"playerInputPopUp": {
  "active": true,
  "popup": {"id": "YESNO", "title": "Do you wish to forsake your throne?",
            "sourceCard": "seven_sin_nebula", "cards": []},
  "buttons": [
    {"mode": 20, "buttonInput": "YES", "caption": "Yes"},
    {"mode": 20, "buttonInput": "NO",  "caption": "No"}
  ]
}
```

Sent: `mode=20&buttonInput=YES`. Note the buttons live on
`playerInputPopUp.buttons`, while `playerPrompt.buttons` is empty — a popup moves the
choices out of the prompt.

### 7.6 Target selection across zones (`0003.json`)

```json
"turnPhase": {"turnPhase": "CHOOSEMULTIZONE", "layer": "PRETRIGGER", "caption": "Choose a card"},
"playerPrompt": {"helpText": "Banish a card with blood debt to give this attack go again?"},
"playerInputPopUp": {
  "active": true,
  "popup": {"id": "CHOOSEMULTIZONE", "cards": [
    {"cardNumber": "eloquent_eulogy_red",            "action": 16, "actionDataOverride": "MYHAND-1"},
    {"cardNumber": "open_the_gate_to_iarathael_red", "action": 16, "actionDataOverride": "MYHAND-2"},
    {"cardNumber": "SKIP",                           "action": 16, "actionDataOverride": "CARDID-SKIP"}
  ]}
}
```

Sent: `mode=16&cardID=MYHAND-1`.

Multizone references are `{ZONE}-{offset}` relative to the acting player, exactly as
described in the backend's `CLAUDE.md`. Values seen in the capture: `MYHAND-0..2`,
`MYAURAS-15`, `MYAURAS-30`, `MYBANISH-12`, `MYBANISH-27`, `MYDECK-11`, `MYDECK-22`,
`THEIRHAND-0`, and the pseudo-card `CARDID-SKIP` for declining. Declining is offered as
a card in the list, not as a button.

### 7.7 Button choice (`0046.json`)

```json
"turnPhase": {"turnPhase": "BUTTONINPUT", "layer": "ATTACKSTEP", "caption": "Choose a button"},
"playerInputPopUp": {
  "active": true,
  "popup": {"id": "BUTTONINPUT",
            "title": "Search your deck for a runechant to play",
            "sourceCard": "sinspeaker_gloomblade_red"},
  "buttons": [
    {"mode": 17, "buttonInput": "Search",    "caption": "Search"},
    {"mode": 17, "buttonInput": "No_search", "caption": "No search"}
  ]
}
```

Sent: `mode=17&buttonInput=Search`, and the next state was a `MAYCHOOSEMULTIZONE`
offering `MYDECK-22` — a two-step prompt chain, which is the normal pattern: a button
choice that then needs a target.

---

## 8. A minimal agent loop

```python
state = get_state(game, seat, auth, last_update=0)
while state["turnPhase"]["turnPhase"] != "OVER":
    if state["havePriority"]:
        action = choose(legal_actions(state))   # section 4
        send_input(game, seat, auth, **action)
    state = get_state(game, seat, auth, last_update=state["lastUpdate"])
```

Two things that bite:

- The poll returns the body `0`, not JSON, when nothing has changed. Keep the previous
  state in that case.
- A single logical play can need several inputs in a row (play → pitch → target →
  button). Do not assume one action per turn; loop until `havePriority` goes false or
  the phase changes.

---

## Open questions

1. **`havePriority` was never `false` in this capture.** The bot answers instantly, so
   every observed state had priority on seat 1. Whether an agent will see
   `havePriority: false` states in a human game, and for how long, is untested here.

3. **`mode=0` with `cardID=undefined`.** Three identical inputs appear during
   `ORDERTRIGGERS` (state 96), and `NetworkingLibraries.php` has no `case 0`. The state
   did advance afterwards. Is this a no-op the frontend sends on click, with the real
   ordering going through `ProcessInputAPI.php`? The intended way for an agent to order
   triggers is not established — modes `31`/`32`/`33` exist for layer reordering but were
   never observed.

4. **`ProcessInputAPI.php` payloads.** 12 inputs in the capture went through the JSON
   POST endpoint and their arguments are not recoverable from the access log. The
   deck-ordering case is understood from the frontend source (`mode: 106` with
   `submission: {cardListTop, cardListBottom}`), but the full set of inputs that require
   this endpoint is not mapped. Capturing it properly needs a proxy or request-body
   logging, not the access log.

5. **Who resolved the `OPT` states?** States 256–258 advanced with no input of ours in
   the log. The likely answer is item 4 — the ordering was posted to
   `ProcessInputAPI.php` — but it is not proven from the capture alone.

6. **`combatChainLinks` granularity.** Resolved links appear only as
   `{"result": "no-hit", "isDraconic": false}`. Whether the cards of past links are
   retrievable from the state (as opposed to `PASTCHAINLINKS` multizone searches) is
   unresolved; the agent may need the chat log to reconstruct chain history.

7. **Zone strides are not documented in the response.** `actionDataOverride` makes the
   stride irrelevant for sending inputs, which is why the rule in section 4.2 is the one
   to follow. But any attempt to construct a `cardID` without a state to read it from
   would need the per-zone stride (`ArsenalPieces()`, `AuraPieces()`, … in the backend).

8. **Hidden-information guarantees.** `opponentHand` is card backs in the states we
   captured, but `BuildGameState.php` reveals hands when `isGameOver`, in replays, and
   for some spectator/friend combinations. An agent trained on captures should not
   assume the opponent's hand is always hidden in every state it is given.
