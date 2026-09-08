> **Generated 2026-05-14 from main `a1afbe0`. Source of truth: `app.openapi()` at runtime.**
> This file is regenerated automatically. Do not edit manually.

# PLMKR — Playmaker API Reference

**Version:** 3.0.0 | **Base URL:** Railway deploy root

All authenticated endpoints require the header `X-API-Key: <your-key>`.

---

## Endpoint Index

Sorted alphabetically by path.

| Method | Path | Auth Required | Summary |
|--------|------|---------------|---------|
| GET | `/admin/dashboard` | Yes (X-API-Key) | Admin Dashboard UI |
| GET | `/api/admin/diagnostics` | Yes (X-API-Key) | Admin Diagnostics |
| GET | `/api/admin/diagnostics/anthropic-stats` | Yes (X-API-Key) | Admin Anthropic Stats |
| GET | `/api/admin/diagnostics/gmail-stats` | Yes (X-API-Key) | Admin Gmail Stats |
| GET | `/api/admin/diagnostics/performance` | Yes (X-API-Key) | Admin Diagnostics Performance |
| GET | `/api/admin/diagnostics/scheduler` | Yes (X-API-Key) | Admin Diagnostics Scheduler |
| GET | `/api/admin/health/deep` | No | Admin Health Deep |
| GET | `/api/admin/stats` | Yes (X-API-Key) | Admin Stats |
| GET | `/api/agents` | Yes (X-API-Key) | List Agents |
| GET | `/api/artist` | Yes (X-API-Key) | Get Artist |
| GET | `/api/artist/lookup` | Yes (X-API-Key) | Lookup Artist |
| POST | `/api/artist/save` | Yes (X-API-Key) | Save Artist |
| POST | `/api/auth/send-otp` | Yes (X-API-Key) | Send Otp |
| POST | `/api/auth/verify-otp` | Yes (X-API-Key) | Verify Otp |
| GET | `/api/avatar/status` | Yes (X-API-Key) | Avatar Status |
| POST | `/api/avatar/talk` | Yes (X-API-Key) | Avatar Talk |
| POST | `/api/billing/create-checkout` | Yes (X-API-Key) | Create Checkout |
| GET | `/api/billing/history` | Yes (X-API-Key) | Get Billing History |
| POST | `/api/billing/upgrade` | Yes (X-API-Key) | Billing Upgrade |
| POST | `/api/billing/webhook` | Yes (X-API-Key) | Billing Webhook |
| GET | `/api/booking-contacts` | Yes (X-API-Key) | List Booking Contacts |
| POST | `/api/booking-contacts` | Yes (X-API-Key) | Create Booking Contact |
| GET | `/api/booking-contacts/{contact_id}` | Yes (X-API-Key) | Get Booking Contact |
| PATCH | `/api/booking-contacts/{contact_id}` | Yes (X-API-Key) | Patch Booking Contact |
| POST | `/api/booking-contacts/seed` | Yes (X-API-Key) | Seed Booking Contacts Endpoint |
| GET | `/api/booking-inquiries` | Yes (X-API-Key) | List Booking Inquiries |
| GET | `/api/booking-inquiries/{inquiry_id}` | Yes (X-API-Key) | Get Booking Inquiry |
| PATCH | `/api/booking-inquiries/{inquiry_id}` | Yes (X-API-Key) | Patch Booking Inquiry |
| POST | `/api/booking-inquiries/batch` | Yes (X-API-Key) | Send Booking Emails |
| POST | `/api/booking-inquiries/followups/queue` | Yes (X-API-Key) | Queue Booking Followups |
| POST | `/api/booking-inquiries/generate` | Yes (X-API-Key) | Api Generate Booking |
| POST | `/api/booking-inquiries/scan` | Yes (X-API-Key) | Api Scan Booking Inbox |
| GET | `/api/buffer/auth` | Yes (X-API-Key) | Buffer Auth |
| GET | `/api/buffer/callback` | Yes (X-API-Key) | Buffer Callback |
| GET | `/api/buffer/profiles` | Yes (X-API-Key) | Buffer Profiles |
| GET | `/api/buffer/status` | Yes (X-API-Key) | Buffer Status |
| POST | `/api/chat_stream` | Yes (X-API-Key) | Chat Stream |
| GET | `/api/curators` | Yes (X-API-Key) | List Curators |
| POST | `/api/curators` | Yes (X-API-Key) | Create Curator |
| GET | `/api/curators/{curator_id}` | Yes (X-API-Key) | Get Curator |
| PATCH | `/api/curators/{curator_id}` | Yes (X-API-Key) | Patch Curator |
| POST | `/api/curators/seed` | Yes (X-API-Key) | Seed Curators Endpoint |
| GET | `/api/gmail/auth` | Yes (X-API-Key) | Gmail Auth |
| GET | `/api/gmail/callback` | Yes (X-API-Key) | Gmail Callback |
| POST | `/api/gmail/send` | Yes (X-API-Key) | Api Send Email |
| GET | `/api/gmail/status` | Yes (X-API-Key) | Gmail Status |
| POST | `/api/greet` | Yes (X-API-Key) | Greet |
| POST | `/api/handoff` | Yes (X-API-Key) | Handoff |
| GET | `/api/health` | Yes (X-API-Key) | Api Health |
| GET | `/api/history` | Yes (X-API-Key) | Get History |
| POST | `/api/inbox/scan` | Yes (X-API-Key) | Api Scan Inbox |
| POST | `/api/inbox/scan-all` | Yes (X-API-Key) | Api Scan All Inbox |
| GET | `/api/notifications/{artist_id}` | Yes (X-API-Key) | Get Notifications |
| POST | `/api/notifications/register` | Yes (X-API-Key) | Register Push Token |
| POST | `/api/notifications/send` | Yes (X-API-Key) | Send Notification |
| POST | `/api/operations` | Yes (X-API-Key) | Create Operation |
| POST | `/api/operations/execute` | Yes (X-API-Key) | Create And Execute Operation |
| GET | `/api/operations` | Yes (X-API-Key) | List Operations |
| GET | `/api/operations/{operation_id}` | Yes (X-API-Key) | Get Operation |
| POST | `/api/operations/{operation_id}/approve` | Yes (X-API-Key) | Approve Operation |
| POST | `/api/operations/{operation_id}/execute` | Yes (X-API-Key) | Execute Operation |
| POST | `/api/operations/{operation_id}/reconcile` | Yes (X-API-Key) | Reconcile Operation |
| GET | `/api/pitches` | Yes (X-API-Key) | List Pitches |
| GET | `/api/pitches/{pitch_id}` | Yes (X-API-Key) | Get Pitch |
| PATCH | `/api/pitches/{pitch_id}` | Yes (X-API-Key) | Patch Pitch |
| POST | `/api/pitches/batch` | Yes (X-API-Key) | Send Pitch Emails |
| POST | `/api/pitches/followups/queue` | Yes (X-API-Key) | Queue Followups |
| POST | `/api/pitches/generate` | Yes (X-API-Key) | Api Generate Pitch |
| GET | `/api/pr-contacts` | Yes (X-API-Key) | List Pr Contacts |
| POST | `/api/pr-contacts` | Yes (X-API-Key) | Create Pr Contact |
| GET | `/api/pr-contacts/{contact_id}` | Yes (X-API-Key) | Get Pr Contact |
| PATCH | `/api/pr-contacts/{contact_id}` | Yes (X-API-Key) | Patch Pr Contact |
| POST | `/api/pr-contacts/seed` | Yes (X-API-Key) | Seed Pr Contacts Endpoint |
| GET | `/api/pr-outreach` | Yes (X-API-Key) | List Pr Outreach |
| GET | `/api/pr-outreach/{outreach_id}` | Yes (X-API-Key) | Get Pr Outreach |
| PATCH | `/api/pr-outreach/{outreach_id}` | Yes (X-API-Key) | Patch Pr Outreach |
| POST | `/api/pr-outreach/batch` | Yes (X-API-Key) | Send Pr Emails |
| POST | `/api/pr-outreach/followups/queue` | Yes (X-API-Key) | Queue Pr Followups |
| POST | `/api/pr-outreach/generate` | Yes (X-API-Key) | Api Generate Pr |
| POST | `/api/pr-outreach/scan` | Yes (X-API-Key) | Api Scan Pr Inbox |
| GET | `/api/releases` | Yes (X-API-Key) | List Releases |
| POST | `/api/releases` | Yes (X-API-Key) | Create Release |
| GET | `/api/releases/{release_id}` | Yes (X-API-Key) | Get Release |
| PATCH | `/api/releases/{release_id}` | Yes (X-API-Key) | Patch Release |
| GET | `/api/releases/{release_id}/campaign` | Yes (X-API-Key) | Get Campaign |
| POST | `/api/releases/{release_id}/campaign/execute-due` | Yes (X-API-Key) | Execute Due Actions |
| POST | `/api/releases/{release_id}/generate-campaign` | Yes (X-API-Key) | Generate Campaign |
| GET | `/api/reports/weekly` | Yes (X-API-Key) | List Weekly Reports |
| GET | `/api/reports/weekly/{report_id}` | Yes (X-API-Key) | Get Weekly Report |
| POST | `/api/reports/weekly/generate` | Yes (X-API-Key) | Api Generate Weekly Report |
| GET | `/api/social/posts` | Yes (X-API-Key) | List Posts |
| POST | `/api/social/posts` | Yes (X-API-Key) | Create Post |
| GET | `/api/social/posts/{post_id}` | Yes (X-API-Key) | Get Post |
| PATCH | `/api/social/posts/{post_id}` | Yes (X-API-Key) | Patch Post |
| DELETE | `/api/social/posts/{post_id}` | Yes (X-API-Key) | Delete Post |
| POST | `/api/social/posts/batch` | Yes (X-API-Key) | Schedule Posts |
| POST | `/api/social/posts/generate` | Yes (X-API-Key) | Api Generate Post |
| POST | `/api/transcribe` | Yes (X-API-Key) | Transcribe |
| GET | `/api/tts` | Yes (X-API-Key) | Tts Endpoint |
| POST | `/api/tts/cancel` | Yes (X-API-Key) | Tts Cancel |
| GET | `/api/tts/status` | Yes (X-API-Key) | Tts Status |
| POST | `/api/tts/synth` | Yes (X-API-Key) | Tts Synth |
| GET | `/health` | No | Liveness check |

