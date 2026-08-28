# PLMKR Trusted-State Ledger — 2026-08-28

**Session type:** Inspection, reconciliation, and documentation only. No product code, architecture,
pricing, agent structure, tools, prompts, billing, OAuth, deployment, or database state was modified.
This document is the deliverable.

---

## 1. Session Scope and Explicit Non-Actions

Per the session brief, this session did **not**: implement the "artist's office" product model,
change the agent/persona architecture, convert specialists into internal desks, create the
Book/Brief/Release Command/approval queue, modify tools/prompts/pricing/billing/OAuth/deployment/DBs,
rename Playmaker/PLMKR branding, delete/move/consolidate/retire anything, "clean up" inconsistencies,
modify existing governance/North Star documents, make external API calls, spend money, contact
production services, or install dependencies. The only intentional change is this file.

---

## 2. Starting Repository State

- **Working directory:** `/home/tommy/maestro`
- **Is a git repository:** Yes
- **Repository root:** `/home/tommy/maestro`
- **Branch:** `main`
- **HEAD commit (start and end of session):** `54ea8e913534447a6b2781df42b6b190268c9a91`
  ("Merge M3 loop consolidation — shared helper + 15 agents migrated (main.py -2289 lines)", 2026-07-08)
- **Remote:** `origin` → `git@github-psychoblast:psychoblast/maestro-backend.git` (fetch+push)
- **Upstream tracking branch:** none configured for `main` (`git rev-parse @{u}` fails with
  "no upstream configured"). `git ls-remote origin main` returns the same SHA as local HEAD
  (`54ea8e9`), so local and remote `main` are in sync even without a tracking ref.
- **`git status --short` at session start:** untracked only —
  `.tmp_audit/`, `.tmp_audit2/`, `.tmp_audit3/`, ten files under `_audit/`, and
  `docs/PLMKR_MASTER_NORTH_STAR.html`. No modified or staged files. All of these pre-date this
  session and were left untouched.
- **Worktrees:** two — `/home/tommy/maestro` (main, `54ea8e9`) and
  `/home/tommy/maestro/.claude/worktrees/agent-ae325b382a012c875` (branch `fix/consult-honesty-sweep`,
  `c8f4de2`). That branch is **24 commits behind** `main` (`git log fix/consult-honesty-sweep..main`
  = 24; `git log main..fix/consult-honesty-sweep` = 0) — a stale checkout, not unmerged work.
- **Tags:** ~90 checkpoint tags (`p3e-*`, `p3f-*`, `phase2*`, `phase3a/3b/3c/3d`, `v0.1-eod-*`,
  `v0.8-pre-fixes`, `ar-pilot-v1`), all historical build markers.
- **Branches:** ~100 local/remote `feat/wire-*`, `feat/*`, `fix/*`, `docs/*` branches remain from the
  agent-wiring build-out; not inventoried individually as out of scope.

---

## 3. Repository / Frontend Identity Findings

- **Backend repository:** confirmed to be `/home/tommy/maestro`, remote `psychoblast/maestro-backend`
  on GitHub, currently on `main` at `54ea8e9`.
- **Frontend/mobile repository:** **not present in this environment.** `docs/PHASE_4_FRONTEND_DEFERRED.md`
  (dated 2026-05-15) states explicitly: *"Frontend work lives in `~/Desktop/ReveNation/` — a **separate
  repository**, separate product (RÊVE NATION / React Native), separate entity (Mind Vision LLC), and
  requires a separate Claude Code session with different rules. Do not conflate with PLMKR (Marquis
  Holdings LLC, `~/maestro/`)."* `~/Desktop/ReveNation/` does not exist on this machine as of this
  session — only `~/revenation-g8-v2/` and a standalone `~/Desktop/App_backup.js` (a "RÊVE NATION"
  splash screen) exist, both under the separate ReveNation product, not PLMKR. **No PLMKR-branded
  frontend/mobile app repository was found anywhere in the immediate project area.**
  - `CLAUDE.md`'s Phase 0 items reference a frontend file `CallScreen.js` (voice mapping, TTS wiring,
    hangup handling) that does not exist in any repository discoverable this session. This claim is
    **not verifiable** — the file cannot be located.
