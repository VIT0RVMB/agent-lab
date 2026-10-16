# How much time an agent may take per decision

An agent that calls an external model will take seconds, not milliseconds, per
decision. This is what the Talishar backend does about that - first what the
code says, then what the tests showed.

Read-only investigation of `../Talishar`; nothing in the backend was changed.

---

## 1. What the code does

### The inactivity timer exists only in public games

`Libraries/SHMOPLibraries.php:220`:

```php
function InactivityTimeoutMs($cacheArr)
{
  if (!is_array($cacheArr)) return 0;
  $visibility = trim((string)($cacheArr[8] ?? ""));
  return $visibility === "1" ? INACTIVITY_TIMEOUT_MS : 0;
}
```

`INACTIVITY_TIMEOUT_MS` is **60 seconds** (`SHMOPLibraries.php:217`). Cache piece
9 (index 8) is the game's visibility, written by `APIs/CreateGame.php:166` as
`1` for public, `2` for friends-only and `0` for private.

So in a private or friends-only game the timeout is `0`, and
`BuildGameState.php:1815` then omits the fields entirely:

```php
$lastActionTime = intval($buildCacheArr[5] ?? 0);
if ($lastActionTime > 0 && $inactivityTimeout > 0) {
  $response->inactivityDeadline = $lastActionTime + $inactivityTimeout;
  $response->serverTime = intval(round(microtime(true) * 1000));
}
```

Confirmed against live states: a game created by `new_game.py` (private) answers
`inactivityDeadline: null, serverTime: null`, while the browser-created game in
`captures/4/` (public) carried `inactivityDeadline: 1792113904355` with
`serverTime: 1792113844406` - a 59,949 ms gap, i.e. the 60 s window.

### Nothing happens automatically when the deadline passes

`$response->inactive` is a display flag and nothing more. Its only consumer is
the frontend banner `OpponentInactive.tsx`. In the backend there is:

- **no automatic pass.** `AutoPassTurn` (`Libraries/PlayerSettings.php:49`) is a
  player setting for holding the pass button down, not a timer.
- **no automatic loss or draw.**
- **no automatic game closure locally.** Deletion after
  `GAME_DELETE_TIMEOUT_MS` (5 minutes) is gated on `$autoDeleteGames`, which
  `HostFiles/Redirector.php:14` sets to `false` in this environment. It is also
  lazy - it only runs when someone scans the game list
  (`Libraries/GameListScan.php:107`).

### What the deadline does enable: the opponent can claim the win

After the deadline the frontend shows a **Claim Victory** button, which sends
`mode=100007`. The handler (`Libraries/NetworkingLibraries.php:1150`):

```php
case 100007: //Claim Victory when opponent is inactive
  if ($isSimulation) return;
  $otherPlayer = $playerID == 1 ? 2 : 1;
  if (!IsGameOver()) PlayerWon($playerID);
  WriteLog("🚩 The opponent forfeited due to inactivity.");
```

**The server does not verify the deadline.** The 60 s check lives entirely in
the frontend; the endpoint grants the win to whoever asks. So against a human
opponent the risk is real and starts at 60 s of silence. Against the built-in
bot there is nobody to press it.

### Polling does not keep you active

`GetNextTurn.php:109` writes only the presence pieces for the seat
(`playerID + 1` and `playerID + 3`), not piece 6:

```php
SetCachePieces($gameName, [$playerID + 1 => $currentTime, $playerID + 3 => "0"]);
```

The timer is piece 6 (index 5), reset by `GamestateUpdated()`
(`SHMOPLibraries.php:233`), which runs when the game state actually changes -
`ProcessInput.php:134` and 14 other sites. An agent that polls while thinking is
still counted as inactive, and the deadline keeps approaching.

Note that the timer is reset by **either** player's action, since it is one
timer per game rather than per seat.

### There is an official keepalive

Two ways to reset the timer without making a move:

1. **`SubmitChat.php?quickChat=25`** ("I'm still here!"). `SubmitChat.php:163`
   forces `$resetTimer = true` for this one quick chat regardless of who sent
   it; any other chat resets the timer only when the sender holds priority
   (`SubmitChat.php:160`). This is what the banner's button uses.
   Verified live: the call returns 200 and bumps `lastUpdate` from 5 to 6.
2. **`ProcessInputAPI.php` with `mode=112`** (`ProcessInputAPI.php:137`), which
   pokes the timer for the priority player without writing gamestate. It is what
   the deck organizer uses while open.

### Not a factor

- **PHP session lifetime** (`session.gc_maxlifetime` = 1440 s). An agent
  authenticates with `authKey` in the query string and has no session to expire.
- **The action lock** (`Libraries/CoreLibraries.php:84`) is taken and released
  within a single request; it has no timeout to trip.
