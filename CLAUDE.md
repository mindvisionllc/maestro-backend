# PLMKR — CLAUDE.md (Backend: ~/maestro/)
# Read this file completely before touching any code.
# These rules are not suggestions. They are hard constraints.

## WHAT YOU ARE BUILDING
PLMKR is an AI-powered artist management platform.
Agents do NOT just give advice — they take real-world action.
An agent that only talks is INCOMPLETE.
Read PLMKR_Master_PRD_v3.docx before every session.

## AUTONOMOUS OPERATION — HOW YOU WORK

You work autonomously through tasks. You do NOT come back to the user mid-task.
You do NOT ask questions. You make decisions and document them.
You do NOT return partial results.
You ONLY return to the user when a task is 100% complete AND verified.
Never pause to ask the user a question or wait for approval between sub-tasks within a phase. Make all decisions independently, document every decision made, and only return to the user when the entire phase is complete and fully verified.

### Before Reporting ANY Task Done — All 4 Must Pass:
1. grep confirms old/broken code is gone — zero hits
2. curl endpoint with real data confirms it works — show actual output
3. Railway logs confirm new code is live — not just status page
4. Screenshot or log output saved as proof

If any of the 4 fail — fix it yourself. Do not report done until all 4 pass.

### If Something Breaks Mid-Task:
- git stash immediately — preserve last working state
- Document exactly what failed and why
- Revert to last commit
- Report the failure with full details
- Do NOT patch forward on top of a broken state
- Do NOT ask the user to run anything manually

### Phase Completion:
- Complete EVERY item in the phase before reporting back
- Run end-to-end test of the entire phase
- Produce a phase completion report:
  - What was built
  - What was tested
  - Test results with proof (curl output, logs, screenshots)
  - What is ready for user review
- Never say a phase is done unless every single item passes verification
- Never move to next phase without explicit user sign-off

---

## HARD RULES — ZERO EXCEPTIONS

### Never Do These:
- Never ask the user to run a command manually — ever
- Never say "this should work" — prove it works
- Never say "likely" or "probably" — read the actual code first
- Never trust status endpoints — always curl with real data
- Never approve a rebuild without: grep zero hits + curl confirmed + screenshot
- Never change more than one thing at a time
- Never use background processes, polling loops, or sleep commands (Railway kills them)
- Never expose API keys in any file that touches Git
- Never modify auth flow, database, or Railway config without explicit instruction
- Never swap a provider or core system without documenting exact current state first
- Never move to the next task without committing the current working change
- Never let a session exceed 2 hours without verified forward progress

### Always Do These:
- Use Plan Mode before making any changes — no exceptions
- Read the actual code before suggesting any fix
- State the exact file, exact line, exact reason before any change
- Commit after every single working change
- Verify Railway is serving new code after every deploy
- Test with real data — not synthetic test data
- Create a new branch before starting any task — never work on main directly
- Check git status before reporting done — no uncommitted changes

---

## INVESTIGATION PROTOCOL

Before forming any hypothesis:
1. Read the actual file — do not assume
2. Check Railway logs — not the status page
3. curl the endpoint with real data
4. State: "I think X is broken because Y" — with file and line number
5. If you cannot point to the specific cause in the code — keep diagnosing

If stuck for more than 20 minutes:
- Stop
- Document what you tried and what you found
- /clear and re-approach with a cleaner prompt

---

## VERIFICATION CHECKLIST (Run After Every Fix)

```
[ ] grep confirms old code is gone — zero hits
[ ] curl endpoint with real payload — show actual response
[ ] Railway logs show new code is live
[ ] Feature tested end-to-end with real data
[ ] Committed to GitHub with descriptive message
[ ] No uncommitted changes remaining
```

---

## COMMIT MESSAGE FORMAT

[PHASE-TASK] short description
Examples:
[0.1] Fix voice mapping — pass agent.voice to tts/synth
[1.2] Add sendEmail() core function with Gmail API
[FIX] Revert broken voice mapping — restore from last working state

---

## COST CONTROL

- Maximum one rebuild per session
- Batch ALL fixes before rebuilding
- Log token usage at start and end of session
- Cache repeated API responses — never pay for the same call twice
- If credits exceed $10 in a session without a working result — stop

---

## RAILWAY / BACKEND KNOWLEDGE

- Always curl actual endpoint with real data after every deploy
- GitHub must be connected for auto-deploy
- Env vars update without rebuild — save and redeploy only
- SSE streaming is buffered by Railway — use separate POST for audio
- Never use background processes or polling loops — Railway kills them
- After any deploy — verify new code is live before reporting done

---

## ENVIRONMENT VARIABLES

Never hardcode. Always use env vars. `ENV_VARS.md` does not exist in this repo (verified
2026-08-28 — see docs/PLMKR_TRUSTED_STATE_LEDGER_2026-08-28.md); `.env.example` is the current
source of documented vars.
Never test with prod keys during development.
Rotate keys immediately if exposed.