---

## Endpoint Groups

---

### health — Liveness check

#### GET /health

- **Summary:** Liveness check — returns 200 OK when the service is running
- **Auth:** No
- **Query params:** none
- **Response:** 200 `{}` — service is up

---

### gmail — Gmail OAuth 2.0 connect/disconnect

#### GET /api/gmail/auth

- **Summary:** Gmail Auth — redirect artist to Google OAuth consent screen
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — redirect or OAuth URL

#### GET /api/gmail/callback

- **Summary:** Gmail Callback — exchange OAuth code for tokens and persist in artist profile
- **Auth:** Yes (X-API-Key)
- **Query params:** `code` (string, required), `state` (string, required)
- **Response:** 200 — tokens stored confirmation

#### GET /api/gmail/status

- **Summary:** Gmail Status — return whether artist has active Gmail tokens
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — `{ connected: bool }`

#### POST /api/gmail/send

- **Summary:** Api Send Email — send a one-off email via artist's connected Gmail account
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `to` (string, required)
  - `subject` (string, required)
  - `body` (string, required)
- **Response:** 200 — send confirmation

---

### curators — Curator contact management

#### GET /api/curators

- **Summary:** List Curators — return all curators, optionally filtered
- **Auth:** Yes (X-API-Key)
- **Query params:** `genre` (string, optional), `tier` (string, optional)
- **Response:** 200 — array of curator objects

#### POST /api/curators

- **Summary:** Create Curator — add a new curator to the database
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `name` (string, required)
  - `contact_email` (string, required)
  - `outlet` (string, optional, default `""`)
  - `genres` (array of string, optional, default `[]`)
  - `tier` (string, optional, default `"C"`)
  - `notes` (string, optional)
  - `response_rate` (number, optional, default `0.0`)
- **Response:** 201 — created curator object

#### GET /api/curators/{curator_id}

- **Summary:** Get Curator — fetch a single curator by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `curator_id` (string, required)
- **Response:** 200 — curator object

#### PATCH /api/curators/{curator_id}

- **Summary:** Patch Curator — update one or more fields on a curator
- **Auth:** Yes (X-API-Key)
- **Path params:** `curator_id` (string, required)
- **Request body:** any subset of `name`, `outlet`, `genres`, `tier`, `contact_email`, `notes`, `response_rate` (all optional/nullable)
- **Response:** 200 — updated curator object

#### POST /api/curators/seed

- **Summary:** Seed Curators Endpoint — load curators from `data/curators_seed.json` (idempotent)
- **Auth:** Yes (X-API-Key)
- **Request body:** none
- **Response:** 200 — seed result summary

---

### pitches — Curator pitch lifecycle — generate, send, scan, follow-up

#### GET /api/pitches

- **Summary:** List Pitches — return all pitches for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — array of pitch objects

#### GET /api/pitches/{pitch_id}

- **Summary:** Get Pitch — fetch a single pitch by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `pitch_id` (string, required)
- **Response:** 200 — pitch object

#### PATCH /api/pitches/{pitch_id}

- **Summary:** Patch Pitch — update pitch status
- **Auth:** Yes (X-API-Key)
- **Path params:** `pitch_id` (string, required)
- **Request body:** `status` (string, optional/nullable)
- **Response:** 200 — updated pitch object

#### POST /api/pitches/generate

- **Summary:** Api Generate Pitch — generate (but do not send) a pitch draft for one curator
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `curator_id` (string, required)
  - `track_metadata` (object, optional, default `{}`)
- **Response:** 200 — `{ subject, body, curator_id }`

#### POST /api/pitches/batch

- **Summary:** Send Pitch Emails — generate and send pitches to multiple curators in one call
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `curator_ids` (array of string, required)
  - `track_metadata` (object, optional, default `{}`)
- **Response:** 200 — `{ sent: N, failed: M, errors: [...], pitch_ids: [...] }`

#### POST /api/inbox/scan

- **Summary:** Api Scan Inbox — manually trigger inbox scan for one artist (curator replies)
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — scan results with reply counts

#### POST /api/pitches/followups/queue

- **Summary:** Queue Followups — find sent pitches at follow-up threshold and send follow-up emails
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, optional, default `""` = all artists)
- **Response:** 200 — `{ queued: N, sent: M, failed: K, details: [...] }`

---

### pr — PR contact management and outreach lifecycle

#### GET /api/pr-contacts

- **Summary:** List Pr Contacts — return all PR contacts, optionally filtered
- **Auth:** Yes (X-API-Key)
- **Query params:** `genre` (string, optional), `tier` (string, optional), `outlet_type` (string, optional)
- **Response:** 200 — array of PR contact objects

#### POST /api/pr-contacts

- **Summary:** Create Pr Contact — add a new PR contact
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `name` (string, required)
  - `contact_email` (string, required)
  - `outlet_type` (string, optional, default `"blog"`)
  - `outlet_name` (string, optional)
  - `genres` (array of string, optional)
  - `tier` (string, optional, default `"C"`)
  - `beat` (string, optional)
  - `notes` (string, optional)
  - `response_rate` (number, optional, default `0.0`)
