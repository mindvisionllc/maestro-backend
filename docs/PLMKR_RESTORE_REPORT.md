# PLMKR Restoration Report — 2026-09-14

Scope: restore OTP/signed-session login and re-verify the Marcus voice path
after a backend restart broke `Send Code` and coincided with a voice-turn
failure. This report separates **local implementation verification** (done,
below) from the **physical-device confirmation** (not done — requires
Tommy's phone; see "Final user action").

No secret value appears anywhere in this report, in any commit, or in any
tool output produced by this session. Where a value had to be inspected, only
its name, presence, and character length were used.

---

## 1. Proven root cause — why OTP worked earlier and broke after restart

Three env vars `_twilio_verify_config()` / `identity_configured()`
(`main.py:14056-14092`, `artist_identity.py:21-25`) require were not what the
running backend needed them to be:

| Var | Required format | Found before this session |
|---|---|---|
| `TWILIO_AUTH_TOKEN` | 32 lowercase hex chars | **present, 24 chars — fails format check** |
| `TWILIO_VERIFY_SERVICE_SID` | `VA`+32 hex | **absent everywhere** |
| `PLMKR_SESSION_SECRET` | any string ≥32 chars | **absent everywhere** |
| `PLMKR_IDENTITY_SECRET` | any string ≥32 chars | **absent everywhere** |
| `TWILIO_ACCOUNT_SID` | `AC`+32 hex | present, correct — not implicated |
| `TWILIO_PHONE_NUMBER` | E.164 | present, correct — not implicated |

Evidence, gathered without ever printing a value:
- `/proc/<pid>/environ` on the running `uvicorn` process (pid 122448, started
  19:38:45, cwd `maestro-backend`, cmdline exactly `uvicorn main:app --host
  0.0.0.0 --port 8000` — no env-var prefix) showed `TWILIO_AUTH_TOKEN` at
  length 24 and no `TWILIO_VERIFY_SERVICE_SID`/`TWILIO_VERIFY_SID` at all.
- The same wrong 24-char token and absent verify-SID were confirmed present
  in **every** process on the machine (uvicorn, expo, node, both `claude`
  sessions, the login shell) — all inherited from one line,
  `~/.bashrc:125`, `export TWILIO_AUTH_TOKEN=<24 chars>`.
- Python-side regex check (`^AC[0-9a-fA-F]{32}$`, `^[0-9a-f]{32}$`,
  `^VA[0-9a-fA-F]{32}$`, matched against shell-derived values only, never
  printed) confirmed: ACCOUNT_SID and PHONE_NUMBER valid; AUTH_TOKEN invalid;
  VERIFY_SERVICE_SID/VERIFY_SID absent.
- `curl -X POST /api/auth/send-otp` against the (pre-fix) running backend
  reproduced a clean `503 {"code":"auth_not_configured"}` — the backend's own
  fail-closed response, confirmed with real request data, not inferred.
- `~/.bash_history` contains several earlier, more careful restart
  sequences that read `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` /
  `TWILIO_VERIFY_SERVICE_SID` / `PLMKR_SESSION_SECRET` / `PLMKR_IDENTITY_SECRET`
  back out of a **previously-running backend process's own
  `/proc/$PID/environ`**, format-validated them, and only then relaunched
  uvicorn with them passed explicitly as a command prefix — i.e. the correct
  values were deliberately never written to disk anywhere (consistent with
  "never store secrets"), and survived only by being re-harvested from the
  outgoing process's memory at each restart.
- The actually-running process's cmdline (`uvicorn main:app --host 0.0.0.0
  --port 8000`, no prefix) shows the most recent restart did **not** use that
  harvest-and-relaunch sequence — it was a bare relaunch. That broke the
  chain: the correct values, which lived only in the old process's memory,
  were lost, and the new process fell back to whatever the shell already had
  — the stale/wrong `TWILIO_AUTH_TOKEN` in `~/.bashrc`, and nothing at all
  for `TWILIO_VERIFY_SERVICE_SID` / `PLMKR_SESSION_SECRET` /
  `PLMKR_IDENTITY_SECRET`, which had never lived in `~/.bashrc` to begin with.

**This is why it worked before and broke after the restart**: the working
configuration was never persisted — it existed only in a running process's
memory, recovered by hand at each careful restart. A plain restart (no
harvest step) silently lost it.

One additional, security-relevant side effect of the missing identity
secrets: `_require_artist_scope()` (`main.py:1688`) fails *open*, not closed,
when `identity_configured()` is `False` — it trusts whatever `artist_id` the
client sends, with no signature check at all. Before this session's fix,
every artist-scoped endpoint on this backend (`/api/artist`, `/api/chat_stream`,
`/api/tts/synth`, …) would accept **any** client-supplied `artist_id` with no
authentication. Restoring `PLMKR_SESSION_SECRET`/`PLMKR_IDENTITY_SECRET`
closes this, independent of the OTP fix — verified in §3.

The frontend text Tommy saw ("Could not send code — The code could not be
sent.") is `authErrors.js`'s `SEND_FALLBACK`, used when the send-otp error
code doesn't match any of the ~5 explicit `SEND_MESSAGES` keys. The current,
reproduced `auth_not_configured` code *is* one of those keys and maps to a
more specific "Verification unavailable" message — so the exact wording Tommy
saw implies the backend was failing in a slightly different shape at that
exact moment (e.g. mid-restart, or a 422/other response `responseJson` treats
as unmapped) than the clean `auth_not_configured` 503 it produces now. The
underlying cause — the credentials above — is proven either way; the literal
on-screen wording at the exact moment isn't fully reconstructable from
evidence and isn't load-bearing for the fix.

---

## 2. Configuration correction (implemented, verified locally)

**Single source of truth going forward:** `maestro-backend/.env`
(permission 600, git-ignored, already loaded unconditionally at import by
`main.py:27`'s `load_dotenv(...)`). Backend startup no longer depends on
`~/.bashrc` or any interactive shell state.

What changed:
1. Generated two new local signing secrets (`openssl rand -hex 32`,
   64 lowercase hex chars each — not third-party credentials, this app's own
   HMAC keys) and wrote them into `.env` as `PLMKR_SESSION_SECRET` /
   `PLMKR_IDENTITY_SECRET`. Chosen fixed and persisted (not
   generated-per-boot) specifically so restarts don't invalidate existing
   sessions going forward.
2. Copied the already-correct `TWILIO_ACCOUNT_SID`, `TWILIO_PHONE_NUMBER`,
   `ELEVENLABS_API_KEY`, `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
   `CLOUDINARY_API_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
   from the live (bashrc-derived) process environment into `.env`, file to
   file, without the values ever passing through this session's visible
   output.
3. Left `TWILIO_AUTH_TOKEN` / `TWILIO_VERIFY_SERVICE_SID` unset in `.env`
   with an explanatory comment — genuinely unrecoverable (checked every
   readable `/proc/*/environ` on the box; none held a valid one).
4. Disabled (commented out, backup kept at `~/.bashrc.bak-plmkr-restore-*`)
   the one proven-wrong line, `~/.bashrc:125`'s `export
   TWILIO_AUTH_TOKEN=...`, by variable-name pattern only (`sed`, never
   referencing the value) — otherwise `load_dotenv(override=False)` would
   let that stale value keep shadowing a correct one entered later.
5. Added `maestro-backend/scripts/setup_local_secrets.sh` — the one secure,
   interactive, idempotent setup command for the two genuinely-missing
   values. Prompts once each for `TWILIO_AUTH_TOKEN` and
   `TWILIO_VERIFY_SERVICE_SID` with `read -rs` (not echoed), format-validates
   before writing, writes only into `.env` (permission 600 preserved, never
   Git, never a shell arg, never logged), atomic write via temp-file+rename.

**Verified locally** (not a status endpoint — a real restart and real
requests): stopped the old process, relaunched `uvicorn main:app --host
0.0.0.0 --port 8000` with **every relevant var explicitly unset from the
shell** (`env -u ANTHROPIC_API_KEY -u TWILIO_* -u PLMKR_* -u ...`) so the
only possible config source was `.env` on disk:
- `GET /api/health` → `{"status":"ok","ai_available":true}` — Anthropic
  config alive from `.env` alone.
- `POST /api/auth/send-otp` → still a clean `503 auth_not_configured` —
  correctly still fail-closed on the two values that are genuinely still
  missing (proves nothing is silently bypassed).
- `GET /api/artist?artist_id=...` with no `Authorization` header →
  **401** (was previously unauthenticated dev-bypass) — confirms
  `identity_configured()` is now `True` from `.env` alone and the artist-scope
  gap in §1 is closed.
- `/proc/<new pid>/environ` confirmed none of `ANTHROPIC_API_KEY`,
  `PLMKR_SESSION_SECRET`, `PLMKR_IDENTITY_SECRET`, `TWILIO_ACCOUNT_SID` were
  present in the inherited shell environment at all — everything came from
  the file.

---

## 3. Voice correction

**Finding: the Marcus voice path is already correct on the current HEAD and
was independently re-verified live, not just trusted from the prior
diagnosis doc.**

`docs/VOICE_DIAGNOSIS.md` (already committed on this branch, tip
`1a2396d`) documents a prior session's full diagnosis and fix of exactly this
symptom class: missing `ANTHROPIC_API_KEY` masquerading as a hang
(`90dfb94`), unbounded Gmail/Anthropic calls (`7c9231d`), unbounded Kokoro
synthesis (`db1da91`), the greeting 503 bug (`cdd0c8d`), and the real
voice-turn signal + reply-length cap (`1a2396d`). Per that doc's own §6, it
was verified with 5/5 passing `voice_probe.py` runs and 70/72 relevant
backend tests before being committed — **before this session started**.

This session did not take that on faith. After minting a locally-signed test
session (§below — zero external calls, no SMS, no Twilio) I re-ran the
**exact reported question**, live, against the freshly-restarted,
`.env`-only backend, through the full authenticated path:

```
POST /api/chat_stream  agent_id=puppet-master voice=true
  message: "What should I focus on first for my November release?"
→ 2.67s, HTTP 200 (stream)
  text: "What genre is the release, and do you have a target drop date within November?"
  actions_taken: []   gmail_not_connected: false   (no side effect invoked — item 11 satisfied)

POST /api/tts/synth  voice=am_onyx (Marcus's real voice id, not a guess)
  text: <the reply above>
→ 3.42s, HTTP 200, 220204 bytes, valid RIFF/WAV
```

Total ~6.1s. Real relevant text, real decodable audio — not a timeout being
caught (item 10 satisfied). No duplicate transcribe/Anthropic/TTS calls: this
was a single chat_stream call and a single tts/synth call, matching
`turnGenRef`/`sendInFlightRef`'s duplicate-turn guards already in
`CallScreen.js` (unchanged — see below).

**Frontend (`plmkr-frontend`, tip `6beae49`) — read, not modified.** Already
implements, and already has tests for, everything task items 13/14 ask for:
- `sendInFlightRef` in `stopRecordingAndSend` (`CallScreen.js:673`) — blocks a
  second PTT release from starting a duplicate transcribe/generate/synth
  turn while one is in flight.
- `turnGenRef`, bumped on every new recording (`CallScreen.js:663`) — every
  async callback from an older turn (`onText`/`onDone`/`onError`/TTS
  resolution) checks it and bails, so a stale turn can never clobber the
  active one's UI or enqueue stale audio.
- `handleTurnError` (`CallScreen.js:526`) sets a specific, accurate status
  message per failure class and **never reverts to `'Live'` automatically**
  — its own comment documents removing that behavior in an earlier pass
  specifically because it produced the misleading "Live … forever" defect.
  It only changes on the artist's next hold-to-talk. Item 13 satisfied.
- `endCall` (`CallScreen.js:772`) aborts the chat stream, aborts any in-flight
  TTS fetch, and POSTs `/api/tts/cancel` so a synthesis already running on
  the backend discards its result — Phase 0.3 satisfied.
- `agentVoice = agent.voice ?? 'am_onyx'` (`CallScreen.js:94`) — real
  per-agent voice id from route params, not hardcoded — Phase 0.1 satisfied.

**Recent timeout/Gmail/voice patches (item 12): reviewed, kept, no revert.**
Backend commits `90dfb94`, `7c9231d`, `db1da91`, `cdd0c8d`, `1a2396d` and
frontend commits `5709f87`, `243f592`, `892461e`, `6beae49` were read in full
(not just their messages) and independently re-verified live above; nothing
in them contradicts current evidence.

**Honest gap:** I cannot fully reconstruct, from evidence alone, the exact
failure mode at the precise moment Tommy observed "timeout containment, but
Marcus still did not answer" — it is consistent with the
already-fixed-and-committed missing-`ANTHROPIC_API_KEY` and
unbounded-Gmail/Kokoro-call classes documented in `VOICE_DIAGNOSIS.md`, and
is not reproducible on the current code against the current (now-correct)
config. If it recurs, the addition below (§4) gives it somewhere to leave a
trace.

---

## 4. Tests and instrumentation added

**New regression risk found and fixed — test isolation from the real
`.env`.** Populating `.env` with real `PLMKR_SESSION_SECRET`/
`PLMKR_IDENTITY_SECRET` (§2) is exactly the fix task item 7 asked for, but
`main.py` loads that same real `.env` unconditionally at import
(`main.py:27`), and there was no `tests/conftest.py` — so every test process
also picked up the real secrets, flipping `identity_configured()` to `True`
globally and turning 19 previously-passing tests' unauthenticated calls into
401s that had nothing to do with what they tested (`test_chat_stream_timeout.py`,
`test_voice_turn_length_budget.py`, `test_marcus_tool_use.py`,
`test_tts_contracts.py`, 2 of `test_ai_status_and_confirmation_gate.py`).

Added `maestro-backend/tests/conftest.py`: an autouse fixture that
`monkeypatch.setenv(key, "")` (not `delenv` — several tests call
`importlib.reload(main)` mid-test, which re-runs `load_dotenv(override=False)`;
`override=False` only skips a key already *present*, and `delenv` makes it
look absent again, silently refilling it from the real file — the exact bug
class `VOICE_DIAGNOSIS.md` §6.6 already documented for `ANTHROPIC_API_KEY`,
confirmed here for the identity/Twilio keys too) for
`PLMKR_SESSION_SECRET`, `PLMKR_IDENTITY_SECRET`, `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE_SID`, `TWILIO_VERIFY_SID` before
every test. Tests that want a real value for one of these already set it
explicitly via their own `monkeypatch.setenv` (`test_twilio_verify_otp.py`,
`test_artist_identity.py`) — autouse fixtures run before a test's own
requested fixtures, so those explicit values still win.

Not added: a new voice-path regression test. The exact scenario (real
question → real relevant text → real audio, no side effects) is already
covered by `scripts/voice_probe.py` plus `tests/test_voice_turn_length_budget.py`,
`test_marcus_tool_use.py`, `test_chat_stream_timeout.py`,
`test_gmail_send_timeout.py`, `test_kokoro_synth_timeout.py`,
`test_tts_contracts.py` (all passing, §5) — duplicating that coverage for one
specific question string would test the same code paths those already do.
The pre-existing `test_missing_or_malformed_provider_config_fails_closed`
(`test_twilio_verify_otp.py`) and `test_missing_identity_secret_never_yields_bare_valid_true`
already parametrize over exactly the wrong-format/missing-value classes found
in §1 — no gap there to fill either.

---

## 5. Test results (local implementation verification only)

```
$ python3 -m pytest -q tests/test_twilio_verify_otp.py tests/test_artist_identity.py \
    tests/test_r17_sms_otp_dev_bypass.py tests/test_r04_health_auth_status.py \
    tests/test_api_key_auth.py tests/test_chat_stream_timeout.py \
    tests/test_kokoro_synth_timeout.py tests/test_kokoro_reload_warmup.py \
    tests/test_gmail_send_timeout.py tests/test_voice_turn_length_budget.py \
    tests/test_marcus_tool_use.py tests/test_tts_contracts.py \
    tests/test_ai_status_and_confirmation_gate.py
152 passed, 2 failed in 114.15s
```
The 2 failures (`test_api_health_reports_ai_unavailable_without_a_key`,
`test_greeting_succeeds_regardless_of_ai_available_confirming_it_is_not_a_health_signal`)
are the pre-existing `ANTHROPIC_API_KEY`/`delenv`+`reload` bug documented in
`VOICE_DIAGNOSIS.md` §6.6, reproducible before any change in this session —
not touched, out of scope (a different key, same class of issue, already
flagged and deliberately deferred by the prior session).

Broader collateral-damage check for the new `conftest.py` autouse fixture
(mirrors the prior session's "broader backend sweep" scope):
```
$ python3 -m pytest -q tests/test_wire_*.py tests/*chat_stream*.py
244 passed, 1 warning in 179.33s
```

Frontend: `node --test tests/*.test.cjs` → **176 passed, 0 failed** (no
frontend files changed this session — confirms the baseline this report's
voice findings rely on).

`git diff --check`: clean in both repos (no whitespace errors).

---

## 6. Files and commits

**maestro-backend** (branch `feat/buffer-profile-discovery`, continued —
same branch `VOICE_DIAGNOSIS.md`'s work already lives on; this is a direct
continuation of that investigation, not a new independent task, so no new
branch was cut for it):
- `.env` — not committed (git-ignored, permission 600) — consolidated
  per §2.
- `~/.bashrc` — not a repo file — one line disabled, backup at
  `~/.bashrc.bak-plmkr-restore-<timestamp>`.
- `scripts/setup_local_secrets.sh` (new) — §2 item 5.
- `tests/conftest.py` (new) — §4.
- `docs/PLMKR_RESTORE_REPORT.md` (new, this file).

**plmkr-frontend**: no changes. Worktree confirmed clean
(`git status --porcelain` empty) before and after.

---

## 7. Repository HEADs and worktree status (at report time)

```
maestro-backend:   feat/buffer-profile-discovery @ (see `git log -1` after commit below)
                   worktree clean after commit
plmkr-frontend:    feat/social-buffer-execution  @ 6beae496fc0ac6a5dc821d8b244fca8286926892
                   worktree clean (no changes made)
```
Nothing pushed, merged, or deployed. No SMS, email, or other external call
was made by this session (the one signed-session token minted for §3's
voice re-test was generated locally from `PLMKR_SESSION_SECRET` via
`artist_identity.issue_session()` — pure HMAC signing, no network call, no
Twilio, no SMS).

---

## 8. Final user action required (this session cannot do these)

1. **One secure setup action** — from a terminal on this machine:
   ```
   cd ~/maestro-backend && ./scripts/setup_local_secrets.sh
   ```
   Enter `TWILIO_AUTH_TOKEN` and `TWILIO_VERIFY_SERVICE_SID` from
   console.twilio.com when prompted (input is not echoed). Then restart the
   backend exactly as the script prints:
   ```
   pkill -f 'uvicorn main:app'; cd ~/maestro-backend && uvicorn main:app --host 0.0.0.0 --port 8000
   ```
2. **One OTP attempt** on the physical iPhone: tap Send Code with a real
   number.
3. **One final ordinary Marcus voice test** on the physical iPhone after
   signing in: ask "What should I focus on first for my November release?"
   (or any ordinary question) and confirm you both see relevant text and
   hear audible speech.

Everything up to those three steps has been verified locally in this
session, live, against a real (non-mocked) restarted backend — not merely by
passing automated tests. The physical-device outcome is not yet known and is
not claimed here.