Required env vars for this backend:
- ANTHROPIC_API_KEY
- ELEVENLABS_API_KEY (paid Starter account — do not use free tier on Railway)
- CLOUDINARY_CLOUD_NAME
- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN (must be exactly 32 lowercase hex chars)
- TWILIO_VERIFY_SERVICE_SID (Twilio Verify service; OTP no longer uses a From number)
- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET
- OPENAI_API_KEY (usage unresolved: verified 2026-08-28 that no OpenAI-TTS fallback code exists
  in main.py — Kokoro local ONNX is the primary TTS engine, ElevenLabs is the coded fallback)

---

## CURRENT BUILD STATUS — BACKEND

**Evidence baseline: `docs/PLMKR_TRUSTED_STATE_LEDGER_2026-08-28.md`** (verified 2026-08-28 against
main @ 54ea8e9, re-confirmed 2026-08-28 follow-up @ 288644d). This section summarizes that ledger;
consult it directly for file:line evidence before relying on any claim below. Distinguish
*implemented in code* from *runtime-verified* from *production-verified* — code existing does not
mean it has been exercised end-to-end.

Implemented in code (verified 2026-08-28):
- FastAPI on Railway — Railway CLI confirms this repo is linked to project `handsome-strength`,
  environment `production`; not re-curled/re-deployed this session, so "live and auto-deploying"
  is not re-confirmed at runtime.
- **44 coded agents** (main.py `AGENTS` list, 1:1 with `skills/maestro-*` dirs) — not 16.
- TTS: **Kokoro (local ONNX) is the primary engine; ElevenLabs is the coded fallback.** No
  OpenAI-TTS fallback code exists despite being documented in `.env.example`. Voice output
  quality/latency not runtime-verified.
- Stripe billing: checkout + signed-webhook-verification code exists and is wired. Live Stripe
  calls, security review, and production behavior not exercised this session (no external calls
  made).
- Gmail: `sendEmail()` exists and is wired to the real Gmail API (`pitch_service.py`) — not "not
  started." OAuth flow and end-to-end delivery not runtime-verified this session.
- Cloudinary photo redirect — uses agent first name at root level (not re-verified this session).
- Persistence is dual-mode by design: SQLite (`memory.db`, local dev fallback) **and** PostgreSQL
  via `DATABASE_URL` (Railway) — not either/or.

Unresolved / not runtime-verified this session:
- Voice mapping, voice delay, hangup handling, and Phase 0's `CallScreen.js` frontend fix: **no
  frontend repository or `CallScreen.js` file was found anywhere in the audited environment.**
  This is not confirmed fixed or broken — it is unlocatable, so treat as unresolved.
  `~/Desktop/maestro` is a stale duplicate clone of *this backend*, not a frontend; repo docs
  (`docs/PHASE_4_FRONTEND_DEFERRED.md`) point PLMKR's mobile frontend at a separate, absent
  repository/entity for the Phase 4 App Store work specifically — that doc does not establish
  where the Phase 0 call-flow frontend lives.
- Agent handoff context-passing: not re-verified this session.
- Twilio: dev-bypass code exists and is guarded against production use on Railway; the
  32-lowercase-hex `TWILIO_AUTH_TOKEN` format check is confirmed in code. Live SMS not tested.
- All databases (curators, press, venues) and function calling on agents: not re-verified this
  session — status as previously stated is unconfirmed either way.

Test status (bounded evidence, 2026-08-28 — see ledger §9 for detail; do not treat as full-suite-passing):
- 3,272 tests collected (`pytest --ignore=tests/integration --collect-only -q`).
- 2,879 passed cleanly, then the process aborted (native `Fatal Python error: Aborted` during a
  Kokoro-related `importlib.reload(main)`) before the remaining ~12% ran.
- `tests/test_sync_agent_assess.py` run in isolation: 15 passed, no abort. Root cause of the
  full-suite abort is **unresolved** — isolation passing narrows but does not determine it.

Governance note: `docs/PLMKR_MASTER_NORTH_STAR.html` is untracked and has not been adopted into
repository governance as of 2026-08-28. Treat its direction as proposed only — do not implement it
without explicit founder sign-off.

---

## PHASE 0 — CURRENT PRIORITY (Fix Before Building Anything New)

Complete in order. Verify each before moving to next.

0.1 Voice mapping — backend is correct. Fix is in frontend CallScreen.js
0.2 Voice delay — verify /api/tts/synth wiring in frontend
0.3 Audio stops on hangup — AbortController in frontend
0.4 Agent handoff — pass full context: profile + history + reason + actions taken
0.5 Twilio SMS OTP — fix auth token, test real OTP end-to-end

Do not start Phase 1 until all of Phase 0 is verified and user has approved.

---

## SESSION OPENING PROTOCOL

Every session must start with:
1. State the single task for this session
2. State starting credit balance
3. Read this CLAUDE.md completely
4. Read PLMKR_Master_PRD_v3.docx
5. Use Plan Mode before touching any code

## SESSION CLOSING PROTOCOL

Before closing every session:
1. Output 3-line handoff note: done / verified / still open
2. Commit every working change
3. State ending credit balance
4. Note what phase item is next

Last updated: March 2026 (CURRENT BUILD STATUS and ENVIRONMENT VARIABLES sections corrected
2026-08-28 per docs/PLMKR_TRUSTED_STATE_LEDGER_2026-08-28.md; remainder of file unchanged)