- **Duplicate backend clone found:** `~/Desktop/maestro/` is a **stale local clone of the same
  `maestro-backend` remote**, frozen at commit `37172f8` (2026-04-29), 108 KB `main.py` vs the current
  827 KB / 16,521-line `main.py`. It is not a distinct product — same remote, far behind `main`.
  ⚠️ **Security note:** that clone's `origin` remote URL embeds a GitHub personal access token in
  plaintext (visible via `git remote -v`). The literal value is deliberately not reproduced in this
  document per credential-handling rules; if that clone is still in use, the token should be rotated
  and the remote re-set to use SSH or a credential helper instead of an embedded PAT.
- **`.openai/hosting.json` or similar governance file:** **not found.** No file matching
  `*.openai*` or `hosting.json` exists in the repository.

---

## 4. Verified Technical Stack

| Component | Evidence | Finding |
|---|---|---|
| Backend framework | `requirements.txt`: `fastapi==0.135.1`, `uvicorn==0.41.0` | FastAPI on Uvicorn |
| Frontend/mobile framework | — | **No frontend repo present to inspect** (see §3) |
| Database (persistence) | `main.py:87` `DATABASE_URL` (Railway Postgres, `psycopg2`, `main.py:1294-1328`); `main.py:14` `sqlite3`, used at `main.py:1194-1269` | **Dual-mode, by design**: PostgreSQL when `DATABASE_URL` is set (Railway), flat SQLite (`memory.db`) fallback for local dev. Not "one or the other" as CLAUDE.md's env list alone might suggest. |
| Auth / dev bypass | `main.py:13431-13440` `SMS_OTP_DEV_BYPASS`; `main.py:13664-13669` refuses boot on Railway if bypass is set in prod | SMS OTP has a documented, guarded dev bypass. `TWILIO_AUTH_TOKEN` format is validated at `main.py:13458` (must be exactly 32 lowercase hex chars) — matches CLAUDE.md's claim of an invalid-format token blocking real Twilio use. |
| Model provider | `requirements.txt`: `anthropic==0.84.0`; `anthropic_utils.py` | Anthropic Claude via `anthropic` SDK |
| TTS | `main.py:622-654` (`get_kokoro()`), `main.py:762-774` (ElevenLabs REST call) | **Kokoro (local ONNX, `kokoro-v1.0.onnx` + `voices-v1.0.bin`, both present on disk) is the primary TTS engine; ElevenLabs is the fallback** — `main.py:13276`: `tts_engine = "kokoro" if get_kokoro() else ("elevenlabs" if ELEVENLABS_API_KEY else "none")`. This contradicts `.env.example`'s own comment ("Fallback to OpenAI TTS if OPENAI_API_KEY is set") — **no OpenAI-TTS fallback code exists anywhere in `main.py`** (`grep` for `openai.*tts` = zero hits). It also isn't mentioned at all in CLAUDE.md's "Working" list, which names only ElevenLabs. |
| STT | `main.py:612-620`, `openai-whisper==20250625` in requirements | Local Whisper (`base` model), not a hosted STT API |
| Background workers / scheduler | `apscheduler==3.10.4` in requirements; used only in `pitch_service.py` (not `main.py`) | APScheduler present and used for Phase 1 Gmail/pitch scheduling only |
| Celery / Redis | `grep -rn "celery\|redis"` across `main.py` and `requirements.txt` | **Zero hits.** Confirmed absent — no Celery, no Redis, anywhere in the codebase. |
| Document generation | `legal_data.py` (45 KB), Lex Cipher doc-writer per prior session memory | Present; not re-verified line-by-line this session (out of bounded scope) |
| Billing | `stripe==14.4.1`; `main.py:13619-13762` — checkout, webhook signature verification (`STRIPE_WEBHOOK_SECRET`), `STRIPE_DEV_ALLOW_UNSIGNED` guard | Stripe checkout + webhook handling code is present and wired, not a stub — **contradicts CLAUDE.md's session-opening framing implying it needs verification; the code itself is real**, though live Stripe calls were not exercised this session (out of scope: "no external API calls, no spending money"). |
| Gmail / outbound action | `pitch_service.py:410` `async def send_email(...)`, `pitch_service.py:424` real `service.users().messages().send(...)` Gmail API call, `pitch_service.py:448` `POST /api/gmail/send` | **`sendEmail()` exists and is wired to the real Gmail API.** This directly **contradicts** CLAUDE.md's "Broken / Not Built" list, which states "Gmail OAuth: not started" and "sendEmail() function: not started." |
| Deployment | `Dockerfile`, `railway.toml`, `railway.json` (both present, consistent: Dockerfile builder, `/health` healthcheck, `/data` volume mount); `~/.railway/config.json` shows this directory linked to Railway project `handsome-strength`, environment `production` | Railway deployment is configured and linked from this machine. Documented candidate production hostname (from `docs/RISK_REGISTER.md`, `docs/HANDOVER_EOD_MAY14.md`, `docs/TOMORROW_CHAT_HANDOVER.md`): `maestro-backend-production-6d9c.up.railway.app` — **not curled this session** (external call, out of scope); this is documentary evidence only, not a live-verified URL. |
| Monitoring/logging | `logging_config.py`, `error_reporting.py`, `performance_metrics.py`; `sentry-sdk[fastapi]>=2.0.0` in requirements | Present |
| Secrets/config | `.env.example` (199 lines, single source of documented env vars) | See below — `ENV_VARS.md` referenced by CLAUDE.md **does not exist** in the repo (`ls ENV_VARS.md` → No such file). `.env.example` is the closest analog. |