- **Response:** 201 — created PR contact object

#### GET /api/pr-contacts/{contact_id}

- **Summary:** Get Pr Contact — fetch a single PR contact by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `contact_id` (string, required)
- **Response:** 200 — PR contact object

#### PATCH /api/pr-contacts/{contact_id}

- **Summary:** Patch Pr Contact — update one or more fields on a PR contact
- **Auth:** Yes (X-API-Key)
- **Path params:** `contact_id` (string, required)
- **Request body:** any subset of `name`, `outlet_type`, `outlet_name`, `genres`, `tier`, `contact_email`, `beat`, `notes`, `response_rate` (all optional/nullable)
- **Response:** 200 — updated PR contact object

#### POST /api/pr-contacts/seed

- **Summary:** Seed Pr Contacts Endpoint — load PR contacts from seed file (idempotent)
- **Auth:** Yes (X-API-Key)
- **Request body:** none
- **Response:** 200 — seed result summary

#### GET /api/pr-outreach

- **Summary:** List Pr Outreach — return all PR outreach records for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — array of PR outreach objects

#### GET /api/pr-outreach/{outreach_id}

- **Summary:** Get Pr Outreach — fetch a single PR outreach record by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `outreach_id` (string, required)
- **Response:** 200 — PR outreach object

#### PATCH /api/pr-outreach/{outreach_id}

- **Summary:** Patch Pr Outreach — update status or feature URL on a PR outreach record
- **Auth:** Yes (X-API-Key)
- **Path params:** `outreach_id` (string, required)
- **Request body:** `status` (string, optional/nullable), `feature_url` (string, optional/nullable)
- **Response:** 200 — updated PR outreach object

#### POST /api/pr-outreach/generate

- **Summary:** Api Generate Pr — generate (but do not send) a PR email draft for one contact
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `contact_id` (string, required)
  - `release_context` (object, optional, default `{}`)
- **Response:** 200 — `{ subject, body, contact_id }`

#### POST /api/pr-outreach/batch

- **Summary:** Send Pr Emails — generate and send PR emails to multiple contacts in one call
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `contact_ids` (array of string, required)
  - `release_context` (object, optional, default `{}`)
- **Response:** 200 — `{ sent: N, failed: M, errors: [...], outreach_ids: [...] }`

#### POST /api/pr-outreach/scan

- **Summary:** Api Scan Pr Inbox — manually trigger PR inbox scan for one artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — scan results with reply counts

#### POST /api/pr-outreach/followups/queue

- **Summary:** Queue Pr Followups — find sent PR outreach on day 3 or 7 and send follow-ups
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, optional, default `""` = all artists)
- **Response:** 200 — `{ queued: N, sent: M, failed: K, details: [...] }`

---

### booking — Booking contact management and inquiry lifecycle

#### GET /api/booking-contacts

- **Summary:** List Booking Contacts — return all booking contacts, optionally filtered
- **Auth:** Yes (X-API-Key)
- **Query params:** `genre` (string, optional), `tier` (string, optional), `type` (string, optional), `city` (string, optional)
- **Response:** 200 — array of booking contact objects

#### POST /api/booking-contacts

- **Summary:** Create Booking Contact — add a new venue/festival booking contact
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `name` (string, required)
  - `contact_email` (string, required)
  - `venue_or_festival` (string, optional)
  - `type` (string, optional, default `"venue"`)
  - `city` (string, optional)
  - `country` (string, optional)
  - `capacity` (integer, optional, default `0`)
  - `genres` (array of string, optional)
  - `tier` (string, optional, default `"C"`)
  - `notes` (string, optional)
  - `response_rate` (number, optional, default `0.0`)
- **Response:** 201 — created booking contact object

#### GET /api/booking-contacts/{contact_id}

- **Summary:** Get Booking Contact — fetch a single booking contact by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `contact_id` (string, required)
- **Response:** 200 — booking contact object

#### PATCH /api/booking-contacts/{contact_id}

- **Summary:** Patch Booking Contact — update one or more fields on a booking contact
- **Auth:** Yes (X-API-Key)
- **Path params:** `contact_id` (string, required)
- **Request body:** any subset of `name`, `venue_or_festival`, `type`, `city`, `country`, `capacity`, `genres`, `tier`, `contact_email`, `notes`, `response_rate` (all optional/nullable)
- **Response:** 200 — updated booking contact object

#### POST /api/booking-contacts/seed

- **Summary:** Seed Booking Contacts Endpoint — load booking contacts from seed file (idempotent)
- **Auth:** Yes (X-API-Key)
- **Request body:** none
- **Response:** 200 — seed result summary

#### GET /api/booking-inquiries

- **Summary:** List Booking Inquiries — return all booking inquiry records for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — array of booking inquiry objects

#### GET /api/booking-inquiries/{inquiry_id}

- **Summary:** Get Booking Inquiry — fetch a single booking inquiry by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `inquiry_id` (string, required)
- **Response:** 200 — booking inquiry object

#### PATCH /api/booking-inquiries/{inquiry_id}

- **Summary:** Patch Booking Inquiry — update status, booking date, or fee on an inquiry
- **Auth:** Yes (X-API-Key)
- **Path params:** `inquiry_id` (string, required)
- **Request body:** `status` (string, optional/nullable), `booking_date` (string, optional/nullable), `booking_fee` (number, optional/nullable)
- **Response:** 200 — updated booking inquiry object

#### POST /api/booking-inquiries/generate

- **Summary:** Api Generate Booking — generate (but do not send) a booking inquiry email draft
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `contact_id` (string, required)
  - `show_context` (object, optional, default `{}`)
- **Response:** 200 — `{ subject, body, contact_id }`

#### POST /api/booking-inquiries/batch

- **Summary:** Send Booking Emails — generate and send booking emails to multiple contacts in one call
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `contact_ids` (array of string, required)
  - `show_context` (object, optional, default `{}`)
- **Response:** 200 — `{ sent: N, failed: M, errors: [...], inquiry_ids: [...] }`

#### POST /api/booking-inquiries/scan

- **Summary:** Api Scan Booking Inbox — manually trigger booking inbox scan for one artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — scan results with reply counts

#### POST /api/inbox/scan-all

- **Summary:** Api Scan All Inbox — single Gmail auth round-trip runs pitch + PR + booking reply detection
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — combined scan results for all three channels

#### POST /api/booking-inquiries/followups/queue

- **Summary:** Queue Booking Followups — find sent booking inquiries on day 5 or 14 and send follow-ups
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, optional, default `""` = all artists)
- **Response:** 200 — `{ queued: N, sent: M, failed: K, details: [...] }`

---

### social — Social post generation, scheduling, and Buffer integration

#### GET /api/social/posts

- **Summary:** List Posts — return all social posts for an artist, optionally filtered
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required), `platform` (string, optional), `status` (string, optional)
- **Response:** 200 — array of social post objects

#### POST /api/social/posts

- **Summary:** Create Post — create a new social post record
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `platform` (string, required)
  - `content` (string, required)
  - `media_url` (string, optional, default `""`)
  - `scheduled_at` (string ISO datetime, optional/nullable)
  - `status` (string, optional, default `"draft"`)
- **Response:** 201 — created post object

#### GET /api/social/posts/{post_id}

- **Summary:** Get Post — fetch a single social post by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `post_id` (string, required)
- **Response:** 200 — social post object

#### PATCH /api/social/posts/{post_id}