- **SSE** calls `set_time_limit(120)` per loop iteration, but an agent polling
  `GetNextTurn.php` never opens that stream.

---

## 2. What the tests showed

`--think-delay <seconds>` was added to `random_agent.py`: every decision that
goes through the policy waits that long before being sent. Automatic passes -
the ones where passing is the only legal action - stay instant, which matches how
a real agent behaves, since those need no model call. Each delayed decision
snapshots `inactive`, `inactivityDeadline` and `serverTime` before and after the
wait, into `runs/{game}/deadlines.jsonl`.

All three runs used games created by `new_game.py`, which are **private**, so
they exercise the engine's behaviour under a slow player but not the public
deadline path. See [the gap](#3-what-these-tests-could-not-cover).

| Delay | Game | Result | Decisions | Wall clock | Rejected inputs | `inactive` seen | Deadline seen |
|---|---|---|---|---|---|---|---|
| 5 s | 40 | finished, `OVER` on turn 9 | 96 | 410 s | 0 | never | never |
| 20 s | 41 | finished, `OVER` on turn 8 | 88 | 1,528 s | 0 | never | never |
| 60 s | 42 | finished, `OVER` on turn 10 | 104 | 5,289 s | 0 | never | never |

What is already settled by the runs:

- **A 60-second pause before every decision does not break anything.** All three
  games ran to `OVER`. The 60 s run took 104 decisions over 88 minutes and had
  zero rejected inputs: every input sent after a full minute of silence was
  accepted normally, with no state loss. The engine has no concept of a late
  input.
- **No third party ever moved for us.** Across the runs, every state change
  followed either our own input or the bot's reply. No automatic pass appeared in
  any decision log, and no decision was rejected as out of phase, which is what
  a phantom pass would have caused.
- **`inactivityDeadline` never appeared and `inactive` was never true** in 162
  observations in the 5 s run, exactly as the code predicts for a private game.
- **Response times did not degrade**: round trips stayed in the tens of
  milliseconds regardless of how long the agent had waited beforehand.

One measurement artefact worth recording: while a 50-game evaluation was running
on the same Apache, the latency runs slowed to roughly 60 s per decision at a 20 s
delay. That is local CPU contention between the test processes, not backend
back-pressure.

---

## 3. What these tests could not cover

The public-game path - the one where `inactivityDeadline` actually exists - was
**not tested**, because `APIs/CreateGame.php:53` refuses a public practice game
without a logged-in session:

```php
if ($visibility == "public" && $deckTestMode != "" && !isset($_SESSION["userid"])) {
```

I have no account on this instance and did not create one. To close this gap,
create a public practice game in the browser while logged in, take
`gameName`/`playerID`/`authKey` from DevTools, and hand it to the agent:

```bash
cd ../agent-lab
python3 random_agent.py <gameName> 1 <authKey> --think-delay 90 --on-unsupported pass
```

Then read `runs/<gameName>/deadlines.jsonl`. The expectation from the code is
that `msToDeadline` goes negative and `inactive` flips to `true` while the agent
thinks, with no other consequence. What is worth confirming is whether the
engine really stays passive once the flag is set.

---

## 4. Conclusion: the maximum safe time per decision

| Setting | Safe budget per decision | What bounds it |
|---|---|---|
| Private or friends-only game, bot opponent | **No limit from the engine** | Nothing times out. The practical bound is your own patience: at 60 s per decision a game takes over an hour, since a game is around 90 decisions and roughly 87% of them go through the policy. |
| Public game, bot opponent | **No hard limit, but stay under 60 s** | The `inactive` flag flips after 60 s. The bot cannot claim victory, so the flag is cosmetic - but the margin is free, so do not spend it. |
| Public game, human opponent | **Keep each decision under 60 s** | After 60 s the opponent gets a Claim Victory button, and `mode=100007` is granted with no server-side check of the deadline. One slow decision is enough to lose the game to a click. |
| Production-like host with `$autoDeleteGames = true` | **Under 5 minutes between state changes** | `GAME_DELETE_TIMEOUT_MS` deletes the game directory after 5 minutes without a gamestate update. |

**Recommended budget: 30 seconds per decision, with a hard ceiling of 55.** That
leaves headroom inside the 60 s window for the round trip and for a retry, and it
needs no keepalive.

For a model call that can exceed that - the 20 s worst case plus a retry, or a
deliberately slow deep-think - send the keepalive instead of risking it:

```
GET /game/SubmitChat.php?gameName=<id>&playerID=<seat>&authKey=<key>&quickChat=25
```

Fire it every ~30 s while a decision is pending. It resets the timer
unconditionally, so the opponent's Claim Victory button never appears. It does
post "I'm still here!" into the game chat, which is visible to the opponent -
acceptable for an agent, and arguably the honest thing to show.

Do not rely on polling as a keepalive. It does not reset the timer.
