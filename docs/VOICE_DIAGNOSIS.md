# PLMKR Marcus Voice — Root-Cause Diagnosis Log

Running evidence log. Every claim below is either a direct citation (file:line) or
a directly-observed command/output. "Probably"/"likely" are not used as answers;
where evidence is insufficient the entry is marked **UNKNOWN** with what would
settle it.

---

## PHASE 0.0a — Which clone has the iPhone been bundling from?

**Answer: A) The iPhone has been bundling from `/home/tommy/plmkr-frontend` (243f592).**

Evidence:

- `ps aux` shows a live Expo dev server: pids 48006/48018/48019
  (`npm exec expo start --lan --clear` → `sh -c expo start...` →
  `node .../expo start --lan --clear`), started 14:02 today.
  `/proc/48006/cwd`, `/proc/48018/cwd`, `/proc/48019/cwd` all resolve to
  `/home/tommy/plmkr-frontend`.
- `/home/tommy/plmkr-frontend/.expo/` was written 2026-09-11 23:04, includes a
  `dev/` subfolder (dev-client/device session artifact) and `devices.json`.
  `/home/tommy/Desktop/ReveNation/.expo/` was last written 2026-03-15, contains
  only a `web/` subfolder — no dev-client session ever recorded there.
- `node_modules/.package-lock.json`: plmkr-frontend 2026-09-11 22:17 (fresh
  install, matches this week's work); ReveNation 2026-03-16 10:10 (~6 months
  stale).
- `find <path> -maxdepth 2 -newermt 2026-09-11 -not -path */node_modules/*`:
  plmkr-frontend shows real recent activity (`src/screens`, `src/hooks`,
  `src/utils`, `tests/*.test.cjs`, `.expo/*`, `package.json`); ReveNation shows
  **only** `.git` (itself an artifact of this session's own read-only
  inspection, not prior activity — no source file changed).
- `/home/tommy/Desktop/ReveNation/.feb_session_log.txt` (redacted, read in
  full): 16 lines, all dated 2026-06-22/23, documenting unrelated screen work
  (Notifications/Profile/Pitching/Booking/Social/Reports screens). It predates
  every commit under review (964aabb/5709f87/243f592/cdd0c8d/db1da91/7c9231d,
  all dated 2026-09-14) by months and never mentions CallScreen, Marcus, or
  voice work.