- **Summary:** Patch Post — update content, status, schedule time, or engagement stats
- **Auth:** Yes (X-API-Key)
- **Path params:** `post_id` (string, required)
- **Request body:** any subset of `content`, `media_url`, `status`, `scheduled_at`, `posted_at`, `post_url`, `engagement_stats` (all optional/nullable)
- **Response:** 200 — updated social post object

#### DELETE /api/social/posts/{post_id}

- **Summary:** Delete Post — delete a social post record
- **Auth:** Yes (X-API-Key)
- **Path params:** `post_id` (string, required)
- **Response:** 204 — no content

#### POST /api/social/posts/generate

- **Summary:** Api Generate Post — generate a single social post draft with AI
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `platform` (string, required)
  - `context` (object, optional, default `{}`)
  - `tone` (string, optional, default `"authentic"`)
- **Response:** 200 — `{ platform, content, tone }`

#### POST /api/social/posts/batch

- **Summary:** Schedule Posts — generate N posts per platform and optionally push to Buffer
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `platforms` (array of string, required)
  - `context` (object, optional, default `{}`)
  - `tone` (string, optional, default `"authentic"`)
  - `posts_per_platform` (integer, optional, default `3`)
  - `schedule_buffer` (boolean, optional, default `false`)
  - `buffer_profile_ids` (array of string, optional, default `[]`)
  - `start_date` (string ISO date, optional/nullable)
- **Response:** 200 — `{ generated: N, scheduled_via_buffer: M, errors: [...], post_ids: [...] }`

---

### buffer — Buffer OAuth 2.0 connect/disconnect

#### GET /api/buffer/auth

- **Summary:** Buffer Auth — redirect artist to Buffer OAuth consent screen
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — redirect or OAuth URL

#### GET /api/buffer/callback

- **Summary:** Buffer Callback — handle Buffer OAuth callback, exchange code for access token and store it
- **Auth:** Yes (X-API-Key)
- **Query params:** `code` (string, required), `state` (string, required)
- **Response:** 200 — token stored confirmation

#### GET /api/buffer/status

- **Summary:** Buffer Status — check whether artist has an active Buffer token
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — `{ connected: bool }`

#### GET /api/buffer/profiles

- **Summary:** Buffer Profiles — discover the profiles available to an artist's connected Buffer account
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Persistent side effects:** None; profile discovery is read-only
- **Response:** 200 — `{ artist_id: string, profiles: [{ id, service, formatted_username, ... }] }`
- **Failures:** 409 when Buffer is not connected; 502 when Buffer returns an invalid or failed response

---

### operations — Durable approved provider execution

Supported action types are `gmail.send` and `social.buffer.schedule`. Pitch, PR, booking, release, and batch operations are rejected.

#### GET /api/operations

- **Summary:** List Operations — return bounded, newest-first durable execution history for the authenticated artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required), `limit` (integer, 1–100, default 50), `status` (optional: `pending`, `failed_retryable`, `executing`, `succeeded`, `failed`, or `unknown`)
- **Response:** 200 — `{ operations: operation[], limit }`
- **Security:** `artist_id` is enforced against the authenticated artist session; another artist's history is never returned

#### POST /api/operations