### Claim Reconciliation Table

| Prior claim | Status | Evidence |
|---|---|---|
| PostgreSQL vs SQLite | **Partially verified — both true, contextually** | `main.py:87,1294-1328` (Postgres via `DATABASE_URL`) and `main.py:14,1194-1269` (SQLite fallback) both exist and are real code paths, not either/or. |
| Celery/Redis | **Contradicted (absent)** | Zero references in `main.py` or `requirements.txt`. |
| Stripe "fully coded" | **Verified (code-complete)** | Checkout + signed webhook handling present at `main.py:13619-13762`; live functioning not tested this session (would spend/contact Stripe). |
| Current backend URL | **Not testable this session** | Candidate hostname documented in three separate docs; not curled per session rules. Railway CLI confirms this directory is linked to project `handsome-strength` / env `production`. |
| Production vs mock tools | **Partially verified** | 24 "consult-only" markers in `main.py` distinguish research/prep-only agents (e.g. Victor/vault-keeper, Luna/vision-forge, Neo/ai-navigator) from action-capable ones. Real outbound HTTP/API calls concentrate in `main.py`, `pitch_service.py` (Gmail), and `social_service.py` (only `*_service.py` file using `httpx`/`requests` directly — Buffer API). Most of the 44 specialist `*_service.py` files do not make outbound calls themselves — actions are centralized through a handful of "wired" services, consistent with README's description. Not exhaustively classified per-tool (out of bounded scope). |
| Frontend/backend integration | **Not testable — no frontend repo present** | See §3. |
| Physical-device readiness | **Not testable this session** | No frontend artifact to test against a device; `phase4_service.py` (device registration, push, version-check, IAP-stub endpoints) exists on the backend per `docs/PHASE_4_FRONTEND_DEFERRED.md`, not independently re-verified line-by-line this session. |
| "16 active agents" (CLAUDE.md) | **Contradicted** | `AGENTS` list in `main.py` (`main.py:119` onward) contains exactly **44** entries; `skills/` directory contains exactly **44** `maestro-*` subdirectories, one per agent ID. Matches README's "5 named + 39 more specialist agents = 44," not CLAUDE.md's "16." |
| "44 roles / 13 departments / 14 callable specialists" (North Star framing) | **Not found in repository** | No occurrence of "14 callable," "13 department," anywhere in `docs/*.md`, `README.md`, or `CLAUDE.md`. The only "16" hit is `docs/HANDOVER_EOD_MAY15_S7.md`: "Phase 0 — Foundation (16 agents...)" — a historical snapshot from an earlier build stage, not a current claim. |
| Gmail OAuth / sendEmail() "not started" (CLAUDE.md) | **Contradicted** | See table above — both exist and are wired to the real Gmail API. |
| ENV_VARS.md exists (CLAUDE.md instructs "document every var in ENV_VARS.md") | **Contradicted (file absent)** | `ls ENV_VARS.md` → not found. `.env.example` (199 lines) is the de facto documentation instead. |

