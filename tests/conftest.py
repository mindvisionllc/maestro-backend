import pytest

# Regression guard (PLMKR restoration, 2026-09-14): main.py's load_dotenv()
# unconditionally loads the real backend/.env at import time. Before this
# session, .env held only ANTHROPIC_API_KEY, so identity_configured() was
# always False in every test process by accident, not by design — every test
# below that calls an artist-scoped endpoint without an Authorization header
# was implicitly relying on that accident to get dev-mode (unauthenticated)
# access. Restoring the previously-working signed-session flow means .env now
# correctly carries PLMKR_SESSION_SECRET / PLMKR_IDENTITY_SECRET (and, once
# scripts/setup_local_secrets.sh is run, real Twilio Verify credentials) —
# without this fixture those real values leak into every test process and
# flip identity_configured() to True, turning the same tests' unauthenticated
# calls into 401s that have nothing to do with what they're testing.
#
# Tests that specifically want one of these set do so explicitly via
# monkeypatch (see tests/test_twilio_verify_otp.py, test_artist_identity.py) —
# autouse fixtures in this file run before a test's own fixtures request
# theirs, so an explicit monkeypatch.setenv still wins for that test.
_ISOLATED_REAL_ENV_KEYS = (
    "PLMKR_SESSION_SECRET",
    "PLMKR_IDENTITY_SECRET",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_VERIFY_SERVICE_SID",
    "TWILIO_VERIFY_SID",
)


@pytest.fixture(autouse=True)
def _isolate_real_dotenv_secrets(monkeypatch):
    """Never let the developer's real .env leak into a test's default env.

    Deliberately setenv("") rather than delenv(): several tests call
    importlib.reload(main) mid-test, which re-runs main.py's top-level
    load_dotenv(path/to/.env). python-dotenv's override=False only skips a
    key that is already *present* in os.environ (empty string counts as
    present) — delenv would make the key look unset again and the reload
    would silently refill it from the real .env, reintroducing exactly the
    bug this fixture exists to prevent. Every consumer (identity_configured(),
    _twilio_verify_config(), the format regexes) treats "" as absent/invalid
    either way, so this stays fail-closed for tests that don't opt in.
    """
    for key in _ISOLATED_REAL_ENV_KEYS:
        monkeypatch.setenv(key, "")