- **Summary:** Create Operation — persist an idempotent operation before approval or execution
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id`, `action_type`, `idempotency_key`, and action-specific `payload`
- **Response:** 200 — `{ operation, created }`

#### POST /api/operations/{operation_id}/approve

- **Summary:** Approve Operation — durably approve a pending or safely retryable operation
- **Auth:** Yes (X-API-Key)
- **Path params:** `operation_id` (string, required)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — operation object with non-null `approved_at`

#### POST /api/operations/{operation_id}/execute

- **Summary:** Execute Operation — execute an approved operation at most once unless a failure is known to be safely retryable
- **Auth:** Yes (X-API-Key)
- **Path params:** `operation_id` (string, required)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — durable operation object
- **Failures:** 409 when the operation has not been approved

#### POST /api/operations/execute

- **Summary:** Create And Execute Operation — resolve an idempotent operation and execute it only if it was previously approved
- **Auth:** Yes (X-API-Key)
- **Request body:** same as `POST /api/operations`
- **Response:** 200 — `{ operation, created }`

#### GET /api/operations/{operation_id}

- **Summary:** Get Operation — retrieve durable status, provider result, error, and reconciliation state
- **Auth:** Yes (X-API-Key)
- **Path params:** `operation_id` (string, required)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — operation object

#### POST /api/operations/{operation_id}/reconcile

- **Summary:** Reconcile Operation — safely inspect an unknown Gmail outcome or mark Buffer for manual reconciliation
- **Auth:** Yes (X-API-Key)
- **Path params:** `operation_id` (string, required)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — updated durable operation object

---

### reports — Weekly activity reports with AI-generated insights

#### GET /api/reports/weekly

- **Summary:** List Weekly Reports — return recent weekly reports for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required), `limit` (integer, optional, default `12`)
- **Response:** 200 — array of weekly report objects

#### GET /api/reports/weekly/{report_id}

- **Summary:** Get Weekly Report — fetch a single weekly report by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `report_id` (string, required)
- **Response:** 200 — weekly report object with AI narrative

#### POST /api/reports/weekly/generate

- **Summary:** Api Generate Weekly Report — generate a new weekly report with AI insights
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `week_start` (string ISO date, optional/nullable)
  - `week_end` (string ISO date, optional/nullable)
- **Response:** 200 — generated weekly report object

---

### releases — Release management and campaign orchestration

#### POST /api/releases

- **Summary:** Create Release — create a new release record
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `title` (string, required)
  - `release_date` (string ISO date, required)
  - `genre` (string, optional/nullable)
  - `mood` (string, optional/nullable)
- **Response:** 200 — created release object

#### GET /api/releases

- **Summary:** List Releases — list all releases for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — array of release objects

#### GET /api/releases/{release_id}

- **Summary:** Get Release — fetch a single release by ID
- **Auth:** Yes (X-API-Key)
- **Path params:** `release_id` (string, required)
- **Response:** 200 — release object

#### PATCH /api/releases/{release_id}

- **Summary:** Patch Release — update release fields
- **Auth:** Yes (X-API-Key)
- **Path params:** `release_id` (string, required)
- **Request body:** any subset of `title`, `release_date`, `genre`, `mood`, `status` (all optional/nullable)
- **Response:** 200 — updated release object

#### POST /api/releases/{release_id}/generate-campaign

- **Summary:** Generate Campaign — generate campaign_actions for a release (idempotent; clears pending actions and regenerates)
- **Auth:** Yes (X-API-Key)
- **Path params:** `release_id` (string, required)
- **Response:** 200 — array of generated campaign actions

#### GET /api/releases/{release_id}/campaign

- **Summary:** Get Campaign — list all campaign actions for a release
- **Auth:** Yes (X-API-Key)
- **Path params:** `release_id` (string, required)
- **Response:** 200 — array of campaign action objects

#### POST /api/releases/{release_id}/campaign/execute-due

- **Summary:** Execute Due Actions — execute all campaign actions for this release where `scheduled_for <= now`
- **Auth:** Yes (X-API-Key)
- **Path params:** `release_id` (string, required)
- **Response:** 200 — `{ executed: N, failed: M, results: [...] }`

---

### admin — Platform administration and diagnostics

#### GET /api/admin/stats

- **Summary:** Admin Stats — return activity stats for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required), `since` (string ISO datetime, optional — omit for all-time)
- **Response:** 200 — pitch/PR/booking/post counts and status breakdown

#### GET /api/admin/diagnostics/anthropic-stats

- **Summary:** Admin Anthropic Stats — per-model Anthropic call counters (total, success, retry, fail)
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — model-keyed counters object

#### GET /api/admin/diagnostics/gmail-stats

- **Summary:** Admin Gmail Stats — per-artist Gmail call counters (total, success, retry, fail)
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — artist-keyed counters object

#### GET /api/admin/diagnostics/performance

- **Summary:** Admin Diagnostics Performance — per-route p50/p95/p99 latency percentiles (rolling 1000 requests)
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — route-keyed latency percentile object

#### GET /api/admin/diagnostics/scheduler

- **Summary:** Admin Diagnostics Scheduler — scheduler queue state: next 10 pending, last 20 completed, 24h status counts
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — `{timestamp, next_pending[{id, release_id, action_type, scheduled_for}], last_completed[{id, release_id, action_type, executed_at, status, result}], counts_24h{status: count}}`

#### GET /api/admin/diagnostics

- **Summary:** Admin Diagnostics — full runtime diagnostics; never exposes env var values
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — runtime diagnostics object (versions, uptime, counts)

#### GET /api/admin/health/deep

- **Summary:** Admin Health Deep — readiness check: DB, scheduler, OAuth tokens, disk, security posture
- **Auth:** No
- **Response:** 200 healthy / 503 when `db_connected=False` (triggers Railway restart)

#### GET /admin/dashboard

- **Summary:** Admin Dashboard — server-rendered HTML monitoring page; consumes all 6 admin diagnostic endpoints via in-page JS
- **Auth:** Yes (X-API-Key — same `_APIKeyMiddleware` as all protected routes)
- **Response:** 200 `text/html` — single-page dashboard; JS prompts for key on first load and stores in `sessionStorage`

---

### agents — Agent roster, TTS, conversation, and billing

#### GET /api/agents

- **Summary:** List Agents — return the full agent roster with skills and voice assignments
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — array of agent objects

#### GET /api/artist

- **Summary:** Get Artist — fetch artist profile
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, optional, default `""`)
- **Response:** 200 — ArtistProfile object

#### GET /api/artist/lookup

- **Summary:** Lookup Artist — find existing artist profile by name (case-insensitive)
- **Auth:** Yes (X-API-Key)
- **Query params:** `name` (string, required)
- **Response:** 200 — ArtistProfile object or null

#### POST /api/artist/save

- **Summary:** Save Artist — create or overwrite an artist profile
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `name` (string, required)
  - `country` (string, optional)
  - `genres` (array of string, optional)
  - `monthly_listeners` (string, optional)
  - `tier` (string, optional, default `"Gold"`)
  - `onboarded` (boolean, optional, default `false`)
  - `bio` (string, optional)
  - `photo` (string, optional/nullable)
- **Response:** 200 — saved ArtistProfile object

#### POST /api/transcribe

- **Summary:** Transcribe — transcribe an audio file to text via Whisper
- **Auth:** Yes (X-API-Key)
- **Request body:** multipart/form-data with `audio` (binary, required)
- **Response:** 200 — `{ text: "..." }`

#### POST /api/greet

- **Summary:** Greet — return the agent's opening line when an artist starts a chat (uses static rotating greetings, zero API calls)
- **Auth:** Yes (X-API-Key)
- **Request body:** form-encoded: `agent_id` (string, required), `tts_on` (string, optional, default `"true"`)
- **Response:** 200 — `{ reply: "...", audio: "..." | null }`

#### POST /api/handoff

- **Summary:** Handoff — new agent delivers a warm personalised greeting after Marcus routes to them, with full conversation context
- **Auth:** Yes (X-API-Key)
- **Request body:** form-encoded:
  - `agent_id` (string, required)
  - `history` (string JSON array, optional, default `"[]"`)
  - `tts_on` (string, optional, default `"true"`)
  - `artist_id` (string, optional, default `""`)
  - `from_agent_id` (string, optional, default `"puppet-master"`)
- **Response:** 200 — `{ reply: "...", audio: "..." | null }`

#### POST /api/chat_stream

- **Summary:** Chat Stream — send a message to an agent and receive a streaming response
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `agent_id` (string, required)
  - `message` (string, required)
  - `artist_id` (string, optional, default `""`)
  - `history` (string JSON array, optional, default `"[]"`)
  - `tts` (boolean, optional, default `true`)
- **Response:** 200 — SSE stream of text chunks + optional audio

#### GET /api/tts

- **Summary:** Tts Endpoint — synthesize text to audio (streaming)
- **Auth:** Yes (X-API-Key)
- **Query params:** `text` (string, required), `voice` (string, optional, default `"am_michael"`)
- **Response:** 200 — audio stream

#### POST /api/tts/cancel

- **Summary:** Tts Cancel — mark a call as ended so any in-flight `/api/tts/synth` for that call returns null
- **Auth:** Yes (X-API-Key)
- **Request body:** `call_id` (string, required)
- **Response:** 200 — confirmation

#### POST /api/tts/synth

- **Summary:** Tts Synth — synthesize text to base64 WAV; used by app to bypass SSE buffering
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `text` (string, required)
  - `voice` (string, optional, default `"am_onyx"`)
  - `call_id` (string, optional, default `""`)
- **Response:** 200 — `{ audio: "..." }` or `{ audio: null, cancelled: true }`

#### GET /api/tts/status

- **Summary:** Tts Status — returns whether TTS is ready (Kokoro loaded OR ElevenLabs key present)
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — `{ ready: bool, engine: "kokoro"|"elevenlabs"|"none" }`

#### GET /api/health

- **Summary:** Api Health — secondary health endpoint
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — `{}`

#### GET /api/history

- **Summary:** Get History — retrieve conversation history for an artist/agent pair
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required), `agent_id` (string, required)
- **Response:** 200 — `{ history: [{ role: string, content: string }, ...] }`

#### POST /api/auth/send-otp

- **Summary:** Send Otp — send a 6-digit OTP via Twilio SMS
- **Auth:** Yes (X-API-Key)
- **Request body:** `phone` (string, required)
- **Response:** 200 — `{ sent: true }`

#### POST /api/auth/verify-otp

- **Summary:** Verify Otp — verify a 6-digit OTP; consumes the code on success
- **Auth:** Yes (X-API-Key)
- **Request body:** `phone` (string, required), `code` (string, required)
- **Response:** 200 — `{ verified: true }` or error

#### POST /api/notifications/register

- **Summary:** Register Push Token — save Expo push token to the artist's profile
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string, required), `push_token` (string, required)
- **Response:** 200 — confirmation

#### POST /api/notifications/send

- **Summary:** Send Notification — send a push notification to an artist via Expo push API and store in history
- **Auth:** Yes (X-API-Key)
- **Request body:**
  - `artist_id` (string, required)
  - `title` (string, required)
  - `body` (string, required)
  - `agent_id` (string, optional, default `""`)
  - `data` (object, optional, default `{}`)
- **Response:** 200 — `{ sent: true, ticket: {...} }`

#### GET /api/notifications/{artist_id}

- **Summary:** Get Notifications — return the artist's notification history
- **Auth:** Yes (X-API-Key)
- **Path params:** `artist_id` (string, required)
- **Response:** 200 — array of notification history objects

#### POST /api/billing/create-checkout

- **Summary:** Create Checkout — create a Stripe checkout session for a tier upgrade
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string, required), `tier` (string, required)
- **Response:** 200 — `{ checkout_url: "https://checkout.stripe.com/..." }`

#### POST /api/billing/upgrade

- **Summary:** Billing Upgrade — directly upgrade an artist's billing tier (admin use)
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string, required), `tier` (string, required)
- **Response:** 200 — updated billing object

#### POST /api/billing/webhook

- **Summary:** Billing Webhook — Stripe webhook handler for payment events
- **Auth:** Yes (X-API-Key)
- **Request body:** raw Stripe webhook payload (Stripe-Signature header required)
- **Response:** 200 — `{ received: true }`

#### GET /api/billing/history

- **Summary:** Get Billing History — return billing event history for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — array of billing event objects

#### POST /api/avatar/talk

- **Summary:** Avatar Talk — send audio chunks to D-ID avatar for lip-sync video generation
- **Auth:** Yes (X-API-Key)
- **Request body:** `agent_id` (string, required), `audio_chunks` (array, required)
- **Response:** 200 — D-ID talk result with video URL

#### GET /api/avatar/status

- **Summary:** Avatar Status — check if D-ID avatar feature is available
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — `{ available: bool }`

---

### Phase 4 — iOS / Push Notification Backend

#### POST /api/devices/register

- **Summary:** Register Device — register an iOS or Android device token for push notifications
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string, required), `platform` ("ios"|"android", required), `token` (string, min 8 chars, required), `app_version` (string, optional)
- **Response:** 201 — `{ id, artist_id, platform, token, app_version }`
- **Notes:** Idempotent — duplicate (artist_id, platform, token) tuples upsert rather than error; `app_version` updated on re-register

#### GET /api/devices

- **Summary:** List Devices — list registered device tokens for an artist
- **Auth:** Yes (X-API-Key)
- **Query params:** `artist_id` (string, required)
- **Response:** 200 — `{ devices: [...] }`

#### POST /api/push/send

- **Summary:** Push Send — send push notification to all registered devices for an artist (internal)
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string, required), `title` (string, required), `body` (string, required), `data` (object, optional, default `{}`)
- **Response:** 200 — `{ sent: int, errors: [...], results: [...] }`
- **Notes:** APNs (iOS) and FCM (Android) clients are stubs behind `APNS_LIVE` / `FCM_LIVE` feature flags (default false); returns `mocked: true` until flags are enabled

#### GET /api/app/config

- **Summary:** App Config — return versioned app configuration, feature flags, kill-switches, and support URLs
- **Auth:** Yes (X-API-Key)
- **Response:** 200 — `{ schema_version, current_version: {ios, android}, min_version: {ios, android}, feature_flags, kill_switches, support_urls }`
- **Notes:** App should cache with a TTL (e.g., 1 hour); kill_switches allow emergency feature disable without a release

#### POST /api/app/version-check

- **Summary:** Version Check — app sends its version; backend responds with update status
- **Auth:** Yes (X-API-Key)
- **Request body:** `platform` ("ios"|"android", required), `current_version` (semver string, required)
- **Response:** 200 — `{ status: "ok"|"soft_update"|"hard_update_required", message, current_version, latest_version, min_version }`
- **Notes:** `hard_update_required` means version is below the minimum floor; `soft_update` means a newer version is available but not required

#### POST /api/iap/validate-receipt

- **Summary:** IAP Validate Receipt — validate Apple in-app purchase receipt (stub)
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_id` (string), `receipt_data` (base64 string), `product_id` (string), `transaction_id` (string)
- **Response:** 200 — `{ valid: bool, mocked: bool, artist_id, product_id, transaction_id, note }`
- **Notes:** Stub behind `IAP_LIVE` flag (default false); Stripe remains the primary billing rail; this endpoint exists for App Store compliance only