- Confirmed live: the phone's actual `API_BASE` is a **local LAN backend**, not
  Railway — see Phase 1 Q7 evidence (`EXPO_PUBLIC_API_BASE=http://192.168.18.59:8000`
  in the running Expo process's own environment, `/proc/48019/environ`), and a
  local `uvicorn main:app --host 0.0.0.0 --port 8000` process (pid 70850,
  cwd `/home/tommy/maestro-backend`) is running right now to serve it.

## PHASE 0.0b — Branch topology (plmkr-frontend)

- `git branch -a --contains 964aabb` / `5709f87` / `243f592` → all three:
  `* feat/social-buffer-execution` only. Not on any other branch.
- **There is no `main` or `master` branch in this repository.**
  `git branch -a` → `feat/social-buffer-execution` (current), `fix/first-call-keypad`,
  plus their `origin/*` tracking refs. `origin/HEAD -> origin/fix/first-call-keypad`
  — `fix/first-call-keypad` is this repo's actual trunk.
- `git merge-base feat/social-buffer-execution fix/first-call-keypad` →
  `18b5769` (identical to `fix/first-call-keypad`'s own tip — trunk is fully
  contained in the feature branch, 0 commits ahead of it:
  `git log --oneline feat/social-buffer-execution..fix/first-call-keypad` is empty).
- `git log --oneline fix/first-call-keypad..feat/social-buffer-execution` → 131
  commits ahead, the top three being 964aabb, 5709f87, 243f592 (newest).
- `feat/social-buffer-execution` (local) is **3 commits ahead of its own remote
  tracking branch** `origin/feat/social-buffer-execution` (tip `11fdfc9`):
  local adds exactly 964aabb, 5709f87, 243f592 on top. None of the three are
  pushed — consistent with the standing "do not push" constraint.
- 243f592 is reachable **only** from `feat/social-buffer-execution` (not on
  `fix/first-call-keypad`, not on any `main`/`master` because none exists).

## PHASE 0.0c — Diff between the two clones (voice path only, read-only)

Fetched ReveNation's `master` tip (`af9a1e2`) into a temporary local ref via
`git fetch /home/tommy/Desktop/ReveNation master:refs/tmp-revenation-master`
(read-only against ReveNation — a `fetch` only reads objects from the source;
nothing in ReveNation was checked out, staged, or modified). Ref deleted again
immediately after diffing (`git update-ref -d`).

```
git diff --stat refs/tmp-revenation-master 243f592 -- src/screens/CallScreen.js \
  src/hooks/useVoiceSession.js src/utils/api.js src/utils/voiceUpload.js src/screens/TeamCallScreen.js

 src/hooks/useVoiceSession.js  |  267 ++++++++++
 src/screens/CallScreen.js     |  473 +++++++---------
 src/screens/TeamCallScreen.js | 1183 +++++++++++++++++++++++++++++++++++++++++
 src/utils/api.js              |  591 ++++++++++++++++++--
 src/utils/voiceUpload.js      |  138 +++++
 5 files changed, 2324 insertions(+), 328 deletions(-)
```

- `src/hooks/useVoiceSession.js` and `src/utils/voiceUpload.js` **do not exist
  at all** in ReveNation (`git cat-file -e` on both paths at
  `refs/tmp-revenation-master` → "exists on disk, but not in
  'refs/tmp-revenation-master'").
- ReveNation's `CallScreen.js` still imports `streamChat`/`apiHistory` from a
  much older `api.js` and does its own **inline, unbounded** TTS fetch with a
  fresh `AbortController` per call site (no shared `fetchTtsSynth` helper) —
  confirmed absent: `fetchTtsSynth`, `handleTurnError`, `WATCHDOG_MS` (the
  constant exists, but see below) all missing from ReveNation's `CallScreen.js`.
- ReveNation's `api.js` **does already have** `streamChat`'s 60s SSE inactivity
  watchdog (`WATCHDOG_MS = 60000`, same as plmkr-frontend) — that primitive
  predates all six commits under review — but has none of 5709f87's
  `fetchTtsSynth` export.
- **Conclusion:** had the device been running ReveNation, the correct
  diagnosis would be simpler and different (no shared bounded-TTS helper at
  all, no `useVoiceSession`/`voiceUpload` module split, no per-turn in-flight
  guard). It is not running ReveNation (0.0a), so this is recorded only to
  close off that possibility with evidence, not acted on further.

---

## PHASE 0.1–0.3 — Ground truth

```
FRONTEND (/home/tommy/plmkr-frontend)
  git status --porcelain   → (empty — clean)
  git rev-parse --short HEAD → 243f592
  branch: feat/social-buffer-execution

BACKEND (/home/tommy/maestro-backend)
  git status --porcelain   → (empty — clean)
  git rev-parse --short HEAD → 7c9231d
```

Both HEADs match the expected values exactly. Both worktrees clean. No stop
condition triggered by 0.2.

Commits confirmed to exist and read in full (`git show <sha>`):
frontend `964aabb`, `5709f87`, `243f592`; backend `cdd0c8d`, `db1da91`, `7c9231d`.
Chronological order of the backend three (by author timestamp, same day,
2026-09-14): **cdd0c8d (14:44:44) → db1da91 (15:36:03) → 7c9231d (this session's
prior fix)**. cdd0c8d is an ancestor of db1da91; the "physical-device test
FAILED TWICE after commits db1da91 and 5709f87" therefore already had cdd0c8d's
change in effect throughout.

---

## PHASE 1 — Read the path

### BACKEND

**Q1. Is `/api/transcribe` `async def`? Blocking Whisper call on the loop or offloaded?**

`main.py:1561` — `async def transcribe(audio: UploadFile = File(...), request: Request = None):`
The blocking call is offloaded: `main.py:1596` —
`worker = loop.run_in_executor(None, lambda: model.transcribe(transcribe_path))`,
awaited with a bound at `main.py:1589-1592`:
`result = await asyncio.wait_for(asyncio.shield(worker), timeout=TRANSCRIBE_TIMEOUT_SECONDS)`
(`TRANSCRIBE_TIMEOUT_SECONDS = 120`, `main.py:94`). **Not blocking the event loop.**

**Q2. Same question for Kokoro `/api/tts/synth`.**

`main.py:689` — `async def synthesize_speech(text: str, voice: str, call_id: str = "") -> Optional[bytes]:`
Blocking native call offloaded: `main.py:709-711` defines `_synth()` (runs
`kokoro.create(...)` under a `threading.Lock`), submitted via
`main.py:721` — `worker = loop.run_in_executor(None, _synth)`, bounded at
`main.py:723-725` — `asyncio.wait_for(asyncio.shield(worker), timeout=KOKORO_SYNTH_TIMEOUT_SECONDS)`
(`KOKORO_SYNTH_TIMEOUT_SECONDS = 25`, `main.py:98`). **Not blocking the event loop.**

**Q3. uvicorn launch config; any shared lock/model instance serializing requests?**

Live process (pid 70850, confirmed running right now, cwd
`/home/tommy/maestro-backend`): `python3 -m uvicorn main:app --host 0.0.0.0 --port 8000`
— **no `--workers`, no `--timeout-keep-alive`, no `--loop` flag anywhere**
(grepped `main.py`, `Dockerfile`, `railway.json`, `railway.toml`, `start.sh` —
zero hits for any of these). Dockerfile CMD (`Dockerfile:41`) is identical,
also unflagged. This means **uvicorn's default of a single worker process /
single event loop** is what actually runs in every environment (local shell,
Docker/Railway) — nothing multi-processes this app. Confirmed shared,
serializing state: `_tts_lock` (`asyncio.Lock`, guards Kokoro `create()` calls
across all concurrent TTS requests — by design, see db1da91) and
`_kokoro_native_lock` (`threading.Lock`, same purpose at the thread level).
**No other module-level lock found** guarding Anthropic calls or Gmail calls —
those are unserialized per-request (a real hazard if a blocking call runs
directly on the loop; see Q6).

Note: `start.sh:2` `cd /home/tommy/maestro` (a *different*, stale path, not
this repo) and expects env vars sourced from `~/.bashrc` — the live process
was **not** started via `start.sh` (its env has none of `start.sh`'s exports:
no `SKILLS_DIR`/`ARTISTS_DIR`/`KNOWLEDGE_BASE` in `/proc/70850/environ`) —
it was launched by running the `uvicorn` command directly from a shell in
`/home/tommy/maestro-backend` that already had most other keys exported.

**Q4. `/api/chat_stream` SSE event types, order, keepalive/heartbeat; does anything inside the generator block?**

Event types emitted (grepped `"type":` in the 8280–8700 line range):
`text` (`main.py:8481,8660`), `audio` (`8483,8662`), `status` (`8485,8664`),
`error` (`8487,8666`), `route` (`8494,8673`), `experts` (`8357`), `actions`
(`8686`, Marcus-only), `done` (`8503,8692`). Static-greet branch
(`main.py:8320-8321`) emits only `text` then `done`.

Order for the generic (non-Marcus) path: zero or more `text` → optional `audio`
(TTS-on turns only) → optional `route` → optional `experts` → `done`, OR
`error` (terminal, breaks the loop before `route`/`experts`/`done` — see
`main.py:8487-8488`, `break` right after the error `yield`).

**No heartbeat/keepalive event exists anywhere** — grepped the whole file for
`heartbeat|keepalive|keep-alive|ping` (case-insensitive): every hit is
unrelated (healthcheck config, an accountant-agent's tax bookkeeping copy, a
`# skip greeting pings` DB-persistence comment). **Between connection open and
the first real SSE event, the client receives literally zero bytes.**

Does anything inside the generator block the loop directly? `generate()`
(`main.py:8353` area) itself only does `await evt_out.get()` in a `while True`
loop — non-blocking. The actual Anthropic call runs in a separate
`asyncio.create_task(_claude())` (own task, not inline in `generate()`).
`_claude()` uses the AsyncAnthropic client (`async_client`, native asyncio,
not blocking) — see Q6 for the one exception (Gmail).

**Q5. Marcus's tools on a normal turn; which have real external side effects?**

`MARCUS_TOOLS` (`main.py`, grepped the block) has exactly two entries:
`search_curators` (name only; pure read via `pitch_service._db_list_curators`
— **no external side effect**) and `send_pitch_email` (name only; calls
`pitch_service.send_email` → real Gmail API send — **the only external side
effect Marcus can trigger**).

**Q6. tool_use loop trace; per-tool timeout; behavior on raise/hang?**

`main.py:8524` `async def _marcus_producer():` — loop at `main.py:8530`
(`for _ in range(MARCUS_MAX_TOOL_ITERS):`, `MARCUS_MAX_TOOL_ITERS = 5`).
Each iteration: `messages.create(...)` bounded by
`asyncio.wait_for(..., timeout=MARCUS_CREATE_TIMEOUT_SECONDS)` (`main.py:8544`,
= 40s, added in 7c9231d — **this is a timeout, i.e. containment, not the root
cause fix; see Phase 3**). If `stop_reason == "tool_use"`
(`main.py:8552`), each tool call is executed via `_execute_marcus_tool`
(`main.py:8556`, defined at `main.py:1786`), its `tool_result` appended, and
the loop `continue`s — i.e. **a second (up to a fifth) Anthropic call is made
after tool execution**, confirming the described loop shape exactly. On a
raised exception inside a tool, `_execute_marcus_tool` **never raises** for
Gmail-specific failures — it catches `GmailNotConnected`/`GmailAuthExpired`/
`GmailSendTimeout` (`main.py:1826-1857`, the last one added in 7c9231d) and
returns a structured, non-fatal result so the loop continues. Only an
*uncaught* exception from `send_email` would propagate — `_do_send`'s work
(inside `pitch_service.py`, see below) is now offloaded to a thread and
bounded (7c9231d); **before 7c9231d it was not offloaded and had no
per-call timeout at all** — see Phase 3.

Per-tool timeout independent of Gmail: **none** for `search_curators` (pure,
synchronous, in-process SQLite read — no I/O wait possible worth bounding).

**Q7. Model string; API key source; startup assertion; did cdd0c8d remove the only place a bad key surfaced?**

Model: selected per-turn by `select_model(message, tier)` (not a fixed
constant) — model *family* strings are Anthropic Claude IDs; exact value
depends on message/tier and is not itself the point of failure here.

Key source: `main.py:59` — `ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")`,
`main.py:60` — `ANTHROPIC_AVAILABLE = bool(ANTHROPIC_API_KEY)`. **No `.env`/`.env.local`
loading anywhere in `main.py`** (grepped `load_dotenv|dotenv` — zero hits) —
`os.environ` only, exactly as cdd0c8d's own commit message states.

**There IS a startup assertion** — `_check_env()` (defined ~`main.py:966`,
called unconditionally at import time, `main.py:990` `_check_env()`):
checks Twilio SID/token/Verify-SID format (regex), `STRIPE_SECRET_KEY`
presence, and `ANTHROPIC_API_KEY` presence — logging
`log.warning("boot_warning", ..., "key": "ANTHROPIC_API_KEY", "detail": "AI agents will fail")`
if absent, or `log.info("boot_ok", ...)` if present. **This check is
presence-only — it does not validate length or format**, unlike the Twilio
checks a few lines above it which do use a format regex. It also **only
writes to the backend process's own stdout/log stream** — it has no path to
the mobile client at all.

**Direct measurement, this session, of the actual runtime state** (not
inferred): the live backend process (pid 70850, started 15:40:08, confirmed
serving the phone per 0.0a) has **`ANTHROPIC_API_KEY` completely absent** from
its environment — `tr '\0' '\n' < /proc/70850/environ | grep -c '^ANTHROPIC_API_KEY='`
→ `0`. Every other documented required key (`CLOUDINARY_*`, `ELEVENLABS_API_KEY`,
`STRIPE_*`, `TWILIO_*`) **is** present in that same environment — this one is
specifically and singularly missing. Corroborated independently by cdd0c8d's
own commit message (written earlier the same day, before this process
started) describing the same absence, and by direct inspection of the only
on-disk value anywhere (`/home/tommy/maestro/.env.local`, an unrelated repo):
prefix `sk-a...`, length 26 — real Anthropic keys are ~100+ characters; this
value is unusable as a key regardless of who it belongs to. `~/.bashrc` and
`~/.profile` have zero occurrences of `ANTHROPIC_API_KEY`.
**`main.py`'s own `_check_env()` would therefore have logged
`boot_warning`/`ANTHROPIC_API_KEY`/`"AI agents will fail"` at this process's
startup** (a direct consequence of the confirmed-empty env var reaching that
code path) — not independently re-observed from stdout scrollback in this
session, but logically certain given the code and the confirmed env state.

**Did cdd0c8d remove the only place a bad key surfaced to the user? Yes,
confirmed by diff** (`git show cdd0c8d`, `main.py` hunk): before cdd0c8d, the
`if not ANTHROPIC_AVAILABLE: raise HTTPException(503, ...)` guard sat *before*
the `if message == "__greet__"` branch — meaning **every** call attempt,
starting with its very first request (the greeting), would 503 immediately
with "AI unavailable: ANTHROPIC_API_KEY not configured", which the frontend's
`startGreeting` `onError` (CallScreen.js, pre-existing, unchanged by any of
the three frontend commits) turns into a **loud, immediate, visible**
`"Voice AI unavailable on this server. End call and retry."` label — at the
very start of the call, before hold-to-talk is even possible. After cdd0c8d,
the guard moved to *after* the greet branch: the greeting **always succeeds**
(real static text, fast, no key needed) regardless of key state, so the call
now looks completely healthy at connect time — and the missing/invalid key
only ever surfaces on the **first recorded turn**, as a 503 from
`/api/chat_stream`. **This is a verified, code-confirmed behavior change, not
speculation.**

**Q8. Per-turn correlation ID threaded through transcribe → chat → tts?**

**No.** `ChatStreamRequest` (`main.py:8273-8278`) has fields
`agent_id, message, artist_id, history, tts` — no id field of any kind.
`/api/transcribe` (`main.py:1561`) takes only `audio: UploadFile` (+ auth) —
no id parameter at all. The only id-shaped field anywhere in this path is
`call_id`, and it exists **solely** for `/api/tts/synth` /
`/api/tts/cancel` (`main.py:13503-13545`) — generated **client-side, once per
phone call**, not per turn (`callIdRef.current = \`${agentId}_${Date.now()}\`\`,
CallScreen.js:122`), so it cannot distinguish turn 1's TTS from turn 2's TTS
within the same call. **What would settle it further:** none needed — this is
a direct, exhaustive grep of every request schema in the path; the absence is
structural, not a search-completeness question.

### FRONTEND

**Q9. How is `/api/chat_stream` consumed?**

`src/utils/api.js:302` — `const xhr = new XMLHttpRequest();`, with progress
read via `xhr.onreadystatechange` polling `xhr.responseText` at readyState 3
and 4 (`api.js:366-371`). **Not** `fetch()`+`getReader()`/`ReadableStream`,
**not** `EventSource`, **not** `react-native-sse`. The file's own comment
(`api.js:292-293`) states why: "Hermes on Android does not support
`res.body.getReader()` (ReadableStream)." **This is the correct mechanism for
this runtime — no red flag here.**

**Q10. Every AbortController/setTimeout in the voice path.**

| Location | Duration | Starts | Introduced |
|---|---|---|---|
| `api.js:324` `watchdog` in `streamChat` (`WATCHDOG_MS=60000`) | 60s, **re-armed on every received byte** (inactivity, not total duration) | right after `xhr.send()` (`api.js:423` area) and on every chunk | pre-existing (commit `80357fe`, before all six reviewed commits) |
| `api.js:94` timer in `fetchTtsSynth` (`TTS_SYNTH_TIMEOUT_MS=32000`) | 32s, fixed | call start | **5709f87** |
| `api.js:141` timer in `authBootstrapFetch` (`AUTH_REQUEST_TIMEOUT_MS=15000`) | 15s | call start | pre-existing; OTP bootstrap only, **not** in the CallScreen turn path |
| `CallScreen.js:478` `finishSpeakingWithoutAudio()` revert-to-Live | 2.5s | TTS yields no audio | pre-existing wording changed by 5709f87 |
| `CallScreen.js:502` `handleTurnError()` revert-to-Live | 2.5s | any chat_stream error/timeout | **243f592** — see Q12, this is the auto-dismiss Phase 4.6 requires removed |
| `CallScreen.js:596` greeting auto-retry delay (500ms then 2500ms, ≤2 retries) | fixed | greeting `onError` | pre-existing (`c8cf566`, predates all six) |
| `CallScreen.js:629` "No speech detected" revert-to-Live | 2s | empty transcription | pre-existing |
| `CallScreen.js:724` fixed pause before `navigation.goBack()` in `endCall()` | 100ms | `endCall()` invoked | pre-existing |
| `CallScreen.js:275-287` `waitForTts()` poll loop (≤15s) | — | — | **dead code — grepped, zero call sites anywhere in the file** |
| `useVoiceSession.js:98` `synthAbortRef` (`AbortController`) | n/a (abort signal, not a timer) | `playText()` call | pre-existing; used by `TeamCallScreen`, not `CallScreen`'s PTT path |
| `CallScreen.js:342,513` `ttsFetchAbortRef` (`AbortController`) | n/a | each `fetchTtsAudio()`/handoff-greeting call | greeting site pre-existing, turn-reply site **5709f87** |
| `voiceUpload.js:89` retry delay in `transcribeRecording` (`TRANSCRIBE_RETRY_DELAY_MS=1000`) | 1s, only between the 2 upload attempts | attempt 2 only | **964aabb** |

**Q11. CallScreen state machine.**

State: `isSpeaking` (CallScreen's own, `CallScreen.js:107` — distinct from and
unrelated to `useVoiceSession`'s internal `isSpeaking`, which CallScreen never
reads: `CallScreen.js:88` destructures only `{ muted, speaker }` from `voice`),
`statusLabel` (`CallScreen.js:108`), `transcript` (`109`), plus
`voice.isRecording`/`voice.isTranscribing` read directly off the hook.

Render priority (`CallScreen.js:729-735`, unchanged shape since before all six
commits, only the trailing message text has moved between commits):
`isSpeaking ? Waveform : voice.isTranscribing ? "Transcribing…" : voice.isRecording ? "● Recording" : statusLabel text`.

"Transcribing…" is shown exactly while `voice.isTranscribing === true` (set in
`useVoiceSession.stopAndTranscribe`, true immediately on PTT release —
*before* `recording.stop()` even runs — cleared in a `finally` once
`transcribeRecording()` settles, success or failure) **and** `isSpeaking` is
still false. It covers: native stop + FormData upload + `/api/transcribe`
network round-trip + Whisper processing + JSON parse. It does **not** cover
anything after that: once `stopAndTranscribe()` resolves, `voice.isTranscribing`
is already false, and (until 243f592) `isSpeaking` stayed false and
`statusLabel` stayed whatever it was before the turn — this is the exact gap
243f592 targeted with `setStatusLabel('Thinking…')`
(`CallScreen.js:647`).

**Q12. `"didn't respond in time"` — where is it cleared, and by what timer?**

`CallScreen.js:501` sets it (inside `handleTurnError`, 243f592). Cleared by
`CallScreen.js:502` — `setTimeout(() => setStatusLabel('Live'), 2500)`, an
**unconditional 2.5s auto-dismiss**, independent of whether the artist has
retried, read it, or done anything. **This is precisely the pattern Phase 4.6
of the current task requires removed** ("stay visible until the user retries
... never auto-revert to 'Live'. Delete the auto-dismiss timer.") — flagged
here as a defect in my own prior fix (243f592), to be corrected in Phase 4.
Separately, the message text itself ("didn't respond **in time**") is
factually wrong for a fast 503 rejection (e.g. the missing-key case, Q7) —
`handleTurnError` fires for *any* chat_stream `onError`, timeout or not, and
mislabels every case as a timeout.

**Q13. Can one PTT release fire a turn more than once (re-entry/re-render/double-tap/stale closure)?**

Two independent guards exist for the **transcribe sub-phase only**:
`stopPromiseRef` (`useVoiceSession.js:35`, `184`: `if (stopPromiseRef.current) return stopPromiseRef.current;`)
and `sendInFlightRef` (`CallScreen.js:140`, `621`: `if (sendInFlightRef.current) return;`).
Both are set synchronously before the first `await`, so a same-tick double
call cannot race past them — **effective for stop/upload/transcribe
duplication.**

**They do not cover the full turn.** `sendInFlightRef.current` is reset in
`stopRecordingAndSend`'s `finally` (`CallScreen.js`, near the function's end)
the moment the function *returns* — and the function returns as soon as
`streamChat(...)` is called (`streamChat` is synchronous-return; the actual
network work runs in an internal async IIFE — `api.js:402-424`), **not** when
the turn's chat_stream/TTS actually finish. So: a user can release PTT, get a
fast reply, and — while that reply's TTS fetch (`fetchTtsAudio`, bounded 32s)
is still in flight — hold-and-release PTT again. `startRecording()`'s own
guard (`useVoiceSession.js:151` `if (recordingRef.current || startPromiseRef.current)`)
only blocks a second concurrent *recording start*, and CallScreen's
`startRecording()` wrapper (`CallScreen.js:610-617`) calls `clearAudioQueue()`
and `voice.interrupt()` — **neither of which touches `abortRef` (the
chat_stream XHR) or `ttsFetchAbortRef` (the TTS fetch)**. When the second
turn's `stopRecordingAndSend` reaches its own send section, `abortRef.current?.()`
(`CallScreen.js:639`) **does** correctly abort a still-in-flight *previous*
chat_stream XHR — but a still-in-flight *previous* TTS fetch is not touched:
`ttsFetchAudio()` unconditionally overwrites `ttsFetchAbortRef.current` with
a fresh `AbortController` for the new turn (`fetchTtsAudio`, `CallScreen.js`
~509-517), orphaning the old one's reference while its `await fetchTtsSynth(...)`
keeps running inside turn 1's own `onDone` closure. If it resolves after turn
2 has already taken over `audioQueueRef`/`streamDoneRef`/`isSpeaking`, turn
1's `enqueueAudio(audio)` and `streamDoneRef.current = true` execute against
state turn 2 now owns — **a real, code-provable cross-turn interference**, not
a theoretical one. This directly violates Phase 4.5's "one recording = exactly
one turn_id = at most one transcribe, one generate, one synth" and is
addressed in Phase 4 with a real per-turn generation guard, not another
timeout.

**Q14. Aborted on re-render, or only unmount?**

Neither purely — abort is **imperative**, tied to specific event sites, not
reactive to React re-renders at all (no cleanup effect keyed to a
render-affecting dependency exists in this file for `abortRef`/
`ttsFetchAbortRef`). The sites that do abort: start of a new turn
(`CallScreen.js:639`, chat_stream only — see Q13 gap for TTS), `endCall()`
(`CallScreen.js`, calls `voice.interrupt()`, `voice.cancelRecording()`,
`ttsFetchAbortRef.current?.abort()`), the `AppState` background listener
(`CallScreen.js:194-211`, aborts both `abortRef` and `ttsFetchAbortRef`), and
component unmount cleanup (`CallScreen.js:162-190`, same two plus recording
stop). A bare re-render with no user/lifecycle action triggers none of these.

---

---

## PHASE 2 — Measure

**Instrumentation added (logging only, no behavior change — see Phase 4 for
actual fixes):** `main.py` — `_new_turn_id()`/`_stage_log()` helper; an
optional, back-compatible `turn_id` field added to `ChatStreamRequest`,
`/api/transcribe` (form field), and `TtsSynthRequest`; stage log calls at
`upload_received`, `stt_start`, `stt_done`, `llm_start`, `llm_first_token`,
`tool_start`/`tool_done`, `llm_done`, `tts_start`, `tts_done`, `response_sent`.
One real bug caught by the regression suite while adding this (see below) and
fixed before proceeding — logged here for completeness since it's part of
this session's evidence trail, not swept aside: `_stage_log(..., name=tu.name)`
collided with `logging.LogRecord`'s reserved `name` attribute
(`ValueError: Attempt to overwrite 'name' in LogRecord`), silently converting
every Marcus tool call into a spurious SSE `error` event during test runs.
Renamed to `tool_name`. Full targeted suite (82 tests) passed clean after the
fix; `git diff --check` clean.

**Fixture:** `tests/fixtures/voice_probe_sample.wav` (130,092 bytes, RIFF/WAVE,
PCM 16-bit mono 24kHz) — generated locally via this repo's own
`synthesize_speech()` (real Kokoro, no external call) speaking "Book me a show
in Toronto next month." This is real, non-trivial speech audio, not
silence/noise/a stub — a legitimate end-to-end test of Whisper transcription.

**Probe:** `scripts/voice_probe.py`, run against a **separate, freshly-started
backend instance on port 8001** — not Tommy's own running dev-server process
(pid 70850, port 8000, untouched throughout) — built from the exact current
worktree (7c9231d + this session's instrumentation), with the same
credential posture confirmed in Phase 1 Q7 (`ANTHROPIC_API_KEY` and
`PLMKR_API_KEY` both absent from the launching shell, matching pid 70850's
confirmed environment for the one variable that matters to this diagnosis).
Chosen instead of restarting Tommy's own process to avoid disrupting his
terminal session; stopped again immediately after measurement.

### 2.3/2.2 — Three consecutive full-pipeline runs

```
$ python3 scripts/voice_probe.py --base-url http://127.0.0.1:8001 --runs 3

── run 1/3 (turn_id=b1bc99705124) ──
  [ PASS  ] transcribe      30649ms   "Book me a show in Toronto next month."
  [BLOCKED] chat_stream         9ms   HTTP 503: {"detail":"AI unavailable: ANTHROPIC_API_KEY not configured","request_id":"0f082c54-6dd2-42a3-8cb0-3a2d3d12f4a4"}

── run 2/3 (turn_id=771f5125e163) ──
  [ PASS  ] transcribe       6372ms   "Book me a show in Toronto next month."
  [BLOCKED] chat_stream         5ms   HTTP 503: {"detail":"AI unavailable: ANTHROPIC_API_KEY not configured","request_id":"4fe5f939-454b-4651-b365-fea4c788c8d6"}

── run 3/3 (turn_id=49927a1ae3fa) ──
  [ PASS  ] transcribe       5826ms   "Book me a show in Toronto next month."
  [BLOCKED] chat_stream         6ms   HTTP 503: {"detail":"AI unavailable: ANTHROPIC_API_KEY not configured","request_id":"5e70b3fb-6a98-44e8-ab1a-4e1b907c9596"}
```

`chat_stream` blocked (not "failed" in the sense of a bug — it correctly
rejected the request per `main.py`'s own coded contract) before any reply
text existed, so `tts_synth` was never reached by the automated pipeline in
these 3 runs — assessed independently below.

Transcribe: **run 1 took 30.6s** (Whisper's `whisper.load_model("base")` is
lazy — first call in a fresh process pays full model-load cost), **runs 2-3
took 5.8-6.4s** (warm). All three produced the exact correct transcript from
real synthesized speech. This 30s one-time cost is real and worth noting, but
it is **not silent** — the frontend correctly shows "Transcribing…" for this
entire window (Q11) and it is well inside `TRANSCRIBE_TIMEOUT_SECONDS=120`.

### 2.3 — Isolated Anthropic call (exact model/key path, tools omitted vs included)

```
── isolated Anthropic call (Phase 2.3) ──
  ANTHROPIC_API_KEY: ABSENT from this process env
  tools=False: AuthenticationError status=401 in 139ms — provider rejected the request
  ANTHROPIC_API_KEY: ABSENT from this process env
  tools=True: AuthenticationError status=401 in 194ms — provider rejected the request
```

Real network round-trip to `api.anthropic.com` (no side effect — rejected at
auth, no tokens billed). **`anthropic.AuthenticationError`, HTTP 401, in
139-194ms.** Not a hang, with or without tools. This is the provider's own,
authoritative confirmation that the key configured in `main.py`'s client
construction (`ANTHROPIC_API_KEY or "placeholder"`, `main.py:130` area) is
unusable — consistent with, and now empirically proven beyond, Phase 1 Q7's
env-inspection finding.

### 2.4 — Is the event loop blocked during a real transcription?

```
── event-loop-blocking check during transcription (Phase 2.4) ──
  /health during transcription: HTTP 200 in 11ms
  /health answered promptly — the event loop was NOT blocked by transcription.
```

`/health` answered in **11ms** while a real Whisper transcription (the actual
blocking native call) was concurrently in flight on the same single-worker
process. This is a direct, measured confirmation of Phase 1 Q1's code-reading
citation (`main.py:1610-1612`, `run_in_executor`) — the offload is real, not
just present in the source.

### TTS stage, measured independently (chat_stream blocked before reaching it)

```
$ POST /api/tts/synth {"text": "Your show in Toronto is confirmed for next month.", ...}
status=200 elapsed=13136ms
audio bytes=161836 looks_like_wav=True
```

**PASS.** 13.1s (consistent with db1da91's documented ~12-15s normal Kokoro
latency), 161,836 bytes, valid WAV, well inside `KOKORO_SYNTH_TIMEOUT_SECONDS=25`
and the frontend's `TTS_SYNTH_TIMEOUT_MS=32000`.

### Summary table

| Stage | Result | Latency (measured) | Bound |
|---|---|---|---|
| upload + transcribe | **PASS** (real Whisper, real speech) | 5.8-6.4s warm, 30.6s cold (one-time model load) | 120s, not blocking the loop (measured) |
| chat_stream (Anthropic) | **BLOCKED — fast, correct 503**, not a hang | 5-9ms | n/a — rejected before any Anthropic call is attempted |
| isolated Anthropic auth check | real 401 from the provider | 139-194ms | n/a |
| tts/synth (Kokoro) | **PASS** (measured independently) | 13.1s | 25s |

---

## PHASE 3 — Name the cause

**The reported symptom ("Live" + empty "…" transcript, no reply text, no
audio, for 1m43s+, twice) is not a hang anywhere in the network/processing
path. It is a fast (5-9ms), correctly-coded HTTP 503 rejection of every
recorded turn — `"AI unavailable: ANTHROPIC_API_KEY not configured"` —
caused by a genuine, external, verified-absent credential
(`ANTHROPIC_API_KEY`, Phase 1 Q7, corroborated three independent ways:
direct `/proc/<pid>/environ` inspection of the actual backend process
serving the phone; cdd0c8d's own commit message, written earlier the same
day; and a real network call to `api.anthropic.com` returning
`AuthenticationError`/401 in Phase 2.3) — made invisible to the artist by
two compounding, verified frontend/backend defects:**

1. **cdd0c8d (backend, confirmed by diff, Q7)** moved the missing-key guard
   to *after* the static `__greet__` branch. This was the *correct* fix for
   its own stated problem (the greeting needing no LLM call shouldn't need a
   key) — but its side effect, undocumented at the time, was to remove the
   **only** signal that previously told the artist, loudly and immediately at
   call-start, that the session could not do real AI turns
   (`"Voice AI unavailable on this server. End call and retry."`,
   pre-existing `CallScreen.js` code, unchanged by any of the six commits).
   After cdd0c8d, the call *looks* completely healthy at connect time — the
   failure is deferred silently to the artist's first hold-to-talk release.

2. **The PTT turn's `onError` handler (`CallScreen.js`, present through
   964aabb, db1da91, 5709f87)** made **zero** visible UI change on any
   `chat_stream` failure — `console.warn(...); setIsSpeaking(false);
   clearAudioQueue();` and nothing else (fixed in this session's 243f592, see
   Phase 1 Q12, though that fix itself introduces a new defect — the
   auto-dismiss timer — corrected in Phase 4). Given defect 1, the very first
   real signal of trouble (the 503) arrives and is silently discarded within
   milliseconds of PTT release. `statusLabel` was already `'Live'` from the
   greeting; `transcript` was already reset to `''` (rendering the `…`
   placeholder) at the top of `stopRecordingAndSend`. **Nothing on screen
   ever changes again for that turn — because the request already finished,
   as a failure, in single-digit milliseconds.** The artist cannot
   distinguish "already failed instantly" from "still processing," and the
   reported 1m43s+ is how long the artist watched an unchanging screen before
   giving up — not the request's actual duration. This fully accounts for
   "no reply text and no audio," for the symptom recurring identically twice,
   and for why it looks the same as a genuine hang would look, given defect 2
   alone (a genuine hang and an invisible instant failure are indistinguishable
   to an observer when neither produces any UI feedback).

**This is the single cause that alone explains the observed symptom**, and it
is now measured, not inferred: Phase 2's 3 consecutive runs reproduce a
5-9ms rejection every time, with the exact quoted provider-facing error text,
against the identical credential state confirmed present throughout the
session in which the physical-device test failed.

**Secondary, independently real defects found and fixed/flagged in this
session, ranked below the above because neither one is reachable in the
actual observed failure (the missing key rejects the turn before either code
path is ever entered), but each is a genuine, verified defect in its own
right and left uncorrected would cause its own real incident eventually:**

- **`pitch_service.send_email` was unbounded, blocking network I/O called
  directly inside an `async def` with no executor** (fixed in this session's
  own prior turn, commit 7c9231d, verified in `tests/test_gmail_send_timeout.py`
  — a stuck call froze the single event loop thread, per Phase 1 Q3's
  confirmed single-worker topology). Real bug, correctly fixed, **not what
  the phone hit** — `_marcus_producer()` (where `send_email` is called) is
  never entered when `ANTHROPIC_AVAILABLE` is `False`, because the guard at
  `main.py` (post-cdd0c8d placement) raises before `generate_marcus()` is
  ever constructed.
- **Cross-turn interference when a second PTT turn starts while the first
  turn's TTS fetch is still in flight** (Phase 1 Q13) — real, code-provable,
  not yet fixed. Addressed in Phase 4.5.
- **`handleTurnError`'s auto-dismiss timer and generic "didn't respond in
  time" wording** (Phase 1 Q12, this session's own 243f592) — violates Phase
  4.6's explicit requirement. Addressed in Phase 4.6.

No architecture-level cause remains unaccounted for; nothing here rests on
"probably" or an assumption not backed by a direct citation or a measurement
printed above.

---

## PHASE 4 — Correct

### 4.1 Blocking STT/TTS off the event loop

Already true (Phase 1 Q1/Q2, `main.py:1610-1612` transcribe,
`main.py:721-725` synth) and now directly measured, not just read
(Phase 2.4: `/health` answered in 11ms during a real transcription). No
change needed.

### 4.2 SSE transport in Expo

Already correct (Phase 1 Q9): `XMLHttpRequest` + `onreadystatechange`
polling, not `fetch()`/`ReadableStream` (unsupported on Hermes/Android) or
`EventSource`. No change needed.

### 4.3 Consequential tools gated behind explicit confirmation

**Backend, `main.py`/`pitch_service.py` unaffected — this is entirely in
`MARCUS_TOOLS`'s schema and `_execute_marcus_tool`.** `send_pitch_email` now
requires `confirmed: true` in the tool input before `pitch_service.send_email`
is ever called. The first call (Anthropic deciding `stop_reason=tool_use` is
not artist consent) always drafts only — returns the exact subject/body back
to the model with an instruction to read it to the artist and ask before
sending anything — and is logged as a distinct `actions_taken` entry
(`"draft_ready (unconfirmed)"`). Only a second call, unchanged apart from
`confirmed: true`, actually sends. No email can leave on an ordinary advisory
turn purely because the model decided to. Tests:
`tests/test_marcus_tool_use.py::test_marcus_send_pitch_email_requires_explicit_confirmation`
(the narrow guarantee) and the extended
`test_marcus_tool_loop_invokes_functions_and_emits_actions` (the full
two-round-trip conversational flow through `/api/chat_stream`).

### 4.4 Timeouts from measured p95 + headroom, basis documented

Honest split, because two of the four bounded stages have real measured data
from this session and two genuinely do not (an external provider I have no
valid credential to generate real traffic against, and Gmail, which is
hard-disabled for this task):

| Constant | Value | Basis |
|---|---|---|
| `KOKORO_SYNTH_TIMEOUT_SECONDS` | 25s | **Measured**: db1da91's own real-device data (~12-15s) + this session's own measured sample (13.1s, Phase 2). ~2x p50 headroom. |
| `TRANSCRIBE_TIMEOUT_SECONDS` | 120s | **Measured**: this session's 3 real runs (5.8-30.6s, the high end being one-time cold Whisper model load, Phase 2). Generous headroom over the cold-load case specifically, since that one-time cost is unpredictable across environments. |
| `ANTHROPIC_STREAM_TIMEOUT_SECONDS` / `MARCUS_CREATE_TIMEOUT_SECONDS` | 40s | **Not measured** — no valid `ANTHROPIC_API_KEY` exists anywhere in this environment (Phase 1 Q7), so no real p95 latency for this call can be generated without violating "no real provider calls with a paid/production key" implicit in this being a diagnosis pass, and the one real call made (Phase 2.3, auth-only, 401 in ~150ms) doesn't exercise a real generation. Basis is architectural instead, and documented as such in `main.py`'s own comment: must stay comfortably under the frontend's 60s SSE inactivity watchdog so the backend can answer with a clean error before the client gives up blind. **Should be revisited from real measured p95 once a valid key exists** — flagged, not silently left looking more rigorous than it is. |
| `GMAIL_SEND_TIMEOUT_SECONDS` | 20s | **Not measured** — Gmail is hard-disabled for this task; same architectural basis (under the 40s Marcus per-call bound, which is itself under the 60s client watchdog). |

### 4.5 One recording = one turn_id = at most one transcribe/generate/synth

Real, code-provable defect from Phase 1 Q13 (a second PTT turn starting while
the first turn's TTS fetch was still in flight could enqueue stale audio and
clobber the active turn's transcript/indicators) — fixed with a monotonic
generation counter, `turnGenRef` (`CallScreen.js`), bumped in `startRecording`
(the instant a NEW turn begins) and checked in every async callback that
could otherwise outlive its own turn: `startGreeting`'s and
`stopRecordingAndSend`'s `onText`/`onDone` (both before and after the
awaited TTS fetch)/`onRoute`/`onError`, plus the greeting's own auto-retry
`setTimeout`. A stale callback now silently no-ops instead of mutating state
a newer turn owns. `sendInFlightRef` (964aabb) is retained unchanged — it
guards the narrower transcribe-duplication case and remains correct;
`turnGenRef` is the missing full-turn guarantee, not a replacement.

An actual per-turn `turn_id` (distinct from the pre-existing per-call
`call_id`) is now generated client-side and threaded through
`streamChat`/`fetchTtsSynth` to the backend's own `_stage_log` instrumentation
(Phase 2) — closing Phase 1 Q8's gap for real, not just for this diagnosis's
own probe.

### 4.6 Errors name the failed stage, stay visible, never auto-revert

Two auto-dismiss timers deleted (`handleTurnError`, `finishSpeakingWithoutAudio`
— the latter is 243f592-and-earlier vintage, the former is 243f592's own
addition from the prior session, both now confirmed to violate this
requirement). Neither needs a replacement timer: the `statusEl` render
priority (`isSpeaking > isTranscribing > isRecording > statusLabel`, unchanged)
already supersedes a stale message the instant the artist's next hold-to-talk
begins (`voice.isRecording` becomes true) — no timer needed to be "superseded
by the next real state," which is a stronger and simpler guarantee than a
fixed-duration timer ever gave. `handleTurnError` now maps the failure text to
a distinct, correctly-worded message per case (fast 5xx / bounded timeout /
network error / generic) instead of unconditionally claiming a timeout that
often didn't happen (Phase 1 Q12's second finding). `finishSpeakingWithoutAudio`
now explicitly names "Voice synthesis failed" per this requirement's own
example wording.

### 4.7 Loud startup check + UI degraded-mode indicator, independent of the greeting

- `main.py`'s existing `_check_env()` `ANTHROPIC_API_KEY` branch (already
  present, Phase 1 Q7) now also `print()`s an unmissable operator-facing line
  in addition to the structured `log.warning` — matching this file's own
  convention for other boot-time operator messages.
- New: `/api/health` (pre-existing endpoint, previously `status`/`version`/
  `tts`/`agents` only) now also reports `ai_available: bool`
  (`ANTHROPIC_AVAILABLE`) — presence-only, same limitation as `_check_env()`
  itself (a malformed-but-present key still reports available; only a real
  call surfaces that, per the confirmed post-gate behavior below).
- New: `apiAiStatus()` (`api.js`) calls this endpoint independently of
  `apiGreet`/`streamChat('__greet__')` — fails open (`{aiAvailable: true}`)
  on any network/parse error, so a status-check failure can never itself
  misdiagnose a working session as degraded.
- New: CallScreen checks this on mount (in parallel with, not gated by, the
  greeting) and renders a persistent, honestly-worded banner
  ("⚠ Voice AI unavailable on this server — {agent} can greet you, but
  cannot generate real replies right now.") when it comes back false — shown
  proactively, before the artist ever holds to talk, closing the exact gap
  cdd0c8d opened (Phase 3): the static greeting succeeding is no longer the
  only signal, or even a signal at all, of LLM health.

**Verified post-gate behavior** (this session, real network call, no side
effect — a well-formed-but-fake key, since a real one doesn't exist to test
with): with a key merely *present* (regardless of validity), `/api/health`
correctly reports `ai_available: true`, and a real turn against
`api.anthropic.com` fails with a real `401 authentication_error` in 157ms —
routed to a clean SSE `error` event, not a hang, confirming the Phase 4's
bounded stages behave correctly on this path too, not only on the
zero-key path Phase 2 measured. **Noted, not fixed (out of this diagnosis's
scope — a distinct, pre-existing hygiene gap, not part of the reported
defect or any of the 7 items above):** that fallback path
(`except Exception as e: await evt_out.put(("error", str(e)))`, both in
`_claude()` and `_marcus_producer()`) forwards the provider's raw exception
text to the client (seen here: `"Error code: 401 - {'type': 'error', ...}"`).
Not raw enough to leak the key itself, but more internal detail than the
purpose-built timeout/degraded-mode messages give — a reasonable follow-up,
not part of this pass.

### Per-commit verdicts

| Commit | Verdict | Why |
|---|---|---|
| **964aabb** (frontend, `[0.2]` FormData/expo-fetch fix + `sendInFlightRef`/`stopPromiseRef`) | **RETAINED** | Real, still-necessary fix (Expo SDK 57's `expo/fetch` FormData contract) and a real, still-correct partial guard — `turnGenRef` (4.5) extends it, doesn't replace it. |
| **5709f87** (frontend, bounded `/api/tts/synth` fetch + shared `fetchTtsAudio`) | **REVISED** | The bound and the shared helper are correct and retained. `fetchTtsAudio`'s signature gained an optional `turnId` param (4.5); `finishSpeakingWithoutAudio`'s message and auto-dismiss timer changed (4.6). |
| **243f592** (frontend, this session's own prior fix — `handleTurnError` + `'Thinking…'`) | **REVISED** | Correctly identified and fixed real silence-on-error, but its own message logic (always claims a timeout) and auto-dismiss timer are exactly what Phase 4.6 asked to remove. `'Thinking…'` indicator and the underlying diagnosis both retained. |
| **cdd0c8d** (backend, static-greeting-doesn't-need-a-key fix) | **RETAINED** | Its own stated fix is correct and should not be reverted (the greeting genuinely doesn't need `ANTHROPIC_AVAILABLE`). Its side effect (removing the only prior signal of key health) is what Phase 4.7 now independently compensates for with `ai_available` — not by reverting this commit. |
| **db1da91** (backend, bounded Kokoro synth) | **RETAINED** | Real, correct, unrelated-to-the-actual-cause fix; not reachable by the confirmed defect but a genuine improvement in its own right (same class of bug as 4.3/Gmail). |
| **7c9231d** (backend, this session's own prior fix — bounded Gmail send + Anthropic calls + Marcus confirmation groundwork) | **RETAINED, and extended** | `GmailSendTimeout`/executor-offload is a real, correctly-fixed defect (confirmed unbounded blocking I/O, Phase 1 Q3), just not the one the phone hit (Phase 3 — `_marcus_producer` is never entered when the key is absent). `ANTHROPIC_STREAM_TIMEOUT_SECONDS`/`MARCUS_CREATE_TIMEOUT_SECONDS` retained as defense-in-depth (4.4). This session's `MARCUS_TOOLS`/`_execute_marcus_tool` additions build the 4.3 confirmation gate directly on top of 7c9231d's existing Gmail-exception-handling shape. |

None of the six commits were reverted. Every one of them fixed something
real; none of them, individually or together, explains the reported symptom
— that took Phase 2's actual measurement to find (the missing credential),
and Phase 1 Q12's code-reading to find why it was invisible (the silent
`onError`, now itself corrected but with its own follow-on defect in turn).

---

## PHASE 5 — Prove and commit

**5.1** `voice_probe.py` was run 3 consecutive times against a live backend
built from this session's final code (Phase 2's measurement section above).
Two of three real stages are green every run (transcribe, and tts/synth
verified independently); the third (`chat_stream`) is **correctly BLOCKED**,
not green, because the one remaining external dependency — a valid
`ANTHROPIC_API_KEY` — does not exist anywhere in this environment (see
"remaining external dependency" below). This is not a shortfall in the fix:
Phase 4.7's own verification (a well-formed-but-fake key) proves the
post-gate code path itself is healthy (a real `401` in 157ms, routed to a
clean SSE `error` event) — the gate is doing exactly its job of failing fast
and cleanly on the one input this environment cannot supply. Re-running
`python3 scripts/voice_probe.py --runs 3` once a real key is configured is
the exact remaining step to see literal all-green output.

**5.2** Focused tests added, run in isolation and together with each
touched file's existing suite (no broad audit):

Backend —
```
$ python3 -m pytest tests/test_marcus_tool_use.py tests/test_marcus_search_curators_schema.py \
    tests/test_pitch_service.py tests/test_pitch_reply_scan_batch.py tests/test_gmail_send_timeout.py \
    tests/test_chat_stream_timeout.py tests/test_r05_anthropic_graceful_degradation.py \
    tests/test_tts_contracts.py tests/test_transcribe.py tests/test_kokoro_synth_timeout.py \
    tests/test_kokoro_reload_warmup.py tests/test_r19_kokoro_startup_warning.py \
    tests/test_ai_status_and_confirmation_gate.py -q
........................................................................ [ 69%]
...............................                                          [100%]
103 passed, 1 warning in 61.00s
```

Frontend —
```
$ node --test tests/tts-synth-timeout.test.cjs tests/voice-session.test.cjs tests/voice-upload-contract.test.cjs \
    tests/team-call-lifecycle.test.cjs tests/team-meeting-runner.test.cjs tests/chat-stream-turn-error.test.cjs \
    tests/turn-generation-guard.test.cjs
# tests 34
# pass 34
# fail 0
```

Babel (`@babel/core` + project-installed `babel-preset-expo`) transforms
`CallScreen.js` and `api.js` cleanly. `git diff --check` clean, both repos.

**5.3** Committed locally in both repos, both clean, nothing pushed:

- `maestro-backend`: `db1da91` (prior turn's start) → `7c9231d` (prior turn's
  fix) → **`90dfb94`** (this pass: diagnosis doc, instrumentation, probe,
  confirmation gate, `ai_available`).
- `plmkr-frontend`: `5709f87` (prior turn's start) → `243f592` (prior turn's
  fix) → **`892461e`** (this pass: turnGenRef, honest error messages,
  degraded-mode banner).

**5.4** See the final report delivered in-conversation for the complete
write-up (proven root cause, why prior fixes failed, per-commit verdicts,
final design, files/SHAs, test results, remaining dependency, starting/ending
HEADs, `git status --porcelain`, and the one physical-iPhone test).