---

## 5. Persona/Agent Inventory Summary

- **44 agents** defined in `main.py`'s `AGENTS` list (`main.py:119` onward), each with `id`, `name`,
  `title`, `skill`, `voice`, `color`, `emoji`, `specialty`. Full list captured in this session's raw
  tool output; not reproduced verbatim here to keep this ledger bounded — see `main.py:119-163` for
  the authoritative source.
- **44 matching `skills/maestro-*` directories** — one-to-one with the `AGENTS` list, no orphans or
  gaps observed in the two counts.
- **24 "consult-only" designations** in `main.py` comments/docstrings, marking agents whose actions
  are search/build/draft/evaluate only (e.g., Victor/vault-keeper, Luna/vision-forge, Neo/ai-navigator,
  Diego/design-studio, Beat/producer-connect — full set not enumerated here; grep target:
  `grep -n "consult-only" main.py`).
- **Named, action-wired outreach agents** per README: Marcus (orchestration), Quinn/pr-agent (press,
  via `pr_service.py`), Avery/booking-agent (venues, via `booking_service.py`), Riley/social-manager
  (Buffer, via `social_service.py`), Sage/release-strategist (cross-phase orchestration, via
  `release_service.py`). Real Gmail sending is centralized in `pitch_service.py`, used by the
  pitch/curator flow.
- This is a mechanical count, not a redesign or adjudication — no agent was retired, renamed, or
  reclassified this session.

---

## 6. Tool/Action Inventory Summary (Bounded)

Not exhaustively enumerated per-tool (would require reading all 44 `*_service.py` files individually,
out of this session's bounded scope). Evidence gathered:

- Outbound HTTP/API libraries (`httpx`/`requests`) appear directly in exactly **one** `*_service.py`
  file: `social_service.py` (Buffer). Other real actions (Gmail send, Stripe, Twilio, TTS/STT) live in
  `main.py` and `pitch_service.py`, not in the individual specialist service files.
  This suggests most of the 44 specialist agents are **consultative/data-prep by architecture**, with
  a small, centralized set of services performing the actual outbound actions — consistent with
  README's description of "outreach agents" as a named subset (Quinn, Avery, Riley, Sage + Gmail via
  Marcus/pitch flow), not all 44.
- Feature-flag-gated stubs exist and are clearly labeled as such in code/docs: `APNS_LIVE`,
  `FCM_LIVE`, `IAP_LIVE` (all default `false`, per `docs/PHASE_4_FRONTEND_DEFERRED.md` and
  `phase4_service.py`), `BUFFER_LIVE` (default `false`, mocked unless explicitly enabled per
  `.env.example:105-110`).
- **Founder-ruling item:** a full mock-vs-real classification of all 44 agents' individual tool
  functions was not performed this session (bounded scope) and should be a named deliverable of a
  future session if the North Star's "invisible operating desks" plan needs to know exactly which
  of the 44 already perform real actions vs. which are consult-only today.

---

## 7. PLMKR/Playmaker Brand Conflict Inventory

- `README.md:1`: **"PLMKR — Playmaker"** — both names used together as the product's own self-identification.
- Entity/ownership split found in `docs/PHASE_4_FRONTEND_DEFERRED.md:40-41`: PLMKR product name is
  associated with **Marquis Holdings LLC**; the separate ReveNation product is associated with
  **Mind Vision LLC**. This is a real, documented entity boundary, not a naming accident.
  This is stated as fact in an existing doc; not independently re-verified against legal records
  this session (out of scope).