#### POST /api/agents/ar-scout/assess

- **Summary:** A&R Deep Assessment — structured scoring of an artist + track using the PLMKR Five-Pillar rubric
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist` (object: name, genre, stage, territory, monthly_listeners, save_rate, release_count, manager, label), `track` (object: title, bpm, duration_sec, lufs, intro_length_sec, genre, features, release_date), `evaluation_stage` (string: watching|approach|due_diligence|internal_pitch), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist, track, evaluation_stage, assessment: { pillars, composite, hard_gates, verdict, trajectory, unfair_advantage, career_ceiling, risk_assessment, five_year_test, remediation_priorities } }`
- **Notes:** `AR_SCOUT_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment without any Anthropic API call. Artist identity is bound from `artist.name` in the request payload (release/track metadata), NOT from the Playmaker account profile.

#### GET /api/agents/ar-scout/assess/demo

- **Summary:** A&R Assessment Demo — renders a standalone HTML scorecard page using the mocked assessment pipeline
- **Auth:** No
- **Response:** 200 — HTML page showing a complete scored assessment with pillar grades, verdict, hard gates, trajectory, risk table, five-year test, and remediation priorities
- **Notes:** Entry point for the A&R pilot; always uses mock mode (no Anthropic call). Demonstrates the full mocked end-to-end: sample artist → assembled prompt → canned response → rendered scorecard.


#### POST /api/agents/grid-prophet/assess

- **Summary:** Marketing Readiness Assessment — structured 8-dimension scoring of an artist + campaign using the PLMKR Marketing rubric
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist` (object: name, genre, stage, territory, monthly_listeners, social_following, save_rate, release_count, has_email_list, email_list_size, has_merch, prior_editorial_placements), `campaign` (object: release_title, release_date, campaign_budget_usd, campaign_window_weeks, primary_platforms, has_tour_dates, tour_territory, is_catalog_campaign), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist, campaign, assessment: { dimensions, composite, band, hard_gates, campaign_priorities, channel_mix_recommendation, confidence_cap, next_best_action } }`
- **Notes:** `GRID_PROPHET_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment without any Anthropic API call. Artist identity is bound from `artist.name` in the request payload, NOT from the Playmaker account profile. Dimensions: virality (0.15), ugc_potential (0.12), platform_fit (0.18), editorial_readiness (0.13), touring_synergy (0.03), merch_d2c (0.10), brand_partnership (0.07), fan_ltv (0.22).


#### POST /api/agents/brand-connect/assess

- **Summary:** Brand Partnership Deal Quality Assessment — structured 8-dimension scoring of an artist + proposed brand deal using the PLMKR DQS rubric
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist` (object: name, genre, stage, territory, tier, monthly_listeners, social_following, engagement_rate), `deal` (object: deal_type, partner_category, exclusivity_category, term_months, has_kill_fee, upfront_payment_pct, diligence_status, prior_artist_deals, has_morality_clause, morality_clause_scoped), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist, deal, assessment: { dimensions, composite, band, hard_gates, deal_priorities, structural_flags, lex_cipher_routing, confidence_cap, next_best_action } }`
- **Notes:** `BRAND_CONNECT_MOCK_MODE=true` (default) returns a canned PROVISIONAL Amber-band assessment without any Anthropic API call. Artist identity is bound from `artist.name` in the request payload, NOT from the Playmaker account profile. Dimensions: strategic_value (0.18), economic_value (0.16), partner_quality (0.14), deal_structure (0.14), risk_exposure (0.14), execution_feasibility (0.10), opportunity_cost (0.08), reversibility (0.06).


#### POST /api/agents/sync-agent/assess

- **Summary:** Sync Licensing Brief-Fit Assessment — four-dimension scoring of an artist track against a sync brief using the PLMKR sync rubric
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `track` (object: title, genre, clearance_status, is_one_stop, has_stems, has_clean_version, duration_sec, bpm, has_samples, has_explicit_lyrics), `brief` (object: project_type, scene_description, budget_range, deadline_days, territory, reference_tracks, lyric_restrictions, exclusivity_required, buyer_class), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, track, brief, assessment: { dimensions, composite, hard_gates, verdict, pitch_rationale, next_action } }`
- **Notes:** `SYNC_AGENT_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 82/100, verdict PITCH) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Dimensions: brief_fit (0.40), clearance_complexity (0.25), turnaround_feasibility (0.20), fee_tier (0.15). Hard gates: clearance UNKNOWN caps composite at 40; turnaround feasibility=1 caps at 25; brief fit ≤2 triggers do-not-pitch.


#### POST /api/agents/lex-cipher/assess

- **Summary:** Legal Deal/Document Quality Assessment — eight-dimension scoring of a music-industry agreement using the PLMKR Deal/Document Quality Rubric. The agent DRAFTS and FLAGS; it never advises, never gives a legal opinion, and never recommends a negotiation position.
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `agreement` (object: agreement_type, parties_described, governing_territory, has_rights_grant, rights_grant_defined, reserved_rights_stated, compensation_defined, royalty_base, deductions_defined, accounting_period_defined, recoupable_costs_enumerated, cross_collateralization, has_termination_for_cause, has_reversion_clause, perpetual_term, has_audit_rights, audit_window_months, has_ip_ownership_warranty, has_indemnification, governing_law_stated, dispute_forum_defined, red_flag_clauses), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, agreement, domain_constraint, assessment: { agreement_type, dimensions, hard_gates, composite, risk_classification, red_flags, not_evaluable, next_best_action, counsel_footer } }`
- **Notes:** `LEX_CIPHER_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 7.4/10, risk NOTABLE_GAPS) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Dimensions: rights_grant_clarity (0.18), compensation_structure (0.16), recoupment_transparency (0.14), exit_reversion (0.13), audit_rights (0.13), warranties_representations (0.12), dispute_resolution (0.08), red_flag_absence (0.06). Four hard gates: HG-1 absent rights grant, HG-2 missing audit rights (or window < 12 months from statement by industry convention), HG-3 unresolved IP-ownership gap, HG-4 active red-flag clause. Every assessment ends with the mandatory qualified-counsel routing footer.

#### POST /api/agents/tour-commander/assess

- **Summary:** Tour Campaign Quality Assessment — eight-dimension scoring of an artist's touring plan using the PLMKR Tour Campaign Quality Rubric. The agent scores PLANNING STATE (not box-office outcome) and routes contracts, work permits, and detailed financial modeling to qualified counsel and the appropriate functions.
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `campaign` (object: campaign_name, tour_type, markets_count, has_international_dates, anchor_dates_confirmed, routing_efficiency_ratio, dead_legs_analyzed, break_even_model_exists, modeled_before_routing, sensitivity_scenarios_modeled, offers_evaluated_against_framework, split_points_analyzed, red_flag_clauses_identified, rider_complete, production_advance_completed, advance_lead_weeks, price_points_market_analyzed, tier_structure_defined, secondary_market_monitored, work_permits_confirmed, withholding_modeled, territory_streaming_signal, merch_planned, hall_fees_documented, settlement_review_process, red_flag_clauses), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, campaign, operating_principle, assessment: { tour_type, dimensions, hard_gates, composite, risk_classification, red_flags, action_profile, not_evaluable, next_best_action, advisory_footer } }`
- **Notes:** `TOUR_COMMANDER_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 3.1/4.3, risk NOTABLE_GAPS) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Dimensions: routing_logic (0.20), financial_model_integrity (0.20), offer_evaluation_quality (0.15), production_readiness (0.15), ticketing_strategy (0.12), international_readiness (0.08), merch_planning (0.06), settlement_process (0.04). Four hard gates: HG-1 break-even model before commitment, HG-2 anchor dates confirmed before routing fill, HG-3 work-permit pathway confirmed for the international leg (NOT APPLICABLE if no international dates), HG-4 production advance completed ≥2 weeks before every show. Composite is on the 0.0–4.3 letter-grade numeric scale; thresholds are industry convention, and risk classification is descriptive severity, not a go/no-go recommendation. Every assessment ends with the operational-advisory footer.

#### POST /api/agents/ink-and-air/assess

- **Summary:** Catalog Health Assessment (publishing & rights) — ten-dimension scoring of an artist's catalog using the PLMKR Catalog Health Rubric. The agent IDENTIFIES and EXPLAINS rights infrastructure; it never advises on negotiation, never gives a legal opinion, and never builds a financial valuation. It answers the Asset-Recovery Frame and routes execution out.
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `catalog` (object: catalog_name, work_count, has_international_usage, pro_registration_complete, mechanical_registration_complete, new_works_registered_before_release, active_income_streams, ugc_monetization_active, neighboring_rights_registered, mlc_unmatched_claims_filed, black_box_claim_strategy, iswc_coverage_complete, ipi_registered_all_parties, active_revenue_without_identifiers, split_sheets_executed, chain_of_title_documented, active_ownership_dispute, one_stop_clearance, sync_rep_in_place, master_rights_clear, subpublishing_in_major_territories, territories_collecting_count, statement_matching_rate, audit_window_status, last_audit_within_window, active_litigation, uncleared_sample_exposure), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, catalog, domain_constraint, assessment: { catalog_name, dimensions, hard_gates, composite, risk_classification, asset_recovery_frame, action_profile, not_evaluable, next_best_action, advisory_footer } }`
- **Notes:** `INK_AND_AIR_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 6.5/10, risk NOTABLE_GAPS) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Ten dimensions: registration_completeness (0.15), collection_coverage (0.15), royalty_recovery_readiness (0.13), identifier_completeness (0.12), ownership_clarity (0.12), licensing_readiness (0.12), territorial_coverage (0.08), metadata_quality (0.07), audit_status (0.04), legal_exposure (0.02). Four hard gates: HG-1 active revenue without ISWC/PRO identifiers, HG-2 formally filed unresolved ownership dispute, HG-3 expired audit window, HG-4 active infringement litigation naming the catalog. Composite is on the 0.0–10.0 letter-grade numeric scale, labeled PROVISIONAL with an unlock condition (≥30 outcome-checked evaluations); risk classification is descriptive severity, not a go/no-go recommendation. Every assessment ends with the rights-infrastructure advisory footer routing execution to counsel, the Finance/Royalties function, and the Sync agent.

#### POST /api/agents/royalty-doctor/assess

- **Summary:** Royalty Recovery Audit — seven-dimension scoring of how completely an artist's catalog is collecting income it has already earned, and how ready it is to recover what it is not, using the PLMKR Royalty Recovery Readiness Rubric. The agent IDENTIFIES, DOCUMENTS, and QUANTIFIES-WHERE-EVIDENCE-SUPPORTS; it rules out normal pipeline lag before claiming loss, never fabricates a recoverable figure, and never renders a legal opinion. It produces a Leak Map and a prioritized Recovery Plan and routes execution out.
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `catalog` (object: catalog_name, work_count, has_international_usage, statement_data_available, registration_export_available, pro_registration_complete, mechanical_registration_complete, neighboring_rights_registered, identifiers_complete, active_revenue_without_identifiers, statements_verified_against_dsp, anomaly_review_practiced, reserve_deductions_reviewed, unmatched_pool_claims_filed, black_box_program_active, proof_of_ownership_ready, active_income_streams, audit_window_status, statement_history_retained, soft_audit_practice, lag_vs_missing_tracked, chain_of_title_documented, split_sheets_executed), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, catalog, domain_constraint, assessment: { catalog_name, dimensions, hard_gates, composite, recovery_posture, leak_map, recovery_plan, not_evaluable, next_best_action, advisory_footer } }`
- **Notes:** `ROYALTY_DOCTOR_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 6.4/10, recovery_posture NOTABLE_LEAKAGE) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Seven dimensions: registration_integrity (0.20), statement_verification (0.18), black_box_recovery_readiness (0.16), pipeline_coverage (0.14), audit_readiness (0.12), collection_timing_discipline (0.10), recovery_documentation (0.10). Four hard gates: HG-1 no statement/registration data (NOT EVALUABLE), HG-2 fabricated recovery figure, HG-3 lag misdiagnosed as underpayment, HG-4 expired audit window (TIME-CRITICAL). Composite is on the 0.0–10.0 letter-grade numeric scale, labeled PROVISIONAL with an unlock condition (≥30 outcome-checked recovery audits); recovery posture is descriptive leakage severity, not a recommendation to sign, sell, or litigate. Recoverable amounts are labeled NOT ESTIMABLE unless statement history and a dated registration-gap range support a figure. Every assessment ends with the advisory footer routing formal audits and legal action to counsel, deal/valuation modeling to the Finance/Royalties function, copyright-law questions to the Publishing function, and sync pursuit to the Sync function.