- Raw occurrence counts across `*.py` files: `"playmaker"` (case-insensitive) — **166** hits;
  `"PLMKR"` (case-insensitive) — **204** hits. Both names are in active, current use in the codebase;
  neither is a dead/legacy name being phased out as far as this session's evidence shows.
- No instruction in this session renamed or touched any of these occurrences, per the session's
  explicit "never rename Playmaker/PLMKR code or branding" constraint.

---

## 8. Governing-Document Inventory

| Document | Path | Date stated | Git status | Assessment |
|---|---|---|---|---|
| Backend CLAUDE.md | `CLAUDE.md` | "Last updated: March 2026" (footer) | Tracked, committed | **Stale relative to repo state** — describes 16 agents (actual: 44), lists Gmail/sendEmail as not-started (actual: built and wired), doesn't mention Kokoro TTS at all. Still the binding operating-rules document for this session per the instruction hierarchy; its *factual build-status claims* are what's stale, not its process rules. |
| Global CLAUDE.md (user) | `~/.claude/CLAUDE.md` | — | N/A (outside repo) | Governs credential handling and autonomy defaults; out of repo scope. |
| README.md | `README.md` | Undated | Tracked | Phase table and agent count (44) are **more current** than CLAUDE.md's build-status section. |
| `docs/PHASE_4_FRONTEND_DEFERRED.md` | `docs/PHASE_4_FRONTEND_DEFERRED.md` | 2026-05-15 (S8) | Tracked | Authoritative source establishing frontend is a separate repo/entity, not present here. |
| `docs/RISK_REGISTER.md` | `docs/RISK_REGISTER.md` | 2026-05-15 S8 final | Tracked | Claims "421/421 GREEN tests" — a **historical snapshot**, superseded by the much larger current suite (227 test files today; this session's own run below). Not a current claim. |
| `docs/API_REFERENCE.md` | `docs/API_REFERENCE.md` | Undated (content current as of Jun 20 mtime) | Tracked | Not cross-checked line-by-line against live endpoints this session (would require running the app / external calls). |
| `docs/PLMKR_MASTER_NORTH_STAR.html` | `docs/PLMKR_MASTER_NORTH_STAR.html` | No embedded date found; file mtime 2026-08-22 | **Untracked (`??`)** — not yet committed | This is the document referenced in the session brief as "the new Master North Star." Its untracked status is itself evidence: **it has not been formally adopted into the repository's governance record.** The "Marcus plus invisible operating desks" direction it presumably describes remains a **proposed founder direction**, not a ratified one, per this session's instruction not to silently supersede the earlier 14-callable-specialist doctrine — and per §4 above, that "14-callable-specialist" doctrine itself was not found anywhere in the current repo, so there is no committed prior doctrine document to compare it against either. |
| `ENV_VARS.md` (referenced by CLAUDE.md) | — | — | **Does not exist** | See §4. |
| `.openai/hosting.json` | — | — | **Does not exist** | No such governance file found. |

---

## 9. Test and Runtime Evidence

- **Test framework:** pytest. **227 test files** under `tests/` (including `tests/integration/`,
  which has its own `conftest.py`). `pytest --collect-only -q` (non-executing, safe) reports
  **3,272 tests collected** with `--ignore=tests/integration`.
- **Documented safe local command** (from `README.md:78-82` and `docs/LOCAL_DEVELOPMENT.md:148-169`):
  `python3 -m pytest --ignore=tests/integration -q` — the integration subtree is run separately.
- **Command run this session:** `python3 -m pytest --ignore=tests/integration -q`, run twice —
  first attempt piped through `tail -60` lost its buffered output when the 300s timeout killed it
  (`exit 143`, logged output: only "Terminated"); second attempt redirected straight to a log file
  with an extended 580s timeout.
- **Result (second run):** exit code 124 (`timeout` fired). Output shows **2,879 of 3,272 tests
  (88%) executed with zero failures or errors** (all `.` markers, no `F`/`E`), then a native-level
  crash:
  ```
  Fatal Python error: Aborted
  Current thread ...:
    File "/home/tommy/maestro/main.py", line 623 in <module>
    File "<frozen importlib._bootstrap_external>", ... exec_module
    File "/usr/lib/python3.12/importlib/__init__.py", line 131 in reload
    File "/home/tommy/maestro/tests/test_sync_agent_assess.py", line 220 in test_assess_no_anthropic_key_mock_mode_still_works
  ```
  `main.py:623` is inside the Kokoro TTS init block (`get_kokoro()`, §4). The crash occurs while a
  test does `importlib.reload(main)` — this pattern (module-level reload of `main.py` to reset
  process-global state between tests) recurs across the suite (`timeout` then reports "the
  monitored command dumped core"). It was not diagnosed further or fixed in the original session
  (out of scope there: inspection only).
  **[2026-08-28 follow-up]** The suite process aborted during a Kokoro-related
  `importlib.reload(main)` after 2,879 tests had passed. The targeted evidence available in this
  session does not establish whether the cause is application logic, test isolation/order
  dependence, native-library state, or another runtime interaction. A bounded reproduction was run
  this follow-up session: `python3 -m pytest tests/test_sync_agent_assess.py -q` (the exact failing
  test's file, run in isolation) → exit 0, `15 passed, 1 warning in 25.19s`, no abort.
  The targeted test passed in isolation, so the abort was not reproduced by that command. This
  narrows but does not determine the cause; an order-dependent or accumulated native-state
  interaction remains possible. This is distinct from the previously known `test_r3x` `/data`-reload
  fragility noted in project memory — both involve module reload across a large test suite, but no
  causal link between the two has been established.
- **Not run this session (deferred as external/paid-service-risk per session rules):**
  `tests/integration/` (documented as a separately-run subtree — conservatively deferred even though
  its `conftest.py` shows local-only fixtures, since the session brief calls for the *smallest*
  meaningful safe set, and the top-level command is what both README and LOCAL_DEVELOPMENT.md name
  as the standard local run), any live curl against the Railway production URL, any Stripe/Twilio/
  Gmail/ElevenLabs live call, `make build-test` (builds a Docker image — non-trivial local resource
  use, not clearly "already established as safe/cheap" in the same sense as `pytest`).

---

## 10. Verified Facts

1. `main.py` is exactly 16,521 lines — matches the last recorded merge state in project memory
   (`54ea8e9`, "main.py 16521 (−2289)"), confirming no drift since that merge.
2. 44 agents are defined in `main.py`'s `AGENTS` list, with an exact 1:1 match to 44
   `skills/maestro-*` directories.
3. Real Gmail send (`pitch_service.py:410-448`) and real Stripe checkout/webhook handling
   (`main.py:13619-13762`) both exist as working code, contradicting CLAUDE.md's "not started"/
   needs-verification framing for these two items.
4. Kokoro (local ONNX) is the primary TTS engine with ElevenLabs as fallback — not the reverse, and
   not "ElevenLabs only" as CLAUDE.md's build-status section implies; no OpenAI-TTS fallback code
   exists despite `.env.example` documenting one.
5. No frontend/mobile repository for PLMKR exists anywhere discoverable in this environment; the
   project's own documentation (`docs/PHASE_4_FRONTEND_DEFERRED.md`) states it lives in a separate,
   separately-owned repository (`~/Desktop/ReveNation/`) that is not present on this machine.
6. This session's own test run of the documented safe command (`pytest --ignore=tests/integration -q`)
   passed cleanly on 2,879 of 3,272 collected tests (88%, zero failures/errors) before the process
   aborted during a Kokoro-related `importlib.reload(main)` — cause not established; see §9.

---

## 11. Contradicted Prior Claims

- CLAUDE.md: "16 active agents" — actual is 44.
- CLAUDE.md: "Gmail OAuth: not started" / "sendEmail() function: not started" — both exist and are wired.
- CLAUDE.md's implicit ElevenLabs-only TTS framing — Kokoro is primary; ElevenLabs is fallback; no
  OpenAI TTS fallback exists despite being documented in `.env.example`.
- CLAUDE.md: "Document every var in ENV_VARS.md" — no such file exists in the repo.
- `docs/RISK_REGISTER.md`'s "421/421 GREEN tests" (2026-05-15 snapshot) — long superseded by a
  227-test-file suite; not a live/current claim (expected staleness, flagged for completeness).

---

## 12. Unverified Claims

- Whether `docs/API_REFERENCE.md` accurately reflects every live endpoint (would require running
  the app and diffing against real routes — out of this session's bounded, no-external-call scope).
- Whether the documented candidate production hostname
  (`maestro-backend-production-6d9c.up.railway.app`) is currently live and serving this exact
  `54ea8e9` build (not curled this session, per rules).
- The full per-agent, per-tool mock-vs-real classification for all 44 agents (only a partial,
  evidence-backed picture was gathered — see §6).
- Whether `~/Desktop/maestro`'s embedded-PAT remote is still actively used for any push/pull
  workflow, and whether that token needs rotation (flagged as a security note, not resolved).
- Full content/currency check of `docs/PLMKR_MASTER_NORTH_STAR.html` against the rest of governance
  docs — explicitly out of scope per session brief ("do not modify existing governance or North Star
  documents"); only its git-tracked status (untracked) was recorded.

---

## 13. Founder Decisions Still Required

1. Whether `docs/PLMKR_MASTER_NORTH_STAR.html` (currently untracked/unadopted) should be formally
   committed as the governing product direction, and how it reconciles with the fact that no prior
   "14-callable-specialist" doctrine document was found in the repo to formally supersede.
2. Whether PLMKR's Phase 4 mobile frontend will be built fresh, adapted from the separate ReveNation
   codebase, or otherwise sourced — `docs/PHASE_4_FRONTEND_DEFERRED.md` leaves this explicitly open.
3. Whether CLAUDE.md's build-status section should be refreshed to match current repository state
   (44 agents, Gmail/Stripe wired, Kokoro-primary TTS, no ENV_VARS.md) — this session did not modify
   it, per scope.
4. Whether the embedded-PAT remote on `~/Desktop/maestro` needs credential rotation and cleanup.

---

## 14. Recommended Next Bounded Session

A single, narrow follow-up: **refresh CLAUDE.md's "Broken / Not Built" and "Working" build-status
lists against this ledger's findings** (44 agents, Gmail/Stripe status, Kokoro/ElevenLabs order,
ENV_VARS.md gap), with no other scope — not an implementation session, not a North Star adoption
decision, just a documentation-accuracy correction gated on founder sign-off per this session's
"never modify governance docs without instruction" boundary being lifted specifically for that one
file, by the founder, next time.

---

## Appendix — Test Run Result

Command: `python3 -m pytest --ignore=tests/integration -q`, run to completion under a 580s
`timeout` wrapper (second attempt; first attempt's output was lost to a `tail`-buffering artifact,
not a test failure).

- 3,272 tests collected (via a separate, non-executing `--collect-only -q` pass).
- 2,879 tests (88%) ran and passed — no `F` (failure) or `E` (error) markers anywhere in the output.
- Run terminated by a native `Fatal Python error: Aborted` inside a Kokoro-TTS-related
  `importlib.reload(main)` call triggered from `tests/test_sync_agent_assess.py::test_assess_no_anthropic_key_mock_mode_still_works`
  (`main.py:623`), before the remaining ~12% of tests could execute.
- Also recorded: an untracked `.tmp_audit*` / `_audit/*` file set already present in `git status`
  at session start suggests prior sessions may have hit adjacent test-harness issues; this session
  did not read those files to confirm, staying within its own bounded verification pass.
- Full raw output was not committed to the repo (ledger summarizes it); available in this session's
  transcript if needed for follow-up debugging.