#### POST /api/agents/producer-connect/assess

- **Summary:** Production Readiness assessment — eight-dimension scoring of how ready a recording project is to reach a release-ready, spec-compliant master on schedule and on budget, using the PLMKR Production Readiness Rubric. Beat (Production Specialist) IDENTIFIES, SCOPES, CASTS, and QC-CHECKS; it never negotiates a contract, prices a placement, splits a composition, or picks the song/talent. It states the two hard gates, computes a PROVISIONAL composite and output band, produces a prioritized Action Profile, and routes execution out.
- **Auth:** Yes (X-API-Key)
- **Request body:** `artist_name` (string), `artist_territory` (string), `project` (object: project_name, project_scope, target_format, reference_tracks_count, creative_brief_signed, sonic_target_measurable, producer_selected, team_credits_genre_matched, rates_confirmed_in_writing, full_team_locked, demo_exists, structure_locked, composition_locked, mix_stage, measured_loudness_in_spec, true_peak_in_spec, format_conforms, stems_complete, qc_bounced, budget_documented, phase_allocated, contingency_reserved, actuals_reconciled, stereo_master_present, isrc_assigned, core_metadata_complete, credits_finalized, schedule_exists, team_availability_confirmed, release_date_buffer_weeks, deal_memos_executed, master_ownership_assigned, wfh_confirmed_session_players, cowrite_flagged), `additional_notes` (string)
- **Response:** 200 — `{ status, mock: bool, artist_name, project, operating_principle, assessment: { project_name, dimensions, hard_gates, composite, readiness_band, action_profile, not_evaluable, next_best_action, advisory_footer } }`
- **Notes:** `PRODUCER_CONNECT_MOCK_MODE=true` (default) returns a canned PROVISIONAL assessment (composite 6.0/10, readiness_band YELLOW_PROCEED_WITH_NAMED_GAPS) without any Anthropic API call. Artist identity is bound from `artist_name` in the request payload, NOT from the Playmaker account profile. Eight dimensions: creative_direction_clarity (0.18), producer_team_fit (0.16), song_arrangement_readiness (0.14), technical_quality_standards (0.14), budget_discipline (0.12), delivery_qc_completeness (0.12), schedule_feasibility (0.08), deal_ownership_structure (0.06). Two hard gates: Technical Quality Gate (Dim-4 = 1 → master NOT DELIVERABLE; only a measured spec failure or a QC bounce triggers it) and Delivery & QC Gate (Dim-6 = 1 → RELEASE BLOCKED; only a confirmed-missing required package element triggers it). Composite is on the 1.0–10.0 letter-grade numeric scale, labeled PROVISIONAL with an unlock condition (≥30 outcome-checked production evaluations); output bands (GREEN/YELLOW/AMBER/RED) and thresholds are industry convention, not a go/no-go verdict. Loudness/true-peak are labeled TARGET until measured, and any platform spec or rate is treated as industry convention / ESTIMATE — NOT QUOTABLE until verified. Every assessment ends with the advisory footer routing contract papering and clause interpretation to qualified counsel, composition splits and co-writes to the Publishing function, placement pricing to the Sync function, and song/talent selection to A&R.

#### POST /api/bank/consult

- **Summary:** Shared knowledge-bank consultation — lets ANY agent (paired or unpaired) pull assembled expert knowledge from ANY of the 9 expert domains (ar, marketing, sync, bizdev, legal, live_touring, publishing, finance_royalties, production). Paired agents (the 9 with a home domain) get their home domain by default plus any query keyword matches; unpaired agents (e.g. merch-empire, storefront) get pure query matches. Retrieval is purely deterministic — no Anthropic/LLM call is made.
- **Auth:** Yes (X-API-Key)
- **Request body:** `agent` (string — agent slug), `query` (string — free-text need)
- **Response:** 200 — `{ status, mock: bool, agent, home_domain (string|null), domains: [domain_key, ...], knowledge: "<assembled text, one section per domain>" }`
- **Notes:** `BANK_CONSULT_MOCK_MODE=true` (default) returns the deterministic consult result directly; the flag only mirrors the assess-route shape — there is no live-LLM branch because retrieval needs none. Domains are routed by keyword over the catalog: the home domain (when the agent is paired) is always included and listed first, then any domain whose trigger keywords appear in the query, de-duplicated and capped at 4. A query with no keyword match and no home domain returns an empty `domains` list and empty `knowledge`. Knowledge text for each domain is assembled from `skills/maestro-<slug>/knowledge` in MANIFEST `load_order`, identical to the per-agent loaders.

## Consequential action safety boundary

Legacy direct execution routes are retained as explicit HTTP 409 tripwires for beta migration.
Gmail sends and Buffer scheduling must use the durable `/api/operations` lifecycle:
create -> approve -> execute (and reconcile when required). Batch pitch, PR, booking, and
direct Buffer scheduling routes may generate/save drafts but may not dispatch providers.

## Authenticated artist identity

When `PLMKR_SESSION_SECRET` and `PLMKR_IDENTITY_SECRET` are configured,
successful `POST /api/auth/verify-otp` responses include a server-owned
`artist_id`, signed Bearer `access_token`, `expires_in`, `expires_at`,
`returning_artist`, and the authenticated artist profile.

Phone numbers are bound to artist accounts using an HMAC fingerprint. Raw phone
numbers are not embedded in artist IDs. Authenticated artist profile, history,
and name-lookup routes reject cross-artist access with HTTP 404 and reject
missing, invalid, or expired artist sessions with HTTP 401. The same ownership
boundary covers chat and handoff context, billing and notifications, Gmail and
Buffer connections, durable operations, pitches, PR outreach, booking
inquiries, social posts, and weekly reports—including routes addressed only by
a resource ID.

Clients send the session as:

`Authorization: Bearer <access_token>`

`POST /api/auth/logout` durably revokes the presented session before the client
clears its local credentials. Revocation records contain only a SHA-256 hash of
the session identifier and expire with the session. SQLite is used for local
operation and PostgreSQL when `DATABASE_URL` is configured. A revoked session
is rejected with HTTP 401 on subsequent requests.

The shared `X-API-Key` remains a separate application-level boundary for
administrative and service access; it is not embedded in mobile clients and is
not an artist identity. OTP bootstrap routes do not require it. A valid artist
Bearer session satisfies middleware for customer API requests. Gmail and Buffer
callbacks rely on signed OAuth state, while the Stripe webhook relies on its
provider signature.

OTP requests are throttled per normalized phone number with a resend cooldown
and rolling send window. Repeated incorrect verification attempts invalidate
the current code and require a new OTP. Release and campaign routes enforce the
same signed artist ownership boundary as other artist resources.

Artist bearer sessions do not grant administrative access. Diagnostic endpoints
under `/api/admin/diagnostics` require the server-side `X-API-Key`, while
`/api/admin/stats` is artist-facing and restricted to the signed artist. Booking,
PR, and curator directories are shared read-only catalogs; creating, editing, or
seeding catalog records requires administrative authorization. Device
registration, device listing, and push delivery are restricted to the signed
artist that owns the device records.
