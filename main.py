import os
import re
import io
import sys
import json
import time
import uuid
import secrets
import base64
import random
import tempfile
import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

# Boot: structured logging must be configured before any other module imports
from logging_config import setup_logging, get_logger, bind_request_id
from artist_identity import (
    ArtistAuthError,
    bearer_token,
    decode_session,
    identity_configured,
    issue_session,
    new_artist_id,
    phone_fingerprint,
)
setup_logging()
log = get_logger("main")

from error_reporting import init_error_reporting, capture_exception
init_error_reporting()

from ar_scout_loader import build_ar_scout_system_prompt
from grid_prophet_loader import build_grid_prophet_system_prompt
from sync_agent_loader import build_sync_agent_system_prompt
from brand_connect_loader import build_brand_connect_system_prompt
from lex_cipher_loader import build_lex_cipher_system_prompt
from tour_commander_loader import build_tour_commander_system_prompt
from ink_and_air_loader import build_ink_and_air_system_prompt
from royalty_doctor_loader import build_royalty_doctor_system_prompt
from producer_connect_loader import build_producer_connect_system_prompt
from knowledge_bank.agent_home import consult_for_agent as _bank_consult_for_agent

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse as _RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import anthropic
import httpx

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_AVAILABLE = bool(ANTHROPIC_API_KEY)

# A&R assessment mock mode — default ON so no live Anthropic calls during testing
AR_SCOUT_MOCK_MODE       = os.environ.get("AR_SCOUT_MOCK_MODE",       "true").lower() != "false"
# Marketing assessment mock mode — default ON so no live Anthropic calls during testing
GRID_PROPHET_MOCK_MODE   = os.environ.get("GRID_PROPHET_MOCK_MODE",   "true").lower() != "false"
# Sync licensing assessment mock mode — default ON so no live Anthropic calls during testing
SYNC_AGENT_MOCK_MODE     = os.environ.get("SYNC_AGENT_MOCK_MODE",     "true").lower() != "false"
# Brand Connect assessment mock mode — default ON so no live Anthropic calls during testing
BRAND_CONNECT_MOCK_MODE  = os.environ.get("BRAND_CONNECT_MOCK_MODE",  "true").lower() != "false"
# Legal (Lex-Cipher) assessment mock mode — default ON so no live Anthropic calls during testing
LEX_CIPHER_MOCK_MODE     = os.environ.get("LEX_CIPHER_MOCK_MODE",     "true").lower() != "false"
# Tour & Live (Tour-Commander) assessment mock mode — default ON so no live Anthropic calls during testing
TOUR_COMMANDER_MOCK_MODE = os.environ.get("TOUR_COMMANDER_MOCK_MODE", "true").lower() != "false"
# Publishing & Rights (Ink-and-Air) assessment mock mode — default ON so no live Anthropic calls during testing
INK_AND_AIR_MOCK_MODE    = os.environ.get("INK_AND_AIR_MOCK_MODE",    "true").lower() != "false"
ROYALTY_DOCTOR_MOCK_MODE = os.environ.get("ROYALTY_DOCTOR_MOCK_MODE", "true").lower() != "false"
PRODUCER_CONNECT_MOCK_MODE = os.environ.get("PRODUCER_CONNECT_MOCK_MODE", "true").lower() != "false"
# Shared knowledge-bank consult mock mode — default ON. The bank is deterministic
# (no LLM), so "mock" here just means the route returns the assembled consult result.
BANK_CONSULT_MOCK_MODE     = os.environ.get("BANK_CONSULT_MOCK_MODE",     "true").lower() != "false"

# Base directory: defaults to the folder containing this file so both local
# and Docker deployments work without explicit env overrides.
_BASE = Path(__file__).parent

SKILLS_DIR    = Path(os.environ.get("SKILLS_DIR",     _BASE / "skills"))
ARTISTS_DIR   = Path(os.environ.get("ARTISTS_DIR",    "/data/artists"))
KNOWLEDGE_BASE= Path(os.environ.get("KNOWLEDGE_BASE", _BASE / "KNOWLEDGE.md"))
AUDIO_CACHE   = Path(os.environ.get("AUDIO_CACHE_DIR", "/data/audio_cache"))
AUDIO_CACHE.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES    = int(os.environ.get("MAX_UPLOAD_SIZE", str(25 * 1024 * 1024)))  # 25 MB default
_ALLOWED_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".webm"}
TRANSCRIBE_TIMEOUT_SECONDS = float(os.environ.get("TRANSCRIBE_TIMEOUT_SECONDS", "120"))

# Cloud integrations (optional — graceful degradation when absent)
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
ELEVENLABS_API_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
DATABASE_URL: str     = os.environ.get("DATABASE_URL", "")  # Railway PostgreSQL — persists artist profiles


# API key auth — set PLMKR_API_KEY in Railway env; unset = dev-permissive mode
_PLMKR_API_KEY        = os.environ.get("PLMKR_API_KEY", "")

# CORS — defaults cover Railway backend + Vercel frontend + local dev.
# Override by setting ALLOWED_ORIGINS as a comma-separated list in Railway env vars.
_DEFAULT_CORS_ORIGINS = [
    "https://maestro-backend-production.up.railway.app",
    "https://plmkr.vercel.app",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080",
]
_raw_origins   = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins else _DEFAULT_CORS_ORIGINS
)


# Sync client for non-streaming endpoints, async client for streaming
client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or "placeholder")
async_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY or "placeholder")

if not ANTHROPIC_AVAILABLE:
    log.warning("boot_warning", extra={"event": "boot_warning", "key": "ANTHROPIC_API_KEY",
                "detail": "AI agent chat and handoff routes will return HTTP 503. Set ANTHROPIC_API_KEY to enable."})

# ── Agent roster ───────────────────────────────────────────────────────────────
AGENTS = [
    {"id": "puppet-master",    "name": "Marcus",  "title": "Artist Manager",       "skill": "maestro-puppet-master",    "voice": "am_onyx",      "color": "#7C3AED", "emoji": "🎯", "specialty": "Career strategy, deal analysis, team coordination"},
    {"id": "lex-cipher",       "name": "Lex",     "title": "Entertainment Lawyer", "skill": "maestro-lex-cipher",       "voice": "af_jessica",   "color": "#1D4ED8", "emoji": "⚖️", "specialty": "Contracts, IP, legal protection"},
    {"id": "fund-phantom",     "name": "Jade",    "title": "Grants & Funding",     "skill": "maestro-fund-phantom",     "voice": "af_heart",     "color": "#047857", "emoji": "💰", "specialty": "Grants, funding, arts council applications"},
    {"id": "rights-pulse",     "name": "Ray",     "title": "Performance Rights",   "skill": "maestro-rights-pulse",     "voice": "am_echo",      "color": "#B45309", "emoji": "🎼", "specialty": "PRO registration, performance royalties"},
    {"id": "border-royalty",   "name": "Cleo",    "title": "Neighbouring Rights",  "skill": "maestro-border-royalty",   "voice": "bf_alice",     "color": "#6D28D9", "emoji": "🌍", "specialty": "Neighbouring rights, international royalties"},
    {"id": "mech-ledger",      "name": "Finn",    "title": "Mechanical Royalties", "skill": "maestro-mech-ledger",      "voice": "am_puck",      "color": "#0369A1", "emoji": "⚙️", "specialty": "Mechanical royalties, licensing rates"},
    {"id": "vault-keeper",     "name": "Victor",  "title": "Business Manager",     "skill": "maestro-vault-keeper",     "voice": "bm_george",    "color": "#065F46", "emoji": "🏦", "specialty": "Budgets, cashflow, business finances"},
    {"id": "ledger-lock",      "name": "Nadia",   "title": "Accountant",           "skill": "maestro-ledger-lock",      "voice": "bf_emma",      "color": "#1E40AF", "emoji": "📊", "specialty": "Tax, bookkeeping, royalty statements"},
    {"id": "signal-blaster",   "name": "Zara",    "title": "Publicist",            "skill": "maestro-signal-blaster",   "voice": "af_bella",     "color": "#9D174D", "emoji": "📣", "specialty": "Press, media, PR campaigns"},
    {"id": "grid-prophet",     "name": "Kai",     "title": "Digital Marketing",    "skill": "maestro-grid-prophet",     "voice": "am_adam",      "color": "#7C2D12", "emoji": "📱", "specialty": "Social media, digital growth, algorithms"},
    {"id": "vision-forge",     "name": "Luna",    "title": "AI Visuals",           "skill": "maestro-vision-forge",     "voice": "af_sky",       "color": "#4C1D95", "emoji": "🎨", "specialty": "AI-generated artwork, visual identity"},
    {"id": "design-studio",    "name": "Diego",   "title": "Brand Designer",       "skill": "maestro-design-studio",    "voice": "em_alex",      "color": "#713F12", "emoji": "✏️", "specialty": "Branding, logo, visual assets"},
    {"id": "venue-hawk",       "name": "Ray B",   "title": "Booking Agent",        "skill": "maestro-venue-hawk",       "voice": "am_fenrir",    "color": "#134E4A", "emoji": "🎤", "specialty": "Venue booking, show deals, routing"},
    {"id": "tour-commander",   "name": "Miles",   "title": "Tour Manager",         "skill": "maestro-tour-commander",   "voice": "bm_lewis",     "color": "#1E3A5F", "emoji": "🚌", "specialty": "Tour logistics, production, crew"},
    {"id": "airwave",          "name": "Solo",    "title": "Radio & Playlist",     "skill": "maestro-airwave",          "voice": "am_liam",      "color": "#166534", "emoji": "📻", "specialty": "Radio plugging, playlist pitching"},
    {"id": "brand-connect",    "name": "Nia",     "title": "Brand Partnerships",   "skill": "maestro-brand-connect",    "voice": "af_nova",      "color": "#831843", "emoji": "🤝", "specialty": "Brand deals, endorsements, sponsorships"},
    {"id": "merch-empire",     "name": "Max",     "title": "Merchandise",          "skill": "maestro-merch-empire",     "voice": "am_michael",   "color": "#78350F", "emoji": "👕", "specialty": "Merch design, production, fulfillment"},
    {"id": "fan-builder",      "name": "Aria",    "title": "Fan Engagement",       "skill": "maestro-fan-builder",      "voice": "af_aoede",     "color": "#5B21B6", "emoji": "❤️", "specialty": "Community building, fan clubs, superfans"},
    {"id": "sync-agent",       "name": "Sync",    "title": "Sync Licensing",       "skill": "maestro-sync-agent",       "voice": "bf_isabella",  "color": "#0C4A6E", "emoji": "🎬", "specialty": "TV, film, ad sync placements"},
    {"id": "global-scout",     "name": "Nova",    "title": "International",        "skill": "maestro-global-scout",     "voice": "af_sarah",     "color": "#064E3B", "emoji": "🌐", "specialty": "Global market entry, international deals"},
    {"id": "creative-director","name": "Cree",    "title": "Creative Director",    "skill": "maestro-creative-director","voice": "af_river",     "color": "#3B0764", "emoji": "🎭", "specialty": "Creative vision, rollout strategy, aesthetics"},
    {"id": "data-oracle",      "name": "Data",    "title": "Analytics",            "skill": "maestro-data-oracle",      "voice": "am_eric",      "color": "#1C1917", "emoji": "📈", "specialty": "Streaming data, audience analytics, DSP insights"},
    {"id": "ar-scout",         "name": "Scout",   "title": "A&R",                  "skill": "maestro-ar-scout",         "voice": "af_kore",      "color": "#292524", "emoji": "🔍", "specialty": "Sound development, A&R strategy, demo feedback"},
    {"id": "producer-connect", "name": "Beat",    "title": "Production",           "skill": "maestro-producer-connect", "voice": "am_santa",     "color": "#44403C", "emoji": "🎧", "specialty": "Producer connections, beat licensing, co-writes"},
    {"id": "music-edu",        "name": "Prof",    "title": "Education",            "skill": "maestro-music-edu",        "voice": "bm_daniel",    "color": "#1E3A5F", "emoji": "🎓", "specialty": "Music business education, industry knowledge"},
    {"id": "collab-connect",   "name": "Collab",  "title": "Networking",           "skill": "maestro-collab-connect",   "voice": "af_nicole",    "color": "#14532D", "emoji": "🔗", "specialty": "Artist collaborations, networking, features"},
    {"id": "artist-wellness",  "name": "Maya",    "title": "Wellness",             "skill": "maestro-artist-wellness",  "voice": "hf_alpha",     "color": "#4A1942", "emoji": "🧘", "specialty": "Mental health, work-life balance, burnout prevention"},
    {"id": "press-monitor",    "name": "Press",   "title": "Media Monitor",        "skill": "maestro-press-monitor",    "voice": "af_alloy",     "color": "#27272A", "emoji": "📰", "specialty": "Press tracking, sentiment, media coverage"},
    {"id": "live-coach",       "name": "Coach",   "title": "Performance Coach",    "skill": "maestro-live-coach",       "voice": "hm_omega",     "color": "#1C1917", "emoji": "🎙️", "specialty": "Stage presence, vocals, live performance"},
    {"id": "audio-quality",    "name": "Audio",   "title": "Quality Control",      "skill": "maestro-audio-quality",    "voice": "im_nicola",    "color": "#0F172A", "emoji": "🔊", "specialty": "Mix feedback, mastering standards, audio QC"},
    {"id": "ai-navigator",     "name": "Neo",     "title": "AI Tools",             "skill": "maestro-ai-navigator",     "voice": "hm_psi",       "color": "#1E1B4B", "emoji": "🤖", "specialty": "AI tools, automation, tech stack for artists"},
    {"id": "royalty-doctor",   "name": "Doc",     "title": "Royalty Recovery",     "skill": "maestro-royalty-doctor",   "voice": "bm_fable",     "color": "#0C4A6E", "emoji": "💊", "specialty": "Unclaimed royalties, black box money, audits"},
    {"id": "video-director",   "name": "Reel",    "title": "Music Video",          "skill": "maestro-video-director",   "voice": "em_santa",     "color": "#1A1A2E", "emoji": "🎥", "specialty": "Music video production, directors, budgets"},
    {"id": "mobile-monetize",  "name": "Mo",      "title": "Monetization",         "skill": "maestro-mobile-monetize",  "voice": "am_michael",   "color": "#064E3B", "emoji": "📲", "specialty": "TikTok, YouTube, mobile platform monetization"},
    {"id": "storefront",       "name": "Store",   "title": "Fan Store",            "skill": "maestro-storefront",       "voice": "bf_lily",      "color": "#3B0764", "emoji": "🛍️", "specialty": "Direct-to-fan store, digital products, memberships"},
    {"id": "ink-and-air",      "name": "Reed",    "title": "Music Publisher",      "skill": "maestro-ink-and-air",      "voice": "pm_alex",      "color": "#1E3A5F", "emoji": "📝", "specialty": "Publishing deals, sync licensing, songwriting royalties"},
    {"id": "live-wire",        "name": "Knox",    "title": "Booking Agent",        "skill": "maestro-live-wire",        "voice": "zm_yunxi",     "color": "#7F1D1D", "emoji": "🎤", "specialty": "Live shows, touring, festival bookings, performance fees"},
    {"id": "label-services",   "name": "Tommy",   "title": "Label Services",       "skill": "maestro-label-services",   "voice": "bm_george",    "color": "#0F172A", "emoji": "🏷️", "specialty": "Distribution, release planning, label setup, delivery to DSPs"},
    {"id": "content-forge",    "name": "Pen",     "title": "Content Creation",     "skill": "maestro-content-forge",    "voice": "if_sara",      "color": "#1C1917", "emoji": "✍️", "specialty": "Captions, bios, press releases, content strategy"},
    {"id": "schedule-keeper",  "name": "Cal",     "title": "Scheduling",           "skill": "maestro-schedule-keeper",  "voice": "af_sarah",     "color": "#27272A", "emoji": "📅", "specialty": "Calendar, release scheduling, deadline management"},
    {"id": "pr-agent",         "name": "Quinn",   "title": "PR Manager",           "skill": "maestro-pr-agent",         "voice": "af_nova",      "color": "#BE185D", "emoji": "📰", "specialty": "Press outreach, blogs, podcasts, magazines, editorial placement"},
    {"id": "booking-agent",    "name": "Avery",   "title": "Booking Agent",        "skill": "maestro-booking-agent",    "voice": "bm_fable",     "color": "#0F766E", "emoji": "🎤", "specialty": "Venue booking, festival pitching, promoter outreach, show deals"},
    {"id": "social-manager",   "name": "Riley",   "title": "Social Media Manager", "skill": "maestro-social-manager",   "voice": "af_sky",       "color": "#0EA5E9", "emoji": "📲", "specialty": "Social post strategy, Buffer scheduling, platform-specific content, trend awareness"},
    {"id": "release-strategist","name": "Sage",   "title": "Release Strategist",   "skill": "maestro-release-strategist","voice": "af_jessica",   "color": "#7C3AED", "emoji": "🚀", "specialty": "Release planning, campaign orchestration, cross-phase launch strategy"},
]

AGENTS_BY_ID = {a["id"]: a for a in AGENTS}

# ── Pre-load all SKILL.md files at startup ─────────────────────────────────────
SKILLS_CACHE: dict[str, str] = {}

def _preload_skills() -> None:
    loaded, missing = 0, 0
    for a in AGENTS:
        p = SKILLS_DIR / a["skill"] / "SKILL.md"
        if p.exists():
            SKILLS_CACHE[a["skill"]] = p.read_text()
            loaded += 1
        else:
            missing += 1
    log.info("skills_preloaded", extra={"event": "skills_preloaded", "loaded": loaded, "missing": missing})

_preload_skills()

# ── Static greetings for ALL agents — zero API calls on agent open ─────────────
#
# FORMAT: one warm, specific, first-person sentence.
# Each agent states their name, title, and one concrete thing they handle.
# No questions. No markdown. Natural spoken cadence.
# Marcus has a rotating pool; all others have 3 variants for variety.
#
_AGENT_GREETINGS: dict[str, list[str]] = {
    "puppet-master": [
        "Hey, I'm Marcus — your Artist Manager at Playmaker. I run point on your entire career, from deals to team coordination. What's the most pressing thing on your plate right now?",
        "I'm Marcus, your Artist Manager at Playmaker. From contract strategy to connecting you with the right specialists, everything runs through me. What do you need to move on today?",
        "Marcus here — your Artist Manager at Playmaker. I've got full visibility across your operation: legal, finance, marketing, touring, all of it. What's going on with your career right now?",
        "Good to connect. I'm Marcus, your Artist Manager at Playmaker — every major decision, deal, and direction goes through me first. What's the priority today?",
        "I'm Marcus, your Artist Manager at Playmaker — strategy, deals, and your full team all run through me. I'm here whenever you need to think through a move. What's on your mind?",
    ],
    "lex-cipher": [
        "I'm Lex, your Entertainment Lawyer at Playmaker. I handle contracts, IP protection, and making sure no one takes advantage of you in a deal. What legal situation are we looking at?",
        "Lex here — your Entertainment Lawyer at Playmaker. Whether it's a record deal, publishing agreement, or a rights dispute, I've got you covered. What do you need reviewed?",
        "I'm Lex, your Entertainment Lawyer at Playmaker. I read every clause so you don't have to. What contract or legal question is on the table?",
    ],
    "fund-phantom": [
        "I'm Jade, your Grants and Funding specialist at Playmaker. I find and secure money that artists leave on the table — arts council grants, government funding, and private opportunities. What stage are you at?",
        "Jade here — Grants and Funding at Playmaker. I specialise in grants and arts funding. There's more money available for independent artists than most realise. Let's find what you qualify for.",
        "I'm Jade, your Funding specialist at Playmaker. From FACTOR to arts councils to private foundations, I know where the money is and how to apply for it. What do you need?",
    ],
    "rights-pulse": [
        "I'm Ray, your Performance Rights specialist at Playmaker. I make sure you're registered with the right PROs and collecting every penny you're owed in performance royalties. Where are we at?",
        "Ray here — Performance Rights at Playmaker. If you're not properly registered with your PRO, you're leaving money on the table every time your music plays. Let's sort that out.",
        "I'm Ray, your Performance Rights officer at Playmaker. PRO registration, performance royalties, live performance income — that's my world. What do you need to get set up?",
    ],
    "border-royalty": [
        "I'm Cleo, your Neighbouring Rights specialist at Playmaker. If your recordings are played on radio or in public spaces, you're owed money most artists never collect. Let's fix that.",
        "Cleo here — Neighbouring Rights at Playmaker. I handle neighbouring rights and international royalty collection. There's money sitting unclaimed in markets around the world with your name on it.",
        "I'm Cleo, your Neighbouring Rights advisor at Playmaker. Master recording royalties from broadcast and public performance are separate from your PRO income — and often overlooked. I'll walk you through it.",
    ],
    "mech-ledger": [
        "I'm Finn, your Mechanical Royalties specialist at Playmaker. Every time someone streams or downloads your music, mechanical royalties are generated. I make sure you collect all of them.",
        "Finn here — Mechanical Royalties at Playmaker. Whether it's streaming mechanicals, download licenses, or physical reproduction, I track the rates and make sure you're paid correctly.",
        "I'm Finn, your Mechanical Royalties advisor at Playmaker. Publishing income, licensing rates, Harry Fox, MLC — I handle the mechanical side of your publishing income. What do you need?",
    ],
    "vault-keeper": [
        "I'm Victor, your Business Manager at Playmaker. I oversee your finances at the macro level — budgets, cashflow, business structure, and making sure your money is working for you.",
        "Victor here — Business Management at Playmaker. Think of me as the CFO for your music career. I handle the financial strategy so you can focus on the music.",
        "I'm Victor, your Business Manager at Playmaker. Revenue planning, expense management, entity structure — I keep the business side of your career solid. What are we working on?",
    ],
    "ledger-lock": [
        "I'm Nadia, your Accountant at Playmaker. Tax returns, bookkeeping, royalty statement reconciliation — I make sure your numbers are clean and you keep as much of your income as legally possible.",
        "Nadia here — Accounting and Tax at Playmaker. Tour expenses, recording deductions, quarterly filings — I handle the detail work that protects your income. What do you need sorted?",
        "I'm Nadia, your Accountant at Playmaker. I specialise in music industry taxation — there are deductions most artists miss entirely. Let's make sure you're not one of them.",
    ],
    "signal-blaster": [
        "I'm Zara, your Publicist at Playmaker. Press coverage, media strategy, narrative control — I make sure the right people are talking about you in the right way. What's the story we're telling?",
        "Zara here — PR and Press at Playmaker. From release campaigns to crisis management, I handle your relationship with the media. What are we launching or protecting?",
        "I'm Zara, your Publicist at Playmaker. Blogs, magazines, radio interviews, editorial features — I open those doors and make sure you walk through them looking great. What do you need?",
    ],
    "grid-prophet": [
        "I'm Kai, your Digital Marketing specialist at Playmaker. Social growth, platform algorithms, paid campaigns — I turn your online presence into a career asset. What platform are we focused on?",
        "Kai here — Digital Marketing at Playmaker. TikTok strategy, Instagram growth, YouTube optimisation, Facebook ads — I know what moves the needle online for independent artists.",
        "I'm Kai, your Digital Marketing advisor at Playmaker. Data-driven growth across every platform. I'll show you exactly where to spend your time and money for maximum return.",
    ],
    "vision-forge": [
        "I'm Luna, your AI Visuals specialist at Playmaker. Album artwork, press photos, video thumbnails, brand visuals — I use AI tools to create professional-grade imagery at a fraction of the cost.",
        "Luna here — AI Visuals at Playmaker. Whether it's a full visual identity or a single cover, I use the latest AI image tools to bring your creative vision to life fast.",
        "I'm Luna, your AI Visuals advisor at Playmaker. I'll help you create a consistent, striking visual world for your music using the best AI image and video tools available.",
    ],
    "design-studio": [
        "I'm Diego, your Brand Designer at Playmaker. Logo, typography, colour palette, visual identity — I build the visual system that makes your brand instantly recognisable.",
        "Diego here — Visual Branding at Playmaker. From your first logo to a full brand overhaul, I create the visual language that represents who you are as an artist.",
        "I'm Diego, your Brand Designer at Playmaker. Merch layouts, press kits, social templates — everything looks consistent, professional, and unmistakably you. What are we building?",
    ],
    "venue-hawk": [
        "I'm Ray B, your Booking Agent at Playmaker. Venues, promoters, show deals, routing — I find the right stages and negotiate the right terms to build your live career.",
        "Ray B here — Global Booking at Playmaker. From local showcases to international tours, I know the venue landscape and the promoters who matter. What are we booking?",
        "I'm Ray B, your Booking Agent at Playmaker. A great live strategy builds your fanbase faster than almost anything else. Let's map out your live game plan.",
    ],
    "tour-commander": [
        "I'm Miles, your Tour Manager at Playmaker. Logistics, production, crew, travel, advancing — I handle everything that has to happen for a show to actually run smoothly.",
        "Miles here — Tour Management at Playmaker. Budget planning, venue advancing, crew hiring, day-of-show operations — I keep tours moving without the chaos. What are we planning?",
        "I'm Miles, your Tour Manager at Playmaker. From the first rehearsal to the last load-out, I make sure every show is executed professionally. What tour are we working on?",
    ],
    "airwave": [
        "I'm Solo, your Radio and Playlist specialist at Playmaker. Getting your music heard by the right programmers, curators, and playlist editors is what I do. What release are we pitching?",
        "Solo here — Radio and Playlist at Playmaker. Commercial radio, college radio, Spotify editorial, Apple Music playlists — I know the gatekeepers and how to reach them.",
        "I'm Solo, your Radio and Playlist advisor at Playmaker. A single playlist placement or radio spin can change your streaming numbers overnight. Let's build a pitching strategy.",
    ],
    "brand-connect": [
        "I'm Nia, your Brand Partnerships specialist at Playmaker. Sponsorships, endorsements, brand deals — I connect your image to brands that make sense and pay well. What's your brand like?",
        "Nia here — Brand Partnerships at Playmaker. From clothing brands to beverage companies to tech sponsors, I find deals that add to your credibility and your income.",
        "I'm Nia, your Brand Partnerships advisor at Playmaker. The right brand deal can fund a tour, boost your visibility, and expand your audience. Let's find your perfect partners.",
    ],
    "merch-empire": [
        "I'm Max, your Merchandise specialist at Playmaker. Design, production, fulfilment, pricing — I build merch operations that actually make money and don't leave you with boxes of unsold stock.",
        "Max here — Merchandise at Playmaker. From your first run of tees to a full product line, I handle the sourcing, production, and strategy to make your merch profitable.",
        "I'm Max, your Merch advisor at Playmaker. Great merch is a revenue stream, a marketing tool, and a fan loyalty system all in one. Let's build yours properly.",
    ],
    "fan-builder": [
        "I'm Aria, your Fan Engagement specialist at Playmaker. Community building, superfan strategy, fan clubs — I turn casual listeners into the loyal core that sustains a long career.",
        "Aria here — Fan Engagement at Playmaker. Discord communities, Patreon, fan newsletters, meet-and-greets — I create the systems that deepen the relationship between you and your fans.",
        "I'm Aria, your Fan Engagement advisor at Playmaker. A thousand true fans beats a million passive listeners every time. Let's build yours intentionally.",
    ],
    "sync-agent": [
        "I'm Sync, your Sync Licensing specialist at Playmaker. TV placements, film scores, advertising, video games — I get your music heard in places that pay well and expand your audience.",
        "Sync here — Sync Licensing at Playmaker. A single ad placement or TV sync can generate more income than months of streaming. I know the music supervisors who make those decisions.",
        "I'm Sync, your Sync Licensing advisor at Playmaker. I'll help you prepare your catalogue for sync, pitch to the right supervisors, and negotiate deals that respect your work.",
    ],
    "global-scout": [
        "I'm Nova, your International Markets specialist at Playmaker. Touring abroad, licensing in new territories, finding international partners — I open doors in markets where your music can grow.",
        "Nova here — International at Playmaker. The global music market is huge and most independent artists ignore 90% of it. I find the territories where your sound has real traction.",
        "I'm Nova, your International advisor at Playmaker. Distribution deals, foreign PRO registration, international touring strategy — I map out your global expansion one market at a time.",
    ],
    "creative-director": [
        "I'm Cree, your Creative Director at Playmaker. Release strategy, aesthetic vision, campaign rollout — I make sure every project you put out has a coherent creative identity that lands.",
        "Cree here — Creative Direction at Playmaker. The gap between a good song and a successful release is usually creative strategy. I close that gap. What are we releasing?",
        "I'm Cree, your Creative Director at Playmaker. From the first single rollout to the album campaign, I design the creative journey that turns a project into a cultural moment.",
    ],
    "data-oracle": [
        "I'm Data, your Analytics specialist at Playmaker. Streaming numbers, audience demographics, DSP insights — I translate the data into decisions that move your career forward.",
        "Data here — Analytics at Playmaker. Spotify for Artists, Apple Music Analytics, social insights — I know what the numbers mean and what you should do about them.",
        "I'm Data, your Analytics advisor at Playmaker. Your streaming data is telling you exactly where your fans are and what they want. Let's listen to it together.",
    ],
    "ar-scout": [
        "I'm Scout, your A&R advisor at Playmaker. Sound development, demo feedback, career positioning — I look at your music through the lens of the industry and help you develop your sound.",
        "Scout here — A&R at Playmaker. I give you the honest feedback that helps you grow, and the strategic advice that helps you find your place in the market.",
        "I'm Scout, your A&R specialist at Playmaker. From refining your sound to identifying what makes you unique in your lane — I help you become the artist you're capable of being.",
    ],
    "producer-connect": [
        "I'm Beat, your Production specialist at Playmaker. Producer connections, beat licensing, co-writing sessions, studio rates — I help you find the right collaborators and get the right deals.",
        "Beat here — Production at Playmaker. Whether you need a producer, a co-writer, or help negotiating a beat lease, I know the landscape and the right people in it.",
        "I'm Beat, your Production advisor at Playmaker. The right producer collaboration can define your sound for years. I help you find that match and structure it properly.",
    ],
    "music-edu": [
        "I'm Prof, your Music Business educator at Playmaker. Industry knowledge, terminology, how deals work, what to watch out for — I make sure you understand the business you're in.",
        "Prof here — Music Business Education at Playmaker. The more you understand how this industry works, the better decisions you make. Ask me anything you've been afraid to ask.",
        "I'm Prof, your Music Business advisor at Playmaker. From royalty splits to label structures to DIY strategies — I explain the music business clearly so you can navigate it confidently.",
    ],
    "collab-connect": [
        "I'm Collab, your Networking specialist at Playmaker. Artist collaborations, features, industry introductions — I help you build the relationships that open doors in this business.",
        "Collab here — Networking and Connections at Playmaker. The right feature, co-write, or industry introduction can change your career trajectory. I help you find and land those opportunities.",
        "I'm Collab, your Networking advisor at Playmaker. A strong network is as valuable as talent in this industry. Let's build yours strategically.",
    ],
    "artist-wellness": [
        "I'm Maya, your Wellness advisor at Playmaker. Mental health, burnout prevention, work-life balance — I make sure the demands of this career don't break the person behind the music.",
        "Maya here — Artist Wellness at Playmaker. The music industry is intense. I help artists build the mental and emotional resilience to sustain a long career without losing themselves.",
        "I'm Maya, your Wellness specialist at Playmaker. Touring stress, creative blocks, financial anxiety, relationship strain — these are real parts of this career. Let's talk about what you're carrying.",
    ],
    "press-monitor": [
        "I'm Press, your Media Monitor at Playmaker. I track every mention, review, and article about you so you always know what's being said and can respond strategically.",
        "Press here — Media Monitoring at Playmaker. Brand sentiment, press coverage, social mentions — I give you a real-time picture of your public narrative.",
        "I'm Press, your Media Monitor at Playmaker. Knowing what the media and public are saying about you is the first step to shaping the story. I keep you informed.",
    ],
    "live-coach": [
        "I'm Coach, your Performance Coach at Playmaker. Stage presence, vocal performance, crowd engagement, nerves — I help you become the performer your recorded music promises you are.",
        "Coach here — Performance Coaching at Playmaker. The gap between a good artist and a great live performer is coachable. I close that gap. What does your live show look like right now?",
        "I'm Coach, your Performance Coach at Playmaker. Whether you're about to play your first show or your hundredth, there's always room to become a more commanding performer.",
    ],
    "audio-quality": [
        "I'm Audio, your Quality Control specialist at Playmaker. Mix feedback, mastering standards, loudness levels, format requirements — I make sure your recordings meet professional release standards.",
        "Audio here — Quality Control at Playmaker. Before anything goes out, I listen with professional ears. Clarity, balance, punch, consistency — I'll tell you exactly where you stand.",
        "I'm Audio, your QC advisor at Playmaker. A bad mix can undermine a great song. I give you the honest technical feedback that helps your music compete at the highest level.",
    ],
    "ai-navigator": [
        "I'm Neo, your AI Tools specialist at Playmaker. From music generation to image creation to workflow automation — I know every AI tool that can save you time and money as an artist.",
        "Neo here — AI Tools and Automation at Playmaker. The AI landscape for musicians is moving fast. I help you find, learn, and use the right tools without the overwhelm.",
        "I'm Neo, your AI Navigator at Playmaker. Whether it's generating stems, creating visuals, writing bios, or automating your schedule — I'll show you exactly which AI tools to use and how.",
    ],
    "royalty-doctor": [
        "I'm Doc, your Royalty Recovery specialist at Playmaker. Unclaimed royalties, black box money, audit discrepancies — if you're owed money somewhere in the system, I find it and recover it.",
        "Doc here — Royalty Recovery at Playmaker. Most artists are owed more than they're collecting. I audit your royalty streams and track down everything that hasn't found its way to you.",
        "I'm Doc, your Royalty Doctor at Playmaker. From international black box funds to DSP underpayments — I've seen every way royalties go missing and I know how to get them back.",
    ],
    "video-director": [
        "I'm Reel, your Music Video strategist at Playmaker. Concept development, director sourcing, budget planning, distribution strategy — I help you make videos that serve your career.",
        "Reel here — Music Video at Playmaker. A great music video is a marketing asset, not just a creative project. I help you plan, produce, and maximise the impact of every video.",
        "I'm Reel, your Video Director advisor at Playmaker. From low-budget performance videos to fully produced visual pieces — I help you get the most out of every dollar you spend on visuals.",
    ],
    "mobile-monetize": [
        "I'm Mo, your Monetization specialist at Playmaker. TikTok Creator Fund, YouTube AdSense, Instagram monetization, Spotify fan tips — I help you activate every revenue stream on every platform.",
        "Mo here — Platform Monetization at Playmaker. Most artists leave significant money on digital platforms because they haven't set up their monetization properly. I fix that.",
        "I'm Mo, your Monetization advisor at Playmaker. From content ID to creator programs to direct-to-fan tools — I map out every platform revenue stream and help you turn them on.",
    ],
    "storefront": [
        "I'm Store, your Fan Store specialist at Playmaker. Direct-to-fan selling, digital downloads, exclusive content, memberships — I help you build a store that your fans actually want to buy from.",
        "Store here — Fan Commerce at Playmaker. Bandcamp, Shopify, Patreon, direct download links — I set up and optimise the systems that let you sell directly to your audience.",
        "I'm Store, your Fan Store advisor at Playmaker. Direct sales have the best margins in the music business. I help you build the storefront and the strategy to make it work.",
    ],
    "content-forge": [
        "I'm Pen, your Content Creation specialist at Playmaker. Bios, press releases, captions, liner notes, EPKs — I write the words that represent you professionally across every platform.",
        "Pen here — Content Creation at Playmaker. From a 150-character Instagram caption to a 1000-word artist bio, I make sure your written presence is as strong as your music.",
        "I'm Pen, your Content advisor at Playmaker. Great content strategy means the right words in the right places at the right time. I handle the writing so you can focus on the music.",
    ],
    "schedule-keeper": [
        "I'm Cal, your Scheduling specialist at Playmaker. Release calendars, content schedules, deadline management, campaign timelines — I keep your career moving at the right pace.",
        "Cal here — Scheduling and Planning at Playmaker. A good release campaign lives or dies on timing. I build the calendars and systems that keep everything on track.",
        "I'm Cal, your Schedule Keeper at Playmaker. From album rollouts to social content plans to studio booking — I map out the timeline and make sure nothing falls through the cracks.",
    ],
    "ink-and-air": [
        "Reed here — Music Publishing at Playmaker. Your songs are assets. I make sure you own them, register them correctly, and get paid every time they're used.",
        "I'm Reed, your Music Publisher at Playmaker. Publishing deals, sync licensing, songwriting royalties — I protect your catalog and maximize what it earns.",
        "Reed, Publishing at Playmaker. Most artists leave serious money on the table because their publishing isn't set up right. Let's fix that.",
    ],
    "live-wire": [
        "Knox here — Booking Agent at Playmaker. Live shows, festivals, touring strategy — I get you on stage and make sure the money is right when you get there.",
        "I'm Knox, your Booking Agent at Playmaker. From local venues to major festivals, I handle the pitches, the contracts, and the performance fees.",
        "Knox, Bookings at Playmaker. A strong live presence builds your fanbase faster than anything else. Let's talk about where you should be performing.",
    ],
    "label-services": [
        "Tommy here — Label Services at Playmaker. Distribution, release planning, DSP delivery, label setup — I make sure your music gets out to the world correctly and on time.",
        "I'm Tommy, your Label Services Manager at Playmaker. From DistroKid to proper label distribution, I handle the infrastructure that gets your music on every platform.",
        "Tommy, Label Services at Playmaker. A great song needs a great release strategy behind it. Let's talk about your next drop and make sure everything is set up right.",
    ],
    "pr-agent": [
        "I'm Quinn, your PR Manager at Playmaker. I write and send personalised outreach emails to press, blogs, podcasts, and magazines — and track every reply so nothing falls through the cracks.",
        "Quinn here — PR at Playmaker. Editorial coverage, interview placements, podcast bookings — I find the right outlets for your music and make the pitch on your behalf.",
        "I'm Quinn, your PR Manager at Playmaker. From Pitchfork to indie blogs, I know who covers your genre and how to reach them. What release are we pushing?",
    ],
    "booking-agent": [
        "I'm Avery, your Booking Agent at Playmaker. I pitch your act to venues, festivals, and promoters — and follow every inquiry through to a confirmed show date.",
        "Avery here — Booking at Playmaker. From local venues to major festivals, I handle the pitches, the negotiations, and the logistics of getting you on stage.",
        "I'm Avery, your Booking Agent at Playmaker. A strong live presence builds your fanbase faster than anything else. Let's map out where you should be performing.",
    ],
    "social-manager": [
        "I'm Riley, your Social Media Manager at Playmaker. I write platform-specific posts, schedule your content calendar, and make sure your online presence actually sounds like you — not a press release.",
        "Riley here — Social at Playmaker. Twitter, Instagram, TikTok, Facebook — I write content that fits each platform's native voice and schedule it at the right times to maximise reach.",
        "I'm Riley, your Social Media Manager at Playmaker. Your music deserves content that connects with real people. I handle the copy, the timing, and the strategy so you can focus on creating.",
    ],
}

# Fallback for any agent not in the dict (shouldn't happen, but just in case)
def _get_greeting(agent: dict) -> str:
    options = _AGENT_GREETINGS.get(agent["id"])
    if options:
        return random.choice(options)
    return f"I'm {agent['name']}, your {agent['title']}. What do you need?"

# ── Mode-specific system prompt suffixes ──────────────────────────────────────
_RULES_SHARED = """
---
EXPERT CALL RULES:
1. You are the expert. Speak with full authority. Never defer, never hedge.
2. Never repeat anything from a previous turn. The artist remembers. Build forward.
3. Use the artist's profile for context. Only mention their genre, stats, or background when directly relevant to their question — never recite it unprompted.
4. CONVERSATION CONTINUITY: If the conversation history contains prior messages, you are RESUMING an existing conversation. Do NOT introduce yourself by name or title. Continue naturally from where you left off. Only introduce yourself on the very first interaction when there is zero prior history.
5. ROUTING — CRITICAL: When a specialist from the AGENT ROSTER is needed, end your response with EXACTLY this sentence and nothing after it:
   "I'll hand you over to [FIRST NAME], our [TITLE]."
   Then STOP completely. Do not ask follow-up questions. Do not continue.
   The exact phrase "hand you over to" triggers the routing system — use it precisely.
   Use the AGENT ROSTER below to choose the right specialist and their exact first name and title.

AGENT ROSTER (routing reference):
Marcus — Artist Manager | Lex — Entertainment Lawyer | Ray — Performance Rights
Cleo — Neighbouring Rights | Finn — Mechanical Royalties | Reed — Music Publisher
Victor — Business Manager | Nadia — Accountant | Jade — Grants & Funding
Mo — Monetization | Tommy — Label Services | Zara — Publicist
Kai — Digital Marketing | Nia — Brand Partnerships | Solo — Radio & Playlist
Aria — Fan Engagement | Max — Merchandise | Store — Fan Store | Pen — Content Creation
Press — Media Monitor | Ray B — Booking Agent | Knox — Booking Agent
Miles — Tour Manager | Coach — Performance Coach | Luna — AI Visuals
Diego — Brand Designer | Cree — Creative Director | Reel — Music Video
Sync — Sync Licensing | Scout — A&R | Beat — Production | Nova — International
Collab — Networking | Prof — Education | Data — Analytics | Audio — Quality Control
Neo — AI Tools | Maya — Wellness | Doc — Royalty Recovery | Cal — Scheduling
Quinn — PR Manager | Avery — Booking Agent | Riley — Social Media Manager
"""

# Voice mode: 150-word hard cap, zero formatting, fast sharp answers
_VOICE_RULES = _RULES_SHARED + """
VOICE MODE — HARD LIMIT: 150 words. Always. No exceptions.
You are speaking on a live call. Be sharp. Be expert. Be complete in 150 words.
ZERO markdown. No asterisks, bullets, dashes, numbers, headers — plain spoken sentences only.
Convert every list to flowing speech: "First... and critically... what you need to know is..."
The artist asks follow-up questions to go deeper. Give the sharp answer, then stop.
"""

# Text mode: full markdown, longer analysis allowed
_TEXT_RULES = _RULES_SHARED + """
TEXT MODE: Use markdown freely. Match depth to question.
Conversational / single topic: 100-200 words.
Analysis / strategy / legal / financial: 300-500 words. Be thorough.
"""

# ── Smart model routing — Haiku for conversation, Sonnet for complexity ────────
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"

_COMPLEX_KEYWORDS = [
    "contract", "clause", "agreement", "deal memo", "deal terms",
    "legal", "lawsuit", "sue", "court", "litigation", "dispute", "arbitration",
    "copyright", "trademark", "patent", "intellectual property", "ip rights",
    "infringement", "breach", "liability", "indemnity",
    "negotiate", "negotiation", "counter-offer",
    "visa", "immigration", "work permit", "o-1", "p-1",
    "tax", "taxes", "audit", "irs", "hmrc", "vat", "withholding",
    "grant application", "grant proposal", "arts council", "funding application",
    "royalty audit", "black box", "unclaimed",
    "license agreement", "sync deal", "publishing deal", "360 deal",
    "master recording", "ownership split", "points", "advance recoupment",
]

def select_model(message: str, tier: str = "") -> tuple[str, int]:
    """
    Returns (model_id, max_tokens) based on artist tier + message complexity.

    GOLD     → always Haiku (fast, cost-effective)
    PLATINUM → Haiku default, Sonnet for complex keywords
    DIAMOND  → always Sonnet (full intelligence every time)
    default  → Haiku
    """
    t = tier.strip().upper()

    if t == "DIAMOND":
        return MODEL_SONNET, 2048

    if t == "GOLD":
        return MODEL_HAIKU, 768

    # PLATINUM and unrecognised tiers: keyword-based routing
    msg_lower = message.lower()
    for kw in _COMPLEX_KEYWORDS:
        if kw in msg_lower:
            return MODEL_SONNET, 2048
    return MODEL_HAIKU, 768

# ── Agent routing detection ────────────────────────────────────────────────────
_ROUTE_LOOKUP: dict[str, dict] = {}
for _a in AGENTS:
    _ROUTE_LOOKUP[_a["name"].lower()]                      = _a
    _ROUTE_LOOKUP[_a["id"]]                                = _a
    _ROUTE_LOOKUP[_a["skill"].replace("maestro-", "")]     = _a
    _ROUTE_LOOKUP[_a["skill"]]                             = _a

# Ordered longest-first so multi-word names ("ray b") match before "ray"
_ROUTE_KEYS = sorted(_ROUTE_LOOKUP.keys(), key=len, reverse=True)

_ROUTING_TRIGGERS = [
    # Primary canonical trigger — automatic handoff, no button in UI
    "hand you over to", "i'll hand you over to", "i will hand you over to",
    # Legacy triggers kept for safety
    "i'm handing you to", "i am handing you to", "handing you to",
    "routing you to", "connecting you with", "switching you to",
    "i'm routing", "i am routing", "let me connect you", "i'll connect you",
    "passing you to", "transfer you to", "handing this to",
    "routed you to", "connected you with", "switched you to",
    "transferred you to", "handed you to", "passed you to",
]

def detect_routing(text: str) -> Optional[dict]:
    """
    Proximity-based routing detection.
    Scans ±150 chars around each trigger phrase for an agent name.
    Returns the target agent (never puppet-master), or None.
    """
    tl = text.lower()

    for trigger in _ROUTING_TRIGGERS:
        pos = tl.find(trigger)
        while pos != -1:
            start  = max(0, pos - 150)
            end    = min(len(tl), pos + len(trigger) + 150)
            window = tl[start:end]
            for key in _ROUTE_KEYS:
                agent = _ROUTE_LOOKUP[key]
                if agent["id"] != "puppet-master" and key in window:
                    return agent
            pos = tl.find(trigger, pos + 1)

    return None

# ── Conversation history helpers ───────────────────────────────────────────────
# Hard caps prevent unbounded token growth across long sessions.
# These are conservative — raise them if artists report losing context.
HISTORY_CAP_VOICE = 10   # turns (5 exchanges) — voice sessions are short
HISTORY_CAP_TEXT  = 20   # turns (10 exchanges) — text sessions can go deeper
HISTORY_CAP_HANDOFF = 20  # turns — full context so receiving agent knows everything discussed

def trim_history(history_list: list, cap: int) -> list:
    """
    Keep the most recent `cap` turns from conversation history.
    Always preserves role integrity (never cuts mid-exchange).
    """
    valid = [t for t in history_list if t.get("role") in ("user", "assistant")]
    if len(valid) <= cap:
        return valid
    trimmed = valid[-cap:]
    # Ensure we start on a user turn (never an orphaned assistant turn)
    while trimmed and trimmed[0].get("role") == "assistant":
        trimmed = trimmed[1:]
    return trimmed

# ── Markdown → clean plain text for TTS ───────────────────────────────────────
def strip_markdown(text: str) -> str:
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_\n]+)_{1,3}', r'\1', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^\)]*\)', r'\1', text)
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*>\s?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    return text.strip()

# ── Sentence boundary detector for streaming TTS ──────────────────────────────
_SENT_END = re.compile(
    r'(?<![A-Z][a-z])'
    r'(?<!Mr)(?<!Dr)(?<!Ms)(?<!vs)'
    r'(?<!Mrs)(?<!etc)'
    r'(?<!Jan)(?<!Feb)(?<!Mar)(?<!Apr)'
    r'(?<!Jun)(?<!Jul)(?<!Aug)(?<!Sep)'
    r'(?<!Oct)(?<!Nov)(?<!Dec)'
    r'[.!?]+\s+(?=[A-Z"\'(]|\d)'
)

def split_sentence(buf: str) -> tuple[str, str]:
    """
    Find the last clean sentence boundary in buf.
    Returns (sentence_to_synthesize, remainder).
    Only splits when sentence is ≥ 60 chars to avoid tiny TTS calls.
    """
    for m in reversed(list(_SENT_END.finditer(buf))):
        sentence = buf[:m.end()].strip()
        rest     = buf[m.end():]
        if len(sentence) >= 60:
            return sentence, rest
    return "", buf

# ── Whisper STT ────────────────────────────────────────────────────────────────
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model("base")
    return _whisper_model

# ── Kokoro TTS ─────────────────────────────────────────────────────────────────
_kokoro = None
_kokoro_available = None
_tts_lock = asyncio.Lock()

def get_kokoro():
    global _kokoro, _kokoro_available
    if _kokoro_available is None:
        onnx_path   = _BASE / "kokoro-v1.0.onnx"
        voices_path = _BASE / "voices-v1.0.bin"
        if not onnx_path.exists() or not voices_path.exists():
            # R-19: print() kept for test_r19 capsys assertion; log for structured output
            print(
                f"[Kokoro] WARNING: Kokoro model files not found at {_BASE}/ "
                "(kokoro-v1.0.onnx and/or voices-v1.0.bin). "
                "TTS will fall back to ElevenLabs. "
                "Expected on Railway (files excluded via .railwayignore); "
                "only an issue if running locally without ElevenLabs configured."
            )
            log.warning("tts_kokoro_missing", extra={
                "event": "tts_kokoro_missing",
                "model_dir": str(_BASE),
                "detail": "Kokoro model files absent; TTS falls back to ElevenLabs. Expected on Railway.",
            })
            _kokoro_available = False
            return None
        try:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(onnx_path), str(voices_path))
            _kokoro_available = True
            log.info("tts_kokoro_ready", extra={"event": "tts_kokoro_ready"})
        except Exception as e:
            log.error("tts_kokoro_error", extra={"event": "tts_kokoro_error", "error": str(e)})
            _kokoro_available = False
    return _kokoro if _kokoro_available else None

async def synthesize_speech(text: str, voice: str) -> Optional[bytes]:
    """Synthesize text → WAV bytes. Uses Kokoro locally, ElevenLabs on cloud."""
    if not text.strip():
        return None
    kokoro = get_kokoro()
    if not kokoro:
        return await _synthesize_elevenlabs(text, voice)

    cache_key  = hashlib.md5(f"{voice}:1.1:{text}".encode()).hexdigest()
    cache_file = AUDIO_CACHE / f"{cache_key}.wav"
    if cache_file.exists():
        return cache_file.read_bytes()

    async with _tts_lock:
        try:
            import soundfile as sf
            loop = asyncio.get_event_loop()

            def _synth():
                samples, sr = kokoro.create(text, voice=voice, speed=1.1, lang="en-us")
                return samples, sr

            samples, sr = await loop.run_in_executor(None, _synth)

            buf = io.BytesIO()
            sf.write(buf, samples, sr, format='WAV')
            audio_bytes = buf.getvalue()

            cache_file.write_bytes(audio_bytes)
            return audio_bytes
        except Exception as e:
            log.error("tts_kokoro_synth_error", extra={"event": "tts_kokoro_synth_error", "error": str(e)})
            return None

# ── ElevenLabs TTS (cloud fallback) ────────────────────────────────────────────
# Maps Kokoro voice prefix → ElevenLabs pre-made voice ID
_RACHEL  = "21m00Tcm4TlvDq8ikWAM"
_BELLA   = "EXAVITQu4vr4xnSDxMaL"
_ADAM    = "pNInz6obpgDQGcFmaJgB"
_MATILDA = "XrExE9yKIg1WjnnlVkGX"  # warm, international female — South Asian prefix fallback
_LIAM    = "TX3LPaxmHKxFdv7VOQHJ"  # articulate male — Asian male prefix fallback

# ── Per-agent distinct ElevenLabs voice IDs — 16 active agents ────────────────
# Each voice is chosen to match the agent's character: gender, age, accent, personality.
# No two active agents share a voice ID.
_EL_VOICE_MAP = {
    "am_onyx":    "nPczCjzI2devNBz1zQrb",  # Marcus — Brian    (deep, commanding Black male)
    "af_jessica": "21m00Tcm4TlvDq8ikWAM",  # Lex    — Rachel   (clear, professional American female)
    "af_heart":   "XrExE9yKIg1WjnnlVkGX",  # Jade   — Matilda  (warm, international female; closest EL pre-made for South Asian character)
    "am_echo":    "N2lVS1w4EtoT3dr4eOWO",  # Ray    — Callum   (natural, calm American male)
    "bf_emma":    "ThT5KcBeYPX3keUQqHPh",  # Nadia  — Dorothy  (precise British female; fits accountant)
    "am_michael": "yoZ06aMxZJJ28mfd3POQ",  # Mo     — Sam      (raspy, street-smart; fits digital monetization)
    "bm_george":  "IKne3meq5aSn9XLyUdCD",  # Tommy  — Charlie  (Australian/British male; fits label services)
    "af_bella":   "AZnzlk1XvdvUeBnXmlld",  # Zara   — Domi     (strong, energetic American female; fits publicist)
    "am_adam":    "TX3LPaxmHKxFdv7VOQHJ",  # Kai    — Liam     (articulate, tech-clear male; East Asian digital marketing)
    "am_liam":    "TxGEqnHWrfWFTfGW9XjX",  # Solo   — Josh     (smooth, charismatic male; fits radio/playlist)
    "am_fenrir":  "ErXwobaYiN019PkySvjV",  # Ray B  — Antoni   (confident, assured; fits booking agent)
    "bm_lewis":   "GBv7mTt0atIp3Br8iCZE",  # Miles  — Thomas   (calm, organized British male; fits tour manager)
    "af_river":   "jBpfuIE2acCO8z3wKNLl",  # Cree   — Gigi     (young, energetic female; Creative Director)
    "bf_isabella":"EXAVITQu4vr4xnSDxMaL",  # Sync   — Bella    (sophisticated British female; fits sync licensing)
    "af_kore":    "piTKgcLEGmPE4e6mEKli",  # Scout  — Nicole   (perceptive, nuanced female; fits A&R)
    "af_sarah":   "XB0fDUnXU5powFXDhCwa",  # Cal    — Charlotte (young professional female; fits scheduling)
}

_EL_VOICES = {
    # Prefix fallback for non-active agents
    "af": _RACHEL,   # American female
    "bf": _BELLA,    # British female
    "ef": _BELLA,    # European female
    "ff": _BELLA,    # French female
    "hf": _MATILDA,  # South Asian / Hindi female — warm international voice
    "if": _BELLA,    # Italian female
    "jf": _RACHEL,   # Japanese female
    "pf": _BELLA,    # Polish female
    "zf": _RACHEL,   # Chinese female
    "am": _ADAM,     # American male
    "bm": _ADAM,     # British male
    "em": _ADAM,     # European male
    "hm": _ADAM,     # Hindi male
    "im": _ADAM,     # Italian male
    "jm": _LIAM,     # Japanese male — articulate male voice
    "pm": _ADAM,     # Polish male
    "zm": _LIAM,     # Chinese / Asian male — articulate male voice
}

def _pcm_to_wav(pcm_bytes: bytes, sr: int = 24000) -> bytes:
    """Wrap 16-bit PCM bytes in a WAV container."""
    import struct
    channels, sw = 1, 2
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(pcm_bytes)))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, channels, sr,
                          sr * channels * sw, channels * sw, sw * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(pcm_bytes)))
    buf.write(pcm_bytes)
    return buf.getvalue()

_tts_last_error: dict = {}  # temporary diagnostic: stores last ElevenLabs error detail

async def _synthesize_elevenlabs(text: str, voice: str) -> Optional[bytes]:
    """ElevenLabs TTS — returns WAV bytes via PCM output. Cloud fallback for Kokoro."""
    if not ELEVENLABS_API_KEY or not text.strip():
        return None
    # Per-agent voice map first (distinct character voices), then prefix fallback
    voice_id = _EL_VOICE_MAP.get(voice) or _EL_VOICES.get(voice[:2] if len(voice) >= 2 else "am", _ADAM)

    cache_key  = hashlib.md5(f"el:{voice_id}:{text}".encode()).hexdigest()
    cache_file = AUDIO_CACHE / f"{cache_key}.wav"
    if cache_file.exists():
        return cache_file.read_bytes()

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        body = {
            "text": text,
            "model_id": "eleven_turbo_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.post(url, json=body, headers=headers)
            log.info("tts_elevenlabs_response", extra={"event": "tts_elevenlabs_response",
                     "status_code": r.status_code, "voice_id": voice_id})
            if r.status_code != 200:
                _tts_last_error["detail"] = f"HTTP {r.status_code}: {r.text[:500]}"
                log.warning("tts_elevenlabs_error", extra={"event": "tts_elevenlabs_error",
                            "status_code": r.status_code, "detail": r.text[:200]})
            r.raise_for_status()
        wav = _pcm_to_wav(r.content, sr=24000)
        cache_file.write_bytes(wav)
        _tts_last_error["detail"] = None
        log.info("tts_elevenlabs_ok", extra={"event": "tts_elevenlabs_ok", "bytes": len(wav)})
        return wav
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _tts_last_error.setdefault("detail", msg)
        log.error("tts_elevenlabs_exception", extra={"event": "tts_elevenlabs_exception", "error": msg})
        return None

async def tts(text: str, voice: str) -> Optional[bytes]:
    """Strip markdown, then synthesize."""
    clean = strip_markdown(text)
    return await synthesize_speech(clean, voice) if clean else None

# ── System prompt with prompt caching ──────────────────────────────────────────
def load_skill(skill_dir: str) -> str:
    if skill_dir in SKILLS_CACHE:
        return SKILLS_CACHE[skill_dir]
    path = SKILLS_DIR / skill_dir / "SKILL.md"
    return path.read_text() if path.exists() else \
        "You are a world-class music industry specialist. Help the artist with expertise."

def load_artist(artist_id: str = "") -> dict:
    if not artist_id:
        return {}
    if DATABASE_URL:
        return _pg_get(artist_id)
    return _sqlite_get_artist(artist_id)

def load_knowledge() -> str:
    return KNOWLEDGE_BASE.read_text() if KNOWLEDGE_BASE.exists() else ""

def build_bank_block(agent_slug: str, question: str = "") -> Optional[str]:
    """
    Consult the knowledge bank on behalf of ``agent_slug`` and return a formatted
    system-prompt section, or ``None`` if the bank surfaced nothing.

    This is the ONE central integration point that makes the bank "live" for every
    agent's real response. ``consult_for_agent`` always includes the agent's
    AGENT_HOME domain (for paired/homed agents) and adds any cross-domain matches
    the deterministic brain finds in ``question``. Retrieval is pure/deterministic
    (no LLM/API call), mirroring BANK_CONSULT_MOCK_MODE behaviour.
    """
    try:
        result = _bank_consult_for_agent(agent_slug, question)
    except Exception:
        return None
    knowledge = (result.get("knowledge") or "").strip()
    if not knowledge:
        return None
    domains = ", ".join(result.get("domains", []))
    return (
        "---\n# KNOWLEDGE BANK CONSULTATION\n"
        f"Expert knowledge retrieved for this turn (domains: {domains}). "
        "Ground your answer in it; do not cite it as a source.\n\n"
        f"{knowledge}"
    )


def build_system_blocks(agent: dict, artist_id: str = "", voice_mode: bool = True, has_history: bool = False, question: str = "") -> list:
    """
    Returns system prompt as cacheable content blocks.

    Block 1 (cached): SKILL.md + KNOWLEDGE.md  — static, large, 90% cheaper on cache hit.
    Block 2 (live):   Artist profile + bank consultation + mode rules — varies per
                      request (the bank block depends on ``question``), not cached.
    """
    skill_text = load_skill(agent["skill"])
    knowledge  = load_knowledge()
    artist     = load_artist(artist_id)

    static = skill_text
    if knowledge:
        static += f"\n\n---\n# MASTER KNOWLEDGE BASE\n{knowledge}"

    blocks = [
        {
            "type": "text",
            "text": static,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    dynamic_parts = []
    if has_history:
        dynamic_parts.append("RETURNING CONVERSATION: This artist has spoken with you before. Do NOT introduce yourself by name or title. Greet them naturally and continue from where you left off.")
    if artist:
        dynamic_parts.append(f"---\n# CURRENT ARTIST PROFILE\n{json.dumps(artist, indent=2)}")
    bank_block = build_bank_block(agent["id"], question)
    if bank_block:
        dynamic_parts.append(bank_block)
    dynamic_parts.append(_VOICE_RULES if voice_mode else _TEXT_RULES)
    blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks

def build_messages(history_list: list, message: str, cap: int) -> list:
    """
    Build Claude messages array from trimmed history + new message.
    `cap` controls how many historical turns are included.
    """
    msgs = []
    for turn in trim_history(history_list, cap):
        msgs.append({"role": turn["role"], "content": turn["content"]})
    msgs.append({"role": "user", "content": message})
    return msgs

# ── SSE helper ─────────────────────────────────────────────────────────────────
def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

# ── Startup env checks ─────────────────────────────────────────────────────────
def _check_env():
    import re
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    phone = os.environ.get("TWILIO_PHONE_NUMBER", "")
    ok = True
    if not sid:
        log.warning("boot_warning", extra={"event": "boot_warning", "key": "TWILIO_ACCOUNT_SID", "detail": "SMS OTP disabled"})
        ok = False
    if not re.fullmatch(r'[0-9a-f]{32}', token.strip().lower()):
        log.warning("boot_warning", extra={"event": "boot_warning", "key": "TWILIO_AUTH_TOKEN",
                    "detail": "invalid format — must be 32 lowercase hex chars; get from console.twilio.com"})
        ok = False
    if not phone:
        log.warning("boot_warning", extra={"event": "boot_warning", "key": "TWILIO_PHONE_NUMBER", "detail": "SMS OTP disabled"})
        ok = False
    if ok:
        log.info("boot_ok", extra={"event": "boot_ok", "key": "TWILIO", "detail": "env vars valid"})
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        log.warning("boot_warning", extra={"event": "boot_warning", "key": "STRIPE_SECRET_KEY", "detail": "billing checkout disabled"})
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        log.warning("boot_warning", extra={"event": "boot_warning", "key": "ANTHROPIC_API_KEY", "detail": "AI agents will fail"})
    else:
        log.info("boot_ok", extra={"event": "boot_ok", "key": "ANTHROPIC_API_KEY", "detail": "present"})

_check_env()


def _check_data_writable() -> None:
    """Warn loudly if /data is not writable — means no Railway volume is mounted."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    probe = data_dir / ".writable_probe"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
        print(f"✓  {data_dir} is writable — volume mount OK")
    except OSError as exc:
        print("=" * 60)
        print(f"WARNING: {data_dir} is NOT writable: {exc}")
        print("  All SQLite DBs and OAuth tokens will be lost on redeploy.")
        print("  Action required: Railway dashboard → Service → Volumes → Add")
        print(f"  Volume name: plmkr-data   Mount path: {data_dir}   Size: 1 GB")
        print("=" * 60)


_check_data_writable()

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="PLMKR — Playmaker API",
    version="3.0.0",
    description=(
        "AI-powered artist management platform. "
        "Agents handle curator pitching, PR outreach, venue booking, "
        "social scheduling, and weekly reporting — no human copy-paste required.\n\n"
        "**Phases:**\n"
        "- Phase 1: Gmail OAuth + Curator pitch lifecycle\n"
        "- Phase 2: PR contacts + Booking inquiries\n"
        "- Phase 3: Social post scheduling + Weekly reports\n\n"
        "All endpoints require an `artist_id` (the artist's unique identifier in the system)."
    ),
    contact={"name": "PLMKR Support", "email": "getnexusai@gmail.com"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {"name": "health",   "description": "Liveness check"},
        {"name": "gmail",    "description": "Gmail OAuth 2.0 connect/disconnect"},
        {"name": "curators", "description": "Curator contact management"},
        {"name": "pitches",  "description": "Curator pitch lifecycle — generate, send, scan, follow-up"},
        {"name": "pr",       "description": "PR contact management and outreach lifecycle"},
        {"name": "booking",  "description": "Booking contact management and inquiry lifecycle"},
        {"name": "social",   "description": "Social post generation, scheduling, and Buffer integration"},
        {"name": "reports",  "description": "Weekly activity reports with AI-generated insights"},
        {"name": "buffer",   "description": "Buffer OAuth 2.0 connect/disconnect"},
        {"name": "agents",   "description": "Agent roster, TTS, conversation, and billing"},
    ],
)

@app.get("/health", tags=["health"], summary="Liveness check")
def health_check():
    """Returns 200 OK when the service is running."""
    return {"status": "ok"}
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Middleware stack (add_middleware is LIFO — last added executes first) ──────
# Execution order for an incoming request:
#   1. _RequestIDMiddleware  — generates UUID4 request_id, binds to contextvar
#   2. _TimingMiddleware     — records duration, adds Server-Timing header, logs slow requests
#   3. _APIKeyMiddleware     — validates X-API-Key header
#   4. CORSMiddleware        — handles preflight / injects CORS headers

_SLOW_REQUEST_THRESHOLD_MS = 2000.0

class _RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate a UUID4 request_id per request, bind via contextvar, echo as X-Request-ID."""
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        bind_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class _TimingMiddleware(BaseHTTPMiddleware):
    """Record request duration; add Server-Timing header; warn on slow requests."""
    async def dispatch(self, request: Request, call_next):
        import performance_metrics as _pm
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0
        response.headers["Server-Timing"] = f"total;dur={duration_ms:.1f}"
        route = request.url.path
        _pm.record_request(route, duration_ms)
        if duration_ms >= _SLOW_REQUEST_THRESHOLD_MS:
            from logging_config import get_logger, get_request_id
            get_logger("timing").warning(
                "slow_request",
                extra={
                    "path":        route,
                    "method":      request.method,
                    "duration_ms": round(duration_ms),
                    "status_code": response.status_code,
                    "request_id":  get_request_id(),
                },
            )
        return response


_SKIP_AUTH_PATHS = {"/health", "/api/admin/health/deep", "/docs", "/redoc", "/openapi.json",
                    "/admin/dashboard"}

class _APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _PLMKR_API_KEY:
            return await call_next(request)
        if request.method == "OPTIONS":  # CORS preflight must reach CORSMiddleware unblocked
            return await call_next(request)
        if request.url.path in _SKIP_AUTH_PATHS:
            return await call_next(request)
        key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(key, _PLMKR_API_KEY):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing X-API-Key header"},
            )
        return await call_next(request)

app.add_middleware(_APIKeyMiddleware)
app.add_middleware(_TimingMiddleware)
app.add_middleware(_RequestIDMiddleware)

if not _PLMKR_API_KEY:
    print("[AUTH] WARNING: PLMKR_API_KEY is not set — all routes are unauthenticated (dev mode)")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """Preserve FastAPI's native 422 format; attach request_id for tracing."""
    from logging_config import get_request_id
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "request_id": get_request_id() or str(uuid.uuid4())},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Preserve HTTPException status code and detail; attach request_id for tracing."""
    from logging_config import get_request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": get_request_id() or str(uuid.uuid4())},
    )


@app.exception_handler(Exception)
async def _generic_error_handler(request: Request, exc: Exception):
    """Return a structured error envelope for genuinely unhandled (500-level) exceptions."""
    from logging_config import get_logger, get_request_id
    request_id = get_request_id() or str(uuid.uuid4())
    get_logger("main").error(
        "unhandled_exception",
        extra={"path": str(request.url.path), "error": str(exc)},
        exc_info=exc,
    )
    capture_exception(exc, path=str(request.url.path), request_id=request_id)
    return JSONResponse(
        status_code=500,
        content={"error": type(exc).__name__, "detail": str(exc), "request_id": request_id},
    )

# ── Phase 1 — Pitch service (Gmail, curators, pitch tracking) ─────────────────
import pitch_service  # noqa: E402  (module ref for Marcus tool_use handlers, Unit 1.7)
import lex_cipher_service  # noqa: E402  (module ref for Lex tool_use handlers, Unit 1.8)
import fund_phantom_service  # noqa: E402  (module ref for Jade/fund-phantom tool_use handlers)
import rights_pulse_service  # noqa: E402  (module ref for Ray/rights-pulse tool_use handlers)
import border_royalty_service  # noqa: E402  (module ref for Cleo/border-royalty tool_use handlers)
import mech_ledger_service  # noqa: E402  (module ref for Finn/mech-ledger tool_use handlers)
import vault_keeper_service  # noqa: E402  (module ref for Victor/vault-keeper tool_use handlers)
import ledger_lock_service  # noqa: E402  (module ref for Nadia/ledger-lock tool_use handlers)
import signal_blaster_service  # noqa: E402  (module ref for Zara/signal-blaster tool_use handlers)
import grid_prophet_service  # noqa: E402  (module ref for Kai/grid-prophet tool_use handlers)
import vision_forge_service  # noqa: E402  (module ref for Luna/vision-forge tool_use handlers)
import design_studio_service  # noqa: E402  (module ref for Diego/design-studio tool_use handlers)
import venue_hawk_service  # noqa: E402  (module ref for Ray B/venue-hawk tool_use handlers)
import tour_commander_service  # noqa: E402  (module ref for Miles/tour-commander tool_use handlers)
import airwave_service  # noqa: E402  (module ref for Solo/airwave tool_use handlers)
import brand_connect_service  # noqa: E402  (module ref for Nia/brand-connect tool_use handlers)
import merch_empire_service  # noqa: E402  (module ref for Max/merch-empire tool_use handlers)
import fan_builder_service  # noqa: E402  (module ref for Aria/fan-builder tool_use handlers)
import sync_agent_service  # noqa: E402  (module ref for Sync/sync-agent tool_use handlers)
import global_scout_service  # noqa: E402  (module ref for Nova/global-scout tool_use handlers)
import creative_director_service  # noqa: E402  (module ref for Cree/creative-director tool_use handlers)
import data_oracle_service  # noqa: E402  (module ref for Data/data-oracle tool_use handlers)
import ar_scout_service  # noqa: E402  (module ref for Scout/ar-scout tool_use handlers)
import producer_connect_service  # noqa: E402  (module ref for Beat/producer-connect tool_use handlers)
import video_director_service  # noqa: E402  (module ref for Reel/video-director tool_use handlers)
import storefront_service  # noqa: E402  (module ref for Store/storefront tool_use handlers)
import schedule_keeper_service  # noqa: E402  (module ref for Cal/schedule-keeper tool_use handlers)
import royalty_doctor_service  # noqa: E402  (module ref for Doc/royalty-doctor tool_use handlers)
import press_monitor_service  # noqa: E402  (module ref for Press/press-monitor tool_use handlers)
import mobile_monetize_service  # noqa: E402  (module ref for Mo/mobile-monetize tool_use handlers)
import live_wire_service  # noqa: E402  (module ref for Knox/live-wire tool_use handlers)
import live_coach_service  # noqa: E402  (module ref for Coach/live-coach tool_use handlers)
import label_services_service  # noqa: E402  (module ref for Tommy/label-services tool_use handlers)
import ink_and_air_service  # noqa: E402  (module ref for Reed/ink-and-air tool_use handlers)
import content_forge_service  # noqa: E402  (module ref for Pen/content-forge tool_use handlers)
import collab_connect_service  # noqa: E402  (module ref for Collab/collab-connect tool_use handlers)
import audio_quality_service  # noqa: E402  (module ref for Audio/audio-quality tool_use handlers)
import artist_wellness_service  # noqa: E402  (module ref for Maya/artist-wellness tool_use handlers)
import ai_navigator_service  # noqa: E402  (module ref for Neo/ai-navigator tool_use handlers)
import release_service  # noqa: E402  (module ref for Sage/release-strategist tool_use handlers)
import social_service  # noqa: E402  (module ref for Riley/social-manager tool_use handlers)
import booking_service  # noqa: E402  (module ref for Avery/booking-agent tool_use handlers)
import pr_service  # noqa: E402  (module ref for Quinn/pr-agent tool_use handlers)
import execution_service  # noqa: E402  (durable supported provider operations)
from pitch_service import router as _pitch_router, init_pitch_db, init_scheduler
app.include_router(_pitch_router)

# ── Phase 2 — PR & Booking outreach services ───────────────────────────────────
from pr_service import router as _pr_router, init_pr_db
from booking_service import router as _booking_router, init_booking_db
app.include_router(_pr_router)
app.include_router(_booking_router)

# ── Phase 3 — Social scheduling + weekly reports ───────────────────────────────
from social_service import router as _social_router, init_social_db, init_report_scheduler
app.include_router(_social_router)

# ── Durable single-action execution ledger ────────────────────────────────────
from execution_service import router as _execution_router, init_execution_db
app.include_router(_execution_router)

# ── Phase 4 — iOS backend foundation (push, app config, version check, IAP) ───
from phase4_service import router as _phase4_router, init_phase4_db
app.include_router(_phase4_router)

# ── Admin — Stats + deep health ───────────────────────────────────────────────
from admin_service import router as _admin_router
app.include_router(_admin_router)

# ── Phase 4 — Release campaign orchestration ──────────────────────────────────
from release_service import router as _release_router, init_release_db, execute_all_due_campaign_actions
app.include_router(_release_router)

# Maps agent ID (e.g. "puppet-master") → lowercase first name slug (e.g. "marcus")
_ID_TO_NAME = {a["id"]: a["name"].lower().replace(" ", "-") for a in AGENTS}

class _CloudinaryPhotoMiddleware(BaseHTTPMiddleware):
    """Redirect /static/agents/* to Cloudinary CDN when CLOUDINARY_CLOUD_NAME is set."""
    async def dispatch(self, request, call_next):
        if CLOUDINARY_CLOUD_NAME and request.url.path.startswith("/static/agents/"):
            filename = request.url.path.split("/static/agents/", 1)[1].split("?")[0]
            base = filename.rsplit(".", 1)[0]  # strip .jpg
            name = _ID_TO_NAME.get(base, base)  # resolve agent ID → lowercase name
            cdn = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/upload/{name}.jpg"
            return _RedirectResponse(url=cdn, status_code=302)
        return await call_next(request)

app.add_middleware(_CloudinaryPhotoMiddleware)

# ── Conversation history DB ────────────────────────────────────────────────────
DB_PATH  = Path(os.environ.get("DB_PATH", "/data/memory.db"))
_db_lock = asyncio.Lock()

def _ensure_db():
    """Create messages + artists tables on first use (idempotent)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id TEXT    NOT NULL,
            agent_id  TEXT    NOT NULL,
            role      TEXT    NOT NULL,
            content   TEXT    NOT NULL,
            ts        INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg ON messages (artist_id, agent_id, id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artists (
            artist_id TEXT PRIMARY KEY,
            data      TEXT NOT NULL DEFAULT '{}',
            timezone  TEXT NOT NULL DEFAULT 'UTC'
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] memory.db ready")

def _sqlite_get_artist(artist_id: str) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    cur  = conn.cursor()
    cur.execute("SELECT data FROM artists WHERE artist_id=?", (artist_id,))
    row  = cur.fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}

def _sqlite_put_artist(artist_id: str, data: dict):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO artists (artist_id, data) VALUES (?, ?)",
        (artist_id, json.dumps(data))
    )
    conn.commit()
    conn.close()

def _sqlite_all_artists() -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    cur  = conn.cursor()
    cur.execute("SELECT data FROM artists")
    rows = cur.fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]

import threading as _threading

async def _get_message_count(artist_id: str, agent_id: str) -> int:
    """Return saved message count for an artist+agent pair (checks DB directly)."""
    if not artist_id or not agent_id:
        return 0
    def _count():
        conn = sqlite3.connect(str(DB_PATH))
        cur  = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE artist_id=? AND agent_id=?",
            (artist_id, agent_id),
        )
        n = cur.fetchone()[0]
        conn.close()
        return n
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _count)

async def _save_exchange(artist_id: str, agent_id: str, user_msg: str, assistant_msg: str):
    """Persist one user+assistant exchange; prune to 40 most recent rows per pair."""
    if not artist_id or not user_msg.strip() or not assistant_msg.strip():
        return
    loop = asyncio.get_event_loop()
    def _write():
        conn = sqlite3.connect(str(DB_PATH))
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO messages (artist_id, agent_id, role, content) VALUES (?,?,?,?)",
            (artist_id, agent_id, "user", user_msg),
        )
        cur.execute(
            "INSERT INTO messages (artist_id, agent_id, role, content) VALUES (?,?,?,?)",
            (artist_id, agent_id, "assistant", assistant_msg),
        )
        # Keep at most 40 rows (20 exchanges) per artist/agent pair
        cur.execute("""
            DELETE FROM messages
            WHERE  artist_id=? AND agent_id=?
            AND    id NOT IN (
                SELECT id FROM messages
                WHERE  artist_id=? AND agent_id=?
                ORDER  BY id DESC LIMIT 40
            )
        """, (artist_id, agent_id, artist_id, agent_id))
        conn.commit()
        conn.close()
    async with _db_lock:
        await loop.run_in_executor(None, _write)

# ── PostgreSQL artist store (used when DATABASE_URL env var is set) ────────────
# Falls back to flat-file storage when DATABASE_URL is absent (local dev).
def _pg_init():
    import psycopg2
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS artists (
                    artist_id TEXT PRIMARY KEY,
                    data      JSONB NOT NULL DEFAULT '{}'
                )
            """)
    print("[DB] PostgreSQL artists table ready")

def _pg_get(artist_id: str) -> dict:
    import psycopg2
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM artists WHERE artist_id = %s", (artist_id,))
            row = cur.fetchone()
    return dict(row[0]) if row else {}

def _pg_put(artist_id: str, data: dict):
    import psycopg2, psycopg2.extras
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artists (artist_id, data) VALUES (%s, %s) "
                "ON CONFLICT (artist_id) DO UPDATE SET data = EXCLUDED.data",
                (artist_id, psycopg2.extras.Json(data))
            )

def _pg_all() -> list[dict]:
    import psycopg2
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM artists")
            rows = cur.fetchall()
    return [dict(r[0]) for r in rows]

def _init_pg_connection(database_url: str) -> str:
    """Initialise Postgres. Returns effective database_url ('' when absent or on fallback).

    Failure behaviour:
      - DATABASE_URL set + Postgres fails + DB_FAILOVER_TO_SQLITE unset → sys.exit(1)
      - DATABASE_URL set + Postgres fails + DB_FAILOVER_TO_SQLITE=true  → warn + return ""
      - DATABASE_URL unset → SQLite intentional, return "" silently
    """
    if not database_url:
        print("[DB] No DATABASE_URL — using file-based artist storage")
        return ""
    _failover = os.environ.get("DB_FAILOVER_TO_SQLITE", "").lower() == "true"
    print(f"[DB] DATABASE_URL detected (prefix: {database_url[:20]}...) — initialising PostgreSQL")
    try:
        _pg_init()
        return database_url
    except Exception as pg_err:
        if _failover:
            print(f"[DB] WARNING: PostgreSQL init FAILED — DB_FAILOVER_TO_SQLITE=true, using SQLite: {pg_err}")
            return ""
        print(f"[DB] FATAL: PostgreSQL init FAILED: {pg_err}")
        print("[DB] Set DB_FAILOVER_TO_SQLITE=true to allow emergency SQLite fallback.")
        sys.exit(1)


# ── Module-level init ──────────────────────────────────────────────────────────
# Run synchronously at import time so the DB and Kokoro are ready before the
# first request arrives (avoids the 20-35s first-call warmup delay).
_ensure_db()
DATABASE_URL = _init_pg_connection(DATABASE_URL)
_threading.Thread(target=get_kokoro, daemon=True, name="kokoro-warmup").start()
init_pitch_db()
init_scheduler()
init_pr_db()
init_booking_db()
init_social_db()
init_execution_db()
init_report_scheduler()
init_release_db()
init_phase4_db()

# Wire campaign executor into scheduler (every 1h)
# Runs in both "true" (live) and "dry_run" modes; execute_all_due_campaign_actions()
# checks _SCHEDULER_DRY_RUN internally and logs instead of firing side effects.
_SCHEDULER_ENABLED_FLAG = os.environ.get("SCHEDULER_ENABLED", "").lower() in ("true", "dry_run")
if _SCHEDULER_ENABLED_FLAG:
    try:
        from pitch_service import _scheduler as _pitch_sched
        if _pitch_sched and _pitch_sched.running:
            _pitch_sched.add_job(
                execute_all_due_campaign_actions,
                "interval",
                hours=1,
                id="campaign_executor",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=120,
            )
            print("[Release] Campaign executor scheduled — every 1h")
    except Exception as _sched_err:
        print(f"[Release] Campaign scheduler hook failed: {_sched_err}")
print("[INIT] DB ready, Kokoro warmup thread started, pitch/PR/booking/social services initialised")

@app.get("/api/agents")
async def list_agents():
    return {"agents": AGENTS}


def _authenticated_artist_id(request: Request) -> str:
    """Return the signed artist principal when artist-session auth is configured."""
    if not identity_configured():
        return ""
    try:
        token = bearer_token(request.headers.get("Authorization", ""))
        return decode_session(token)["sub"]
    except ArtistAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


def _require_artist_scope(request: Request, claimed_artist_id: str) -> str:
    """Fail closed when a signed artist attempts to access another artist."""
    if not identity_configured():
        return claimed_artist_id
    principal = _authenticated_artist_id(request)
    if not claimed_artist_id or not secrets.compare_digest(principal, claimed_artist_id):
        raise HTTPException(status_code=404, detail="Artist resource not found")
    return principal


@app.get("/api/artist")
async def get_artist(request: Request, artist_id: str = ""):
    scoped_artist_id = _require_artist_scope(request, artist_id)
    return load_artist(scoped_artist_id)


def _delete_transcription_temp(path: str):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("transcription_temp_cleanup_failed", path=path, error_type=type(exc).__name__)


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), request: Request = None):
    tmp = None
    try:
        filename = audio.filename or "voice.m4a"
        ext      = (os.path.splitext(filename)[1] or ".m4a").lower()

        if ext not in _ALLOWED_AUDIO_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio format '{ext}'. Allowed: {sorted(_ALLOWED_AUDIO_EXTS)}",
            )

        # Reject oversized uploads before reading the body when Content-Length is present.
        cl = audio.headers.get("content-length") if audio.headers else None
        if cl is not None and int(cl) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit",
            )

        data = await audio.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit",
            )

        model = get_whisper()
        print(f"[TRANSCRIBE] received {len(data)} bytes, filename={filename}, ext={ext}")
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp = f.name
        loop   = asyncio.get_event_loop()
        transcribe_path = tmp
        worker = loop.run_in_executor(None, lambda: model.transcribe(transcribe_path))
        try:
            # Shield keeps the worker future observable after the request timeout,
            # allowing cleanup only after the thread has stopped using the file.
            result = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=TRANSCRIBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            worker.add_done_callback(lambda _: _delete_transcription_temp(transcribe_path))
            tmp = None
            raise
        except asyncio.CancelledError:
            worker.add_done_callback(lambda _: _delete_transcription_temp(transcribe_path))
            tmp = None
            raise
        text = result["text"].strip()
        print(f"[TRANSCRIBE] result: {repr(text)}")
        return {"text": text}
    except asyncio.TimeoutError:
        print("[TRANSCRIBE] ERROR: provider timeout")
        raise HTTPException(status_code=504, detail="Transcription timed out")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[TRANSCRIBE] ERROR: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="Transcription unavailable")
    finally:
        if tmp:
            _delete_transcription_temp(tmp)

@app.post("/api/greet")
async def greet(agent_id: str = Form(...), tts_on: str = Form(default="true", alias="tts")):
    """
    Returns the agent's opening line when an artist enters a chat.

    ALL agents now use static rotating greetings — zero API calls.
    This was the single biggest source of unnecessary token spend during testing.
    Each agent has 3–5 handcrafted variants; Marcus has 5.
    """
    agent = AGENTS_BY_ID.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    greeting_text = _get_greeting(agent)

    audio_b64 = None
    if tts_on.lower() == "true":
        audio_bytes = await tts(greeting_text, agent["voice"])
        if audio_bytes:
            audio_b64 = base64.b64encode(audio_bytes).decode()

    return {"reply": greeting_text, "audio": audio_b64}


@app.post("/api/handoff")
async def handoff(
    agent_id:      str = Form(...),
    history:       str = Form(default="[]"),
    tts_on:        str = Form(default="true", alias="tts"),
    artist_id:     str = Form(default=""),
    from_agent_id: str = Form(default="puppet-master"),
):
    """
    New agent delivers a warm personalised greeting after Marcus routes to them.

    Receiving agent gets full conversation context (HISTORY_CAP_HANDOFF turns)
    so they can greet the artist by name and reference what was discussed.
    The from_agent_id param makes the handoff prompt dynamic (not hardcoded to Marcus).

    The greeting prompt now instructs the agent to use the artist's name and
    reference something specific from the recent conversation. This fixes the
    generic "I'm routing you to X" wording from the previous version.
    """
    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="AI unavailable: ANTHROPIC_API_KEY not configured")
    agent = AGENTS_BY_ID.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        history_list = json.loads(history)
    except Exception:
        history_list = []

    artist      = load_artist(artist_id)
    artist_name = artist.get("artist_name", "you")
    do_tts      = tts_on.lower() == "true"

    from_agent  = AGENTS_BY_ID.get(from_agent_id)
    from_name   = from_agent["name"] if from_agent else "Marcus"

    # Full conversation history — receiving agent gets complete context
    trimmed_history = trim_history(history_list, HISTORY_CAP_HANDOFF)

    # Pass has_history so receiving agent doesn't intro themselves when context exists.
    # The latest user message seeds the bank consultation so the receiving agent's
    # greeting is grounded in the relevant domain(s) — its home plus any cross-domain match.
    _last_user_msg = next((t["content"] for t in reversed(trimmed_history) if t.get("role") == "user"), "")
    system_blocks = build_system_blocks(agent, artist_id=artist_id, voice_mode=do_tts, has_history=bool(trimmed_history), question=_last_user_msg)

    # Build rich handoff context: last 3 user messages (topic) + any actions already taken by from_agent
    user_msgs    = [t["content"] for t in trimmed_history if t.get("role") == "user"]
    agent_msgs   = [t["content"] for t in trimmed_history if t.get("role") == "assistant"]
    topic        = " | ".join(user_msgs[-3:])[:350] if user_msgs else ""
    _action_kws  = ["i sent", "i emailed", "i pitched", "i submitted", "i scheduled",
                    "i booked", "i drafted", "i found", "i created", "i looked", "i reviewed"]
    actions_done = next(
        (m[:200] for m in reversed(agent_msgs[-5:])
         if any(w in m.lower() for w in _action_kws)),
        ""
    )

    context_block = ""
    if topic:
        context_block += f"What the artist needs: {topic}\n"
    if actions_done:
        context_block += f"Actions {from_name} already took: {actions_done}\n"

    handoff_prompt = (
        f"{from_name} just handed {artist_name} directly to you. Full context is above.\n"
        f"{context_block}"
        f'Open with EXACTLY this pattern — fill in the specific issue from context:\n'
        f'"{artist_name}, {from_name} filled me in — I understand you need help with [specific issue]. '
        f'Let me take it from here."\n'
        f"One sentence only. No preamble. No filler. Sound like the expert you are."
    )

    messages = []
    for turn in trimmed_history:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": handoff_prompt})

    try:
        resp = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=120,
            system=system_blocks,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        reply = resp.content[0].text
    except Exception:
        reply = f"Hey {artist_name}, I'm {agent['name']} — your {agent['title']}. Marcus filled me in. Let's get straight to it — what do you need?"

    audio_b64 = None
    if do_tts:
        audio_bytes = await tts(reply, agent["voice"])
        if audio_bytes:
            audio_b64 = base64.b64encode(audio_bytes).decode()

    return {"reply": reply, "audio": audio_b64}

# ═══════════════════════════════════════════════════════════════════════════════
# Unit 1.7 — Marcus (puppet-master) tool_use: search_curators + send_pitch_email
# ═══════════════════════════════════════════════════════════════════════════════
# These tools are passed to the Anthropic API for the puppet-master agent ONLY
# (see the gate in chat_stream). Every other agent takes the unchanged text-stream
# path and never receives `tools`. Handlers map straight onto EXISTING pitch_service
# functions — they do not reimplement curator search or email sending.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
MARCUS_MAX_TOOL_ITERS = 5

MARCUS_TOOLS = [
    {
        "name": "search_curators",
        "description": "Search the curator database by genre",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
            },
        },
    },
    {
        "name": "send_pitch_email",
        "description": "Draft and send a pitch email to a curator on behalf of the artist",
        "input_schema": {
            "type": "object",
            "properties": {
                "curator_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["curator_id", "subject", "body"],
        },
    },
]


async def _execute_marcus_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Marcus tool call against existing pitch_service functions.

    Returns (result_for_model, action_summary, gmail_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions_taken SSE event.
      - gmail_not_connected: True when the send was blocked on missing/expired Gmail auth.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Marcus can explain the outcome to the artist.
    """
    tool_input = dict(tool_input or {})

    if name == "search_curators":
        genre = (tool_input.get("genre") or "").strip()
        # Filter on genre only — the sole axis the tool advertises. The curators
        # table is keyed on genre/tier; tier is available at the service layer
        # (_db_list_curators) but is not exposed as a tool param.
        rows = pitch_service._db_list_curators(genre=genre)
        curators = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "outlet": r.get("outlet", ""),
                "genres": r.get("genres", []),
                "tier": r.get("tier", ""),
                "email": r.get("contact_email", ""),
            }
            for r in rows[:20]
        ]
        result = {"curators": curators, "count": len(rows)}
        summary = {
            "input": f"genre={genre or 'any'}",
            "result": f"{len(rows)} curator(s) found",
        }
        return result, summary, False

    if name == "send_pitch_email":
        curator_id = (tool_input.get("curator_id") or "").strip()
        subject    = tool_input.get("subject") or ""
        body       = tool_input.get("body") or ""
        curator    = pitch_service._db_get_curator(curator_id)
        if not curator:
            return (
                {"error": "curator_not_found", "curator_id": curator_id},
                {"input": f"curator_id={curator_id}", "result": "curator not found"},
                False,
            )
        to = curator.get("contact_email", "")
        cname = curator.get("name", curator_id)
        try:
            sent = await pitch_service.send_email(artist_id, to, subject, body)
            return (
                {"status": "sent", "message_id": sent.get("message_id"), "curator": cname},
                {"input": f"curator={cname} subject={subject[:40]}", "result": "email sent"},
                False,
            )
        except pitch_service.GmailNotConnected:
            return (
                {
                    "gmail_not_connected": True,
                    "message": ("Artist has not connected Gmail. Tell them to connect it at "
                                "/api/gmail/auth before you can send pitches."),
                },
                {"input": f"curator={cname}", "result": "gmail_not_connected"},
                True,
            )
        except pitch_service.GmailAuthExpired:
            return (
                {
                    "gmail_not_connected": True,
                    "message": ("Gmail authorization expired. Tell the artist to re-connect at "
                                "/api/gmail/auth before you can send pitches."),
                },
                {"input": f"curator={cname}", "result": "gmail_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Lex (lex-cipher) tool_use: lookup_legal_concepts + build_legal_doc_scaffold
# ═══════════════════════════════════════════════════════════════════════════════
# DOC-WRITER Option B (legal-education engine). These tools are passed to
# the Anthropic API for the lex-cipher agent ONLY (see the gate in chat_stream).
# Every other agent takes its own unchanged path and never receives
# LEX_CIPHER_TOOLS. Handlers map straight onto the legal-education
# lex_cipher_service functions; they make ZERO network calls, read no secrets,
# and are UNGATED (both are pure corpus-read / data-scaffold tools). THE ONE RULE:
# Lex never gives legal advice and never produces a signable contract — every
# scaffold is preparation material FOR THE ARTIST'S OWN LAWYER.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
LEX_CIPHER_MAX_TOOL_ITERS = 5

LEX_CIPHER_TOOLS = [
    {
        "name": "lookup_legal_concepts",
        "description": (
            "Look up legal-education material from Lex's corpus — filter by agreement "
            "type, clause term, red-flag key, and/or jurisdiction key. Returns "
            "preparation material FOR THE ARTIST'S OWN LAWYER; it is not legal advice. "
            "Call with no filters to browse the available keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agreement_type": {
                    "type": "string",
                    "enum": list(lex_cipher_service.legal_data.AGREEMENT_TYPES),
                },
                "clause_term": {
                    "type": "string",
                    "enum": list(lex_cipher_service.legal_data.CLAUSE_GLOSSARY),
                },
                "flag_key": {
                    "type": "string",
                    "enum": list(lex_cipher_service.legal_data.RED_FLAG_DOCTRINE),
                },
                "jurisdiction_key": {
                    "type": "string",
                    "enum": list(lex_cipher_service.legal_data.JURISDICTION_DIVERGENCE),
                },
            },
        },
    },
    {
        "name": "build_legal_doc_scaffold",
        "description": (
            "Assemble COMPACT ingredients for a legal-prep document the artist takes "
            "to their own lawyer — a contract_review_brief or a negotiation_prep_memo. "
            "Returns a checklist, glossary questions, red-flag levers, and gap markers; "
            "you write the prose in your turn. This is NEVER a signable contract and "
            "NEVER legal advice. Jurisdiction-specific content is withheld unless a "
            "jurisdiction is supplied."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": list(lex_cipher_service.DOC_TYPES),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Artist-supplied fields: agreement_type, jurisdiction, "
                        "deal_points / contract_text (review brief), priorities "
                        "(negotiation memo). Anything unsupplied becomes a gap marker."
                    ),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_lex_cipher_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Lex tool call against the legal-education lex_cipher_service.

    Returns (result_for_model, action_summary, registry_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - registry_not_connected: retained for the shared generator's 3-tuple
        contract; ALWAYS False now — Lex's tools are pure corpus-read / data-
        scaffold tools with no connected account and no gate (the old IP-registry
        filing action and its gate are retired; filing lives with ledger-lock).
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Lex can explain the outcome to the artist. Mirrors
    _execute_marcus_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_legal_concepts":
        agreement_type   = (tool_input.get("agreement_type") or "").strip()
        clause_term      = (tool_input.get("clause_term") or "").strip()
        flag_key         = (tool_input.get("flag_key") or "").strip()
        jurisdiction_key = (tool_input.get("jurisdiction_key") or "").strip()
        res = await lex_cipher_service.lookup_legal_concepts(
            agreement_type=agreement_type, clause_term=clause_term,
            flag_key=flag_key, jurisdiction_key=jurisdiction_key,
        )
        filters = ",".join(f for f in (agreement_type, clause_term, flag_key,
                                       jurisdiction_key) if f) or "none"
        if res.get("mode") == "index":
            result_str = "index browsed"
        else:
            hit = (len(res.get("agreement_types", [])) + len(res.get("clauses", []))
                   + len(res.get("red_flags", [])) + len(res.get("jurisdictions", [])))
            miss = len(res.get("not_found", []))
            result_str = f"{hit} match(es), {miss} not found"
        summary = {"input": f"filters={filters}", "result": result_str}
        return res, summary, False

    if name == "build_legal_doc_scaffold":
        doc_type   = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await lex_cipher_service.build_legal_doc_scaffold(
            doc_type=doc_type, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Jade (fund-phantom) tool_use: search_grant_programs + check_eligibility
#            + submit_grant_application
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) pattern exactly: these tools are
# passed to the Anthropic API for the fund-phantom agent ONLY (see the gate in
# chat_stream). Every other agent — including Marcus and Lex — takes its own
# unchanged path and never receives FUND_PHANTOM_TOOLS. Handlers map straight onto
# the mock-first fund_phantom_service functions; they make ZERO network calls and
# read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
FUND_PHANTOM_MAX_TOOL_ITERS = 5

FUND_PHANTOM_TOOLS = [
    {
        "name": "search_grant_programs",
        "description": ("Search open arts grant/funding programs by country (e.g. 'CA', 'UK'), "
                        "track (industry|arts_council|crowdfunding), genre, region, or max_award "
                        "ceiling (only return grants available at or below this amount). "
                        "Crowdfunding is excluded unless track='crowdfunding'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "region": {"type": "string", "enum": ["national", "regional"]},
                "max_award": {
                    "type": "integer",
                    "description": "Only return grants available at or below this amount (a ceiling, in the grant's own currency).",
                },
                "country": {"type": "string"},
                "track": {"type": "string", "enum": ["industry", "arts_council", "crowdfunding"]},
            },
        },
    },
    {
        "name": "check_eligibility",
        "description": "Screen a project against a grant program's rules and return an eligibility assessment",
        "input_schema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "string"},
                "requested_amount": {"type": "integer"},
                "project_type": {
                    "type": "string",
                    "enum": ["recording", "touring", "production", "video"],
                },
            },
            "required": ["program_id"],
        },
    },
    {
        "name": "submit_grant_application",
        "description": "Submit a grant application to a program on behalf of the artist",
        "input_schema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "string"},
                "project_title": {"type": "string"},
                "requested_amount": {"type": "integer"},
            },
            "required": ["program_id", "project_title"],
        },
    },
    {
        "name": "lookup_grant_deadline",
        "description": ("Look up the CURRENT/upcoming deadline for one specific fund by program_id. "
                        "Use this whenever an artist asks about a particular fund's deadline, timing, "
                        "or when the next round opens. It returns a LIVE status — which may be "
                        "'round not announced yet' or 'lookup unavailable' — NOT a stored date, and "
                        "it never guesses. Relay the returned message to the artist and never invent "
                        "or assert a date yourself."),
        "input_schema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "string"},
            },
            "required": ["program_id"],
        },
    },
    {
        "name": "build_grant_application_scaffold",
        "description": ("Build a section-by-section application scaffold for one specific fund. "
                        "Call this ONLY AFTER you have interviewed the artist and gathered the "
                        "detail THIS fund's track actually requires (industry funders like FACTOR: "
                        "career momentum, release/marketing plan with concrete targets, budget with "
                        "the artist's cost-share; arts councils like ACE/Canada Council: the Need, "
                        "public Outcomes, Audience). YOU judge when enough is gathered — most "
                        "artists don't know what a grant needs. Pass everything gathered in "
                        "artist_inputs. The tool returns a compact scaffold: per-section guidance, "
                        "an honest funder/artist cost-share, and explicit [NEEDS: ...] gaps for "
                        "anything missing. Then WRITE the application draft yourself in your reply "
                        "from that scaffold — follow each section's guidance, keep every "
                        "[NEEDS: ...] gap marker verbatim, and NEVER invent a fact, number, or "
                        "credential. The draft is a starting point for the artist and their manager "
                        "to review — NOT submit-ready, NOT a legal document."),
        "input_schema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "string"},
                "artist_inputs": {
                    "type": "object",
                    "description": ("Everything gathered from the artist in the interview, as "
                                    "free-form keys, e.g. bio, career_stage, career_highlights, "
                                    "project, timeline, marketing_plan, release_plan, targets, "
                                    "budget_lines, requested_amount, match_source, need, outcomes, "
                                    "audience, activities, evaluation. Include ONLY what the artist "
                                    "actually told you — omit anything not gathered so it comes "
                                    "back as an explicit gap instead of an invented fact."),
                },
            },
            "required": ["program_id"],
        },
    },
    {
        "name": "suggest_crowdfunding",
        "description": ("Decide whether to raise crowdfunding as an option for THIS artist. "
                        "Crowdfunding is a SECONDARY path, not a default: raise it when the artist "
                        "does NOT qualify for the available grants (a real alternative), OR when it "
                        "complements a grant (matched-funding income / proof of fan demand). Stay "
                        "quiet when the artist clearly qualifies for grants and just needs to land "
                        "one. Returns {raise, reason, platforms}; platforms is populated only when "
                        "it recommends raising. Read/consult only — takes no external action."),
        "input_schema": {
            "type": "object",
            "properties": {
                "qualifies_for_grants": {
                    "type": "boolean",
                    "description": "True if the artist qualifies for the grants currently available to them.",
                },
                "complements_grant": {
                    "type": "boolean",
                    "description": "True if crowdfunding would complement a grant (matched-funding income or proof of fan demand).",
                },
            },
            "required": ["qualifies_for_grants"],
        },
    },
]


async def _execute_fund_phantom_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Jade tool call against the mock-first fund_phantom_service.

    Returns (result_for_model, action_summary, portal_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - portal_not_connected: True when a submission was blocked on a missing/expired
        funding-portal account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Jade can explain the outcome to the artist. Mirrors
    _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_grant_programs":
        genre     = (tool_input.get("genre") or "").strip()
        region    = (tool_input.get("region") or "").strip()
        max_award = tool_input.get("max_award") or 0
        country   = (tool_input.get("country") or "").strip()
        track     = (tool_input.get("track") or "").strip()
        res = await fund_phantom_service.search_grant_programs(
            genre=genre, region=region, max_award=max_award,
            country=country, track=track,
        )
        summary = {
            "input": f"genre={genre or 'any'} region={region or 'any'} "
                     f"country={country or 'any'} track={track or 'any'}",
            "result": f"{res['count']} program(s) found",
        }
        return res, summary, False

    if name == "check_eligibility":
        program_id       = (tool_input.get("program_id") or "").strip()
        requested_amount = tool_input.get("requested_amount") or 0
        project_type     = (tool_input.get("project_type") or "").strip()
        res = await fund_phantom_service.check_eligibility(
            artist_id, program_id=program_id,
            requested_amount=requested_amount, project_type=project_type,
        )
        summary = {
            "input": f"program={program_id or 'unspecified'} amount={requested_amount or 0}",
            "result": f"eligible={res['eligible']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "submit_grant_application":
        program_id       = (tool_input.get("program_id") or "").strip()
        project_title    = (tool_input.get("project_title") or "").strip()
        requested_amount = tool_input.get("requested_amount") or 0
        if not program_id or not project_title:
            return (
                {"error": "missing_program_or_title"},
                {"input": f"program={program_id or ''} title={project_title or ''}",
                 "result": "missing program or title"},
                False,
            )
        try:
            sub = await fund_phantom_service.submit_grant_application(
                artist_id, program_id, project_title, requested_amount,
            )
            return (
                {"status": "submitted", "reference": sub.get("reference"),
                 "program_id": program_id, "project_title": project_title},
                {"input": f"program={program_id} title={project_title[:40]}",
                 "result": "application submitted"},
                False,
            )
        except fund_phantom_service.FundingPortalNotConnected:
            return (
                {
                    "portal_not_connected": True,
                    "message": ("Artist has not connected a funding-portal submission account. Tell them "
                                "to connect one before you can submit applications."),
                },
                {"input": f"program={program_id}", "result": "portal_not_connected"},
                True,
            )
        except fund_phantom_service.FundingPortalAuthExpired:
            return (
                {
                    "portal_not_connected": True,
                    "message": ("Funding-portal authorization expired. Tell the artist to re-connect their "
                                "submission account before you can submit applications."),
                },
                {"input": f"program={program_id}", "result": "portal_auth_expired"},
                True,
            )

    if name == "lookup_grant_deadline":
        # Read/consult action — NOT a submission. Deliberately NOT gated behind
        # FUNDING_PORTAL_CONNECTED (that flag is only for submit). The public
        # lookup_grant_deadline owns all degradation (including translating an
        # unavailable lookup mechanism into a graceful status), so this never
        # raises and portal_not_connected is always False here.
        program_id = (tool_input.get("program_id") or "").strip()
        res = await fund_phantom_service.lookup_grant_deadline(artist_id, program_id)
        summary = {
            "input": f"program={program_id or 'unspecified'}",
            "result": res.get("status", "unknown"),
        }
        return res, summary, False

    if name == "build_grant_application_scaffold":
        # Read/consult action — drafting help, NOT a submission. Deliberately NOT
        # gated behind FUNDING_PORTAL_CONNECTED (that flag is only for submit).
        # The service owns all degradation (program_not_found is a structured
        # result), so this never raises and portal_not_connected is always False.
        program_id    = (tool_input.get("program_id") or "").strip()
        artist_inputs = tool_input.get("artist_inputs")
        if not isinstance(artist_inputs, dict):
            artist_inputs = {}
        res = await fund_phantom_service.build_grant_application_scaffold(
            artist_id, program_id, artist_inputs,
        )
        gaps = res.get("missing") or []
        result_desc = res.get("status", "unknown")
        if res.get("status") == "scaffold_ready":
            result_desc += f", {len(res.get('sections', []))} section(s), {len(gaps)} gap(s)"
        summary = {
            "input": f"program={program_id or 'unspecified'} inputs={len(artist_inputs)} field(s)",
            "result": result_desc,
        }
        return res, summary, False

    if name == "suggest_crowdfunding":
        # Read/consult action — a pure situational decision helper, NOT a
        # submission. Deliberately NOT gated behind FUNDING_PORTAL_CONNECTED
        # (that flag is only for submit); this never raises, so portal_not_connected
        # is always False.
        qualifies    = bool(tool_input.get("qualifies_for_grants"))
        complements  = bool(tool_input.get("complements_grant"))
        res = fund_phantom_service.suggest_crowdfunding(
            qualifies_for_grants=qualifies, complements_grant=complements,
        )
        summary = {
            "input": f"qualifies={qualifies} complements={complements}",
            "result": f"raise={res['raise']}, {len(res['platforms'])} platform(s)",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Ray (rights-pulse) tool_use: search_pro_organizations + check_registration_status
#            + register_work
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade pattern exactly: these
# tools are passed to the Anthropic API for the rights-pulse agent ONLY (see the
# gate in chat_stream). Every other agent — including Marcus, Lex, and Jade —
# takes its own unchanged path and never receives RIGHTS_PULSE_TOOLS. Handlers map
# straight onto the mock-first rights_pulse_service functions; they make ZERO
# network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
RIGHTS_PULSE_MAX_TOOL_ITERS = 5

RIGHTS_PULSE_TOOLS = [
    {
        "name": "search_pro_organizations",
        "description": "Search Performance Rights Organizations (PROs) by territory or collection type",
        "input_schema": {
            "type": "object",
            "properties": {
                "territory": {"type": "string"},
                "org_type": {
                    "type": "string",
                    "enum": ["performing_rights", "digital_performance"],
                },
            },
        },
    },
    {
        "name": "check_registration_status",
        "description": "Screen a work against a PRO's registration requirements and return a readiness assessment",
        "input_schema": {
            "type": "object",
            "properties": {
                "work_title": {"type": "string"},
                "pro_id": {"type": "string"},
                "writer_share": {"type": "integer"},
            },
            "required": ["pro_id"],
        },
    },
    {
        "name": "register_work",
        "description": "Register a work with a PRO on behalf of the artist so it collects performance royalties",
        "input_schema": {
            "type": "object",
            "properties": {
                "work_title": {"type": "string"},
                "pro_id": {"type": "string"},
                "writer_share": {"type": "integer"},
            },
            "required": ["work_title", "pro_id"],
        },
    },
]


async def _execute_rights_pulse_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Ray tool call against the mock-first rights_pulse_service.

    Returns (result_for_model, action_summary, pro_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - pro_not_connected: True when a registration was blocked on a missing/expired
        PRO account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Ray can explain the outcome to the artist. Mirrors
    _execute_fund_phantom_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_pro_organizations":
        territory = (tool_input.get("territory") or "").strip()
        org_type  = (tool_input.get("org_type") or "").strip()
        res = await rights_pulse_service.search_pro_organizations(
            territory=territory, org_type=org_type,
        )
        summary = {
            "input": f"territory={territory or 'any'} type={org_type or 'any'}",
            "result": f"{res['count']} PRO(s) found",
        }
        return res, summary, False

    if name == "check_registration_status":
        work_title   = (tool_input.get("work_title") or "").strip()
        pro_id       = (tool_input.get("pro_id") or "").strip()
        writer_share = tool_input.get("writer_share") or 0
        res = await rights_pulse_service.check_registration_status(
            artist_id, work_title=work_title, pro_id=pro_id, writer_share=writer_share,
        )
        summary = {
            "input": f"work={work_title or 'unspecified'} pro={pro_id or 'unspecified'}",
            "result": f"ready={res['ready']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "register_work":
        work_title   = (tool_input.get("work_title") or "").strip()
        pro_id       = (tool_input.get("pro_id") or "").strip()
        writer_share = tool_input.get("writer_share") or 0
        if not work_title or not pro_id:
            return (
                {"error": "missing_work_or_pro"},
                {"input": f"work={work_title or ''} pro={pro_id or ''}",
                 "result": "missing work or pro"},
                False,
            )
        try:
            reg = await rights_pulse_service.register_work(
                artist_id, work_title, pro_id, writer_share,
            )
            return (
                {"status": "registered", "reference": reg.get("reference"),
                 "work_title": work_title, "pro_id": pro_id},
                {"input": f"work={work_title[:40]} pro={pro_id}",
                 "result": "work registered"},
                False,
            )
        except rights_pulse_service.ProAccountNotConnected:
            return (
                {
                    "pro_not_connected": True,
                    "message": ("Artist has not connected a PRO registration account. Tell them "
                                "to connect one before you can register works."),
                },
                {"input": f"work={work_title}", "result": "pro_not_connected"},
                True,
            )
        except rights_pulse_service.ProAccountAuthExpired:
            return (
                {
                    "pro_not_connected": True,
                    "message": ("PRO account authorization expired. Tell the artist to re-connect their "
                                "account before you can register works."),
                },
                {"input": f"work={work_title}", "result": "pro_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray pattern exactly:
# these tools are passed to the Anthropic API for the border-royalty agent ONLY
# (see the gate in chat_stream). Every other agent — including Marcus, Lex, Jade,
# and Ray — takes its own unchanged path and never receives BORDER_ROYALTY_TOOLS.
# Handlers map straight onto the mock-first border_royalty_service functions; they
# make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
BORDER_ROYALTY_MAX_TOOL_ITERS = 5

BORDER_ROYALTY_TOOLS = [
    {
        "name": "search_collection_societies",
        "description": "Search neighbouring-rights collection societies (CMOs) by territory or collected right type",
        "input_schema": {
            "type": "object",
            "properties": {
                "territory": {"type": "string"},
                "right_type": {
                    "type": "string",
                    "enum": ["performer", "producer"],
                },
            },
        },
    },
    {
        "name": "check_claim_readiness",
        "description": "Screen a sound recording against a society's neighbouring-rights requirements and return a readiness assessment",
        "input_schema": {
            "type": "object",
            "properties": {
                "recording_title": {"type": "string"},
                "society_id": {"type": "string"},
                "performer_role": {
                    "type": "string",
                    "enum": ["featured", "non_featured", "producer"],
                },
            },
            "required": ["society_id"],
        },
    },
    {
        "name": "register_neighbouring_rights_claim",
        "description": "File a neighbouring-rights claim with a society on behalf of the artist so it collects international royalties on the recording",
        "input_schema": {
            "type": "object",
            "properties": {
                "recording_title": {"type": "string"},
                "society_id": {"type": "string"},
                "performer_role": {
                    "type": "string",
                    "enum": ["featured", "non_featured", "producer"],
                },
            },
            "required": ["recording_title", "society_id"],
        },
    },
]


async def _execute_border_royalty_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Cleo tool call against the mock-first border_royalty_service.

    Returns (result_for_model, action_summary, society_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - society_not_connected: True when a claim was blocked on a missing/expired
        society account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Cleo can explain the outcome to the artist. Mirrors
    _execute_rights_pulse_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_collection_societies":
        territory  = (tool_input.get("territory") or "").strip()
        right_type = (tool_input.get("right_type") or "").strip()
        res = await border_royalty_service.search_collection_societies(
            territory=territory, right_type=right_type,
        )
        summary = {
            "input": f"territory={territory or 'any'} right={right_type or 'any'}",
            "result": f"{res['count']} society(ies) found",
        }
        return res, summary, False

    if name == "check_claim_readiness":
        recording_title = (tool_input.get("recording_title") or "").strip()
        society_id      = (tool_input.get("society_id") or "").strip()
        performer_role  = (tool_input.get("performer_role") or "").strip()
        res = await border_royalty_service.check_claim_readiness(
            artist_id, recording_title=recording_title, society_id=society_id,
            performer_role=performer_role,
        )
        summary = {
            "input": f"recording={recording_title or 'unspecified'} society={society_id or 'unspecified'}",
            "result": f"ready={res['ready']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "register_neighbouring_rights_claim":
        recording_title = (tool_input.get("recording_title") or "").strip()
        society_id      = (tool_input.get("society_id") or "").strip()
        performer_role  = (tool_input.get("performer_role") or "").strip()
        if not recording_title or not society_id:
            return (
                {"error": "missing_recording_or_society"},
                {"input": f"recording={recording_title or ''} society={society_id or ''}",
                 "result": "missing recording or society"},
                False,
            )
        try:
            claim = await border_royalty_service.register_neighbouring_rights_claim(
                artist_id, recording_title, society_id, performer_role,
            )
            return (
                {"status": "filed", "reference": claim.get("reference"),
                 "recording_title": recording_title, "society_id": society_id},
                {"input": f"recording={recording_title[:40]} society={society_id}",
                 "result": "claim filed"},
                False,
            )
        except border_royalty_service.SocietyAccountNotConnected:
            return (
                {
                    "society_not_connected": True,
                    "message": ("Artist has not connected a neighbouring-rights society account. Tell "
                                "them to connect one before you can file claims."),
                },
                {"input": f"recording={recording_title}", "result": "society_not_connected"},
                True,
            )
        except border_royalty_service.SocietyAccountAuthExpired:
            return (
                {
                    "society_not_connected": True,
                    "message": ("Society account authorization expired. Tell the artist to re-connect "
                                "their account before you can file claims."),
                },
                {"input": f"recording={recording_title}", "result": "society_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Mech-Ledger (mech-ledger) tool_use: search_mechanical_agencies +
#    check_registration_readiness + register_mechanical_work ────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo pattern
# exactly: these tools are passed to the Anthropic API for the mech-ledger agent
# ONLY (see the gate in chat_stream). Every other agent — including Marcus, Lex,
# Jade, Ray, and Cleo — takes its own unchanged path and never receives
# MECH_LEDGER_TOOLS. Handlers map straight onto the mock-first mech_ledger_service
# functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
MECH_LEDGER_MAX_TOOL_ITERS = 5

MECH_LEDGER_TOOLS = [
    {
        "name": "search_mechanical_agencies",
        "description": "Search mechanical-rights collection agencies / administrators by territory or collected right type",
        "input_schema": {
            "type": "object",
            "properties": {
                "territory": {"type": "string"},
                "right_type": {
                    "type": "string",
                    "enum": ["songwriter", "publisher"],
                },
            },
        },
    },
    {
        "name": "check_registration_readiness",
        "description": "Screen a musical work against a mechanical-rights agency's registration requirements and return a readiness assessment",
        "input_schema": {
            "type": "object",
            "properties": {
                "work_title": {"type": "string"},
                "agency_id": {"type": "string"},
                "writer_role": {
                    "type": "string",
                    "enum": ["writer", "co_writer", "publisher"],
                },
            },
            "required": ["agency_id"],
        },
    },
    {
        "name": "register_mechanical_work",
        "description": "Register a musical work with a mechanical-rights agency on behalf of the artist so it collects the mechanical royalties owed on that composition",
        "input_schema": {
            "type": "object",
            "properties": {
                "work_title": {"type": "string"},
                "agency_id": {"type": "string"},
                "writer_role": {
                    "type": "string",
                    "enum": ["writer", "co_writer", "publisher"],
                },
            },
            "required": ["work_title", "agency_id"],
        },
    },
]


async def _execute_mech_ledger_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Finn tool call against the mock-first mech_ledger_service.

    Returns (result_for_model, action_summary, mech_account_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - mech_account_not_connected: True when a registration was blocked on a
        missing/expired agency account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Finn can explain the outcome to the artist. Mirrors
    _execute_border_royalty_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_mechanical_agencies":
        territory  = (tool_input.get("territory") or "").strip()
        right_type = (tool_input.get("right_type") or "").strip()
        res = await mech_ledger_service.search_mechanical_agencies(
            territory=territory, right_type=right_type,
        )
        summary = {
            "input": f"territory={territory or 'any'} right={right_type or 'any'}",
            "result": f"{res['count']} agency(ies) found",
        }
        return res, summary, False

    if name == "check_registration_readiness":
        work_title  = (tool_input.get("work_title") or "").strip()
        agency_id   = (tool_input.get("agency_id") or "").strip()
        writer_role = (tool_input.get("writer_role") or "").strip()
        res = await mech_ledger_service.check_registration_readiness(
            artist_id, work_title=work_title, agency_id=agency_id,
            writer_role=writer_role,
        )
        summary = {
            "input": f"work={work_title or 'unspecified'} agency={agency_id or 'unspecified'}",
            "result": f"ready={res['ready']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "register_mechanical_work":
        work_title  = (tool_input.get("work_title") or "").strip()
        agency_id   = (tool_input.get("agency_id") or "").strip()
        writer_role = (tool_input.get("writer_role") or "").strip()
        if not work_title or not agency_id:
            return (
                {"error": "missing_work_or_agency"},
                {"input": f"work={work_title or ''} agency={agency_id or ''}",
                 "result": "missing work or agency"},
                False,
            )
        try:
            work = await mech_ledger_service.register_mechanical_work(
                artist_id, work_title, agency_id, writer_role,
            )
            return (
                {"status": "registered", "reference": work.get("reference"),
                 "work_title": work_title, "agency_id": agency_id},
                {"input": f"work={work_title[:40]} agency={agency_id}",
                 "result": "work registered"},
                False,
            )
        except mech_ledger_service.MechAccountNotConnected:
            return (
                {
                    "mech_account_not_connected": True,
                    "message": ("Artist has not connected a mechanical-rights agency account. Tell "
                                "them to connect one before you can register works."),
                },
                {"input": f"work={work_title}", "result": "mech_account_not_connected"},
                True,
            )
        except mech_ledger_service.MechAccountAuthExpired:
            return (
                {
                    "mech_account_not_connected": True,
                    "message": ("Mechanical-rights account authorization expired. Tell the artist to "
                                "re-connect their account before you can register works."),
                },
                {"input": f"work={work_title}", "result": "mech_account_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Vault-Keeper (vault-keeper) tool_use: search_budget_templates + build_project_budget
# (schedule_expense_payment was a mock terminal-action tool gated behind
#  VAULT_KEEPER_ACCOUNT_CONNECTED — retired; Victor is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn pattern
# exactly: these tools are passed to the Anthropic API for the vault-keeper agent
# ONLY (see the gate in chat_stream). Every other agent — including Marcus, Lex,
# Jade, Ray, Cleo, and Finn — takes its own unchanged path and never receives
# VAULT_KEEPER_TOOLS. Handlers map straight onto the mock-first vault_keeper_service
# functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
VAULT_KEEPER_MAX_TOOL_ITERS = 5

VAULT_KEEPER_TOOLS = [
    {
        "name": "search_budget_templates",
        "description": "Search business-manager budget templates by project type or tier",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_type": {
                    "type": "string",
                    "enum": ["release", "tour", "video", "campaign"],
                },
                "tier": {
                    "type": "string",
                    "enum": ["starter", "pro"],
                },
            },
        },
    },
    {
        "name": "build_project_budget",
        "description": "Build a concrete project budget by applying a template's category allocations to an estimated revenue figure",
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "project_name": {"type": "string"},
                "estimated_revenue": {"type": "number"},
            },
            "required": ["template_id"],
        },
    },
]


async def _execute_vault_keeper_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Victor tool call against the mock-first vault_keeper_service.

    Returns (result_for_model, action_summary, not_connected). Victor is
    consult-only (search + build); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_budget_templates":
        project_type = (tool_input.get("project_type") or "").strip()
        tier         = (tool_input.get("tier") or "").strip()
        res = await vault_keeper_service.search_budget_templates(
            project_type=project_type, tier=tier,
        )
        summary = {
            "input": f"type={project_type or 'any'} tier={tier or 'any'}",
            "result": f"{res['count']} template(s) found",
        }
        return res, summary, False

    if name == "build_project_budget":
        template_id       = (tool_input.get("template_id") or "").strip()
        project_name      = (tool_input.get("project_name") or "").strip()
        estimated_revenue = tool_input.get("estimated_revenue") or 0
        res = await vault_keeper_service.build_project_budget(
            artist_id, template_id=template_id, project_name=project_name,
            estimated_revenue=estimated_revenue,
        )
        summary = {
            "input": f"project={project_name or 'unspecified'} template={template_id or 'unspecified'}",
            "result": f"viable={res['viable']}, {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Ledger-Lock (ledger-lock) tool_use: search_royalty_sources +
#    reconcile_royalty_statement + file_tax_document ───────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor pattern exactly: these tools are passed to the Anthropic API for the
# ledger-lock agent ONLY (see the gate in chat_stream). Every other agent —
# including Marcus, Lex, Jade, Ray, Cleo, Finn, and Victor — takes its own
# unchanged path and never receives LEDGER_LOCK_TOOLS. Handlers map straight onto
# the mock-first ledger_lock_service functions; they make ZERO network calls and
# read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
LEDGER_LOCK_MAX_TOOL_ITERS = 5

LEDGER_LOCK_TOOLS = [
    {
        "name": "search_royalty_sources",
        "description": ("Search royalty income sources by source type or region. Sources carry NO "
                        "stored withholding rates (honesty pass) — each match resolves the "
                        "withholding MECHANISM to verify against: the US statutory 30% default on "
                        "US-source royalties to foreign persons is the ONLY rate stated as a "
                        "number; treaty reductions vary by treaty AND income category (W-8BEN / "
                        "W-8BEN-E filed with the withholding agent — verify via IRS treaty "
                        "tables), and non-US regimes vary (home society statement + tax "
                        "professional). Never state any other withholding number."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["streaming", "mechanical", "performance", "sync", "neighbouring"],
                },
                "region": {
                    "type": "string",
                    "enum": ["domestic", "foreign"],
                },
            },
        },
    },
    {
        "name": "reconcile_royalty_statement",
        "description": ("Reconcile a royalty statement from the figures ON the statement: the "
                        "gross and the withheld amount the statement itself reports. NEVER "
                        "computes or assumes a withholding rate — if the statement's withheld "
                        "figure is missing, the result returns a [NEEDS:withheld_amount] gap "
                        "plus the withholding-mechanism records to verify against (US statutory "
                        "30% default / treaty varies — verify via IRS treaty tables / non-US "
                        "varies — home society statement + tax professional)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "statement_period": {"type": "string"},
                "gross_amount": {"type": "number"},
                "withheld_amount": {"type": "number",
                                    "description": "The withholding figure AS REPORTED on the statement — omit if the statement does not show one; never estimate it"},
            },
            "required": ["source_id"],
        },
    },
    {
        "name": "file_tax_document",
        "description": "File a tax document (estimate, annual return, 1099, etc.) with the artist's connected bookkeeping account on their behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "filing_type": {
                    "type": "string",
                    "enum": ["quarterly_estimate", "annual_return", "1099", "vat_return", "self_assessment"],
                },
                "period": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["filing_type", "period"],
        },
    },
    {
        "name": "lookup_recording_societies",
        "description": ("Look up who collects RECORDING-side royalties (neighbouring rights / "
                        "recording performance) in one country: the recording body records with "
                        "their capacities (performers / rights_owners / both) and registration "
                        "notes, plus the composition-side society ids for context (that side is "
                        "Reed's domain). Covered: CA, US, UK, AU, NZ, DE, FR, SE, DK, NO, FI. "
                        "US: SoundExchange digital non-interactive ONLY — no terrestrial-radio "
                        "neighbouring right. FR: four bodies split by role. A country whose "
                        "recording side is unverified (NZ) returns None + a verify-live note; "
                        "any other country returns country_not_in_corpus — relay that honestly "
                        "and NEVER guess a body. Never state a split as fact except the US "
                        "statutory SoundExchange 50/45/5 — everything else is "
                        "varies_verify_with_society."),
        "input_schema": {
            "type": "object",
            "properties": {
                "country_code": {"type": "string",
                                 "description": "Country code, e.g. 'CA', 'US', 'UK' — the artist's HOME country"},
            },
            "required": ["country_code"],
        },
    },
    {
        "name": "build_registration_checklist",
        "description": ("Build the artist's royalty-registration checklist from their EXPLICITLY "
                        "confirmed situation. Pass ONLY flags the artist actually confirmed — "
                        "OMIT anything unconfirmed so it comes back as an explicit [NEEDS:<flag>] "
                        "gap and that branch is skipped, instead of a guessed registration "
                        "(flags are never inferred). Axes: country_of_residence (code), "
                        "self_published (bool), owns_masters (bool), performed_on_recording "
                        "(bool), has_producers_or_session_players (bool). Returns ordered "
                        "registration entries (body + capacity + reason + stream), metadata-"
                        "consistency reminders (ISRC/ISWC/IPI/legal names), and the split "
                        "discipline (only the US statutory 50/45/5 is ever quoted; all other "
                        "splits are varies_verify_with_society). Keep every [NEEDS:...] marker "
                        "verbatim in your reply. This is registration logistics — NOT tax or "
                        "legal advice; route tax questions to a professional and agreements to "
                        "Lex as draft-for-review."),
        "input_schema": {
            "type": "object",
            "properties": {
                "situation": {
                    "type": "object",
                    "description": ("The artist's explicitly confirmed flags. Include ONLY what "
                                    "the artist actually told you: country_of_residence (e.g. "
                                    "'US'), self_published, owns_masters, performed_on_recording, "
                                    "has_producers_or_session_players (booleans). Extra keys pass "
                                    "through as unparsed notes."),
                },
            },
        },
    },
    {
        "name": "build_royalty_doc_scaffold",
        "description": ("Build a compact document scaffold for one of two royalty documents, "
                        "AFTER you have interviewed the artist and gathered what the document "
                        "actually requires. doc_type 'registration_checklist_doc': pass "
                        "inputs.situation with ONLY the flags the artist explicitly confirmed "
                        "(same axes as build_registration_checklist) — the scaffold returns the "
                        "situation summary verbatim, one registration step per stream (body + "
                        "capacity + reason), the metadata-consistency block (ISRC/ISWC/IPI/legal "
                        "names), a Letter-of-Direction step when applicable, and the reminders. "
                        "doc_type 'letter_of_direction': pass the canonical LOD fields "
                        "(artist_legal_name, payee_legal_name, payee_contact, recordings_covered "
                        "with ISRCs, percentage_directed, effective_date, "
                        "signatures_both_parties) — percentage_directed is ALWAYS a supplied "
                        "input the parties agreed; NEVER compute or suggest one; omit it if "
                        "unknown so it comes back as [NEEDS:percentage_directed]. Include ONLY "
                        "what the artist actually told you — omit unknowns so they come back as "
                        "[NEEDS:...] gaps instead of invented facts. Then WRITE the document "
                        "draft yourself in your reply from the scaffold: follow each section's "
                        "guidance, keep every [NEEDS:...] and [ARTIST-SUPPLIED: ...] marker "
                        "verbatim, and never invent a rate, %, or society rule. The draft is a "
                        "review starting point — NOT submit-ready, NOT a legal document, NOT tax "
                        "or legal advice."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string",
                             "enum": ["registration_checklist_doc", "letter_of_direction"]},
                "inputs": {
                    "type": "object",
                    "description": ("Everything gathered from the artist. For "
                                    "registration_checklist_doc: 'situation' (object of "
                                    "explicitly confirmed flags). For letter_of_direction: the "
                                    "canonical LOD field names. Omit anything not gathered — "
                                    "unmapped extras ride along verbatim."),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_ledger_lock_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Nadia tool call against the mock-first ledger_lock_service.

    Returns (result_for_model, action_summary, ledger_account_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - ledger_account_not_connected: True when a filing was blocked on a
        missing/expired bookkeeping account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Nadia can explain the outcome to the artist. Mirrors
    _execute_vault_keeper_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_royalty_sources":
        source_type = (tool_input.get("source_type") or "").strip()
        region      = (tool_input.get("region") or "").strip()
        res = await ledger_lock_service.search_royalty_sources(
            source_type=source_type, region=region,
        )
        summary = {
            "input": f"type={source_type or 'any'} region={region or 'any'}",
            "result": f"{res['count']} source(s) found",
        }
        return res, summary, False

    if name == "reconcile_royalty_statement":
        source_id        = (tool_input.get("source_id") or "").strip()
        statement_period = (tool_input.get("statement_period") or "").strip()
        gross_amount     = tool_input.get("gross_amount") or 0
        withheld_amount  = tool_input.get("withheld_amount")  # None = not on statement
        res = await ledger_lock_service.reconcile_royalty_statement(
            artist_id, source_id=source_id, statement_period=statement_period,
            gross_amount=gross_amount, withheld_amount=withheld_amount,
        )
        summary = {
            "input": f"source={source_id or 'unspecified'} period={statement_period or 'unspecified'}",
            "result": f"reconciled={res['reconciled']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "file_tax_document":
        filing_type = (tool_input.get("filing_type") or "").strip()
        period      = (tool_input.get("period") or "").strip()
        amount      = tool_input.get("amount") or 0
        if not filing_type:
            return (
                {"error": "missing_filing_type"},
                {"input": f"filing_type={filing_type or ''} period={period}",
                 "result": "missing filing type"},
                False,
            )
        try:
            filing = await ledger_lock_service.file_tax_document(
                artist_id, filing_type, period, amount,
            )
            return (
                {"status": "filed", "reference": filing.get("reference"),
                 "filing_type": filing.get("filing_type"), "period": filing.get("period"),
                 "amount": filing.get("amount")},
                {"input": f"filing_type={filing_type[:40]} period={period}",
                 "result": "tax document filed"},
                False,
            )
        except ledger_lock_service.LedgerAccountNotConnected:
            return (
                {
                    "ledger_account_not_connected": True,
                    "message": ("Artist has not connected a bookkeeping/tax account. Tell them "
                                "to connect one before you can file tax documents."),
                },
                {"input": f"filing_type={filing_type}", "result": "ledger_account_not_connected"},
                True,
            )
        except ledger_lock_service.LedgerAccountAuthExpired:
            return (
                {
                    "ledger_account_not_connected": True,
                    "message": ("Bookkeeping-account authorization expired. Tell the artist to "
                                "re-connect their account before you can file tax documents."),
                },
                {"input": f"filing_type={filing_type}", "result": "ledger_account_auth_expired"},
                True,
            )

    if name == "lookup_recording_societies":
        # Deliberately NOT gated on LEDGER_LOCK_ACCOUNT_CONNECTED — pure corpus
        # read, no account needed (mirrors Reed's ungated lookup tool).
        code = (tool_input.get("country_code") or "").strip()
        res = await ledger_lock_service.lookup_recording_societies(code)
        if res.get("status") == "ok":
            bodies = res.get("recording_bodies")
            result_str = (f"{len(bodies)} recording bod(y/ies)" if bodies is not None
                          else "recording side unverified — verify live")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"country_code={code or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "build_registration_checklist":
        # Deliberately NOT gated — pure corpus-rule application, no account needed.
        situation = tool_input.get("situation")
        situation = situation if isinstance(situation, dict) else {}
        res = await ledger_lock_service.build_registration_checklist(situation)
        summary = {
            "input": f"flags={sorted(res['situation'])!r}",
            "result": (f"{len(res['registrations'])} registration(s), "
                       f"{len(res['needs'])} gap(s)"),
        }
        return res, summary, False

    if name == "build_royalty_doc_scaffold":
        # Deliberately NOT gated — data/scaffold tool, no account needed.
        dt = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await ledger_lock_service.build_royalty_doc_scaffold(
            doc_type=dt, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={dt or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Signal-Blaster (signal-blaster) tool_use: search_media_outlets +
# build_pitch_plan + lookup_publicity_doctrine + send_press_pitch ─────────────
# Reworked to the Marcus/Nia/Solo OUTREACH pattern (Zara U2). These tools are
# passed to the Anthropic API for the signal-blaster agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# SIGNAL_BLASTER_TOOLS. Handlers map straight onto the mock-first
# signal_blaster_service functions; they make ZERO network calls and read no
# secrets. Press-release DRAFTING is NOT here — it is creative-director's
# build_copy_scaffold (Cree writes, Zara sends); the boundary is encoded in
# publicity_data doctrine, not a cross-service dispatch call.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
SIGNAL_BLASTER_MAX_TOOL_ITERS = 5

SIGNAL_BLASTER_TOOLS = [
    {
        "name": "search_media_outlets",
        "description": ("Filter an ARTIST-SUPPLIED media list by beat or level. This "
                        "tool NEVER invents outlet or journalist names — it has no "
                        "built-in directory. Pass the artist's own media list in "
                        "`media_list`; with no list it returns a [NEEDS:media_targets] "
                        "gap you must surface (ask for a personalized list of writers "
                        "covering the artist's level and current beat)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "beat": {"type": "string"},
                "level": {"type": "string"},
                "media_list": {
                    "type": "array",
                    "description": ("Artist-supplied outlets/journalists. Each item is "
                                    "an object with any of: name, beat, level/tier. "
                                    "Never fabricate entries."),
                    "items": {"type": "object"},
                },
            },
        },
    },
    {
        "name": "build_pitch_plan",
        "description": ("Build COMPACT pitch-plan ingredients (you write the prose): a "
                        "recommended pitch mode from the goal, a lead-ordered campaign "
                        "timeline anchored to a release date, and a package checklist "
                        "with an aggregated missing[]. If weeks_to_release is short, "
                        "slots that cannot get their normal lead are flagged and an "
                        "HONEST compression warning is returned — the schedule is "
                        "never silently compressed. The press release is only "
                        "REFERENCED (creative-director's build_copy_scaffold), never "
                        "drafted here."),
        "input_schema": {
            "type": "object",
            "properties": {
                "release_date": {"type": "string"},
                "weeks_to_release": {"type": "integer"},
                "goal": {
                    "type": "string",
                    "description": ("The campaign goal (e.g. max impressions, a top-tier "
                                    "feature, a coordinated announcement) — drives the "
                                    "recommended mode."),
                },
                "package": {
                    "type": "object",
                    "description": ("What the artist already has for the pitch package "
                                    "(audio link, artwork, photos, bio, credits, story "
                                    "angle, release date). Absent items become "
                                    "[NEEDS:] gaps."),
                },
            },
        },
    },
    {
        "name": "lookup_publicity_doctrine",
        "description": ("Look up the doctrine for ONE publicity topic before acting: "
                        "'pitch_modes' (standard / embargo / exclusive + when to use "
                        "each), 'embargo' (the hard embargo rules), 'lead_time' (the "
                        "campaign timeline + windows), 'personalization' (list "
                        "discipline), 'pitch_package' (what a complete package "
                        "carries + follow-up rule), 'integrity', or 'boundaries' "
                        "(what belongs to creative-director / puppet-master / airwave "
                        "/ brand-connect). HARD RULES OF THIS DOMAIN: outlet, "
                        "journalist, and coverage claims are NEVER invented; "
                        "press-release / bio / EPK DRAFTING belongs to "
                        "creative-director's build_copy_scaffold (Cree writes, Zara "
                        "sends); an embargo needs a zoned lift; earned media is never "
                        "paid; coverage is never guaranteed."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["pitch_modes", "embargo", "lead_time", "personalization",
                             "pitch_package", "integrity", "boundaries"],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "send_press_pitch",
        "description": ("Send a personalized press pitch on the artist's behalf. YOU "
                        "write the pitch subject and body in your turn and pass them "
                        "in — this tool SENDS them, it never writes or edits the pitch "
                        "and never invents an outlet or a coverage claim. Identify the "
                        "outlet with `outlet_id` or `outlet`. `pitch_mode` is explicit "
                        "(standard / embargo / exclusive); an EMBARGO pitch REQUIRES "
                        "`embargo_lift_datetime` stated WITH a time zone or the send is "
                        "held. If the artist has not connected a press account the send "
                        "is blocked and you must tell them to connect one first."),
        "input_schema": {
            "type": "object",
            "properties": {
                "outlet_id": {"type": "string"},
                "outlet": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "pitch_mode": {
                    "type": "string",
                    "enum": ["standard", "embargo", "exclusive"],
                },
                "embargo_lift_datetime": {
                    "type": "string",
                    "description": "Lift date/time WITH time zone — required for embargo mode.",
                },
            },
            "required": ["subject", "body"],
        },
    },
]


async def _execute_signal_blaster_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Zara tool call against the mock-first signal_blaster_service.

    Returns (result_for_model, action_summary, press_account_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - press_account_not_connected: True when a pitch was blocked on a
        missing/expired press account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Zara can explain the outcome to the artist. Mirrors
    _execute_ledger_lock_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_media_outlets":
        beat       = (tool_input.get("beat") or "").strip()
        level      = (tool_input.get("level") or "").strip()
        media_list = tool_input.get("media_list")
        res = await signal_blaster_service.search_media_outlets(
            beat=beat, level=level, media_list=media_list,
        )
        if res.get("status") == "needs_targets":
            result_str = "no media list supplied — [NEEDS:media_targets]"
        else:
            result_str = f"{res['count']} artist-supplied outlet(s) matched"
        summary = {
            "input": f"beat={beat or 'any'} level={level or 'any'}",
            "result": result_str,
        }
        return res, summary, False

    if name == "build_pitch_plan":
        release_date     = (tool_input.get("release_date") or "").strip()
        weeks_to_release = tool_input.get("weeks_to_release")
        goal             = (tool_input.get("goal") or "").strip()
        package          = tool_input.get("package")
        res = await signal_blaster_service.build_pitch_plan(
            artist_id, release_date=release_date, weeks_to_release=weeks_to_release,
            goal=goal, package=package,
        )
        warn = " (compression warning)" if res.get("compression_warning") else ""
        result_str = (f"mode={res['recommended_mode']}, "
                      f"{len(res.get('missing', []))} package gap(s){warn}")
        summary = {
            "input": f"release_date={release_date or 'unspecified'} weeks={weeks_to_release if weeks_to_release is not None else 'unspecified'}",
            "result": result_str,
        }
        return res, summary, False

    if name == "lookup_publicity_doctrine":
        # Deliberately NOT gated — pure corpus read, no account needed.
        topic = (tool_input.get("topic") or "").strip()
        res = await signal_blaster_service.lookup_publicity_doctrine(topic)
        if res.get("status") == "ok":
            result_str = f"{len(res['honesty_rules'])} honesty rule(s)"
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"topic={topic or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "send_press_pitch":
        # Marcus send seam: the MODEL wrote subject/body; the tool only sends.
        outlet_id = (tool_input.get("outlet_id") or "").strip()
        outlet    = (tool_input.get("outlet") or "").strip()
        subject   = tool_input.get("subject") or ""
        body      = tool_input.get("body") or ""
        pitch_mode = (tool_input.get("pitch_mode") or "standard").strip()
        lift       = tool_input.get("embargo_lift_datetime")
        try:
            sent = await signal_blaster_service.send_press_pitch(
                artist_id, outlet_id=outlet_id, outlet=outlet, subject=subject,
                body=body, pitch_mode=pitch_mode, embargo_lift_datetime=lift,
            )
            status = sent.get("status")
            if status == "missing_outlet":
                return (
                    {"error": "missing_outlet"},
                    {"input": f"outlet_id={outlet_id or ''} outlet={outlet or ''}",
                     "result": "missing outlet"},
                    False,
                )
            if status == "needs_embargo_lift":
                return (
                    sent,
                    {"input": f"outlet={outlet or outlet_id} mode=embargo",
                     "result": "needs embargo lift datetime"},
                    False,
                )
            return (
                sent,
                {"input": f"outlet={outlet or outlet_id} mode={pitch_mode} subject={subject[:40]}",
                 "result": "press pitch sent"},
                False,
            )
        except signal_blaster_service.PressAccountNotConnected:
            return (
                {
                    "press_account_not_connected": True,
                    "message": ("Artist has not connected a press/email account. Tell them "
                                "to connect one before you can send press pitches."),
                },
                {"input": f"outlet={outlet or outlet_id}", "result": "press_account_not_connected"},
                True,
            )
        except signal_blaster_service.PressAccountAuthExpired:
            return (
                {
                    "press_account_not_connected": True,
                    "message": ("Press-account authorization expired. Tell the artist to "
                                "re-connect their account before you can send press pitches."),
                },
                {"input": f"outlet={outlet or outlet_id}", "result": "press_account_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Grid-Prophet (Kai) tool_use ────────────────────────────────────────
# DOC-WRITER Option B (digital-marketing engine). These tools are passed to the
# Anthropic API for the grid-prophet agent ONLY (see the gate in chat_stream).
# Every other agent takes its own unchanged path and never receives
# GRID_PROPHET_TOOLS. Handlers map straight onto the digital-marketing
# grid_prophet_service functions; they make ZERO network calls, read no
# secrets, and are UNGATED (both are pure corpus-read / data-scaffold tools).
# The old mock+gate growth-channel/content-plan/schedule-post tools and their
# connected-account gate are RETIRED — Kai's real value is prep documents
# (campaign plans, ad-test briefs) built from Kai's own digital-marketing
# knowledge base, not a mock social-scheduling API.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
GRID_PROPHET_MAX_TOOL_ITERS = 5

GRID_PROPHET_TOOLS = [
    {
        "name": "lookup_digital_marketing_doctrine",
        "description": (
            "Look up digital-marketing doctrine from Kai's corpus — filter by channel-"
            "sequence key, organic-proof key, platform-selection key, budget-mechanics "
            "key, measurement key, and/or first-72-hours (momentum) key. Call with no "
            "filters to browse the available keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sequence_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.CHANNEL_SEQUENCE),
                },
                "proof_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.ORGANIC_PROOF_FIRST),
                },
                "platform_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.PLATFORM_SELECTION),
                },
                "budget_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.BUDGET_MECHANICS),
                },
                "measurement_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.MEASUREMENT),
                },
                "momentum_key": {
                    "type": "string",
                    "enum": list(grid_prophet_service.digital_marketing_data.FIRST_72_HOURS),
                },
            },
        },
    },
    {
        "name": "build_marketing_doc_scaffold",
        "description": (
            "Assemble COMPACT ingredients for a digital-marketing document — a "
            "campaign_plan or an ad_test_brief. Returns checklists, doctrine, field "
            "lists, questions, and gap markers; you write the prose in your turn. "
            "Budget/spend is ALWAYS artist-supplied and out of scope — this tool never "
            "computes or echoes a spend figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": list(grid_prophet_service.DOC_TYPES),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Artist-supplied fields: channels_in_place (campaign_plan) — a "
                        "list of which of the four sequencing stages "
                        "(streaming_platform_optimization, organic_short_form_content, "
                        "email, paid_promotion) are already dialed in. Any budget/spend "
                        "figure supplied here is never echoed back as a number by this "
                        "tool — budget specifics stay artist-supplied and out of scope. "
                        "Anything unsupplied becomes a gap marker."
                    ),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_grid_prophet_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Kai tool call against the digital-marketing grid_prophet_service.

    Returns (result_for_model, action_summary, social_account_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - social_account_not_connected: retained for the shared generator's 3-tuple
        contract; ALWAYS False now — Kai's tools are pure corpus-read / data-
        scaffold tools with no connected account and no gate (the old
        connected-account gate and its schedule-post action are retired).
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Kai can explain the outcome to the artist. Mirrors
    _execute_tour_commander_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_digital_marketing_doctrine":
        sequence_key    = (tool_input.get("sequence_key") or "").strip()
        proof_key       = (tool_input.get("proof_key") or "").strip()
        platform_key    = (tool_input.get("platform_key") or "").strip()
        budget_key      = (tool_input.get("budget_key") or "").strip()
        measurement_key = (tool_input.get("measurement_key") or "").strip()
        momentum_key    = (tool_input.get("momentum_key") or "").strip()
        res = await grid_prophet_service.lookup_digital_marketing_doctrine(
            sequence_key=sequence_key, proof_key=proof_key, platform_key=platform_key,
            budget_key=budget_key, measurement_key=measurement_key, momentum_key=momentum_key,
        )
        filters = ",".join(f for f in (sequence_key, proof_key, platform_key,
                                       budget_key, measurement_key, momentum_key) if f) or "none"
        if res.get("mode") == "index":
            result_str = "index browsed"
        else:
            hit = (len(res.get("sequence", [])) + len(res.get("proof", []))
                   + len(res.get("platform", [])) + len(res.get("budget", []))
                   + len(res.get("measurement", [])) + len(res.get("momentum", [])))
            miss = len(res.get("not_found", []))
            result_str = f"{hit} match(es), {miss} not found"
        summary = {"input": f"filters={filters}", "result": result_str}
        return res, summary, False

    if name == "build_marketing_doc_scaffold":
        doc_type   = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await grid_prophet_service.build_marketing_doc_scaffold(
            doc_type=doc_type, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Vision-Forge (Luna — AI Visuals) tool_use surface: search_visual_styles +
# draft_visual_brief ────────────────────────────────────────────────────────────
# (generate_artwork was a mock terminal-action tool gated behind
#  VISION_FORGE_ACCOUNT_CONNECTED — retired; Luna is consult-only.)
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai pattern exactly: these tools are passed to the
# Anthropic API for the vision-forge agent ONLY (see the gate in chat_stream).
# Every other agent — including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia,
# Zara, and Kai — takes its own unchanged path and never receives
# VISION_FORGE_TOOLS. Handlers map straight onto the mock-first
# vision_forge_service functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
VISION_FORGE_MAX_TOOL_ITERS = 5

VISION_FORGE_TOOLS = [
    {
        "name": "search_visual_styles",
        "description": "Search visual styles by the medium they target or their production tier (A=flagship, B=mid, C=quick/social)",
        "input_schema": {
            "type": "object",
            "properties": {
                "medium": {
                    "type": "string",
                    "enum": ["cover_art", "poster", "social", "merch", "lyric_video"],
                },
                "tier": {
                    "type": "string",
                    "enum": ["A", "B", "C"],
                },
            },
        },
    },
    {
        "name": "draft_visual_brief",
        "description": "Draft a structured art brief from a concept and target medium (with an optional colour palette)",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string"},
                "medium": {"type": "string"},
                "palette": {"type": "string"},
            },
            "required": ["concept"],
        },
    },
]


async def _execute_vision_forge_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Luna tool call against the mock-first vision_forge_service.

    Returns (result_for_model, action_summary, not_connected). Luna is
    consult-only (search + draft); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_visual_styles":
        medium = (tool_input.get("medium") or "").strip()
        tier   = (tool_input.get("tier") or "").strip()
        res = await vision_forge_service.search_visual_styles(medium=medium, tier=tier)
        summary = {
            "input": f"medium={medium or 'any'} tier={tier or 'any'}",
            "result": f"{res['count']} style(s) found",
        }
        return res, summary, False

    if name == "draft_visual_brief":
        concept = (tool_input.get("concept") or "").strip()
        medium  = (tool_input.get("medium") or "").strip()
        palette = (tool_input.get("palette") or "").strip()
        res = await vision_forge_service.draft_visual_brief(
            artist_id, concept=concept, medium=medium, palette=palette,
        )
        summary = {
            "input": f"concept={concept[:40] or 'unspecified'} medium={medium[:40] or 'unspecified'}",
            "result": f"drafted={res['drafted']}, {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna pattern exactly: these tools are passed to the
# Anthropic API for the design-studio agent ONLY (see the gate in chat_stream).
# Every other agent — including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia,
# Zara, Kai, and Luna — takes its own unchanged path and never receives
# DESIGN_STUDIO_TOOLS. Handlers map straight onto the mock-first
# design_studio_service functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
DESIGN_STUDIO_MAX_TOOL_ITERS = 5

DESIGN_STUDIO_TOOLS = [
    {
        "name": "search_brand_styles",
        "description": "Search brand-identity styles by the asset type they target (logo, wordmark, palette, type, social kit) or their production tier (A=flagship, B=mid, C=quick/social)",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_type": {
                    "type": "string",
                    "enum": ["logo", "wordmark", "color_palette", "typography", "social_kit"],
                },
                "tier": {
                    "type": "string",
                    "enum": ["A", "B", "C"],
                },
            },
        },
    },
    {
        "name": "draft_brand_brief",
        "description": "Draft a structured brand brief from a concept and target asset type (with an optional tone/voice note)",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string"},
                "asset_type": {"type": "string"},
                "tone": {"type": "string"},
            },
            "required": ["concept"],
        },
    },
]


async def _execute_design_studio_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Diego tool call against the mock-first design_studio_service.

    Returns (result_for_model, action_summary, not_connected). Diego is
    consult-only (search + draft); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_brand_styles":
        asset_type = (tool_input.get("asset_type") or "").strip()
        tier       = (tool_input.get("tier") or "").strip()
        res = await design_studio_service.search_brand_styles(asset_type=asset_type, tier=tier)
        summary = {
            "input": f"asset_type={asset_type or 'any'} tier={tier or 'any'}",
            "result": f"{res['count']} style(s) found",
        }
        return res, summary, False

    if name == "draft_brand_brief":
        concept    = (tool_input.get("concept") or "").strip()
        asset_type = (tool_input.get("asset_type") or "").strip()
        tone       = (tool_input.get("tone") or "").strip()
        res = await design_studio_service.draft_brand_brief(
            artist_id, concept=concept, asset_type=asset_type, tone=tone,
        )
        summary = {
            "input": f"concept={concept[:40] or 'unspecified'} asset_type={asset_type[:40] or 'unspecified'}",
            "result": f"drafted={res['drafted']}, {res['recommendation']}",
        }
        return res, summary, False


    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Venue-Hawk (Ray B) tool_use ────────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego pattern exactly: these tools are passed
# to the Anthropic API for the venue-hawk agent ONLY (see the gate in chat_stream).
# Every other agent — including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia,
# Zara, Kai, Luna, and Diego — takes its own unchanged path and never receives
# VENUE_HAWK_TOOLS. Handlers map straight onto the mock-first venue_hawk_service
# functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
VENUE_HAWK_MAX_TOOL_ITERS = 5

VENUE_HAWK_TOOLS = [
    {
        "name": "search_venues",
        "description": ("Filter an ARTIST-SUPPLIED venue list by market/region, "
                        "capacity tier, or genre fit. This tool NEVER invents venue "
                        "or promoter names — it has no built-in directory. Pass the "
                        "artist's own venue list in `venue_list`; with no list it "
                        "returns a [NEEDS:venue_targets] gap you must surface to the "
                        "artist (ask them for target venues or a connected source)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "capacity_tier": {"type": "string"},
                "genre": {"type": "string"},
                "venue_list": {
                    "type": "array",
                    "description": ("Artist-supplied venues. Each item is an object "
                                    "with any of: name, market/region, capacity_tier, "
                                    "genre. Never fabricate entries."),
                    "items": {"type": "object"},
                },
            },
        },
    },
    {
        "name": "submit_booking_hold",
        "description": ("Build and record a hold REQUEST (venue, date(s), act, "
                        "deal-structure ask, hold_type/pencil order). This does NOT "
                        "send anything and does NOT confirm a booking — it assembles "
                        "the state and returns an aggregated `missing` list of any "
                        "gaps. A hold is a request, not a confirmed booking."),
        "input_schema": {
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "venue_id": {"type": "string"},
                "show_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more target dates for the hold.",
                },
                "act": {"type": "string"},
                "deal_structure": {
                    "type": "string",
                    "description": ("The deal STRUCTURE being asked for (e.g. flat "
                                    "guarantee, door split, versus deal) — the "
                                    "structure, not an invented figure."),
                },
                "hold_type": {
                    "type": "string",
                    "enum": ["first", "second", "third"],
                },
            },
        },
    },
    {
        "name": "lookup_booking_doctrine",
        "description": ("Look up the doctrine for ONE booking topic before acting: "
                        "'hold_system' (holds/pencils/challenge chain), "
                        "'deal_mechanisms' (flat guarantee, door split, versus, "
                        "bonus, percentage-after-split — structures only), "
                        "'deal_memo' (the minimum deal-memo fields), 'rider' "
                        "(hospitality vs technical), 'outreach' (routing, avails, "
                        "cadence), 'agent_economics', or 'boundaries' (what belongs "
                        "to tour-commander / puppet-master / airwave). THE HARD RULE "
                        "OF THIS DOMAIN: NO guarantee, split percentage, commission, "
                        "or currency figure is ever invented or quoted — mechanisms "
                        "are encoded, numbers are negotiated and only ever the "
                        "artist's own; venue names are never fabricated; a hold is "
                        "never a confirmation; deal evaluation is STRUCTURAL."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["hold_system", "deal_mechanisms", "deal_memo", "rider",
                             "outreach", "agent_economics", "boundaries"],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "send_booking_inquiry",
        "description": ("Send a personalized booking inquiry on the artist's behalf. "
                        "YOU write the inquiry subject and body in your turn and pass "
                        "them in — this tool SENDS them, it never writes or edits the "
                        "inquiry for you, and it never invents a venue or a figure "
                        "(any guarantee must be one the artist supplied). Identify "
                        "the venue with `venue_id` or `venue`. If the artist has not "
                        "connected a booking account the send is blocked and you must "
                        "tell them to connect one first. Keep it short and pertinent "
                        "— the ask, the act, the routing window, the avails; no bio, "
                        "no persuasive essay."),
        "input_schema": {
            "type": "object",
            "properties": {
                "venue_id": {"type": "string"},
                "venue": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["subject", "body"],
        },
    },
]


async def _execute_venue_hawk_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Ray B tool call against the mock-first venue_hawk_service.

    Returns (result_for_model, action_summary, venue_booking_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - venue_booking_not_connected: True when a hold was blocked on a
        missing/expired booking account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Ray B can explain the outcome to the artist. Mirrors
    _execute_vault_keeper_tool / _execute_design_studio_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_venues":
        market        = (tool_input.get("market") or "").strip()
        capacity_tier = (tool_input.get("capacity_tier") or "").strip()
        genre         = (tool_input.get("genre") or "").strip()
        venue_list    = tool_input.get("venue_list")
        res = await venue_hawk_service.search_venues(
            market=market, capacity_tier=capacity_tier, genre=genre,
            venue_list=venue_list,
        )
        if res.get("status") == "needs_targets":
            result_str = "no venue list supplied — [NEEDS:venue_targets]"
        else:
            result_str = f"{res['count']} artist-supplied venue(s) matched"
        summary = {
            "input": f"market={market or 'any'} tier={capacity_tier or 'any'} genre={genre or 'any'}",
            "result": result_str,
        }
        return res, summary, False

    if name == "submit_booking_hold":
        venue      = (tool_input.get("venue") or "").strip()
        venue_id   = (tool_input.get("venue_id") or "").strip()
        show_dates = tool_input.get("show_dates")
        act        = (tool_input.get("act") or "").strip()
        deal       = (tool_input.get("deal_structure") or "").strip()
        hold_type  = (tool_input.get("hold_type") or "first").strip()
        res = await venue_hawk_service.submit_booking_hold(
            artist_id, venue=venue, venue_id=venue_id, show_dates=show_dates,
            act=act, deal_structure=deal, hold_type=hold_type,
        )
        if res.get("status") == "recorded":
            result_str = f"hold request recorded ({hold_type})"
        else:
            result_str = f"incomplete — missing {', '.join(res.get('missing', [])) or 'fields'}"
        summary = {
            "input": f"venue={venue or venue_id or 'unspecified'} hold_type={hold_type}",
            "result": result_str,
        }
        return res, summary, False

    if name == "lookup_booking_doctrine":
        # Deliberately NOT gated — pure corpus read, no account needed.
        topic = (tool_input.get("topic") or "").strip()
        res = await venue_hawk_service.lookup_booking_doctrine(topic)
        if res.get("status") == "ok":
            result_str = f"{len(res['honesty_rules'])} honesty rule(s)"
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"topic={topic or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "send_booking_inquiry":
        # Marcus send seam: the MODEL wrote subject/body; the tool only sends.
        venue_id = (tool_input.get("venue_id") or "").strip()
        venue    = (tool_input.get("venue") or "").strip()
        subject  = tool_input.get("subject") or ""
        body     = tool_input.get("body") or ""
        try:
            sent = await venue_hawk_service.send_booking_inquiry(
                artist_id, venue_id=venue_id, venue=venue, subject=subject, body=body,
            )
            if sent.get("status") == "missing_venue":
                return (
                    {"error": "missing_venue"},
                    {"input": f"venue_id={venue_id or ''} venue={venue or ''}",
                     "result": "missing venue"},
                    False,
                )
            return (
                sent,
                {"input": f"venue={venue or venue_id} subject={subject[:40]}",
                 "result": "booking inquiry sent"},
                False,
            )
        except venue_hawk_service.VenueBookingNotConnected:
            return (
                {
                    "venue_booking_not_connected": True,
                    "message": ("Artist has not connected a booking/ticketing account. Tell them "
                                "to connect one before you can send booking inquiries."),
                },
                {"input": f"venue={venue or venue_id}", "result": "venue_booking_not_connected"},
                True,
            )
        except venue_hawk_service.VenueBookingAuthExpired:
            return (
                {
                    "venue_booking_not_connected": True,
                    "message": ("Booking-account authorization expired. Tell the artist to "
                                "re-connect their booking account before you can send inquiries."),
                },
                {"input": f"venue={venue or venue_id}", "result": "venue_booking_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Global-Scout (Nova) tool_use ───────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / ... / Ray B (venue-hawk) pattern
# exactly: these tools are passed to the Anthropic API for the global-scout agent
# ONLY (see the gate in chat_stream). Every other agent takes its own unchanged path
# and never receives GLOBAL_SCOUT_TOOLS. Handlers map straight onto the mock-first
# global_scout_service functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
GLOBAL_SCOUT_MAX_TOOL_ITERS = 5

GLOBAL_SCOUT_TOOLS = [
    {
        "name": "search_markets",
        "description": "Search the world market directory for territories where the artist's sound has traction, by genre and/or region (Latin America, Europe, Asia Pacific, Africa)",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "region": {"type": "string"},
            },
        },
    },
    {
        "name": "draft_market_entry_plan",
        "description": "Draft a concrete international market-entry plan by applying a market's streaming economics to a genre and marketing budget, estimating addressable reach, projected streams and projected revenue",
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {"type": "string"},
                "genre": {"type": "string"},
                "marketing_budget": {"type": "number"},
            },
        },
    },
]


async def _execute_global_scout_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Nova tool call against the mock-first global_scout_service.

    Returns (result_for_model, action_summary, not_connected). Nova is
    consult-only (search + draft); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_markets":
        genre  = (tool_input.get("genre") or "").strip()
        region = (tool_input.get("region") or "").strip()
        res = await global_scout_service.search_markets(genre=genre, region=region)
        summary = {
            "input": f"genre={genre or 'any'} region={region or 'any'}",
            "result": f"{res['count']} market(s) found",
        }
        return res, summary, False

    if name == "draft_market_entry_plan":
        market_id        = (tool_input.get("market_id") or "").strip()
        genre            = (tool_input.get("genre") or "").strip()
        marketing_budget = tool_input.get("marketing_budget") or 0
        res = await global_scout_service.draft_market_entry_plan(
            artist_id, market_id=market_id, genre=genre, marketing_budget=marketing_budget,
        )
        summary = {
            "input": f"market_id={market_id or 'unspecified'} genre={genre or 'unspecified'}",
            "result": f"viable={res['viable']}, {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Tour-Commander (Miles) tool_use ────────────────────────────────────────────
# DOC-WRITER Option B (tour-operations engine). These tools are passed to the
# Anthropic API for the tour-commander agent ONLY (see the gate in chat_stream).
# Every other agent takes its own unchanged path and never receives
# TOUR_COMMANDER_TOOLS. Handlers map straight onto the tour-operations
# tour_commander_service functions; they make ZERO network calls, read no
# secrets, and are UNGATED (both are pure corpus-read / data-scaffold tools).
# The old mock+gate crewing/routing/budget tools and their connected-account
# gate are RETIRED — Miles's real value is prep documents (advance packs, day
# sheets, settlement-prep sheets) built from Miles's own tour-ops knowledge
# base, not a mock crewing/production API.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
TOUR_COMMANDER_MAX_TOOL_ITERS = 5

TOUR_COMMANDER_TOOLS = [
    {
        "name": "lookup_tour_ops_doctrine",
        "description": (
            "Look up tour-operations doctrine from Miles's corpus — filter by advancing "
            "key, day-sheet field, settlement-prep key, routing key, festival key, and/or "
            "settlement-vocabulary term. Call with no filters to browse the available keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "advancing_key": {
                    "type": "string",
                    "enum": list(tour_commander_service.tour_ops_data.ADVANCING_DOCTRINE),
                },
                "day_sheet_field": {
                    "type": "string",
                    "enum": [rec["field"] for rec in tour_commander_service.tour_ops_data.DAY_SHEET_SPEC],
                },
                "settlement_key": {
                    "type": "string",
                    "enum": list(tour_commander_service.tour_ops_data.SETTLEMENT_PREP_DOCTRINE),
                },
                "routing_key": {
                    "type": "string",
                    "enum": list(tour_commander_service.tour_ops_data.ROUTING_AND_PREP),
                },
                "festival_key": {
                    "type": "string",
                    "enum": list(tour_commander_service.tour_ops_data.FESTIVAL_VARIANT),
                },
                "vocabulary_term": {
                    "type": "string",
                    "enum": list(tour_commander_service.tour_ops_data.SETTLEMENT_VOCABULARY),
                },
            },
        },
    },
    {
        "name": "build_tour_doc_scaffold",
        "description": (
            "Assemble COMPACT ingredients for a tour-ops document — an advance_pack, a "
            "day_sheet, or a settlement_prep_sheet. Returns checklists, doctrine, field "
            "lists, questions, and gap markers; you write the prose in your turn. Sensitive "
            "day-sheet fields (hotel info, door codes, flight details) are excluded from "
            "the default output unless explicitly requested. Settlement prep never "
            "contains a computed total — questions and restated inputs only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": list(tour_commander_service.DOC_TYPES),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Artist-supplied fields: deal_memo (advance_pack); variant, "
                        "include_sensitive, and per-field values like doors/curfew/"
                        "artist_hotel_info (day_sheet); deal_terms (settlement_prep_sheet). "
                        "Anything unsupplied becomes a gap marker."
                    ),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_tour_commander_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Miles tool call against the tour-operations tour_commander_service.

    Returns (result_for_model, action_summary, tour_ops_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - tour_ops_not_connected: retained for the shared generator's 3-tuple
        contract; ALWAYS False now — Miles's tools are pure corpus-read / data-
        scaffold tools with no connected account and no gate (the old
        connected-account gate and its crewing action are retired).
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Miles can explain the outcome to the artist. Mirrors
    _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_tour_ops_doctrine":
        advancing_key   = (tool_input.get("advancing_key") or "").strip()
        day_sheet_field = (tool_input.get("day_sheet_field") or "").strip()
        settlement_key  = (tool_input.get("settlement_key") or "").strip()
        routing_key     = (tool_input.get("routing_key") or "").strip()
        festival_key    = (tool_input.get("festival_key") or "").strip()
        vocabulary_term = (tool_input.get("vocabulary_term") or "").strip()
        res = await tour_commander_service.lookup_tour_ops_doctrine(
            advancing_key=advancing_key, day_sheet_field=day_sheet_field,
            settlement_key=settlement_key, routing_key=routing_key,
            festival_key=festival_key, vocabulary_term=vocabulary_term,
        )
        filters = ",".join(f for f in (advancing_key, day_sheet_field, settlement_key,
                                       routing_key, festival_key, vocabulary_term) if f) or "none"
        if res.get("mode") == "index":
            result_str = "index browsed"
        else:
            hit = (len(res.get("advancing", [])) + len(res.get("day_sheet", []))
                   + len(res.get("settlement", [])) + len(res.get("routing", []))
                   + len(res.get("festival", [])) + len(res.get("vocabulary", [])))
            miss = len(res.get("not_found", []))
            result_str = f"{hit} match(es), {miss} not found"
        summary = {"input": f"filters={filters}", "result": result_str}
        return res, summary, False

    if name == "build_tour_doc_scaffold":
        doc_type   = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await tour_commander_service.build_tour_doc_scaffold(
            doc_type=doc_type, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Brand Connect (Nia) tool_use ───────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo pattern exactly:
# these tools are passed to the Anthropic API for the brand-connect agent ONLY (see
# the gate in chat_stream). Every other agent — including Marcus, Lex, Jade, Ray,
# Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, Miles, and Solo — takes
# its own unchanged path and never receives BRAND_CONNECT_TOOLS. Handlers map
# straight onto the mock-first brand_connect_service functions; they make ZERO
# network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
BRAND_CONNECT_MAX_TOOL_ITERS = 5

BRAND_CONNECT_TOOLS = [
    {
        "name": "search_brand_partners",
        "description": "Search the directory of brands seeking artist partnerships by the category a brand sits in (tech, apparel, beverage, gaming, beauty) or the sponsorship budget tier it operates at (emerging, growth, premium)",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["tech", "apparel", "beverage", "gaming", "beauty"],
                },
                "budget_tier": {
                    "type": "string",
                    "enum": ["emerging", "growth", "premium"],
                },
            },
        },
    },
    {
        "name": "draft_partnership_proposal",
        "description": "Draft a concrete brand-partnership proposal by applying a brand's typical fee and audience-fit to a campaign, estimating the fair deal value against the artist's proposed fee",
        "input_schema": {
            "type": "object",
            "properties": {
                "brand_id": {"type": "string"},
                "campaign_type": {"type": "string"},
                "fee": {"type": "integer"},
            },
        },
    },
    {
        "name": "submit_partnership_proposal",
        "description": "Submit a brand-partnership proposal for a campaign through the artist's connected brand-partnerships account on their behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "brand_id": {"type": "string"},
                "campaign_type": {"type": "string"},
                "fee": {"type": "integer"},
            },
            "required": ["brand_id", "campaign_type"],
        },
    },
    {
        "name": "lookup_brand_deal_doctrine",
        "description": ("Look up the doctrine for ONE brand-deal topic before acting: "
                        "deliverables, compensation, usage_rights, exclusivity, "
                        "approval_workflow, disclosure, or termination_morals (the deal "
                        "terms), plus 'categories' (the brand-category surface) and "
                        "'outreach' (the outreach doctrine). Use it to structure a deal "
                        "or a pitch. THE HARD RULE OF THIS DOMAIN: NO market rate, fee "
                        "range, or currency figure is ever invented or quoted — "
                        "compensation is only ever the artist's own supplied number; deal "
                        "evaluation is STRUCTURAL (which terms are present/missing), never "
                        "a good/bad verdict; disclosure is the current convention, not "
                        "legal advice (verify live)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["deliverables", "compensation", "usage_rights",
                             "exclusivity", "approval_workflow", "disclosure",
                             "termination_morals", "categories", "outreach"],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "send_brand_pitch",
        "description": ("Send a personalized brand pitch on the artist's behalf. YOU write "
                        "the pitch subject and body in your turn and pass them in — this "
                        "tool SENDS them, it never writes or edits the pitch for you, and "
                        "it never invents a rate (any figure must be one the artist "
                        "supplied). Pass the brand_id of a brand from the directory. If the "
                        "artist has not connected a brand-partnerships account the send is "
                        "blocked and you must tell them to connect one first."),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["brand_id", "subject", "body"],
        },
    },
]


async def _execute_brand_connect_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Nia tool call against the mock-first brand_connect_service.

    Returns (result_for_model, action_summary, partnerships_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - partnerships_not_connected: True when a submission was blocked on a
        missing/expired brand-partnerships account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Nia can explain the outcome to the artist. Mirrors
    _execute_airwave_tool / _execute_venue_hawk_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_brand_partners":
        category    = (tool_input.get("category") or "").strip()
        budget_tier = (tool_input.get("budget_tier") or "").strip()
        res = await brand_connect_service.search_brand_partners(
            category=category, budget_tier=budget_tier,
        )
        summary = {
            "input": f"category={category or 'any'} budget_tier={budget_tier or 'any'}",
            "result": f"{res['count']} brand(s) found",
        }
        return res, summary, False

    if name == "draft_partnership_proposal":
        brand_id      = (tool_input.get("brand_id") or "").strip()
        campaign_type = (tool_input.get("campaign_type") or "").strip()
        fee           = tool_input.get("fee") or 0
        res = await brand_connect_service.draft_partnership_proposal(
            artist_id, brand_id=brand_id, campaign_type=campaign_type, fee=fee,
        )
        summary = {
            "input": f"brand_id={brand_id or 'unspecified'} campaign={campaign_type or 'unspecified'}",
            "result": f"viable={res['viable']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "submit_partnership_proposal":
        brand_id      = (tool_input.get("brand_id") or "").strip()
        campaign_type = (tool_input.get("campaign_type") or "").strip()
        fee           = tool_input.get("fee") or 0
        if not brand_id:
            return (
                {"error": "missing_brand_id"},
                {"input": f"brand_id={brand_id or ''} campaign={campaign_type}",
                 "result": "missing brand id"},
                False,
            )
        try:
            submitted = await brand_connect_service.submit_partnership_proposal(
                artist_id, brand_id, campaign_type, fee,
            )
            if submitted.get("status") == "unknown_brand":
                return (
                    {"error": "unknown_brand", "brand_id": submitted.get("brand_id")},
                    {"input": f"brand_id={brand_id}", "result": "unknown brand"},
                    False,
                )
            return (
                {"status": "submitted", "reference": submitted.get("reference"),
                 "brand_id": submitted.get("brand_id"), "brand_name": submitted.get("brand_name"),
                 "campaign_type": submitted.get("campaign_type"),
                 "proposed_fee": submitted.get("proposed_fee")},
                {"input": f"brand_id={brand_id} campaign={campaign_type}",
                 "result": "partnership proposal submitted"},
                False,
            )
        except brand_connect_service.BrandConnectAccountNotConnected:
            return (
                {
                    "partnerships_not_connected": True,
                    "message": ("Artist has not connected a brand-partnerships account. Tell "
                                "them to connect one before you can submit partnership proposals."),
                },
                {"input": f"brand_id={brand_id}", "result": "partnerships_not_connected"},
                True,
            )
        except brand_connect_service.BrandConnectAuthExpired:
            return (
                {
                    "partnerships_not_connected": True,
                    "message": ("Brand-partnerships authorization expired. Tell the artist to "
                                "re-connect their account before you can submit proposals."),
                },
                {"input": f"brand_id={brand_id}", "result": "partnerships_auth_expired"},
                True,
            )

    if name == "lookup_brand_deal_doctrine":
        # Deliberately NOT gated on BRAND_CONNECT_ACCOUNT_CONNECTED — pure corpus
        # read, no account needed (mirrors the ungated doctrine lookups).
        topic = (tool_input.get("topic") or "").strip()
        res = await brand_connect_service.lookup_brand_deal_doctrine(topic)
        if res.get("status") == "ok":
            result_str = f"{len(res['honesty_rules'])} honesty rule(s)"
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"topic={topic or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "send_brand_pitch":
        # Marcus send seam: the MODEL wrote subject/body; the tool only sends.
        brand_id = (tool_input.get("brand_id") or "").strip()
        subject  = tool_input.get("subject") or ""
        body     = tool_input.get("body") or ""
        if not brand_id:
            return (
                {"error": "missing_brand_id"},
                {"input": f"brand_id={brand_id or ''}", "result": "missing brand id"},
                False,
            )
        try:
            sent = await brand_connect_service.send_brand_pitch(
                artist_id, brand_id, subject, body,
            )
            if sent.get("status") == "unknown_brand":
                return (
                    {"error": "unknown_brand", "brand_id": sent.get("brand_id")},
                    {"input": f"brand_id={brand_id}", "result": "unknown brand"},
                    False,
                )
            return (
                sent,
                {"input": f"brand_id={brand_id} subject={subject[:40]}",
                 "result": "brand pitch sent"},
                False,
            )
        except brand_connect_service.BrandConnectAccountNotConnected:
            return (
                {
                    "partnerships_not_connected": True,
                    "message": ("Artist has not connected a brand-partnerships account. Tell "
                                "them to connect one before you can send brand pitches."),
                },
                {"input": f"brand_id={brand_id}", "result": "partnerships_not_connected"},
                True,
            )
        except brand_connect_service.BrandConnectAuthExpired:
            return (
                {
                    "partnerships_not_connected": True,
                    "message": ("Brand-partnerships authorization expired. Tell the artist to "
                                "re-connect their account before you can send brand pitches."),
                },
                {"input": f"brand_id={brand_id}", "result": "partnerships_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Merch-Empire (Max) tool_use ────────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo / Nia pattern
# exactly: these tools are passed to the Anthropic API for the merch-empire agent
# ONLY (see the gate in chat_stream). Every other agent — including Marcus, Lex,
# Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, Miles, Solo,
# and Nia — takes its own unchanged path and never receives MERCH_EMPIRE_TOOLS.
# Handlers map straight onto the mock-first merch_empire_service functions; they
# make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
MERCH_EMPIRE_MAX_TOOL_ITERS = 5

MERCH_EMPIRE_TOOLS = [
    {
        "name": "search_merch_products",
        "description": "Search the catalogue of blank merch products the platform can produce by the category a product sits in (apparel, print, accessories, music) or the production tier it belongs to (starter, pro)",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["apparel", "print", "accessories", "music"],
                },
                "tier": {
                    "type": "string",
                    "enum": ["starter", "pro"],
                },
            },
        },
    },
    {
        "name": "build_production_run",
        "description": "Build a concrete merch production run by applying a product's per-unit cost and suggested retail to a quantity, estimating total cost, projected revenue, and margin for the run",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "design_name": {"type": "string"},
                "quantity": {"type": "integer"},
            },
        },
    },
]


async def _execute_merch_empire_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Max tool call against the mock-first merch_empire_service.

    Returns (result_for_model, action_summary, not_connected). Max is
    consult-only (search + cost); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_merch_products":
        category = (tool_input.get("category") or "").strip()
        tier     = (tool_input.get("tier") or "").strip()
        res = await merch_empire_service.search_merch_products(
            category=category, tier=tier,
        )
        summary = {
            "input": f"category={category or 'any'} tier={tier or 'any'}",
            "result": f"{res['count']} product(s) found",
        }
        return res, summary, False

    if name == "build_production_run":
        product_id  = (tool_input.get("product_id") or "").strip()
        design_name = (tool_input.get("design_name") or "").strip()
        quantity    = tool_input.get("quantity") or 0
        res = await merch_empire_service.build_production_run(
            artist_id, product_id=product_id, design_name=design_name, quantity=quantity,
        )
        summary = {
            "input": f"product_id={product_id or 'unspecified'} qty={quantity or 'unspecified'}",
            "result": f"viable={res['viable']}, {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Airwave (Solo) tool_use ────────────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles pattern exactly: these
# tools are passed to the Anthropic API for the airwave agent ONLY (see the gate in
# chat_stream). Every other agent — including Marcus, Lex, Jade, Ray, Cleo, Finn,
# Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, and Miles — takes its own unchanged
# path and never receives AIRWAVE_TOOLS. Handlers map straight onto the mock-first
# airwave_service functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
AIRWAVE_MAX_TOOL_ITERS = 5

AIRWAVE_TOOLS = [
    {
        "name": "search_airplay_targets",
        "description": "Search the directory of radio stations and playlist outlets by the format a target programs (indie, pop, rock, electronic) or the market/region it serves",
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["indie", "pop", "rock", "electronic"],
                },
                "market": {"type": "string"},
            },
        },
    },
    {
        "name": "draft_airplay_pitch",
        "description": "Draft a concrete airplay/playlist pitch by applying a target's reach and placement odds to a track, estimating the audience the artist could reach and how many add slots that converts to",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "track_title": {"type": "string"},
                "release_date": {"type": "string"},
            },
        },
    },
    {
        "name": "submit_airplay_pitch",
        "description": "Submit an airplay/playlist pitch for a track through the artist's connected radio-plugging / DSP-pitching account on their behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "track_title": {"type": "string"},
                "release_date": {"type": "string"},
            },
            "required": ["target_id", "track_title"],
        },
    },
    {
        "name": "lookup_radio_promo_doctrine",
        "description": ("Look up the doctrine for ONE radio / DSP-editorial topic before "
                        "acting: college_radio (NACC / Earshot mechanics + campaign "
                        "shape), cancon (the 35%/50% quota + MAPL points), "
                        "commercial_radio (the honest barrier), dsp_editorial (Spotify "
                        "window/via/copy), servicing_platforms (the delivery layer), "
                        "delivery_vs_outreach, or satellite_public. HARD RULES: NEVER "
                        "assert or compute a song's CanCon / MAPL status — only the "
                        "artist's DECLARED letters pass through (misdeclaration risks the "
                        "STATION's licence); a pitch is CONSIDERATION, never a placement; "
                        "costs/panel-sizes/processes are current conventions, verify "
                        "live. Playlist-CURATOR outreach is OUT OF SCOPE — it belongs to "
                        "the management department (Marcus), and a curator topic here "
                        "returns that boundary."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["college_radio", "cancon", "commercial_radio",
                             "dsp_editorial", "servicing_platforms",
                             "delivery_vs_outreach", "satellite_public"],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "send_radio_pitch",
        "description": ("Send a personalized radio / DSP-editorial pitch on the artist's "
                        "behalf. YOU write the pitch subject and body in your turn and "
                        "pass them in — this tool SENDS them, it never writes or edits the "
                        "pitch for you. NEVER assert a CanCon/MAPL status: pass the "
                        "artist's own DECLARED letters (e.g. 'MAL') in mapl_declaration if "
                        "they gave them, otherwise leave it and the pitch carries a "
                        "[NEEDS:mapl_declaration] gap. Pass the target_id of a target from "
                        "the directory. If the artist has not connected a plugging account "
                        "the send is blocked and you must tell them to connect one."),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "mapl_declaration": {"type": "string"},
            },
            "required": ["target_id", "subject", "body"],
        },
    },
]


async def _execute_airwave_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Solo tool call against the mock-first airwave_service.

    Returns (result_for_model, action_summary, plugging_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - plugging_not_connected: True when a pitch was blocked on a
        missing/expired plugging account.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Solo can explain the outcome to the artist. Mirrors
    _execute_venue_hawk_tool / _execute_tour_commander_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_airplay_targets":
        fmt    = (tool_input.get("format") or "").strip()
        market = (tool_input.get("market") or "").strip()
        res = await airwave_service.search_airplay_targets(format=fmt, market=market)
        summary = {
            "input": f"format={fmt or 'any'} market={market or 'any'}",
            "result": f"{res['count']} target(s) found",
        }
        return res, summary, False

    if name == "draft_airplay_pitch":
        target_id    = (tool_input.get("target_id") or "").strip()
        track_title  = (tool_input.get("track_title") or "").strip()
        release_date = (tool_input.get("release_date") or "").strip()
        res = await airwave_service.draft_airplay_pitch(
            artist_id, target_id=target_id, track_title=track_title, release_date=release_date,
        )
        summary = {
            "input": f"target_id={target_id or 'unspecified'} track={track_title or 'unspecified'}",
            "result": f"viable={res['viable']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "submit_airplay_pitch":
        target_id    = (tool_input.get("target_id") or "").strip()
        track_title  = (tool_input.get("track_title") or "").strip()
        release_date = (tool_input.get("release_date") or "").strip()
        if not target_id:
            return (
                {"error": "missing_target_id"},
                {"input": f"target_id={target_id or ''} track={track_title}",
                 "result": "missing target id"},
                False,
            )
        try:
            pitched = await airwave_service.submit_airplay_pitch(
                artist_id, target_id, track_title, release_date,
            )
            if pitched.get("status") == "unknown_target":
                return (
                    {"error": "unknown_target", "target_id": pitched.get("target_id")},
                    {"input": f"target_id={target_id}", "result": "unknown target"},
                    False,
                )
            return (
                {"status": "pitched", "reference": pitched.get("reference"),
                 "target_id": pitched.get("target_id"), "target_name": pitched.get("target_name"),
                 "track_title": pitched.get("track_title"), "release_date": pitched.get("release_date")},
                {"input": f"target_id={target_id} track={track_title}",
                 "result": "airplay pitch submitted"},
                False,
            )
        except airwave_service.AirwaveAccountNotConnected:
            return (
                {
                    "plugging_not_connected": True,
                    "message": ("Artist has not connected a radio-plugging/DSP-pitching account. Tell "
                                "them to connect one before you can submit airplay pitches."),
                },
                {"input": f"target_id={target_id}", "result": "plugging_not_connected"},
                True,
            )
        except airwave_service.AirwaveAuthExpired:
            return (
                {
                    "plugging_not_connected": True,
                    "message": ("Plugging-account authorization expired. Tell the artist to "
                                "re-connect their plugging account before you can submit pitches."),
                },
                {"input": f"target_id={target_id}", "result": "plugging_auth_expired"},
                True,
            )

    if name == "lookup_radio_promo_doctrine":
        # Deliberately NOT gated — pure corpus read. A curator topic returns the
        # out-of-scope boundary (Marcus), never a curator action.
        topic = (tool_input.get("topic") or "").strip()
        res = await airwave_service.lookup_radio_promo_doctrine(topic)
        status = res.get("status")
        if status == "ok":
            result_str = f"{len(res['honesty_rules'])} honesty rule(s)"
        else:
            result_str = status or "error"
        summary = {"input": f"topic={topic or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "send_radio_pitch":
        # Marcus send seam: the MODEL wrote subject/body; the tool only sends and
        # NEVER asserts a MAPL status — declared letters ride through or a gap.
        target_id = (tool_input.get("target_id") or "").strip()
        subject   = tool_input.get("subject") or ""
        body      = tool_input.get("body") or ""
        mapl      = tool_input.get("mapl_declaration")
        if not target_id:
            return (
                {"error": "missing_target_id"},
                {"input": f"target_id={target_id or ''}", "result": "missing target id"},
                False,
            )
        try:
            sent = await airwave_service.send_radio_pitch(
                artist_id, target_id, subject, body, mapl_declaration=mapl,
            )
            if sent.get("status") == "unknown_target":
                return (
                    {"error": "unknown_target", "target_id": sent.get("target_id")},
                    {"input": f"target_id={target_id}", "result": "unknown target"},
                    False,
                )
            return (
                sent,
                {"input": f"target_id={target_id} subject={subject[:40]}",
                 "result": "radio pitch sent"},
                False,
            )
        except airwave_service.AirwaveAccountNotConnected:
            return (
                {
                    "plugging_not_connected": True,
                    "message": ("Artist has not connected a radio-plugging/DSP-pitching account. "
                                "Tell them to connect one before you can send radio pitches."),
                },
                {"input": f"target_id={target_id}", "result": "plugging_not_connected"},
                True,
            )
        except airwave_service.AirwaveAuthExpired:
            return (
                {
                    "plugging_not_connected": True,
                    "message": ("Plugging-account authorization expired. Tell the artist to "
                                "re-connect their plugging account before you can send pitches."),
                },
                {"input": f"target_id={target_id}", "result": "plugging_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Unit — Fan-Builder (fan-builder) tool_use: lookup_engagement_doctrine +
#        build_engagement_doc_scaffold
# ═══════════════════════════════════════════════════════════════════════════════
# DOC-WRITER Option B (fan-engagement engine). These tools are passed to the
# Anthropic API for the fan-builder agent ONLY (see the gate in chat_stream).
# Every other agent — including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor,
# Nadia, Zara, Kai, Luna, Diego, Ray B, Miles, Solo, Nia, and Max — takes its
# own unchanged path and never receives FAN_BUILDER_TOOLS. Handlers map
# straight onto the fan-engagement fan_builder_service functions; they make
# ZERO network calls, read no secrets, and are UNGATED (both are pure
# corpus-read / data-scaffold tools). The old mock+gate segment-search/
# campaign-build/broadcast-schedule tools and their connected-account gate
# are RETIRED — Aria's real value is prep documents (engagement plans,
# superfan program outlines) built from Aria's own fan-engagement knowledge
# base, not a mock fan-CRM/broadcast API. Aria never claims to have sent
# anything — that infrastructure does not exist yet.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
FAN_BUILDER_MAX_TOOL_ITERS = 5

FAN_BUILDER_TOOLS = [
    {
        "name": "lookup_engagement_doctrine",
        "description": (
            "Look up fan-engagement doctrine from Aria's corpus — filter by funnel "
            "key, true-fans-principle key, superfan-signal key, owned-channel key, "
            "cadence key, and/or time-waste key. Call with no filters to browse the "
            "available keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "funnel_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.FAN_FUNNEL),
                },
                "principle_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.THOUSAND_TRUE_FANS),
                },
                "signal_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.SUPERFAN_IDENTIFICATION),
                },
                "channel_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.OWNED_CHANNELS),
                },
                "cadence_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.CADENCE_SPEC),
                },
                "waste_key": {
                    "type": "string",
                    "enum": list(fan_builder_service.engagement_data.WHAT_WASTES_TIME),
                },
            },
        },
    },
    {
        "name": "build_engagement_doc_scaffold",
        "description": (
            "Assemble COMPACT ingredients for a fan-engagement document — an "
            "engagement_plan or a superfan_program_outline. Returns checklists, "
            "doctrine, questions, tier structure, and gap markers; you write the "
            "prose in your turn. Never claims a message was sent — Aria drafts "
            "copy and cadence only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": list(fan_builder_service.DOC_TYPES),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Artist-supplied fields: current_weekly_focus (engagement_plan, "
                        "optional restatement of current practice); offerings — a dict of "
                        "perk_type -> value like early_listens/decision_votes/beta_merch "
                        "(superfan_program_outline). Anything unsupplied becomes a gap marker."
                    ),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_fan_builder_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Aria tool call against the fan-engagement fan_builder_service.

    Returns (result_for_model, action_summary, fan_platform_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - fan_platform_not_connected: retained for the shared generator's 3-tuple
        contract; ALWAYS False now — Aria's tools are pure corpus-read / data-
        scaffold tools with no connected account and no gate (the old
        connected-account gate and its broadcast action are retired).
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Aria can explain the outcome to the artist. Mirrors
    _execute_tour_commander_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_engagement_doctrine":
        funnel_key    = (tool_input.get("funnel_key") or "").strip()
        principle_key = (tool_input.get("principle_key") or "").strip()
        signal_key    = (tool_input.get("signal_key") or "").strip()
        channel_key   = (tool_input.get("channel_key") or "").strip()
        cadence_key   = (tool_input.get("cadence_key") or "").strip()
        waste_key     = (tool_input.get("waste_key") or "").strip()
        res = await fan_builder_service.lookup_engagement_doctrine(
            funnel_key=funnel_key, principle_key=principle_key,
            signal_key=signal_key, channel_key=channel_key,
            cadence_key=cadence_key, waste_key=waste_key,
        )
        filters = ",".join(f for f in (funnel_key, principle_key, signal_key,
                                       channel_key, cadence_key, waste_key) if f) or "none"
        if res.get("mode") == "index":
            result_str = "index browsed"
        else:
            hit = (len(res.get("funnel", [])) + len(res.get("principles", []))
                   + len(res.get("signals", [])) + len(res.get("channels", []))
                   + len(res.get("cadence", [])) + len(res.get("waste", [])))
            miss = len(res.get("not_found", []))
            result_str = f"{hit} match(es), {miss} not found"
        summary = {"input": f"filters={filters}", "result": result_str}
        return res, summary, False

    if name == "build_engagement_doc_scaffold":
        doc_type   = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await fan_builder_service.build_engagement_doc_scaffold(
            doc_type=doc_type, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Sync-Agent (sync-agent) tool_use: search_sync_briefs +
#    assess_track_sync_fit + submit_sync_pitch ─────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo / Nia / Max /
# Aria pattern exactly: these tools are passed to the Anthropic API for the
# sync-agent agent ONLY (see the gate in chat_stream). Every other agent — including
# Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B,
# Miles, Solo, Nia, Max, and Aria — takes its own unchanged path and never receives
# SYNC_AGENT_TOOLS. Handlers map straight onto the mock-first sync_agent_service
# functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
SYNC_AGENT_MAX_TOOL_ITERS = 5

SYNC_AGENT_TOOLS = [
    {
        "name": "search_sync_briefs",
        "description": "Search open sync-placement briefs (TV, film, ad, game) by medium or genre",
        "input_schema": {
            "type": "object",
            "properties": {
                "medium": {
                    "type": "string",
                    "enum": ["tv", "film", "ad", "game"],
                },
                "genre": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_track_sync_fit",
        "description": "Assess how well a specific track fits a chosen sync brief's genre, tempo, and instrumental requirements",
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "track_title": {"type": "string"},
                "genre": {"type": "string"},
                "tempo_bpm": {"type": "number"},
                "has_instrumental": {"type": "boolean"},
            },
            "required": ["brief_id"],
        },
    },
    {
        "name": "submit_sync_pitch",
        "description": "Submit a pitch of a track to the music supervisor on a sync brief, on the artist's behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "track_title": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["brief_id", "track_title"],
        },
    },
]


async def _execute_sync_agent_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Sync tool call against the mock-first sync_agent_service.

    Returns (result_for_model, action_summary, sync_catalogue_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - sync_catalogue_not_connected: True when a pitch was blocked on a
        missing/expired sync catalogue.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Sync can explain the outcome to the artist. Mirrors
    _execute_vault_keeper_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_sync_briefs":
        medium = (tool_input.get("medium") or "").strip()
        genre  = (tool_input.get("genre") or "").strip()
        res = await sync_agent_service.search_sync_briefs(medium=medium, genre=genre)
        summary = {
            "input": f"medium={medium or 'any'} genre={genre or 'any'}",
            "result": f"{res['count']} brief(s) found",
        }
        return res, summary, False

    if name == "assess_track_sync_fit":
        brief_id         = (tool_input.get("brief_id") or "").strip()
        track_title      = (tool_input.get("track_title") or "").strip()
        genre            = (tool_input.get("genre") or "").strip()
        tempo_bpm        = tool_input.get("tempo_bpm") or 0
        has_instrumental = bool(tool_input.get("has_instrumental") or False)
        res = await sync_agent_service.assess_track_sync_fit(
            artist_id, brief_id=brief_id, track_title=track_title, genre=genre,
            tempo_bpm=tempo_bpm, has_instrumental=has_instrumental,
        )
        summary = {
            "input": f"track={track_title or 'unspecified'} brief={brief_id or 'unspecified'}",
            "result": f"fit={res['fit']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "submit_sync_pitch":
        brief_id    = (tool_input.get("brief_id") or "").strip()
        track_title = (tool_input.get("track_title") or "").strip()
        note        = tool_input.get("note") or ""
        if not track_title:
            return (
                {"error": "missing_track_title"},
                {"input": f"brief={brief_id or ''} track={track_title or ''}",
                 "result": "missing track title"},
                False,
            )
        try:
            pitch = await sync_agent_service.submit_sync_pitch(
                artist_id, brief_id, track_title, note,
            )
            return (
                {"status": "submitted", "reference": pitch.get("reference"),
                 "brief_id": pitch.get("brief_id"), "track_title": pitch.get("track_title")},
                {"input": f"brief={brief_id or 'unspecified'} track={track_title[:40]}",
                 "result": "pitch submitted"},
                False,
            )
        except sync_agent_service.SyncCatalogueNotConnected:
            return (
                {
                    "sync_catalogue_not_connected": True,
                    "message": ("Artist has not connected a sync catalogue / rights account. Tell "
                                "them to connect one before you can submit sync pitches."),
                },
                {"input": f"brief={brief_id}", "result": "sync_catalogue_not_connected"},
                True,
            )
        except sync_agent_service.SyncCatalogueAuthExpired:
            return (
                {
                    "sync_catalogue_not_connected": True,
                    "message": ("Sync-catalogue authorization expired. Tell the artist to "
                                "re-connect it before you can submit sync pitches."),
                },
                {"input": f"brief={brief_id}", "result": "sync_catalogue_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Unit — Creative-Director (creative-director) tool_use: search_rollout_templates
#    + assess_creative_concept + schedule_rollout + lookup_copy_conventions
#    + build_copy_scaffold ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo / Nia / Max /
# Aria / Sync / Nova pattern exactly: these tools are passed to the Anthropic API
# for the creative-director agent ONLY (see the gate in chat_stream). Every other
# agent — including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai,
# Luna, Diego, Ray B, Miles, Solo, Nia, Max, Aria, Sync, and Nova — takes its own
# unchanged path and never receives CREATIVE_DIRECTOR_TOOLS. Handlers map straight
# onto the mock-first creative_director_service functions; they make ZERO network
# calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
CREATIVE_DIRECTOR_MAX_TOOL_ITERS = 5

CREATIVE_DIRECTOR_TOOLS = [
    {
        "name": "search_rollout_templates",
        "description": "Search proven release-rollout campaign templates (single, EP, album) by release type or creative goal",
        "input_schema": {
            "type": "object",
            "properties": {
                "release_type": {
                    "type": "string",
                    "enum": ["single", "ep", "album"],
                },
                "goal": {
                    "type": "string",
                    "enum": ["awareness", "streams", "superfans", "press"],
                },
            },
        },
    },
    {
        "name": "assess_creative_concept",
        "description": "Assess how ready a specific creative concept is against a chosen rollout template's aesthetic, timing, and visual-asset requirements",
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "release_title": {"type": "string"},
                "theme": {"type": "string"},
                "weeks_to_release": {"type": "number"},
                "has_visual_assets": {"type": "boolean"},
            },
            "required": ["template_id"],
        },
    },
    {
        "name": "schedule_rollout",
        "description": "Schedule a release rollout for the artist against a chosen template, on the artist's behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "release_title": {"type": "string"},
                "kickoff": {"type": "string"},
            },
            "required": ["template_id", "release_title"],
        },
    },
    {
        "name": "lookup_copy_conventions",
        "description": ("Look up the structural conventions for ONE copy document type before "
                        "drafting it: the spec (word ranges / ordered sections / elements), the "
                        "conventions or doctrine that govern it, and the honesty rules. "
                        "doc_type is one of bio_short, bio_medium, bio_long, press_release, "
                        "one_sheet, epk_outline, caption_set. Bio word ranges are in words; "
                        "bio_long's upper bound is None — genuinely open-ended, never a guessed "
                        "ceiling. THE HARD RULE OF THIS DOMAIN: no fact, stat, press quote, or "
                        "comparison is EVER invented — every fact in any copy document is the "
                        "artist's supplied input verbatim, an explicit [NEEDS:<fact>] gap, or an "
                        "[ARTIST-SUPPLIED:<confirm>] reminder. Quotes appear only verbatim WITH "
                        "their source; stats only as supplied; comparisons only if the artist "
                        "supplied them. Every draft is for the artist's review — never "
                        "publish-ready."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": ["bio_short", "bio_medium", "bio_long", "press_release",
                             "one_sheet", "epk_outline", "caption_set"],
                },
            },
            "required": ["doc_type"],
        },
    },
    {
        "name": "build_copy_scaffold",
        "description": ("Build a compact ingredient scaffold for ONE copy document, AFTER you "
                        "have interviewed the artist and gathered what the document actually "
                        "requires. Include ONLY what the artist actually told you — omit "
                        "unknowns so they come back as explicit [NEEDS:<fact>] gaps instead of "
                        "invented facts; NO fact, stat, press quote, or comparison is EVER "
                        "invented. doc_type bio_short/bio_medium/bio_long: pass the fact slots "
                        "you gathered (artist_name, genre_or_sound, distinctive_hook, "
                        "hometown_or_scene, current_project, achievements, origin_story, "
                        "artistic_direction as applicable; optional press_quote WITH "
                        "press_quote_source) — the scaffold returns the slots verbatim plus the "
                        "word range and bio conventions as reminders. press_release: headline, "
                        "city, date, news_item, supporting_context, short_bio, boilerplate, "
                        "contact_name/role/email, music_link, press_photos_link; a quote is "
                        "included ONLY with quote_source — a quote without its source is "
                        "withheld and comes back as [NEEDS:quote_source]. one_sheet: "
                        "artist_name, genre, press_photo_link, short_bio, stats (passed through "
                        "VERBATIM), press_quotes, release_title/date/one_sentence, "
                        "social_streaming_links, contact_name/role; an empty stats block "
                        "surfaces the skip-unimpressive-stats doctrine as a choice to OFFER the "
                        "artist — never decide it silently. epk_outline: the three bios, "
                        "artist_brief, promo_photos, tracks, video_links, press_and_reviews, "
                        "highlights, social_streaming_links, contact, plus optional fact_sheet/"
                        "tour_dates/artwork/rider/lyrics_liner_notes. caption_set: hook_line, "
                        "context_line, cta, tag_link_placeholders — no invented urgency or "
                        "milestones. Then WRITE the draft yourself in your reply from the "
                        "scaffold: follow each section's guidance, keep every [NEEDS:...] and "
                        "[ARTIST-SUPPLIED: ...] marker verbatim, and never invent a fact. The "
                        "draft is for the artist's review — NEVER publish-ready."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": ["bio_short", "bio_medium", "bio_long", "press_release",
                             "one_sheet", "epk_outline", "caption_set"],
                },
                "inputs": {
                    "type": "object",
                    "description": ("Everything gathered from the artist, keyed by the fact "
                                    "field names above. Omit anything not gathered — unmapped "
                                    "extras ride along verbatim."),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_creative_director_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Cree tool call against the mock-first creative_director_service.

    Returns (result_for_model, action_summary, creative_studio_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - creative_studio_not_connected: True when a rollout was blocked on a
        missing/expired creative studio.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Cree can explain the outcome to the artist. Mirrors
    _execute_sync_agent_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_rollout_templates":
        release_type = (tool_input.get("release_type") or "").strip()
        goal         = (tool_input.get("goal") or "").strip()
        res = await creative_director_service.search_rollout_templates(
            release_type=release_type, goal=goal,
        )
        summary = {
            "input": f"release_type={release_type or 'any'} goal={goal or 'any'}",
            "result": f"{res['count']} template(s) found",
        }
        return res, summary, False

    if name == "assess_creative_concept":
        template_id       = (tool_input.get("template_id") or "").strip()
        release_title     = (tool_input.get("release_title") or "").strip()
        theme             = (tool_input.get("theme") or "").strip()
        weeks_to_release  = tool_input.get("weeks_to_release") or 0
        has_visual_assets = bool(tool_input.get("has_visual_assets") or False)
        res = await creative_director_service.assess_creative_concept(
            artist_id, template_id=template_id, release_title=release_title, theme=theme,
            weeks_to_release=weeks_to_release, has_visual_assets=has_visual_assets,
        )
        summary = {
            "input": f"release={release_title or 'unspecified'} template={template_id or 'unspecified'}",
            "result": f"ready={res['ready']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "schedule_rollout":
        template_id   = (tool_input.get("template_id") or "").strip()
        release_title = (tool_input.get("release_title") or "").strip()
        kickoff       = tool_input.get("kickoff") or ""
        if not release_title:
            return (
                {"error": "missing_release_title"},
                {"input": f"template={template_id or ''} release={release_title or ''}",
                 "result": "missing release title"},
                False,
            )
        try:
            rollout = await creative_director_service.schedule_rollout(
                artist_id, template_id, release_title, kickoff,
            )
            return (
                {"status": "scheduled", "reference": rollout.get("reference"),
                 "template_id": rollout.get("template_id"), "release_title": rollout.get("release_title")},
                {"input": f"template={template_id or 'unspecified'} release={release_title[:40]}",
                 "result": "rollout scheduled"},
                False,
            )
        except creative_director_service.CreativeStudioNotConnected:
            return (
                {
                    "creative_studio_not_connected": True,
                    "message": ("Artist has not connected a creative studio / content account. Tell "
                                "them to connect one before you can schedule rollouts."),
                },
                {"input": f"template={template_id}", "result": "creative_studio_not_connected"},
                True,
            )
        except creative_director_service.CreativeStudioAuthExpired:
            return (
                {
                    "creative_studio_not_connected": True,
                    "message": ("Creative-studio authorization expired. Tell the artist to "
                                "re-connect it before you can schedule rollouts."),
                },
                {"input": f"template={template_id}", "result": "creative_studio_auth_expired"},
                True,
            )

    if name == "lookup_copy_conventions":
        # Deliberately NOT gated on CREATIVE_DIRECTOR_STUDIO_CONNECTED — pure
        # corpus read, no studio account needed (mirrors Nadia's ungated lookup).
        dt = (tool_input.get("doc_type") or "").strip()
        res = await creative_director_service.lookup_copy_conventions(dt)
        if res.get("status") == "ok":
            result_str = (f"{len(res['conventions'])} convention(s), "
                          f"{len(res['honesty_rules'])} honesty rule(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={dt or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "build_copy_scaffold":
        # Deliberately NOT gated — data/scaffold tool, no studio account needed.
        dt = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await creative_director_service.build_copy_scaffold(
            doc_type=dt, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={dt or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Unit — Data-Oracle (data-oracle) tool_use: lookup_analytics_doctrine
#    + build_analytics_doc_scaffold (DOC-WRITER Option B) ───────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# DOC-WRITER Option B (analytics engine). These tools are passed to the
# Anthropic API for the data-oracle agent ONLY (see the gate in chat_stream).
# Every other agent takes its own unchanged path and never receives
# DATA_ORACLE_TOOLS. Handlers map straight onto the analytics
# data_oracle_service functions; they make ZERO network calls, read no
# secrets, and are UNGATED (both are pure corpus-read / data-scaffold tools).
# The old mock+gate dataset-search/metric-analysis/export-scheduling tools and
# their connected-warehouse gate are RETIRED — Data's real value is
# organizing an artist's own supplied numbers against Data's analytics
# doctrine (metrics readouts, stakeholder stat sheets), not a mock
# warehouse-export API.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
DATA_ORACLE_MAX_TOOL_ITERS = 5

DATA_ORACLE_TOOLS = [
    {
        "name": "lookup_analytics_doctrine",
        "description": (
            "Look up analytics doctrine from Data's corpus — filter by metric key, "
            "interpretation-band key, source key, diagnosis-pair key, quality-vs-vanity "
            "key, and/or stakeholder key. Call with no filters to browse the available keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.METRIC_DEFINITIONS),
                },
                "band_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.INTERPRETATION_BANDS),
                },
                "source_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.SOURCE_BREAKDOWN),
                },
                "diagnosis_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.DIAGNOSIS_PAIRS),
                },
                "quality_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.QUALITY_VS_VANITY),
                },
                "stakeholder_key": {
                    "type": "string",
                    "enum": list(data_oracle_service.analytics_data.STAKEHOLDER_FRAMING),
                },
            },
        },
    },
    {
        "name": "build_analytics_doc_scaffold",
        "description": (
            "Assemble COMPACT ingredients for an analytics document — a "
            "metrics_readout or a stakeholder_stat_sheet. Returns supplied-metric "
            "fields, applicable diagnosis-pair keys, interpretation bands, dig-in "
            "questions, and gap markers; you write the prose in your turn. Never "
            "computes a percentage, ratio, or score — arithmetic is always the "
            "agent's own turn, never this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": list(data_oracle_service.DOC_TYPES),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Artist-supplied fields: metric fields like stream, "
                        "monthly_listeners, saves, followers, save_rate, "
                        "streams_per_listener_ratio (metrics_readout); stakeholder "
                        "(one of venues_and_agents/labels_and_ar) plus that "
                        "stakeholder's want fields (stakeholder_stat_sheet). "
                        "Anything unsupplied becomes a gap marker."
                    ),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_data_oracle_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Data tool call against the analytics data_oracle_service.

    Returns (result_for_model, action_summary, data_warehouse_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - data_warehouse_not_connected: retained for the shared generator's
        3-tuple contract; ALWAYS False now — Data's tools are pure
        corpus-read / data-scaffold tools with no connected account and no
        gate (the old connected-warehouse gate and its export action are
        retired).
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Data can explain the outcome to the artist. Mirrors
    _execute_tour_commander_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_analytics_doctrine":
        metric_key      = (tool_input.get("metric_key") or "").strip()
        band_key        = (tool_input.get("band_key") or "").strip()
        source_key      = (tool_input.get("source_key") or "").strip()
        diagnosis_key   = (tool_input.get("diagnosis_key") or "").strip()
        quality_key     = (tool_input.get("quality_key") or "").strip()
        stakeholder_key = (tool_input.get("stakeholder_key") or "").strip()
        res = await data_oracle_service.lookup_analytics_doctrine(
            metric_key=metric_key, band_key=band_key, source_key=source_key,
            diagnosis_key=diagnosis_key, quality_key=quality_key,
            stakeholder_key=stakeholder_key,
        )
        filters = ",".join(f for f in (metric_key, band_key, source_key, diagnosis_key,
                                       quality_key, stakeholder_key) if f) or "none"
        if res.get("mode") == "index":
            result_str = "index browsed"
        else:
            hit = (len(res.get("metrics", [])) + len(res.get("bands", []))
                   + len(res.get("sources", [])) + len(res.get("diagnosis", []))
                   + len(res.get("quality", [])) + len(res.get("stakeholders", [])))
            miss = len(res.get("not_found", []))
            result_str = f"{hit} match(es), {miss} not found"
        summary = {"input": f"filters={filters}", "result": result_str}
        return res, summary, False

    if name == "build_analytics_doc_scaffold":
        doc_type   = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await data_oracle_service.build_analytics_doc_scaffold(
            doc_type=doc_type, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── AR-Scout (Scout) tool_use ──────────────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo / Nia / Max /
# Aria / Sync / Nova / Cree / Data pattern exactly: these tools are passed to the
# Anthropic API for the ar-scout agent ONLY (see the gate in chat_stream). Every
# other agent — including all of the above — takes its own unchanged path and never
# receives AR_SCOUT_TOOLS. Handlers map straight onto the mock-first ar_scout_service
# functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
AR_SCOUT_MAX_TOOL_ITERS = 5

AR_SCOUT_TOOLS = [
    {
        "name": "search_prospects",
        "description": "Search the A&R prospect directory for emerging/unsigned artists by genre, region, or development stage (unsigned, emerging, buzzing)",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "region": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["unsigned", "emerging", "buzzing"],
                },
            },
        },
    },
    {
        "name": "evaluate_demo",
        "description": "Run a structured demo/sound evaluation that scores a track across the five A&R pillars (hook, production, vocal, originality, market fit) and returns a develop / pass / sign-track recommendation",
        "input_schema": {
            "type": "object",
            "properties": {
                "track_title": {"type": "string"},
                "genre": {"type": "string"},
            },
        },
    },
    {
        "name": "log_scouting_note",
        "description": "Log a scouting note and rating on a prospect through the artist's connected A&R CRM on their behalf",
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string"},
                "note": {"type": "string"},
                "rating": {"type": "integer"},
            },
            "required": ["prospect_id"],
        },
    },
]


async def _execute_ar_scout_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Scout tool call against the mock-first ar_scout_service.

    Returns (result_for_model, action_summary, ar_scout_crm_not_connected).
      - result_for_model: dict fed back as the tool_result content (JSON-encoded).
      - action_summary:   {"input": str, "result": str} for the actions SSE event.
      - ar_scout_crm_not_connected: True when logging a note was blocked on a
        missing/expired A&R CRM.
    Never raises — every failure is converted into a structured tool_result so the
    loop can continue and Scout can explain the outcome to the artist. Mirrors
    _execute_venue_hawk_tool / _execute_data_oracle_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_prospects":
        genre  = (tool_input.get("genre") or "").strip()
        region = (tool_input.get("region") or "").strip()
        stage  = (tool_input.get("stage") or "").strip()
        res = await ar_scout_service.search_prospects(genre=genre, region=region, stage=stage)
        summary = {
            "input": f"genre={genre or 'any'} region={region or 'any'} stage={stage or 'any'}",
            "result": f"{res['count']} prospect(s) found",
        }
        return res, summary, False

    if name == "evaluate_demo":
        track_title = (tool_input.get("track_title") or "").strip()
        genre       = (tool_input.get("genre") or "").strip()
        res = await ar_scout_service.evaluate_demo(
            artist_id, track_title=track_title, genre=genre,
        )
        summary = {
            "input": f"track={track_title or 'unspecified'} genre={genre or 'any'}",
            "result": f"composite={res['composite']}, {res['recommendation']}",
        }
        return res, summary, False

    if name == "log_scouting_note":
        prospect_id = (tool_input.get("prospect_id") or "").strip()
        note        = tool_input.get("note") or ""
        rating      = tool_input.get("rating") or 0
        if not prospect_id:
            return (
                {"error": "missing_prospect_id"},
                {"input": f"prospect_id={prospect_id or ''}", "result": "missing prospect id"},
                False,
            )
        try:
            logged = await ar_scout_service.log_scouting_note(
                artist_id, prospect_id, note, rating,
            )
            if logged.get("status") == "unknown_prospect":
                return (
                    {"error": "unknown_prospect", "prospect_id": logged.get("prospect_id")},
                    {"input": f"prospect_id={prospect_id}", "result": "unknown prospect"},
                    False,
                )
            return (
                {"status": "logged", "reference": logged.get("reference"),
                 "prospect_id": logged.get("prospect_id"), "prospect_name": logged.get("prospect_name"),
                 "rating": logged.get("rating")},
                {"input": f"prospect_id={prospect_id} rating={logged.get('rating')}",
                 "result": "scouting note logged"},
                False,
            )
        except ar_scout_service.ArScoutCRMNotConnected:
            return (
                {
                    "ar_scout_crm_not_connected": True,
                    "message": ("Artist has not connected an A&R CRM / scouting account. Tell them "
                                "to connect one before you can log scouting notes."),
                },
                {"input": f"prospect_id={prospect_id}", "result": "ar_scout_crm_not_connected"},
                True,
            )
        except ar_scout_service.ArScoutCRMAuthExpired:
            return (
                {
                    "ar_scout_crm_not_connected": True,
                    "message": ("A&R CRM authorization expired. Tell the artist to re-connect it "
                                "before you can log scouting notes."),
                },
                {"input": f"prospect_id={prospect_id}", "result": "ar_scout_crm_auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── Producer-Connect (Beat) tool_use ───────────────────────────────────────────
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) / Jade / Ray / Cleo / Finn /
# Victor / Nadia / Zara / Kai / Luna / Diego / Ray B / Miles / Solo / Nia / Max /
# Aria / Sync / Nova / Cree / Data / Scout pattern exactly: these tools are passed to
# the Anthropic API for the producer-connect agent ONLY (see the gate in chat_stream).
# Every other agent — including all of the above — takes its own unchanged path and
# never receives PRODUCER_CONNECT_TOOLS. Handlers map straight onto the mock-first
# producer_connect_service functions; they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
PRODUCER_CONNECT_MAX_TOOL_ITERS = 5

PRODUCER_CONNECT_TOOLS = [
    {
        "name": "search_producers",
        "description": "Search the producer directory for producers/beatmakers by genre, region, or budget tier (budget, mid, premium)",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "region": {"type": "string"},
                "tier": {
                    "type": "string",
                    "enum": ["budget", "mid", "premium"],
                },
            },
        },
    },
    {
        "name": "evaluate_beat_deal",
        "description": "Run a structured evaluation of a beat-licensing offer that scores it across the five deal pillars (value, rights, flexibility, production fit, market) and returns an accept / negotiate / pass recommendation",
        "input_schema": {
            "type": "object",
            "properties": {
                "beat_title": {"type": "string"},
                "license_type": {
                    "type": "string",
                    "enum": ["lease", "exclusive"],
                },
                "price_usd": {"type": "integer"},
            },
        },
    },
]


async def _execute_producer_connect_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Beat tool call against the mock-first producer_connect_service.

    Returns (result_for_model, action_summary, not_connected). Beat is
    consult-only (search + evaluate); not_connected is structurally always
    False. Never raises — every failure is converted into a structured
    tool_result so the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_producers":
        genre  = (tool_input.get("genre") or "").strip()
        region = (tool_input.get("region") or "").strip()
        tier   = (tool_input.get("tier") or "").strip()
        res = await producer_connect_service.search_producers(genre=genre, region=region, tier=tier)
        summary = {
            "input": f"genre={genre or 'any'} region={region or 'any'} tier={tier or 'any'}",
            "result": f"{res['count']} producer(s) found",
        }
        return res, summary, False

    if name == "evaluate_beat_deal":
        beat_title   = (tool_input.get("beat_title") or "").strip()
        license_type = (tool_input.get("license_type") or "").strip()
        price_usd    = tool_input.get("price_usd") or 0
        res = await producer_connect_service.evaluate_beat_deal(
            artist_id, beat_title=beat_title, license_type=license_type, price_usd=price_usd,
        )
        summary = {
            "input": f"beat={beat_title or 'unspecified'} license={license_type or 'any'}",
            "result": f"composite={res['composite']}, {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Neo (ai-navigator) tool_use: search_ai_tools + assess_tech_stack
# (provision_automation was a mock terminal-action tool gated behind AI_NAVIGATOR_CONNECTED
#  — retired; Neo is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Marcus (Unit 1.7) / Lex (Unit 1.8) pattern exactly: these tools are
# passed to the Anthropic API for the ai-navigator agent ONLY (see the gate in
# chat_stream). Every other agent — including Marcus and Lex — takes its own
# unchanged path and never receives AI_NAVIGATOR_TOOLS. Handlers map straight onto
# the mock-first ai_navigator_service functions; they make ZERO network calls and
# read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
AI_NAVIGATOR_MAX_TOOL_ITERS = 5

AI_NAVIGATOR_TOOLS = [
    {
        "name": "search_ai_tools",
        "description": "Search the AI-tool catalog by category or use case",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["audio", "visual", "content", "workflow"],
                },
                "use_case": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_tech_stack",
        "description": "Assess the artist's current tooling for gaps against common capabilities",
        "input_schema": {
            "type": "object",
            "properties": {
                "current_tools": {"type": "string"},
                "goal": {"type": "string"},
            },
            "required": ["current_tools"],
        },
    },
]


async def _execute_ai_navigator_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one AI-Navigator tool call against the mock-first ai_navigator_service.

    Returns (result_for_model, action_summary, not_connected). Neo is
    consult-only (search + assess); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_ai_tools":
        category = (tool_input.get("category") or "").strip()
        use_case = (tool_input.get("use_case") or "").strip()
        res = await ai_navigator_service.search_ai_tools(category=category, use_case=use_case)
        summary = {
            "input": f"category={category or 'any'} use_case={use_case or 'any'}",
            "result": f"{res['count']} tool(s) found",
        }
        return res, summary, False

    if name == "assess_tech_stack":
        current_tools = tool_input.get("current_tools") or ""
        goal = (tool_input.get("goal") or "").strip()
        res = await ai_navigator_service.assess_tech_stack(
            artist_id, current_tools=current_tools, goal=goal,
        )
        summary = {
            "input": f"goal={goal or 'unspecified'} chars={len(current_tools)}",
            "result": f"{res['gap_count']} gap(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Maya (artist-wellness) tool_use: search_wellness_resources + assess_burnout_risk
# (schedule_wellness_checkin was a mock terminal-action tool gated behind ARTIST_WELLNESS_CONNECTED
#  — retired; Maya is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the artist-wellness agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# ARTIST_WELLNESS_TOOLS. Handlers map straight onto the mock-first artist_wellness_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
ARTIST_WELLNESS_MAX_TOOL_ITERS = 5

ARTIST_WELLNESS_TOOLS = [
    {
        "name": "search_wellness_resources",
        "description": "Search the wellness resource library by category or format",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ['mindfulness', 'physical', 'recovery', 'boundaries']},
                "format": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_burnout_risk",
        "description": "Screen self-reported signals for burnout risk indicators",
        "input_schema": {
            "type": "object",
            "properties": {
                "signals": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["signals"],
        },
    },
]


async def _execute_artist_wellness_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Maya tool call against the mock-first artist_wellness_service.

    Returns (result_for_model, action_summary, not_connected). Maya is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_wellness_resources":
        a = (tool_input.get("category") or "").strip()
        b = (tool_input.get("format") or "").strip()
        res = await artist_wellness_service.search_wellness_resources(category=a, format=b)
        summary = {
            "input": f"category={a or 'any'} format={b or 'any'}",
            "result": f"{res['count']} resource(s) found",
        }
        return res, summary, False

    if name == "assess_burnout_risk":
        txt = tool_input.get("signals") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await artist_wellness_service.assess_burnout_risk(artist_id, signals=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} risk(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Audio (audio-quality) tool_use: search_quality_standards + analyze_mix
# (submit_master_qc was a mock terminal-action tool gated behind AUDIO_QUALITY_CONNECTED
#  — retired; Audio is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the audio-quality agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# AUDIO_QUALITY_TOOLS. Handlers map straight onto the mock-first audio_quality_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
AUDIO_QUALITY_MAX_TOOL_ITERS = 5

AUDIO_QUALITY_TOOLS = [
    {
        "name": "search_quality_standards",
        "description": "Search platform loudness/quality standards by platform or stage",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ['spotify', 'apple_music', 'youtube', 'soundcloud']},
                "stage": {"type": "string"},
            },
        },
    },
    {
        "name": "analyze_mix",
        "description": "Screen mix notes for common quality-control red flags",
        "input_schema": {
            "type": "object",
            "properties": {
                "mix_notes": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["mix_notes"],
        },
    },
]


async def _execute_audio_quality_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Audio tool call against the mock-first audio_quality_service.

    Returns (result_for_model, action_summary, not_connected). Audio is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_quality_standards":
        a = (tool_input.get("platform") or "").strip()
        b = (tool_input.get("stage") or "").strip()
        res = await audio_quality_service.search_quality_standards(platform=a, stage=b)
        summary = {
            "input": f"platform={a or 'any'} stage={b or 'any'}",
            "result": f"{res['count']} standard(s) found",
        }
        return res, summary, False

    if name == "analyze_mix":
        txt = tool_input.get("mix_notes") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await audio_quality_service.analyze_mix(artist_id, mix_notes=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Collab (collab-connect) tool_use: search_collaborators + assess_collab_fit
# (send_collab_invite was a mock terminal-action tool gated behind COLLAB_CONNECT_CONNECTED
#  — retired; Collab is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the collab-connect agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# COLLAB_CONNECT_TOOLS. Handlers map straight onto the mock-first collab_connect_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
COLLAB_CONNECT_MAX_TOOL_ITERS = 5

COLLAB_CONNECT_TOOLS = [
    {
        "name": "search_collaborators",
        "description": "Search the collaborator network by genre or role",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string", "enum": ['pop', 'hip_hop', 'electronic', 'rnb', 'indie']},
                "role": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_collab_fit",
        "description": "Screen a collaborator profile for fit and risk signals",
        "input_schema": {
            "type": "object",
            "properties": {
                "collaborator_profile": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["collaborator_profile"],
        },
    },
]


async def _execute_collab_connect_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Collab tool call against the mock-first collab_connect_service.

    Returns (result_for_model, action_summary, not_connected). Collab is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_collaborators":
        a = (tool_input.get("genre") or "").strip()
        b = (tool_input.get("role") or "").strip()
        res = await collab_connect_service.search_collaborators(genre=a, role=b)
        summary = {
            "input": f"genre={a or 'any'} role={b or 'any'}",
            "result": f"{res['count']} collaborator(s) found",
        }
        return res, summary, False

    if name == "assess_collab_fit":
        txt = tool_input.get("collaborator_profile") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await collab_connect_service.assess_collab_fit(artist_id, collaborator_profile=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} concern(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pen (content-forge) tool_use: search_content_templates + review_copy + publish_content_draft
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the content-forge agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# CONTENT_FORGE_TOOLS. Handlers map straight onto the mock-first content_forge_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
CONTENT_FORGE_MAX_TOOL_ITERS = 5

CONTENT_FORGE_TOOLS = [
    {
        "name": "search_content_templates",
        "description": "Search the content template library by platform or content type",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ['instagram', 'tiktok', 'twitter', 'newsletter']},
                "content_type": {"type": "string"},
            },
        },
    },
    {
        "name": "review_copy",
        "description": "Screen a draft for copy red flags",
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["draft_text"],
        },
    },
    {
        "name": "publish_content_draft",
        "description": "Publish a content draft to the connected CMS",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "channel": {"type": "string", "enum": ['blog', 'newsletter', 'landing']},
            },
            "required": ["title"],
        },
    },
]


async def _execute_content_forge_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Pen tool call against the mock-first content_forge_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_content_templates":
        a = (tool_input.get("platform") or "").strip()
        b = (tool_input.get("content_type") or "").strip()
        res = await content_forge_service.search_content_templates(platform=a, content_type=b)
        summary = {
            "input": f"platform={a or 'any'} content_type={b or 'any'}",
            "result": f"{res['count']} template(s) found",
        }
        return res, summary, False

    if name == "review_copy":
        txt = tool_input.get("draft_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await content_forge_service.review_copy(artist_id, draft_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    if name == "publish_content_draft":
        nm  = (tool_input.get("title") or "").strip()
        opt = (tool_input.get("channel") or "blog").strip()
        if not nm:
            return (
                {"error": "missing_title"},
                {"input": "title=", "result": "missing title"},
                False,
            )
        try:
            done = await content_forge_service.publish_content_draft(artist_id, nm, opt)
            return (
                {"status": "done", "reference": done.get("reference"), "title": nm},
                {"input": f"title={nm} channel={opt}", "result": "draft published"},
                False,
            )
        except content_forge_service.CmsNotConnected:
            return (
                {
                    "not_connected": True,
                    "message": ("Artist has not connected a content management account. Tell them to connect one "
                                "before you can complete this action."),
                },
                {"input": f"title={nm}", "result": "not_connected"},
                True,
            )
        except content_forge_service.CmsAuthExpired:
            return (
                {
                    "not_connected": True,
                    "message": ("Content management account authorization expired. Tell the artist to re-connect "
                                "before you can complete this action."),
                },
                {"input": f"title={nm}", "result": "auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Reed (ink-and-air) tool_use: search_publishing_deals + review_split_sheet + register_composition
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the ink-and-air agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# INK_AND_AIR_TOOLS. Handlers map straight onto the mock-first ink_and_air_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
INK_AND_AIR_MAX_TOOL_ITERS = 5

INK_AND_AIR_TOOLS = [
    {
        "name": "search_publishing_deals",
        "description": ("Look up publishing deal-TYPE doctrine: how each structure works (admin, "
                        "co-publishing, exclusive/full publishing, work for hire) — ownership, "
                        "writer income flow, typical (ALWAYS negotiable) fee/term shapes — plus "
                        "the trap terms to surface (recoupment, cross-collateralization, "
                        "retention period, pipeline songs, at-source collection, and any "
                        "language touching the writer's share — a red flag). Explain structures "
                        "and flag traps ONLY: NEVER evaluate a specific offer as good or bad, "
                        "never quote a number as a market rate for a specific deal — the only "
                        "numeric shape in the doctrine is the admin-fee typical range, and "
                        "every deal is negotiable. A real agreement routes to Lex framed as "
                        "draft-for-review."),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_type": {"type": "string", "enum": ['admin', 'co_publishing', 'full_publishing', 'work_for_hire']},
            },
        },
    },
    {
        "name": "review_split_sheet",
        "description": "Screen a split sheet for red flags",
        "input_schema": {
            "type": "object",
            "properties": {
                "split_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["split_text"],
        },
    },
    {
        "name": "register_composition",
        "description": "Register a composition with the connected publishing admin",
        "input_schema": {
            "type": "object",
            "properties": {
                "work_title": {"type": "string"},
                "work_type": {"type": "string", "enum": ['song', 'instrumental', 'arrangement']},
            },
            "required": ["work_title"],
        },
    },
    {
        "name": "lookup_publishing_societies",
        "description": ("Look up where a songwriter registers in one country: the performance (PRO) "
                        "and mechanical societies, whether one unified CMO covers both streams, and "
                        "the home-society-once/CISAC doctrine (a writer joins their HOME society once "
                        "and collects worldwide — never registers with multiple PROs). Covered: CA, "
                        "US, UK, AU, NZ, DE, FR, SE, DK, NO, FI. Any other country returns "
                        "country_not_in_corpus — relay that honestly and NEVER guess a society."),
        "input_schema": {
            "type": "object",
            "properties": {
                "country_code": {"type": "string",
                                 "description": "Country code, e.g. 'CA', 'US', 'UK' — the writer's HOME country"},
            },
            "required": ["country_code"],
        },
    },
    {
        "name": "validate_split_sheet",
        "description": ("Validate a STRUCTURED split sheet. Pass the song fields and one record per "
                        "contributor using the canonical field names. Include ONLY values the artist "
                        "actually supplied — OMIT anything unknown so it comes back as an explicit "
                        "[NEEDS: ...] gap instead of an invented fact (never invent an IPI or a %). "
                        "Percentage sums are checked ONLY over supplied values; a side with a missing "
                        "share reports sum_not_checkable and the remainder is NOT inferred. Free-text "
                        "notes pass through unparsed. Keep every [NEEDS: ...] marker verbatim in your "
                        "reply. The canonical spec requires per contributor: legal_name, contact, "
                        "role, lyrics_percent, music_percent, pro_affiliation, writer_ipi, "
                        "publisher_name ('SELF' if self-published), publisher_ipi, signature; and per "
                        "song: song_title, date, samples_used (+ sample_sources when samples_used is "
                        "yes)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "song": {
                    "type": "object",
                    "description": "Song-level fields the artist supplied",
                    "properties": {
                        "song_title":       {"type": "string"},
                        "alternate_titles": {"type": "string"},
                        "date":             {"type": "string"},
                        "samples_used":     {"type": "string", "enum": ["yes", "no"]},
                        "sample_sources":   {"type": "string"},
                    },
                },
                "contributors": {
                    "type": "array",
                    "description": ("One record per contributor. Omit unknown fields — they surface "
                                    "as [NEEDS: ...] gaps. publisher_share_percent is needed for the "
                                    "publisher-side 100% sum check. Extra keys (e.g. residency, deal "
                                    "notes) pass through as unparsed notes."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "legal_name":              {"type": "string"},
                            "contact":                 {"type": "string"},
                            "role":                    {"type": "string"},
                            "lyrics_percent":          {"type": "number"},
                            "music_percent":           {"type": "number"},
                            "pro_affiliation":         {"type": "string"},
                            "writer_ipi":              {"type": "string"},
                            "publisher_name":          {"type": "string"},
                            "publisher_ipi":           {"type": "string"},
                            "publisher_share_percent": {"type": "number"},
                            "signature":               {"type": "string"},
                        },
                    },
                },
            },
            "required": ["contributors"],
        },
    },
    {
        "name": "build_publishing_doc_scaffold",
        "description": ("Build a compact document scaffold for one of two publishing documents, "
                        "AFTER you have interviewed the artist and gathered what the document "
                        "actually requires. doc_type 'split_sheet': song fields + one record per "
                        "contributor (same canonical fields as validate_split_sheet) — the scaffold "
                        "returns ordered sections, both 100%-sum checks over supplied values, the "
                        "best-practice master-side block, amendment rule, and signature lines. "
                        "doc_type 'sync_pack': the sync metadata fields plus one_stop_confirmations "
                        "— one-stop is asserted ONLY when master control, 100% publishing control, "
                        "and no-uncleared-samples are EACH explicitly confirmed by the artist; "
                        "anything unconfirmed returns cannot_assert_one_stop with the missing "
                        "conditions named. Include ONLY what the artist actually told you — omit "
                        "unknowns so they come back as [NEEDS: ...] gaps instead of invented facts. "
                        "Then WRITE the document draft yourself in your reply from the scaffold: "
                        "follow each section's guidance, keep every [NEEDS: ...] and "
                        "[ARTIST-SUPPLIED: ...] marker verbatim, and never invent a fact, IPI, or %. "
                        "The draft is a review starting point for the artist and their manager — "
                        "NOT submit-ready, NOT a legal document."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["split_sheet", "sync_pack"]},
                "inputs": {
                    "type": "object",
                    "description": ("Everything gathered from the artist. For split_sheet: 'song' "
                                    "(object) and 'contributors' (array of records with the "
                                    "canonical field names), plus optional master-side fields "
                                    "(recording_info, isrc, master_ownership_percent). For "
                                    "sync_pack: the metadata fields (genre_specific, moods, "
                                    "tempo_bpm, instrumentation, vocals, similar_artists, "
                                    "suggested_placements, rights_breakdown, "
                                    "clearance_contact_composition, clearance_contact_master, "
                                    "stems_available, instrumental_available, "
                                    "clean_version_available, samples_cleared_declaration, isrc, "
                                    "iswc, pro_affiliation, ipi) plus 'one_stop_confirmations' "
                                    "(object with booleans master_control_confirmed, "
                                    "publishing_control_100_confirmed, no_uncleared_samples — set "
                                    "true ONLY on the artist's explicit confirmation). Omit "
                                    "anything not gathered."),
                },
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_ink_and_air_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Reed tool call against the mock-first ink_and_air_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_publishing_deals":
        a = (tool_input.get("deal_type") or "").strip()
        b = (tool_input.get("territory") or "").strip()
        res = await ink_and_air_service.search_publishing_deals(deal_type=a, territory=b)
        summary = {
            "input": f"deal_type={a or 'any'} territory={b or 'any'}",
            "result": f"{res['count']} deal(s) found",
        }
        return res, summary, False

    if name == "review_split_sheet":
        txt = tool_input.get("split_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await ink_and_air_service.review_split_sheet(artist_id, split_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    if name == "register_composition":
        nm  = (tool_input.get("work_title") or "").strip()
        opt = (tool_input.get("work_type") or "song").strip()
        if not nm:
            return (
                {"error": "missing_work_title"},
                {"input": "work_title=", "result": "missing work_title"},
                False,
            )
        try:
            done = await ink_and_air_service.register_composition(artist_id, nm, opt)
            return (
                {"status": "done", "reference": done.get("reference"), "work_title": nm},
                {"input": f"work_title={nm} work_type={opt}", "result": "composition registered"},
                False,
            )
        except ink_and_air_service.PublishingAdminNotConnected:
            return (
                {
                    "not_connected": True,
                    "message": ("Artist has not connected a publishing administration account. Tell them to connect one "
                                "before you can complete this action."),
                },
                {"input": f"work_title={nm}", "result": "not_connected"},
                True,
            )
        except ink_and_air_service.PublishingAdminAuthExpired:
            return (
                {
                    "not_connected": True,
                    "message": ("Publishing administration account authorization expired. Tell the artist to re-connect "
                                "before you can complete this action."),
                },
                {"input": f"work_title={nm}", "result": "auth_expired"},
                True,
            )

    if name == "lookup_publishing_societies":
        # Deliberately NOT gated on INK_AND_AIR_CONNECTED — pure corpus read,
        # no account needed (mirrors Jade's ungated lookup/scaffold tools).
        code = (tool_input.get("country_code") or "").strip()
        res = await ink_and_air_service.lookup_publishing_societies(code)
        if res.get("status") == "ok":
            result_str = (f"{len(res['performance'])} performance + "
                          f"{len(res['mechanical'])} mechanical societies")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"country_code={code or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "validate_split_sheet":
        # Deliberately NOT gated — pure validation, no account needed.
        song = tool_input.get("song")
        contributors = tool_input.get("contributors")
        song = song if isinstance(song, dict) else {}
        contributors = contributors if isinstance(contributors, list) else []
        res = await ink_and_air_service.validate_split_sheet(
            artist_id, song=song, contributors=contributors,
        )
        summary = {
            "input": f"contributors={len(contributors)}",
            "result": (f"valid_structure={res['valid_structure']}, "
                       f"{len(res['needs'])} gap(s)"),
        }
        return res, summary, False

    if name == "build_publishing_doc_scaffold":
        # Deliberately NOT gated — data/scaffold tool, no account needed.
        dt = (tool_input.get("doc_type") or "").strip()
        raw_inputs = tool_input.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        res = await ink_and_air_service.build_publishing_doc_scaffold(
            artist_id, doc_type=dt, inputs=raw_inputs,
        )
        if res.get("status") == "scaffold_ready":
            result_str = (f"scaffold_ready, {len(res['sections'])} section(s), "
                          f"{len(res['missing'])} gap(s)")
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={dt or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tommy (label-services) tool_use: search_distribution_requirements + validate_release_metadata + deliver_to_dsps
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the label-services agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# LABEL_SERVICES_TOOLS. Handlers map straight onto the mock-first label_services_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
LABEL_SERVICES_MAX_TOOL_ITERS = 5

LABEL_SERVICES_TOOLS = [
    {
        "name": "search_distribution_requirements",
        "description": "Search distribution/store delivery requirements by store or asset type",
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ['spotify', 'apple_music', 'beatport', 'bandcamp']},
                "asset_type": {"type": "string"},
            },
        },
    },
    {
        "name": "validate_release_metadata",
        "description": "Screen release metadata for delivery-blocking issues",
        "input_schema": {
            "type": "object",
            "properties": {
                "metadata_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["metadata_text"],
        },
    },
    {
        "name": "deliver_to_dsps",
        "description": "Queue a release for delivery to DSPs via the connected distributor",
        "input_schema": {
            "type": "object",
            "properties": {
                "release_title": {"type": "string"},
                "store": {"type": "string", "enum": ['all', 'spotify', 'apple_music']},
            },
            "required": ["release_title"],
        },
    },
    {
        "name": "lookup_release_requirements",
        "description": ("Look up the delivery conventions for ONE release topic before "
                        "acting: identifiers (ISRC/UPC/ISWC new-vs-same rules + the "
                        "duplicate doctrine), metadata (ordered release- and track-level "
                        "fields), artwork (the current artwork spec), timeline (the "
                        "work-backwards lead times + editorial windows), release_record "
                        "(the permanent per-release record), or distributor_switch (the "
                        "ordered switch mechanism). Every spec is a CURRENT CONVENTION — "
                        "tell the artist to verify it with their distributor/platform "
                        "live. THE HARD RULE OF THIS DOMAIN: no identifier (ISRC/UPC/"
                        "ISWC), release date, or credit is EVER invented — each is the "
                        "artist's supplied input verbatim, an explicit [NEEDS:<fact>] "
                        "gap, or an [ARTIST-SUPPLIED:<confirm>] reminder; and a supplied "
                        "artist name is passed through byte-exact, never re-cased."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["identifiers", "metadata", "artwork", "timeline",
                             "release_record", "distributor_switch"],
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "build_release_checklist",
        "description": ("Build an ordered, work-backwards release checklist from the "
                        "timeline doctrine, AFTER you have gathered the release type, "
                        "how many WEEKS until the release date, and whether this is the "
                        "artist's FIRST release. Include ONLY what the artist actually "
                        "told you — each unsupplied axis (release_type / "
                        "weeks_to_release / first_release) comes back as an explicit "
                        "[NEEDS:<axis>] gap, NEVER defaulted. When weeks_to_release is "
                        "inside the four-week upload lead, the checklist attaches an "
                        "honest timeline_already_inside_lead warning and NEVER silently "
                        "re-plans or invents a compressed day-by-day schedule — surface "
                        "the risk and let the artist decide. Cross-references (the "
                        "ink-and-air split sheet + sync pack) are named, not resolved "
                        "here."),
        "input_schema": {
            "type": "object",
            "properties": {
                "release_type": {"type": "string", "enum": ["single", "ep", "album"]},
                "weeks_to_release": {"type": "integer"},
                "first_release": {"type": "boolean"},
            },
        },
    },
    {
        "name": "build_release_doc_scaffold",
        "description": ("Assemble a release document SCAFFOLD once the artist wants a "
                        "metadata sheet or a permanent release record written up: "
                        "doc_type='metadata_sheet' lays out the ordered release-level "
                        "fields plus a per-track section for every track (or every empty "
                        "slot when only a track_count is known); doc_type='release_record' "
                        "lays out the permanent per-release record. Pass everything the "
                        "artist has actually supplied in 'inputs'. THE HARD RULE: no "
                        "identifier (ISRC/UPC), release date, or credit is EVER generated "
                        "— each field comes back as the supplied value VERBATIM (a supplied "
                        "artist name passes through byte-exact, never re-cased), an explicit "
                        "[NEEDS:<fact>] gap (an absent per-track ISRC is a "
                        "[NEEDS:isrc_track_N] slot, never a minted code), or an "
                        "[ARTIST-SUPPLIED:<field>] confirm for an optional field. The "
                        "result is a scaffold to review and complete — NOT submit-ready."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string",
                             "enum": ["metadata_sheet", "release_record"]},
                "inputs": {"type": "object",
                           "description": ("Whatever the artist has supplied — release-level "
                                           "fields, a 'tracks' list (or a 'track_count'), and "
                                           "any record fields. Missing items become gaps, "
                                           "never invented.")},
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_label_services_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Tommy tool call against the mock-first label_services_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_distribution_requirements":
        a = (tool_input.get("store") or "").strip()
        b = (tool_input.get("asset_type") or "").strip()
        res = await label_services_service.search_distribution_requirements(store=a, asset_type=b)
        summary = {
            "input": f"store={a or 'any'} asset_type={b or 'any'}",
            "result": f"{res['count']} requirement(s) found",
        }
        return res, summary, False

    if name == "validate_release_metadata":
        txt = tool_input.get("metadata_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await label_services_service.validate_release_metadata(artist_id, metadata_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    if name == "deliver_to_dsps":
        nm  = (tool_input.get("release_title") or "").strip()
        opt = (tool_input.get("store") or "all").strip()
        if not nm:
            return (
                {"error": "missing_release_title"},
                {"input": "release_title=", "result": "missing release_title"},
                False,
            )
        try:
            done = await label_services_service.deliver_to_dsps(artist_id, nm, opt)
            return (
                {"status": "done", "reference": done.get("reference"), "release_title": nm},
                {"input": f"release_title={nm} store={opt}", "result": "delivery queued"},
                False,
            )
        except label_services_service.DistributorNotConnected:
            return (
                {
                    "not_connected": True,
                    "message": ("Artist has not connected a distributor account. Tell them to connect one "
                                "before you can complete this action."),
                },
                {"input": f"release_title={nm}", "result": "not_connected"},
                True,
            )
        except label_services_service.DistributorAuthExpired:
            return (
                {
                    "not_connected": True,
                    "message": ("Distributor account authorization expired. Tell the artist to re-connect "
                                "before you can complete this action."),
                },
                {"input": f"release_title={nm}", "result": "auth_expired"},
                True,
            )

    if name == "lookup_release_requirements":
        # Deliberately NOT gated on LABEL_SERVICES_CONNECTED — pure corpus read,
        # no distributor account needed (mirrors Cree's ungated lookup).
        topic = (tool_input.get("topic") or "").strip()
        res = await label_services_service.lookup_release_requirements(topic)
        if res.get("status") == "ok":
            result_str = f"{len(res['honesty_rules'])} honesty rule(s)"
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"topic={topic or '(missing)'}", "result": result_str}
        return res, summary, False

    if name == "build_release_checklist":
        # Deliberately NOT gated — deterministic checklist over the corpus.
        res = await label_services_service.build_release_checklist(
            release_type=tool_input.get("release_type"),
            weeks_to_release=tool_input.get("weeks_to_release"),
            first_release=tool_input.get("first_release"),
        )
        rt = res["release_type"]
        wk = res["weeks_to_release"]
        summary = {
            "input": f"type={rt} weeks={wk}",
            "result": (f"{len(res['checklist'])} item(s), {len(res['missing'])} gap(s), "
                       f"{len(res['warnings'])} warning(s)"),
        }
        return res, summary, False

    if name == "build_release_doc_scaffold":
        # Deliberately NOT gated — data-only scaffold over the corpus (option B).
        doc_type = (tool_input.get("doc_type") or "").strip()
        res = await label_services_service.build_release_doc_scaffold(
            doc_type=doc_type,
            inputs=tool_input.get("inputs"),
        )
        if res.get("status") == "scaffold_ready":
            result_str = f"scaffold_ready ({len(res['missing'])} gap(s))"
        else:
            result_str = res.get("status", "error")
        summary = {"input": f"doc_type={doc_type or '(missing)'}", "result": result_str}
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Coach (live-coach) tool_use: search_coaching_drills + assess_stage_presence
# (schedule_coaching_session was a mock terminal-action tool gated behind LIVE_COACH_CONNECTED
#  — retired; Coach is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the live-coach agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# LIVE_COACH_TOOLS. Handlers map straight onto the mock-first live_coach_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
LIVE_COACH_MAX_TOOL_ITERS = 5

LIVE_COACH_TOOLS = [
    {
        "name": "search_coaching_drills",
        "description": "Search the performance-coaching drill library by focus or level",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string", "enum": ['vocals', 'stage_presence', 'breath', 'endurance']},
                "level": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_stage_presence",
        "description": "Screen performance notes for stage-presence issues",
        "input_schema": {
            "type": "object",
            "properties": {
                "performance_notes": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["performance_notes"],
        },
    },
]


async def _execute_live_coach_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Coach tool call against the mock-first live_coach_service.

    Returns (result_for_model, action_summary, not_connected). Coach is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_coaching_drills":
        a = (tool_input.get("focus") or "").strip()
        b = (tool_input.get("level") or "").strip()
        res = await live_coach_service.search_coaching_drills(focus=a, level=b)
        summary = {
            "input": f"focus={a or 'any'} level={b or 'any'}",
            "result": f"{res['count']} drill(s) found",
        }
        return res, summary, False

    if name == "assess_stage_presence":
        txt = tool_input.get("performance_notes") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await live_coach_service.assess_stage_presence(artist_id, performance_notes=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Knox (live-wire) tool_use: assess_show_offer
# (search_venues + submit_booking_hold were DUPLICATES of Ray B's booking tools and
#  were removed this build — Ray B / venue-hawk OWNS booking; see the step-0 report.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the live-wire agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# LIVE_WIRE_TOOLS. Handlers map straight onto the mock-first live_wire_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
LIVE_WIRE_MAX_TOOL_ITERS = 5

LIVE_WIRE_TOOLS = [
    {
        "name": "assess_show_offer",
        "description": "Screen a show offer for unfavourable terms",
        "input_schema": {
            "type": "object",
            "properties": {
                "offer_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["offer_text"],
        },
    },
]


async def _execute_live_wire_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Knox tool call against the mock-first live_wire_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "assess_show_offer":
        txt = tool_input.get("offer_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await live_wire_service.assess_show_offer(artist_id, offer_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Mo (mobile-monetize) tool_use: lookup_monetization_doctrine + build_monetization_doc_scaffold
# ═══════════════════════════════════════════════════════════════════════════════
# DOC-WRITER Option B (Miles / Data / Lex precedent): these tools are passed to
# the Anthropic API for the mobile-monetize agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never
# receives MOBILE_MONETIZE_TOOLS. Handlers map straight onto the pure,
# UNGATED mobile_monetize_service functions; they make ZERO network calls,
# ZERO model calls, and read no secrets. The old mock+gate surface
# (search_monetization_programs / analyze_monetization / enable_monetization,
# the platform-monetization-account connection gate, and its
# not-connected/auth-expired exceptions) is RETIRED — flipping a live
# monetization switch is not Mo's domain; organizing the artist's own stated
# revenue picture against Mo's doctrine is.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
MOBILE_MONETIZE_MAX_TOOL_ITERS = 5

MOBILE_MONETIZE_TOOLS = [
    {
        "name": "lookup_monetization_doctrine",
        "description": ("Look up Mo's revenue-diversification doctrine. With no filter, "
                        "returns an index of the available keys per block so you can browse. "
                        "With a filter, returns the matched full record(s). Mo's standing "
                        "doctrine, integrity rules, and boundaries always ride along. THE HARD "
                        "RULE OF THIS DOMAIN: no income projection or dollar figure of any kind "
                        "is ever invented — every 'how much' question gets mechanism + "
                        "'it varies', never a number."),
        "input_schema": {
            "type": "object",
            "properties": {
                "stream_key": {"type": "string", "enum": list(mobile_monetize_service.monetization_data.REVENUE_STREAM_TAXONOMY)},
                "diversification_key": {"type": "string", "enum": list(mobile_monetize_service.monetization_data.DIVERSIFICATION)},
                "sequencing_key": {"type": "string", "enum": list(mobile_monetize_service.monetization_data.SEQUENCING)},
                "admin_key": {"type": "string", "enum": list(mobile_monetize_service.monetization_data.ADMIN)},
            },
        },
    },
    {
        "name": "build_monetization_doc_scaffold",
        "description": ("Build compact ingredients for ONE monetization document; YOU write "
                        "the prose in your turn. doc_type is 'revenue_map' (organizes the "
                        "artist's current streams against the full taxonomy, plus addressable "
                        "next streams) or 'diversification_plan' (surfaces 2-3 next-stream "
                        "candidates with their prerequisites and owning department). Pass "
                        "inputs as a free-form object: 'active_streams' (list of the artist's "
                        "current revenue-stream taxonomy keys, verbatim) and 'audience_stage' "
                        "('pre_audience' or 'has_audience', artist-supplied — never guessed). "
                        "NEVER a computed income figure of any kind, ever."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": list(mobile_monetize_service.DOC_TYPES)},
                "inputs": {"type": "object"},
            },
            "required": ["doc_type"],
        },
    },
]


async def _execute_mobile_monetize_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Mo tool call against the pure, ungated mobile_monetize_service.

    Returns (result_for_model, action_summary, not_connected). ``not_connected``
    is ALWAYS False now — the old platform-monetization-account gate is
    retired; the field is kept only for the shared generator's 3-tuple
    contract. Never raises — every failure is converted into a structured
    tool_result so the loop can continue. Mirrors _execute_tour_commander_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "lookup_monetization_doctrine":
        res = await mobile_monetize_service.lookup_monetization_doctrine(
            stream_key=(tool_input.get("stream_key") or "").strip(),
            diversification_key=(tool_input.get("diversification_key") or "").strip(),
            sequencing_key=(tool_input.get("sequencing_key") or "").strip(),
            admin_key=(tool_input.get("admin_key") or "").strip(),
        )
        not_found_count = len(res.get("not_found", []))
        summary = {
            "input": f"mode={res.get('mode')}",
            "result": (f"{not_found_count} not-found filter(s)" if res.get("mode") == "filtered"
                       else "index returned"),
        }
        return res, summary, False

    if name == "build_monetization_doc_scaffold":
        dt = (tool_input.get("doc_type") or "").strip()
        ins = tool_input.get("inputs") or {}
        res = await mobile_monetize_service.build_monetization_doc_scaffold(doc_type=dt, inputs=ins)
        summary = {
            "input": f"doc_type={dt or '(missing)'}",
            "result": (f"{len(res.get('sections', []))} section(s) ready" if res.get("status") == "scaffold_ready"
                       else res.get("status", "error")),
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Press (press-monitor) tool_use: search_media_outlets + analyze_sentiment + create_media_alert
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the press-monitor agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# PRESS_MONITOR_TOOLS. Handlers map straight onto the mock-first press_monitor_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
PRESS_MONITOR_MAX_TOOL_ITERS = 5

PRESS_MONITOR_TOOLS = [
    {
        "name": "search_media_outlets",
        "description": "Search the media outlet directory by beat or region",
        "input_schema": {
            "type": "object",
            "properties": {
                "beat": {"type": "string", "enum": ['indie', 'hip_hop', 'electronic', 'general']},
                "region": {"type": "string"},
            },
        },
    },
    {
        "name": "analyze_sentiment",
        "description": "Screen coverage text for negative sentiment signals",
        "input_schema": {
            "type": "object",
            "properties": {
                "coverage_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["coverage_text"],
        },
    },
    {
        "name": "create_media_alert",
        "description": "Create a media alert on the connected monitoring account",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "channel": {"type": "string", "enum": ['email', 'slack', 'sms']},
            },
            "required": ["keyword"],
        },
    },
]


async def _execute_press_monitor_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Press tool call against the mock-first press_monitor_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_media_outlets":
        a = (tool_input.get("beat") or "").strip()
        b = (tool_input.get("region") or "").strip()
        res = await press_monitor_service.search_media_outlets(beat=a, region=b)
        summary = {
            "input": f"beat={a or 'any'} region={b or 'any'}",
            "result": f"{res['count']} outlet(s) found",
        }
        return res, summary, False

    if name == "analyze_sentiment":
        txt = tool_input.get("coverage_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await press_monitor_service.analyze_sentiment(artist_id, coverage_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} signal(s), {res['recommendation']}",
        }
        return res, summary, False

    if name == "create_media_alert":
        nm  = (tool_input.get("keyword") or "").strip()
        opt = (tool_input.get("channel") or "email").strip()
        if not nm:
            return (
                {"error": "missing_keyword"},
                {"input": "keyword=", "result": "missing keyword"},
                False,
            )
        try:
            done = await press_monitor_service.create_media_alert(artist_id, nm, opt)
            return (
                {"status": "done", "reference": done.get("reference"), "keyword": nm},
                {"input": f"keyword={nm} channel={opt}", "result": "alert created"},
                False,
            )
        except press_monitor_service.MonitoringNotConnected:
            return (
                {
                    "not_connected": True,
                    "message": ("Artist has not connected a media monitoring account. Tell them to connect one "
                                "before you can complete this action."),
                },
                {"input": f"keyword={nm}", "result": "not_connected"},
                True,
            )
        except press_monitor_service.MonitoringAuthExpired:
            return (
                {
                    "not_connected": True,
                    "message": ("Media monitoring account authorization expired. Tell the artist to re-connect "
                                "before you can complete this action."),
                },
                {"input": f"keyword={nm}", "result": "auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Doc (royalty-doctor) tool_use: search_royalty_sources + assess_black_box + file_royalty_claim
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the royalty-doctor agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# ROYALTY_DOCTOR_TOOLS. Handlers map straight onto the mock-first royalty_doctor_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
ROYALTY_DOCTOR_MAX_TOOL_ITERS = 5

ROYALTY_DOCTOR_TOOLS = [
    {
        "name": "search_royalty_sources",
        "description": "Search unclaimed-royalty source types by source or territory",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string", "enum": ['neighbouring', 'mechanical', 'performance', 'digital']},
                "territory": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_black_box",
        "description": "Screen catalog notes for unclaimed-royalty exposure",
        "input_schema": {
            "type": "object",
            "properties": {
                "catalog_notes": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["catalog_notes"],
        },
    },
    {
        "name": "file_royalty_claim",
        "description": "File a royalty claim with the connected collection society",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_subject": {"type": "string"},
                "society": {"type": "string", "enum": ['pro', 'mlc', 'neighbouring']},
            },
            "required": ["claim_subject"],
        },
    },
]


async def _execute_royalty_doctor_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Doc tool call against the mock-first royalty_doctor_service.

    Returns (result_for_model, action_summary, not_connected). Never raises —
    every failure is converted into a structured tool_result so the loop can
    continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_royalty_sources":
        a = (tool_input.get("source_type") or "").strip()
        b = (tool_input.get("territory") or "").strip()
        res = await royalty_doctor_service.search_royalty_sources(source_type=a, territory=b)
        summary = {
            "input": f"source_type={a or 'any'} territory={b or 'any'}",
            "result": f"{res['count']} source(s) found",
        }
        return res, summary, False

    if name == "assess_black_box":
        txt = tool_input.get("catalog_notes") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await royalty_doctor_service.assess_black_box(artist_id, catalog_notes=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} gap(s), {res['recommendation']}",
        }
        return res, summary, False

    if name == "file_royalty_claim":
        nm  = (tool_input.get("claim_subject") or "").strip()
        opt = (tool_input.get("society") or "pro").strip()
        if not nm:
            return (
                {"error": "missing_claim_subject"},
                {"input": "claim_subject=", "result": "missing claim_subject"},
                False,
            )
        try:
            done = await royalty_doctor_service.file_royalty_claim(artist_id, nm, opt)
            return (
                {"status": "done", "reference": done.get("reference"), "claim_subject": nm},
                {"input": f"claim_subject={nm} society={opt}", "result": "claim filed"},
                False,
            )
        except royalty_doctor_service.CollectionAccountNotConnected:
            return (
                {
                    "not_connected": True,
                    "message": ("Artist has not connected a collection society account. Tell them to connect one "
                                "before you can complete this action."),
                },
                {"input": f"claim_subject={nm}", "result": "not_connected"},
                True,
            )
        except royalty_doctor_service.CollectionAccountAuthExpired:
            return (
                {
                    "not_connected": True,
                    "message": ("Collection society account authorization expired. Tell the artist to re-connect "
                                "before you can complete this action."),
                },
                {"input": f"claim_subject={nm}", "result": "auth_expired"},
                True,
            )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Cal (schedule-keeper) tool_use: search_schedule_templates + check_conflicts
# (schedule_event was a mock terminal-action tool gated behind SCHEDULE_KEEPER_CONNECTED
#  — retired; Cal is consult-only. See _audit/phase2_stop_report.md precedent pattern.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the schedule-keeper agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# SCHEDULE_KEEPER_TOOLS. Handlers map straight onto the mock-first schedule_keeper_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
SCHEDULE_KEEPER_MAX_TOOL_ITERS = 5

SCHEDULE_KEEPER_TOOLS = [
    {
        "name": "search_schedule_templates",
        "description": "Search schedule/checklist templates by category or horizon",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ['release', 'tour', 'content', 'admin']},
                "horizon": {"type": "string"},
            },
        },
    },
    {
        "name": "check_conflicts",
        "description": "Screen a proposed schedule for conflicts and overload",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_text": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["schedule_text"],
        },
    },
]


async def _execute_schedule_keeper_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Cal tool call against the mock-first schedule_keeper_service.

    Returns (result_for_model, action_summary, not_connected). Cal is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_schedule_templates":
        a = (tool_input.get("category") or "").strip()
        b = (tool_input.get("horizon") or "").strip()
        res = await schedule_keeper_service.search_schedule_templates(category=a, horizon=b)
        summary = {
            "input": f"category={a or 'any'} horizon={b or 'any'}",
            "result": f"{res['count']} template(s) found",
        }
        return res, summary, False

    if name == "check_conflicts":
        txt = tool_input.get("schedule_text") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await schedule_keeper_service.check_conflicts(artist_id, schedule_text=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} conflict(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Store (storefront) tool_use: search_product_types + assess_pricing
# (publish_store_product was a mock terminal-action tool gated behind STOREFRONT_CONNECTED
#  — retired; Store is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the storefront agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# STOREFRONT_TOOLS. Handlers map straight onto the mock-first storefront_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
STOREFRONT_MAX_TOOL_ITERS = 5

STOREFRONT_TOOLS = [
    {
        "name": "search_product_types",
        "description": "Search direct-to-fan product types by category or format",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ['music', 'merch', 'membership', 'experience']},
                "format": {"type": "string"},
            },
        },
    },
    {
        "name": "assess_pricing",
        "description": "Screen pricing notes for margin and positioning issues",
        "input_schema": {
            "type": "object",
            "properties": {
                "pricing_notes": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["pricing_notes"],
        },
    },
]


async def _execute_storefront_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Store tool call against the mock-first storefront_service.

    Returns (result_for_model, action_summary, not_connected). Store is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_product_types":
        a = (tool_input.get("category") or "").strip()
        b = (tool_input.get("format") or "").strip()
        res = await storefront_service.search_product_types(category=a, format=b)
        summary = {
            "input": f"category={a or 'any'} format={b or 'any'}",
            "result": f"{res['count']} product type(s) found",
        }
        return res, summary, False

    if name == "assess_pricing":
        txt = tool_input.get("pricing_notes") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await storefront_service.assess_pricing(artist_id, pricing_notes=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} issue(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Reel (video-director) tool_use: search_directors + estimate_video_budget
# (book_video_shoot was a mock terminal-action tool gated behind VIDEO_DIRECTOR_CONNECTED
#  — retired; Reel is consult-only.)
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) / ai-navigator pattern exactly: these tools are
# passed to the Anthropic API for the video-director agent ONLY (see the gate in
# chat_stream). Every other agent takes its own unchanged path and never receives
# VIDEO_DIRECTOR_TOOLS. Handlers map straight onto the mock-first video_director_service functions;
# they make ZERO network calls and read no secrets.

# Cap on tool_use round-trips per turn — backstop against a runaway tool loop.
VIDEO_DIRECTOR_MAX_TOOL_ITERS = 5

VIDEO_DIRECTOR_TOOLS = [
    {
        "name": "search_directors",
        "description": "Search the director roster by style or budget tier",
        "input_schema": {
            "type": "object",
            "properties": {
                "style": {"type": "string", "enum": ['narrative', 'performance', 'animation', 'experimental']},
                "budget_tier": {"type": "string"},
            },
        },
    },
    {
        "name": "estimate_video_budget",
        "description": "Screen a treatment for budget-risk cost drivers",
        "input_schema": {
            "type": "object",
            "properties": {
                "treatment_notes": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["treatment_notes"],
        },
    },
]


async def _execute_video_director_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Reel tool call against the mock-first video_director_service.

    Returns (result_for_model, action_summary, not_connected). Reel is
    consult-only (search + screen); not_connected is structurally always False.
    Never raises — every failure is converted into a structured tool_result so
    the loop can continue. Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "search_directors":
        a = (tool_input.get("style") or "").strip()
        b = (tool_input.get("budget_tier") or "").strip()
        res = await video_director_service.search_directors(style=a, budget_tier=b)
        summary = {
            "input": f"style={a or 'any'} budget_tier={b or 'any'}",
            "result": f"{res['count']} director(s) found",
        }
        return res, summary, False

    if name == "estimate_video_budget":
        txt = tool_input.get("treatment_notes") or ""
        ctx = (tool_input.get("context") or "").strip()
        res = await video_director_service.estimate_video_budget(artist_id, treatment_notes=txt, context=ctx)
        summary = {
            "input": f"context={ctx or 'unspecified'} chars={len(txt)}",
            "result": f"{res['finding_count']} cost driver(s), {res['recommendation']}",
        }
        return res, summary, False

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Quinn (pr-agent) tool_use: list_pr_contacts + log_pr_outreach + send_pr_pitch
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) pattern, but wired to the REAL pr_service functions
# (DB-backed, zero network). These tools are passed to the Anthropic API for the
# pr-agent ONLY (see the gate in chat_stream); every other agent takes its own
# unchanged path and never receives PR_AGENT_TOOLS. The send action is gated on a
# Gmail-connection check (env-driven, mock — mirrors lex's registry check) so a
# missing/expired credential degrades gracefully instead of hitting a wire.

PR_AGENT_MAX_TOOL_ITERS = 5


def _pr_gmail_state() -> str:
    """Mock Gmail-connection state for pr-agent sends (env-driven, ZERO network).

    Values: "expired" → expired; "1"/"true"/"yes"/"connected" → connected;
    anything else / unset → not connected.
    """
    val = (os.environ.get("PR_GMAIL_CONNECTED", "") or "").strip().lower()
    if val == "expired":
        return "expired"
    return "connected" if val in ("1", "true", "yes", "connected") else "not_connected"


PR_AGENT_TOOLS = [
    {
        "name": "list_pr_contacts",
        "description": "Search the PR contact database by genre, tier, or outlet type",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "tier": {"type": "string", "enum": ["A", "B", "C"]},
                "outlet_type": {"type": "string"},
            },
        },
    },
    {
        "name": "log_pr_outreach",
        "description": "Log a draft PR outreach for a contact in the outreach database",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["contact_id", "subject"],
        },
    },
    {
        "name": "send_pr_pitch",
        "description": "Queue a PR pitch to a contact via the artist's connected Gmail account",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["contact_id"],
        },
    },
]


async def _execute_pr_agent_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Quinn tool call against the REAL pr_service (DB-backed, no network).

    Returns (result_for_model, action_summary, not_connected). Never raises.
    Mirrors _execute_lex_cipher_tool.
    """
    tool_input = dict(tool_input or {})

    if name == "list_pr_contacts":
        genre       = (tool_input.get("genre") or "").strip()
        tier        = (tool_input.get("tier") or "").strip()
        outlet_type = (tool_input.get("outlet_type") or "").strip()
        contacts = pr_service._db_list_pr_contacts(genre=genre, tier=tier, outlet_type=outlet_type)
        res = {"contacts": contacts, "count": len(contacts)}
        summary = {
            "input": f"genre={genre or 'any'} tier={tier or 'any'} outlet={outlet_type or 'any'}",
            "result": f"{len(contacts)} contact(s) found",
        }
        return res, summary, False

    if name == "log_pr_outreach":
        contact_id = (tool_input.get("contact_id") or "").strip()
        subject    = (tool_input.get("subject") or "").strip()
        body       = tool_input.get("body") or ""
        if not contact_id or not subject:
            return (
                {"error": "missing_contact_or_subject"},
                {"input": f"contact={contact_id or 'none'}", "result": "missing contact or subject"},
                False,
            )
        oid = "pro-" + hashlib.sha1(f"{artist_id}:{contact_id}:{subject}".encode("utf-8")).hexdigest()[:10]
        pr_service._db_create_pr_outreach({
            "id": oid, "artist_id": artist_id, "contact_id": contact_id,
            "status": "draft", "subject": subject, "body": body,
        })
        return (
            {"status": "logged", "outreach_id": oid},
            {"input": f"contact={contact_id}", "result": "outreach logged"},
            False,
        )

    if name == "send_pr_pitch":
        contact_id = (tool_input.get("contact_id") or "").strip()
        subject    = (tool_input.get("subject") or "PR Pitch").strip()
        if not contact_id:
            return (
                {"error": "missing_contact_id"},
                {"input": "contact_id=", "result": "missing contact id"},
                False,
            )
        state = _pr_gmail_state()
        if state == "not_connected":
            return (
                {"not_connected": True,
                 "message": ("Artist has not connected a Gmail account. Tell them to connect one "
                             "before you can send PR pitches.")},
                {"input": f"contact={contact_id}", "result": "not_connected"},
                True,
            )
        if state == "expired":
            return (
                {"not_connected": True,
                 "message": ("Gmail authorization expired. Tell the artist to re-connect before you "
                             "can send PR pitches.")},
                {"input": f"contact={contact_id}", "result": "auth_expired"},
                True,
            )
        oid = "prs-" + hashlib.sha1(f"{artist_id}:{contact_id}:{subject}".encode("utf-8")).hexdigest()[:10]
        pr_service._db_create_pr_outreach({
            "id": oid, "artist_id": artist_id, "contact_id": contact_id,
            "status": "queued", "subject": subject, "body": "",
        })
        return (
            {"status": "queued", "outreach_id": oid},
            {"input": f"contact={contact_id} subject={subject}", "result": "pitch queued"},
            False,
        )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Avery (booking-agent) tool_use: list_booking_contacts + log_booking_inquiry
#            + queue_booking_inquiry
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) pattern, but wired to the REAL booking_service
# functions (DB-backed, zero network). Passed to the Anthropic API for the
# booking-agent ONLY (see the gate in chat_stream); every other agent takes its
# own unchanged path and never receives BOOKING_AGENT_TOOLS. The send action is
# gated on a Gmail-connection check (env-driven, mock — mirrors lex) so a
# missing/expired credential degrades gracefully instead of hitting a wire.
#
# [FIX-COLLISION] Renamed from send_booking_inquiry (collided with Ray B's
# venue-hawk tool of the same name — a different schema/persistence entirely).
# Ray B keeps send_booking_inquiry; this tool never actually sends over a wire
# either (mock-first, same as before) — it only ever writes a "queued"-status
# DB row pending real Gmail-send infrastructure — so queue_booking_inquiry is
# the honest name for what it already did.

BOOKING_AGENT_MAX_TOOL_ITERS = 5


def _booking_gmail_state() -> str:
    """Mock Gmail-connection state for booking-agent sends (env-driven, ZERO network)."""
    val = (os.environ.get("BOOKING_GMAIL_CONNECTED", "") or "").strip().lower()
    if val == "expired":
        return "expired"
    return "connected" if val in ("1", "true", "yes", "connected") else "not_connected"


BOOKING_AGENT_TOOLS = [
    {
        "name": "list_booking_contacts",
        "description": "Search the booking contact database by genre, tier, or city",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "tier": {"type": "string", "enum": ["A", "B", "C"]},
                "city": {"type": "string"},
            },
        },
    },
    {
        "name": "log_booking_inquiry",
        "description": "Log a draft booking inquiry for a contact in the inquiry database",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["contact_id", "subject"],
        },
    },
    {
        "name": "queue_booking_inquiry",
        "description": "Queue a booking inquiry to a promoter/venue via the artist's connected Gmail account",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["contact_id"],
        },
    },
]


async def _execute_booking_agent_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Avery tool call against the REAL booking_service (DB-backed, no network)."""
    tool_input = dict(tool_input or {})

    if name == "list_booking_contacts":
        genre = (tool_input.get("genre") or "").strip()
        tier  = (tool_input.get("tier") or "").strip()
        city  = (tool_input.get("city") or "").strip()
        contacts = booking_service._db_list_booking_contacts(genre=genre, tier=tier, city=city)
        res = {"contacts": contacts, "count": len(contacts)}
        summary = {
            "input": f"genre={genre or 'any'} tier={tier or 'any'} city={city or 'any'}",
            "result": f"{len(contacts)} contact(s) found",
        }
        return res, summary, False

    if name == "log_booking_inquiry":
        contact_id = (tool_input.get("contact_id") or "").strip()
        subject    = (tool_input.get("subject") or "").strip()
        body       = tool_input.get("body") or ""
        if not contact_id or not subject:
            return (
                {"error": "missing_contact_or_subject"},
                {"input": f"contact={contact_id or 'none'}", "result": "missing contact or subject"},
                False,
            )
        iid = "bio-" + hashlib.sha1(f"{artist_id}:{contact_id}:{subject}".encode("utf-8")).hexdigest()[:10]
        booking_service._db_create_booking_inquiry({
            "id": iid, "artist_id": artist_id, "contact_id": contact_id,
            "status": "draft", "subject": subject, "body": body,
        })
        return (
            {"status": "logged", "inquiry_id": iid},
            {"input": f"contact={contact_id}", "result": "inquiry logged"},
            False,
        )

    if name == "queue_booking_inquiry":
        contact_id = (tool_input.get("contact_id") or "").strip()
        subject    = (tool_input.get("subject") or "Booking Inquiry").strip()
        if not contact_id:
            return (
                {"error": "missing_contact_id"},
                {"input": "contact_id=", "result": "missing contact id"},
                False,
            )
        state = _booking_gmail_state()
        if state == "not_connected":
            return (
                {"not_connected": True,
                 "message": ("Artist has not connected a Gmail account. Tell them to connect one "
                             "before you can send booking inquiries.")},
                {"input": f"contact={contact_id}", "result": "not_connected"},
                True,
            )
        if state == "expired":
            return (
                {"not_connected": True,
                 "message": ("Gmail authorization expired. Tell the artist to re-connect before you "
                             "can send booking inquiries.")},
                {"input": f"contact={contact_id}", "result": "auth_expired"},
                True,
            )
        iid = "bis-" + hashlib.sha1(f"{artist_id}:{contact_id}:{subject}".encode("utf-8")).hexdigest()[:10]
        booking_service._db_create_booking_inquiry({
            "id": iid, "artist_id": artist_id, "contact_id": contact_id,
            "status": "queued", "subject": subject, "body": "",
        })
        return (
            {"status": "queued", "inquiry_id": iid},
            {"input": f"contact={contact_id} subject={subject}", "result": "inquiry queued"},
            False,
        )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Riley (social-manager) tool_use: list_social_posts + draft_social_post
#            + schedule_post
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) pattern, but wired to the REAL social_service
# functions (DB-backed, zero network). Passed to the Anthropic API for the
# social-manager ONLY (see the gate in chat_stream); every other agent takes its
# own unchanged path and never receives SOCIAL_MANAGER_TOOLS. The schedule action
# is gated on the artist's real Buffer connection (social_service._load_buffer_tokens)
# so a missing/expired Buffer link degrades gracefully instead of hitting a wire.

SOCIAL_MANAGER_MAX_TOOL_ITERS = 5

SOCIAL_MANAGER_TOOLS = [
    {
        "name": "list_social_posts",
        "description": "List the artist's social posts, optionally filtered by platform or status",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "draft_social_post",
        "description": "Create a draft social post for the artist",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "content": {"type": "string"},
                "scheduled_at": {"type": "string"},
            },
            "required": ["platform", "content"],
        },
    },
    {
        "name": "schedule_post",
        "description": "Schedule a drafted post via the artist's connected Buffer account",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string"},
            },
            "required": ["post_id"],
        },
    },
]


async def _execute_social_manager_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Riley tool call against the REAL social_service (DB-backed, no network)."""
    tool_input = dict(tool_input or {})

    if name == "list_social_posts":
        platform = (tool_input.get("platform") or "").strip()
        status   = (tool_input.get("status") or "").strip()
        posts = social_service._db_list_posts(artist_id, platform=platform, status=status)
        res = {"posts": posts, "count": len(posts)}
        summary = {
            "input": f"platform={platform or 'any'} status={status or 'any'}",
            "result": f"{len(posts)} post(s) found",
        }
        return res, summary, False

    if name == "draft_social_post":
        platform = (tool_input.get("platform") or "").strip()
        content  = tool_input.get("content") or ""
        sched    = (tool_input.get("scheduled_at") or "").strip()
        if not platform or not content:
            return (
                {"error": "missing_platform_or_content"},
                {"input": f"platform={platform or 'none'}", "result": "missing platform or content"},
                False,
            )
        pid = "sp-" + hashlib.sha1(f"{artist_id}:{platform}:{content}".encode("utf-8")).hexdigest()[:10]
        social_service._db_create_post({
            "id": pid, "artist_id": artist_id, "platform": platform,
            "content": content, "status": "draft", "scheduled_at": sched or None,
        })
        return (
            {"status": "drafted", "post_id": pid},
            {"input": f"platform={platform} chars={len(content)}", "result": "post drafted"},
            False,
        )

    if name == "schedule_post":
        post_id = (tool_input.get("post_id") or "").strip()
        if not post_id:
            return (
                {"error": "missing_post_id"},
                {"input": "post_id=", "result": "missing post id"},
                False,
            )
        tokens = social_service._load_buffer_tokens(artist_id) or {}
        if not tokens:
            return (
                {"not_connected": True,
                 "message": ("Artist has not connected a Buffer account. Tell them to connect one "
                             "before you can schedule posts.")},
                {"input": f"post={post_id}", "result": "not_connected"},
                True,
            )
        if tokens.get("expired"):
            return (
                {"not_connected": True,
                 "message": ("Buffer authorization expired. Tell the artist to re-connect before you "
                             "can schedule posts.")},
                {"input": f"post={post_id}", "result": "auth_expired"},
                True,
            )
        social_service._db_update_post(post_id, {"status": "scheduled"})
        return (
            {"status": "scheduled", "post_id": post_id},
            {"input": f"post={post_id}", "result": "post scheduled"},
            False,
        )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Sage (release-strategist) tool_use: list_releases + create_release
#            + schedule_campaign
# ═══════════════════════════════════════════════════════════════════════════════
# Mirrors the Lex (Unit 1.8) pattern, but wired to the REAL release_service
# functions (DB-backed + the pure _build_campaign_actions planner, zero network).
# Passed to the Anthropic API for the release-strategist ONLY (see the gate in
# chat_stream); every other agent takes its own unchanged path and never receives
# RELEASE_STRATEGIST_TOOLS. The schedule action is gated on a campaign-automation
# connection check (env-driven, mock — mirrors lex) so a missing/expired
# credential degrades gracefully instead of firing real campaign actions.

RELEASE_STRATEGIST_MAX_TOOL_ITERS = 5


def _release_automation_state() -> str:
    """Mock campaign-automation connection state (env-driven, ZERO network)."""
    val = (os.environ.get("RELEASE_AUTOMATION_CONNECTED", "") or "").strip().lower()
    if val == "expired":
        return "expired"
    return "connected" if val in ("1", "true", "yes", "connected") else "not_connected"


RELEASE_STRATEGIST_TOOLS = [
    {
        "name": "list_releases",
        "description": "List the artist's releases",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_release",
        "description": "Create a release record for the artist",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "release_date": {"type": "string", "description": "YYYY-MM-DD"},
                "genre": {"type": "string"},
            },
            "required": ["title", "release_date"],
        },
    },
    {
        "name": "schedule_campaign",
        "description": "Plan and schedule the rollout campaign for a release via connected automation",
        "input_schema": {
            "type": "object",
            "properties": {
                "release_id": {"type": "string"},
            },
            "required": ["release_id"],
        },
    },
]


async def _execute_release_strategist_tool(name: str, tool_input: dict, artist_id: str) -> tuple[dict, dict, bool]:
    """Execute one Sage tool call against the REAL release_service (DB-backed, no network)."""
    tool_input = dict(tool_input or {})

    if name == "list_releases":
        releases = release_service._db_list_releases(artist_id)
        res = {"releases": releases, "count": len(releases)}
        summary = {"input": "artist", "result": f"{len(releases)} release(s) found"}
        return res, summary, False

    if name == "create_release":
        title = (tool_input.get("title") or "").strip()
        date  = (tool_input.get("release_date") or "").strip()
        genre = (tool_input.get("genre") or "").strip()
        if not title or not date:
            return (
                {"error": "missing_title_or_date"},
                {"input": f"title={title or 'none'}", "result": "missing title or date"},
                False,
            )
        rid = "rel-" + hashlib.sha1(f"{artist_id}:{title}:{date}".encode("utf-8")).hexdigest()[:10]
        release_service._db_create_release({
            "id": rid, "artist_id": artist_id, "title": title,
            "release_date": date, "genre": genre, "status": "draft",
        })
        return (
            {"status": "created", "release_id": rid},
            {"input": f"title={title} date={date}", "result": "release created"},
            False,
        )

    if name == "schedule_campaign":
        release_id = (tool_input.get("release_id") or "").strip()
        if not release_id:
            return (
                {"error": "missing_release_id"},
                {"input": "release_id=", "result": "missing release id"},
                False,
            )
        state = _release_automation_state()
        if state == "not_connected":
            return (
                {"not_connected": True,
                 "message": ("Artist has not connected campaign automation. Tell them to connect it "
                             "before you can schedule a rollout.")},
                {"input": f"release={release_id}", "result": "not_connected"},
                True,
            )
        if state == "expired":
            return (
                {"not_connected": True,
                 "message": ("Campaign automation authorization expired. Tell the artist to re-connect "
                             "before you can schedule a rollout.")},
                {"input": f"release={release_id}", "result": "auth_expired"},
                True,
            )
        release = release_service._db_get_release(release_id)
        if not release:
            return (
                {"error": "release_not_found", "release_id": release_id},
                {"input": f"release={release_id}", "result": "release not found"},
                False,
            )
        actions = release_service._build_campaign_actions(release)
        for a in actions:
            release_service._db_create_action(a)
        return (
            {"status": "scheduled", "release_id": release_id, "actions_scheduled": len(actions)},
            {"input": f"release={release_id} actions={len(actions)}", "result": "campaign scheduled"},
            False,
        )

    return (
        {"error": "unknown_tool", "tool": name},
        {"input": "", "result": "unknown tool"},
        False,
    )


# ── M3 loop consolidation ────────────────────────────────────────────────────
# Single parameterized streaming generator that replaces the 43 near-identical
# inline `generate_*` tool_use loops in /api/chat_stream. Its body is the
# archetype of those loops verbatim (the non-streaming Anthropic tool_use loop
# → tool_result feedback → sentence-by-sentence TTS streaming → route/experts/
# actions/done tail), with ONLY the genuine per-agent surface parameterized:
#   • tools             — the X_TOOLS schema list passed to messages.create
#   • execute_tool      — the bound _execute_X_tool coroutine
#                         (name, input, artist_id) -> (result, summary, nc_bool)
#   • max_iters         — the X_MAX_TOOL_ITERS cap
#   • not_connected_key — the EXACT SSE `actions`-event key string the frontend
#                         and 58 tests pin (bespoke per agent, e.g.
#                         "gmail_not_connected", or the generic "not_connected")
# Everything else is request context passed in explicitly. `client` / `tts_fn` /
# `save_exchange_fn` default to the module globals and exist only as injectable
# seams for unit tests. Migrating an agent = delete its inline generator + elif
# branch and add one STREAM_AGENT_REGISTRY entry (done stage-by-stage).
async def stream_tool_use_agent(
    *,
    tools,
    execute_tool,
    max_iters,
    not_connected_key,
    messages,
    model,
    max_tokens,
    system_blocks,
    voice,
    do_tts,
    artist_id,
    agent_id,
    message,
    experts_event,
    client=None,
    tts_fn=None,
    save_exchange_fn=None,
):
    _client = client if client is not None else async_client
    _tts    = tts_fn if tts_fn is not None else tts
    _save   = save_exchange_fn if save_exchange_fn is not None else _save_exchange

    full_text     = ""
    actions_taken = []
    not_connected = False

    tts_in  = asyncio.Queue()
    evt_out = asyncio.Queue()

    async def _producer():
        nonlocal full_text, actions_taken, not_connected
        try:
            loop_messages = list(messages)
            final_text    = ""
            last_text     = ""
            for _ in range(max_iters):
                resp = await _client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=loop_messages,
                    tools=tools,
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                )
                blocks    = list(getattr(resp, "content", []) or [])
                tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                text_parts = [getattr(b, "text", "") for b in blocks
                              if getattr(b, "type", None) == "text"]
                last_text = "".join(text_parts).strip() or last_text

                if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                    loop_messages.append({"role": "assistant", "content": blocks})
                    results_content = []
                    for tu in tool_uses:
                        result, summary, nc = await execute_tool(
                            tu.name, getattr(tu, "input", {}) or {}, artist_id,
                        )
                        if nc:
                            not_connected = True
                        actions_taken.append({
                            "tool":   tu.name,
                            "input":  summary["input"],
                            "result": summary["result"],
                        })
                        results_content.append({
                            "type":        "tool_result",
                            "tool_use_id": tu.id,
                            "content":     json.dumps(result),
                        })
                    loop_messages.append({"role": "user", "content": results_content})
                    continue

                final_text = "".join(text_parts).strip()
                break

            if not final_text:
                final_text = last_text or "I've taken the actions I can on that for now."
            full_text = final_text

            buf       = final_text
            route_cut = False
            while True:
                sentence, buf = split_sentence(buf)
                if not sentence:
                    break
                await evt_out.put(("text", sentence))
                if do_tts:
                    await tts_in.put(sentence)
                if detect_routing(sentence):
                    route_cut = True
                    break
            if not route_cut:
                remainder = buf.strip()
                if remainder:
                    await evt_out.put(("text", remainder))
                    if do_tts:
                        await tts_in.put(remainder)
        except Exception as e:
            await evt_out.put(("error", str(e)))
        finally:
            await tts_in.put(None)

    async def _tts_worker():
        task_q = asyncio.Queue()
        first  = True

        async def _submit():
            nonlocal first
            while True:
                sentence = await tts_in.get()
                if sentence is None:
                    await task_q.put(None)
                    return
                if first:
                    await evt_out.put(("status", "Generating voice…"))
                    first = False
                task = asyncio.create_task(_tts(sentence, voice))
                await task_q.put(task)

        asyncio.create_task(_submit())
        while True:
            item = await task_q.get()
            if item is None:
                break
            audio_bytes = await item
            if audio_bytes:
                await evt_out.put(("audio", audio_bytes))
        await evt_out.put(None)

    asyncio.create_task(_producer())
    if do_tts:
        asyncio.create_task(_tts_worker())
    else:
        async def _no_tts_closer():
            while True:
                s = await tts_in.get()
                if s is None:
                    break
            await evt_out.put(None)
        asyncio.create_task(_no_tts_closer())

    while True:
        item = await evt_out.get()
        if item is None:
            break
        evt_type, data = item
        if evt_type == "text":
            yield sse({"type": "text", "text": data})
        elif evt_type == "audio":
            yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
        elif evt_type == "status":
            yield sse({"type": "status", "text": data})
        elif evt_type == "error":
            yield sse({"type": "error", "message": data})
            break

    route = detect_routing(full_text)
    if route:
        slug = route["name"].lower().replace(" ", "-")
        yield sse({
            "type":        "route",
            "agent_id":    route["id"],
            "agent_name":  route["name"],
            "agent_title": route["title"],
            "agent_voice": route["voice"],
            "agent_slug":  slug,
        })
    if experts_event:
        yield sse(experts_event)
    try:
        yield sse({
            "type":            "actions",
            "actions_taken":   actions_taken,
            not_connected_key: not_connected,
        })
    except Exception:
        pass
    yield sse({"type": "done", "full_text": full_text})

    if full_text and message != "__greet__":
        asyncio.create_task(_save(artist_id, agent_id, message, full_text))


# Registry: agent_id -> (tools, execute_tool, max_iters, not_connected_key).
# Populated stage-by-stage as inline generators are retired. Dispatch consults
# this dict first (added in the canary stage); an agent absent here keeps its
# inline generator. Every entry below is an agent whose SSE key is the generic
# "not_connected" (the safest tranche); bespoke-key and danger-zone agents are
# migrated in later, separately-attended stages.
STREAM_AGENT_REGISTRY = {
    # ── canaries (3 true 128-line-template agents, generic key) ──
    "content-forge":  (CONTENT_FORGE_TOOLS,  _execute_content_forge_tool,  CONTENT_FORGE_MAX_TOOL_ITERS,  "not_connected"),
    "press-monitor":  (PRESS_MONITOR_TOOLS,  _execute_press_monitor_tool,  PRESS_MONITOR_MAX_TOOL_ITERS,  "not_connected"),
    "royalty-doctor": (ROYALTY_DOCTOR_TOOLS, _execute_royalty_doctor_tool, ROYALTY_DOCTOR_MAX_TOOL_ITERS, "not_connected"),
    # ── U3a: generic-key tranche batch a ──
    "ai-navigator":    (AI_NAVIGATOR_TOOLS,    _execute_ai_navigator_tool,    AI_NAVIGATOR_MAX_TOOL_ITERS,    "not_connected"),
    "artist-wellness": (ARTIST_WELLNESS_TOOLS, _execute_artist_wellness_tool, ARTIST_WELLNESS_MAX_TOOL_ITERS, "not_connected"),
    "audio-quality":   (AUDIO_QUALITY_TOOLS,   _execute_audio_quality_tool,   AUDIO_QUALITY_MAX_TOOL_ITERS,   "not_connected"),
    "collab-connect":  (COLLAB_CONNECT_TOOLS,  _execute_collab_connect_tool,  COLLAB_CONNECT_MAX_TOOL_ITERS,  "not_connected"),
    # ── U3b: generic-key tranche batch b ──
    "ink-and-air":     (INK_AND_AIR_TOOLS,     _execute_ink_and_air_tool,     INK_AND_AIR_MAX_TOOL_ITERS,     "not_connected"),
    "label-services":  (LABEL_SERVICES_TOOLS,  _execute_label_services_tool,  LABEL_SERVICES_MAX_TOOL_ITERS,  "not_connected"),
    "live-coach":      (LIVE_COACH_TOOLS,      _execute_live_coach_tool,      LIVE_COACH_MAX_TOOL_ITERS,      "not_connected"),
    "live-wire":       (LIVE_WIRE_TOOLS,       _execute_live_wire_tool,       LIVE_WIRE_MAX_TOOL_ITERS,       "not_connected"),
    # ── U3c: generic-key tranche batch c ──
    "mobile-monetize": (MOBILE_MONETIZE_TOOLS, _execute_mobile_monetize_tool, MOBILE_MONETIZE_MAX_TOOL_ITERS, "not_connected"),
    "schedule-keeper": (SCHEDULE_KEEPER_TOOLS, _execute_schedule_keeper_tool, SCHEDULE_KEEPER_MAX_TOOL_ITERS, "not_connected"),
    "storefront":      (STOREFRONT_TOOLS,      _execute_storefront_tool,      STOREFRONT_MAX_TOOL_ITERS,      "not_connected"),
    "video-director":  (VIDEO_DIRECTOR_TOOLS,  _execute_video_director_tool,  VIDEO_DIRECTOR_MAX_TOOL_ITERS,  "not_connected"),
}


class ChatStreamRequest(BaseModel):
    agent_id:  str
    message:   str
    artist_id: str    = ""
    history:   str    = "[]"   # JSON-encoded array
    tts:       bool   = True

@app.post("/api/chat_stream")
async def chat_stream(req: ChatStreamRequest):
    agent_id  = req.agent_id
    message   = req.message
    artist_id = req.artist_id
    history   = req.history
    tts_on    = "true" if req.tts else "false"
    """
    Streaming endpoint. Yields SSE events:
      {type:"text",  text:"..."}           — sentence of text as it arrives
      {type:"audio", audio:"<b64 wav>"}    — synthesized audio for that sentence
      {type:"route", agent_id, agent_name} — if Marcus routes to another agent
      {type:"done",  full_text:"..."}      — final full response text
      {type:"error", message:"..."}        — on failure

    History is trimmed before each call to prevent unbounded token growth.
    Voice sessions use a tighter cap than text sessions.
    """
    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="AI unavailable: ANTHROPIC_API_KEY not configured")
    agent = AGENTS_BY_ID.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    skill_path = SKILLS_DIR / agent["skill"] / "SKILL.md"
    print(f"\n[AGENT] {agent['name']} ({agent_id}) | SKILL: {'OK' if skill_path.exists() else 'MISSING'}")

    try:
        history_list = json.loads(history)
    except Exception:
        history_list = []

    do_tts = tts_on.lower() == "true"

    if message == "__greet__":
        greeting_text = _get_greeting(agent)
        print(f"[GREET] {agent['name']} — static greeting ({len(greeting_text)} chars)")
        async def _static_greet():
            yield sse({"type": "text",  "text": greeting_text})
            yield sse({"type": "done",  "full_text": greeting_text})
        return StreamingResponse(
            _static_greet(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        has_history = len(history_list) > 0

    system_blocks = build_system_blocks(agent, artist_id=artist_id, voice_mode=do_tts, has_history=has_history, question=message)

    # Use tighter history cap for voice (faster, cheaper); wider for text (more context)
    history_cap = HISTORY_CAP_VOICE if do_tts else HISTORY_CAP_TEXT
    messages    = build_messages(history_list, message, cap=history_cap)

    voice         = agent["voice"]
    tier          = load_artist(artist_id).get("tier", "")
    model, max_tokens = select_model(message, tier)

    # Voice mode: hard cap at 300 tokens (~150 words) regardless of model
    if do_tts:
        max_tokens = 300

    print(f"[ROUTE] {agent['name']} | tier={tier or 'default'} | model={model.split('-')[1]} | voice={do_tts} | history={len(messages)-1} turns | max_tok={max_tokens}")

    # Knowledge-bank experts consulted for this turn (deterministic, NO LLM) — the
    # same pure retrieval build_system_blocks already ran, surfaced to the client so
    # the UI can show which expert domains grounded the answer. home_domain first.
    experts_event = None
    try:
        _consult = _bank_consult_for_agent(agent["id"], message)
        experts_event = {
            "type":        "experts",
            "home_domain": _consult.get("home_domain"),
            "domains":     _consult.get("domains", []),
        }
    except Exception:
        experts_event = None

    async def generate():
        full_text = ""

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _claude():
            nonlocal full_text
            buf = ""
            route_cut = False
            try:
                async with async_client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=messages,
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                ) as stream:
                    async for chunk in stream.text_stream:
                        full_text += chunk
                        buf       += chunk
                        while True:
                            sentence, buf = split_sentence(buf)
                            if not sentence:
                                break
                            await evt_out.put(("text", sentence))
                            if do_tts:
                                await tts_in.put(sentence)
                            if detect_routing(sentence):
                                route_cut = True
                                break
                        if route_cut:
                            break

                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)

            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            # Eager synthesis: start each sentence's TTS task as soon as it arrives
            # so sentence N+1 queues on the lock while sentence N is still transmitting.
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    # Create the synthesis task immediately — it queues behind the
                    # TTS lock so N+1 starts the moment N finishes, eliminating the gap.
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_claude())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        yield sse({"type": "done", "full_text": full_text})

        # Persist exchange to history DB (skip greeting pings)
        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_marcus():
        # Marcus-only path (Unit 1.7): a standard Anthropic tool_use loop. Runs the
        # non-streaming messages.create with tools=MARCUS_TOOLS, executes each emitted
        # tool_use against pitch_service, feeds tool_result back, and repeats (capped at
        # MARCUS_MAX_TOOL_ITERS) until Marcus returns a final text answer. The final
        # text is then streamed out sentence-by-sentence through the SAME TTS pipeline
        # the default path uses, so the call UI behaves identically aside from the new
        # `actions` event. Every other agent never reaches here.
        full_text          = ""
        actions_taken      = []
        gmail_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _marcus_producer():
            nonlocal full_text, actions_taken, gmail_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(MARCUS_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=MARCUS_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, gnc = await _execute_marcus_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if gnc:
                                gmail_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_marcus_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Marcus took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                "actions",
                "actions_taken":       actions_taken,
                "gmail_not_connected": gmail_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_lex_cipher():
        # Lex-only path (Unit 1.8): the SAME Anthropic tool_use loop as Marcus, but
        # pointed at LEX_CIPHER_TOOLS / _execute_lex_cipher_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against lex_cipher_service, feeds tool_result back, and repeats (capped at
        # LEX_CIPHER_MAX_TOOL_ITERS) until Lex returns a final text answer. The final
        # text is then streamed out sentence-by-sentence through the SAME TTS pipeline
        # the default path uses, so the call UI behaves identically aside from the
        # `actions` event. Every other agent (including Marcus) never reaches here.
        full_text             = ""
        actions_taken         = []
        registry_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _lex_producer():
            nonlocal full_text, actions_taken, registry_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(LEX_CIPHER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=LEX_CIPHER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, rnc = await _execute_lex_cipher_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if rnc:
                                registry_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_lex_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Lex took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                   "actions",
                "actions_taken":          actions_taken,
                "registry_not_connected": registry_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_fund_phantom():
        # Fund-Phantom-only path (Jade): the SAME Anthropic tool_use loop as Marcus
        # and Lex, but pointed at FUND_PHANTOM_TOOLS / _execute_fund_phantom_tool
        # instead. Runs the non-streaming messages.create with tools, executes each
        # emitted tool_use against fund_phantom_service, feeds tool_result back, and
        # repeats (capped at FUND_PHANTOM_MAX_TOOL_ITERS) until Jade returns a final
        # text answer. The final text is then streamed out sentence-by-sentence
        # through the SAME TTS pipeline the default path uses, so the call UI behaves
        # identically aside from the `actions` event. Every other agent (including
        # Marcus and Lex) never reaches here.
        full_text            = ""
        actions_taken        = []
        portal_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _fund_phantom_producer():
            nonlocal full_text, actions_taken, portal_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(FUND_PHANTOM_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=FUND_PHANTOM_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnc = await _execute_fund_phantom_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnc:
                                portal_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_fund_phantom_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Jade took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                 "actions",
                "actions_taken":        actions_taken,
                "portal_not_connected": portal_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_rights_pulse():
        # Rights-Pulse-only path (Ray): the SAME Anthropic tool_use loop as Marcus,
        # Lex, and Jade, but pointed at RIGHTS_PULSE_TOOLS / _execute_rights_pulse_tool
        # instead. Runs the non-streaming messages.create with tools, executes each
        # emitted tool_use against rights_pulse_service, feeds tool_result back, and
        # repeats (capped at RIGHTS_PULSE_MAX_TOOL_ITERS) until Ray returns a final
        # text answer. The final text is then streamed out sentence-by-sentence
        # through the SAME TTS pipeline the default path uses, so the call UI behaves
        # identically aside from the `actions` event. Every other agent (including
        # Marcus, Lex, and Jade) never reaches here.
        full_text         = ""
        actions_taken     = []
        pro_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _rights_pulse_producer():
            nonlocal full_text, actions_taken, pro_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(RIGHTS_PULSE_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=RIGHTS_PULSE_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnc = await _execute_rights_pulse_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnc:
                                pro_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_rights_pulse_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Ray took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":              "actions",
                "actions_taken":     actions_taken,
                "pro_not_connected": pro_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_border_royalty():
        # Border-Royalty-only path (Cleo): the SAME Anthropic tool_use loop as
        # Marcus, Lex, Jade, and Ray, but pointed at BORDER_ROYALTY_TOOLS /
        # _execute_border_royalty_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # border_royalty_service, feeds tool_result back, and repeats (capped at
        # BORDER_ROYALTY_MAX_TOOL_ITERS) until Cleo returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside
        # from the `actions` event. Every other agent (including Marcus, Lex, Jade,
        # and Ray) never reaches here.
        full_text             = ""
        actions_taken         = []
        society_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _border_royalty_producer():
            nonlocal full_text, actions_taken, society_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(BORDER_ROYALTY_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=BORDER_ROYALTY_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, snc = await _execute_border_royalty_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if snc:
                                society_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_border_royalty_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Cleo took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                  "actions",
                "actions_taken":         actions_taken,
                "society_not_connected": society_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_mech_ledger():
        # Mech-Ledger-only path (Finn): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, and Cleo, but pointed at MECH_LEDGER_TOOLS /
        # _execute_mech_ledger_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against mech_ledger_service,
        # feeds tool_result back, and repeats (capped at MECH_LEDGER_MAX_TOOL_ITERS)
        # until Finn returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent (including Marcus, Lex, Jade, Ray, and Cleo) never reaches here.
        full_text                  = ""
        actions_taken              = []
        mech_account_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _mech_ledger_producer():
            nonlocal full_text, actions_taken, mech_account_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(MECH_LEDGER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=MECH_LEDGER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, mnc = await _execute_mech_ledger_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if mnc:
                                mech_account_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_mech_ledger_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Finn took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                       "actions",
                "actions_taken":              actions_taken,
                "mech_account_not_connected": mech_account_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_vault_keeper():
        # Vault-Keeper-only path (Victor): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, and Finn, but pointed at VAULT_KEEPER_TOOLS /
        # _execute_vault_keeper_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against vault_keeper_service,
        # feeds tool_result back, and repeats (capped at VAULT_KEEPER_MAX_TOOL_ITERS)
        # until Victor returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent (including Marcus, Lex, Jade, Ray, Cleo, and Finn) never reaches here.
        full_text                   = ""
        actions_taken               = []
        vault_account_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _vault_keeper_producer():
            nonlocal full_text, actions_taken, vault_account_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(VAULT_KEEPER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=VAULT_KEEPER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, vnc = await _execute_vault_keeper_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if vnc:
                                vault_account_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_vault_keeper_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Victor took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                        "actions",
                "actions_taken":               actions_taken,
                "vault_account_not_connected": vault_account_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_ledger_lock():
        # Ledger-Lock-only path (Nadia): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, and Victor, but pointed at LEDGER_LOCK_TOOLS /
        # _execute_ledger_lock_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against ledger_lock_service,
        # feeds tool_result back, and repeats (capped at LEDGER_LOCK_MAX_TOOL_ITERS)
        # until Nadia returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent (including Marcus, Lex, Jade, Ray, Cleo, Finn, and Victor) never reaches here.
        full_text                    = ""
        actions_taken                = []
        ledger_account_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _ledger_lock_producer():
            nonlocal full_text, actions_taken, ledger_account_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(LEDGER_LOCK_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=LEDGER_LOCK_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, lnc = await _execute_ledger_lock_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if lnc:
                                ledger_account_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_ledger_lock_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Nadia took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                         "actions",
                "actions_taken":                actions_taken,
                "ledger_account_not_connected": ledger_account_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_signal_blaster():
        # Signal-Blaster-only path (Zara): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, and Nadia, but pointed at
        # SIGNAL_BLASTER_TOOLS / _execute_signal_blaster_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against signal_blaster_service, feeds tool_result back, and repeats (capped
        # at SIGNAL_BLASTER_MAX_TOOL_ITERS) until Zara returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside from
        # the `actions` event. Every other agent (including Marcus, Lex, Jade, Ray,
        # Cleo, Finn, Victor, and Nadia) never reaches here.
        full_text                   = ""
        actions_taken               = []
        press_account_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _signal_blaster_producer():
            nonlocal full_text, actions_taken, press_account_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(SIGNAL_BLASTER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=SIGNAL_BLASTER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnc = await _execute_signal_blaster_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnc:
                                press_account_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_signal_blaster_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Zara took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                        "actions",
                "actions_taken":               actions_taken,
                "press_account_not_connected": press_account_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_grid_prophet():
        # Grid-Prophet-only path (Kai): the SAME Anthropic tool_use loop as Marcus
        # and Miles (DOC-WRITER Option B), but pointed at GRID_PROPHET_TOOLS /
        # _execute_grid_prophet_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # grid_prophet_service, feeds tool_result back, and repeats (capped at
        # GRID_PROPHET_MAX_TOOL_ITERS) until Kai returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside
        # from the `actions` event. The old social-account gate is retired — Kai's
        # tools are pure corpus-read / data-scaffold tools, so
        # `social_account_not_connected` is always False now (kept only for the
        # shared generator's 3-tuple contract). Every other agent never reaches here.
        full_text                    = ""
        actions_taken                = []
        social_account_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _grid_prophet_producer():
            nonlocal full_text, actions_taken, social_account_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(GRID_PROPHET_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=GRID_PROPHET_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, snc = await _execute_grid_prophet_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if snc:
                                social_account_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_grid_prophet_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Kai took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                         "actions",
                "actions_taken":                actions_taken,
                "social_account_not_connected": social_account_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_vision_forge():
        # Vision-Forge-only path (Luna): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, and Kai, but pointed at
        # VISION_FORGE_TOOLS / _execute_vision_forge_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against vision_forge_service, feeds tool_result back, and repeats (capped
        # at VISION_FORGE_MAX_TOOL_ITERS) until Luna returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside from
        # the `actions` event. Every other agent (including Marcus, Lex, Jade, Ray,
        # Cleo, Finn, Victor, Nadia, Zara, and Kai) never reaches here.
        full_text                       = ""
        actions_taken                   = []
        render_workspace_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _vision_forge_producer():
            nonlocal full_text, actions_taken, render_workspace_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(VISION_FORGE_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=VISION_FORGE_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, rwnc = await _execute_vision_forge_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if rwnc:
                                render_workspace_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_vision_forge_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Luna took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                            "actions",
                "actions_taken":                   actions_taken,
                "render_workspace_not_connected":  render_workspace_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_design_studio():
        # Design-Studio-only path (Diego): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, and Luna, but pointed at
        # DESIGN_STUDIO_TOOLS / _execute_design_studio_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against design_studio_service, feeds tool_result back, and repeats (capped
        # at DESIGN_STUDIO_MAX_TOOL_ITERS) until Diego returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside from
        # the `actions` event. Every other agent (including Marcus, Lex, Jade, Ray,
        # Cleo, Finn, Victor, Nadia, Zara, Kai, and Luna) never reaches here.
        full_text                       = ""
        actions_taken                   = []
        design_workspace_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _design_studio_producer():
            nonlocal full_text, actions_taken, design_workspace_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(DESIGN_STUDIO_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=DESIGN_STUDIO_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, dwnc = await _execute_design_studio_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if dwnc:
                                design_workspace_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_design_studio_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Diego took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                            "actions",
                "actions_taken":                   actions_taken,
                "design_workspace_not_connected":  design_workspace_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_venue_hawk():
        # Venue-Hawk-only path (Ray B): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, and Diego, but
        # pointed at VENUE_HAWK_TOOLS / _execute_venue_hawk_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against venue_hawk_service, feeds tool_result back, and repeats (capped at
        # VENUE_HAWK_MAX_TOOL_ITERS) until Ray B returns a final text answer. The final
        # text is then streamed out sentence-by-sentence through the SAME TTS pipeline
        # the default path uses, so the call UI behaves identically aside from the
        # `actions` event. Every other agent (including Marcus, Lex, Jade, Ray, Cleo,
        # Finn, Victor, Nadia, Zara, Kai, Luna, and Diego) never reaches here.
        full_text                    = ""
        actions_taken                = []
        venue_booking_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _venue_hawk_producer():
            nonlocal full_text, actions_taken, venue_booking_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(VENUE_HAWK_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=VENUE_HAWK_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, vbnc = await _execute_venue_hawk_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if vbnc:
                                venue_booking_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_venue_hawk_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Ray B took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                          "actions",
                "actions_taken":                 actions_taken,
                "venue_booking_not_connected":   venue_booking_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_tour_commander():
        # Tour-Commander-only path (Miles): the SAME Anthropic tool_use loop as Marcus
        # and Lex (DOC-WRITER Option B), but pointed at TOUR_COMMANDER_TOOLS /
        # _execute_tour_commander_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # tour_commander_service, feeds tool_result back, and repeats (capped at
        # TOUR_COMMANDER_MAX_TOOL_ITERS) until Miles returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside
        # from the `actions` event. The old tour-ops-account gate is retired — Miles's
        # tools are pure corpus-read / data-scaffold tools, so
        # `tour_ops_not_connected` is always False now (kept only for the shared
        # generator's 3-tuple contract). Every other agent never reaches here.
        full_text                = ""
        actions_taken            = []
        tour_ops_not_connected   = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _tour_commander_producer():
            nonlocal full_text, actions_taken, tour_ops_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(TOUR_COMMANDER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=TOUR_COMMANDER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, tonc = await _execute_tour_commander_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if tonc:
                                tour_ops_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_tour_commander_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Miles took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                     "actions",
                "actions_taken":            actions_taken,
                "tour_ops_not_connected":   tour_ops_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_airwave():
        # Airwave-only path (Solo): the SAME Anthropic tool_use loop as Marcus, Lex,
        # Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, and
        # Miles, but pointed at AIRWAVE_TOOLS / _execute_airwave_tool instead. Runs
        # the non-streaming messages.create with tools, executes each emitted tool_use
        # against airwave_service, feeds tool_result back, and repeats (capped at
        # AIRWAVE_MAX_TOOL_ITERS) until Solo returns a final text answer. The final
        # text is then streamed out sentence-by-sentence through the SAME TTS pipeline
        # the default path uses, so the call UI behaves identically aside from the
        # `actions` event. Every other agent (including Marcus, Lex, Jade, Ray, Cleo,
        # Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, and Miles) never reaches here.
        full_text                = ""
        actions_taken            = []
        plugging_not_connected   = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _airwave_producer():
            nonlocal full_text, actions_taken, plugging_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(AIRWAVE_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=AIRWAVE_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnc = await _execute_airwave_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnc:
                                plugging_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_airwave_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Solo took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                    "actions",
                "actions_taken":           actions_taken,
                "plugging_not_connected":  plugging_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_brand_connect():
        # Brand-Connect-only path (Nia): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B,
        # Miles, and Solo, but pointed at BRAND_CONNECT_TOOLS /
        # _execute_brand_connect_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # brand_connect_service, feeds tool_result back, and repeats (capped at
        # BRAND_CONNECT_MAX_TOOL_ITERS) until Nia returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside
        # from the `actions` event. Every other agent (including Marcus, Lex, Jade,
        # Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B, Miles, and
        # Solo) never reaches here.
        full_text                   = ""
        actions_taken               = []
        partnerships_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _brand_connect_producer():
            nonlocal full_text, actions_taken, partnerships_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(BRAND_CONNECT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=BRAND_CONNECT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnc = await _execute_brand_connect_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnc:
                                partnerships_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_brand_connect_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Nia took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                        "actions",
                "actions_taken":               actions_taken,
                "partnerships_not_connected":  partnerships_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_merch_empire():
        # Merch-Empire-only path (Max): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B,
        # Miles, Solo, and Nia, but pointed at MERCH_EMPIRE_TOOLS /
        # _execute_merch_empire_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against merch_empire_service,
        # feeds tool_result back, and repeats (capped at MERCH_EMPIRE_MAX_TOOL_ITERS)
        # until Max returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent (including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara,
        # Kai, Luna, Diego, Ray B, Miles, Solo, and Nia) never reaches here.
        full_text                  = ""
        actions_taken              = []
        fulfillment_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _merch_empire_producer():
            nonlocal full_text, actions_taken, fulfillment_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(MERCH_EMPIRE_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=MERCH_EMPIRE_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, fnc = await _execute_merch_empire_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if fnc:
                                fulfillment_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_merch_empire_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Max took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                       "actions",
                "actions_taken":              actions_taken,
                "fulfillment_not_connected":  fulfillment_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_fan_builder():
        # Fan-Builder-only path (Aria): the SAME Anthropic tool_use loop as Marcus
        # and Miles (DOC-WRITER Option B), but pointed at FAN_BUILDER_TOOLS /
        # _execute_fan_builder_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against fan_builder_service,
        # feeds tool_result back, and repeats (capped at FAN_BUILDER_MAX_TOOL_ITERS)
        # until Aria returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. The old
        # fan-platform/CRM connection gate is retired — Aria's tools are pure
        # corpus-read / data-scaffold tools, so `fan_platform_not_connected` is
        # always False now (kept only for the shared generator's 3-tuple contract).
        # Every other agent (including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor,
        # Nadia, Zara, Kai, Luna, Diego, Ray B, Miles, Solo, Nia, and Max) never
        # reaches here.
        full_text                   = ""
        actions_taken               = []
        fan_platform_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _fan_builder_producer():
            nonlocal full_text, actions_taken, fan_platform_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(FAN_BUILDER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=FAN_BUILDER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, fnc = await _execute_fan_builder_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if fnc:
                                fan_platform_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_fan_builder_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Aria took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                        "actions",
                "actions_taken":               actions_taken,
                "fan_platform_not_connected":  fan_platform_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_sync_agent():
        # Sync-Agent-only path (Sync): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B,
        # Miles, Solo, Nia, Max, and Aria, but pointed at SYNC_AGENT_TOOLS /
        # _execute_sync_agent_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against sync_agent_service,
        # feeds tool_result back, and repeats (capped at SYNC_AGENT_MAX_TOOL_ITERS)
        # until Sync returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent (including Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara,
        # Kai, Luna, Diego, Ray B, Miles, Solo, Nia, Max, and Aria) never reaches here.
        full_text                     = ""
        actions_taken                 = []
        sync_catalogue_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _sync_agent_producer():
            nonlocal full_text, actions_taken, sync_catalogue_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(SYNC_AGENT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=SYNC_AGENT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, snc = await _execute_sync_agent_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if snc:
                                sync_catalogue_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_sync_agent_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Sync took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                          "actions",
                "actions_taken":                 actions_taken,
                "sync_catalogue_not_connected":  sync_catalogue_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_global_scout():
        # Global-Scout-only path (Nova): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Ray B, and the rest, but pointed at GLOBAL_SCOUT_TOOLS /
        # _execute_global_scout_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against global_scout_service,
        # feeds tool_result back, and repeats (capped at GLOBAL_SCOUT_MAX_TOOL_ITERS)
        # until Nova returns a final text answer. The final text is then streamed out
        # sentence-by-sentence through the SAME TTS pipeline the default path uses, so
        # the call UI behaves identically aside from the `actions` event. Every other
        # agent never reaches here.
        full_text                        = ""
        actions_taken                    = []
        global_distribution_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _global_scout_producer():
            nonlocal full_text, actions_taken, global_distribution_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(GLOBAL_SCOUT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=GLOBAL_SCOUT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, gdnc = await _execute_global_scout_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if gdnc:
                                global_distribution_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_global_scout_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Nova took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                              "actions",
                "actions_taken":                     actions_taken,
                "global_distribution_not_connected": global_distribution_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_creative_director():
        # Creative-Director-only path (Cree): the SAME Anthropic tool_use loop as
        # Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego,
        # Ray B, Miles, Solo, Nia, Max, Aria, Sync, and Nova, but pointed at
        # CREATIVE_DIRECTOR_TOOLS / _execute_creative_director_tool instead. Runs the
        # non-streaming messages.create with tools, executes each emitted tool_use
        # against creative_director_service, feeds tool_result back, and repeats
        # (capped at CREATIVE_DIRECTOR_MAX_TOOL_ITERS) until Cree returns a final text
        # answer. The final text is then streamed out sentence-by-sentence through the
        # SAME TTS pipeline the default path uses, so the call UI behaves identically
        # aside from the `actions` event. Every other agent never reaches here.
        full_text                     = ""
        actions_taken                 = []
        creative_studio_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _creative_director_producer():
            nonlocal full_text, actions_taken, creative_studio_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(CREATIVE_DIRECTOR_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=CREATIVE_DIRECTOR_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, csnc = await _execute_creative_director_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if csnc:
                                creative_studio_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_creative_director_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Cree took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                          "actions",
                "actions_taken":                 actions_taken,
                "creative_studio_not_connected": creative_studio_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_data_oracle():
        # Data-Oracle-only path (Data): the SAME Anthropic tool_use loop as Marcus,
        # Lex, and Miles (DOC-WRITER Option B), but pointed at DATA_ORACLE_TOOLS /
        # _execute_data_oracle_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # data_oracle_service, feeds tool_result back, and repeats (capped at
        # DATA_ORACLE_MAX_TOOL_ITERS) until Data returns a final text answer. The
        # final text is then streamed out sentence-by-sentence through the SAME TTS
        # pipeline the default path uses, so the call UI behaves identically aside
        # from the `actions` event. The old data-warehouse gate is retired — Data's
        # tools are pure corpus-read / data-scaffold tools, so
        # `data_warehouse_not_connected` is always False now (kept only for the
        # shared generator's 3-tuple contract). Every other agent never reaches here.
        full_text                    = ""
        actions_taken                = []
        data_warehouse_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _data_oracle_producer():
            nonlocal full_text, actions_taken, data_warehouse_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(DATA_ORACLE_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=DATA_ORACLE_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, dwnc = await _execute_data_oracle_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if dwnc:
                                data_warehouse_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_data_oracle_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Data took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                         "actions",
                "actions_taken":                actions_taken,
                "data_warehouse_not_connected": data_warehouse_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_ar_scout():
        # AR-Scout-only path (Scout): the SAME Anthropic tool_use loop as Marcus,
        # Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego, Ray B,
        # Miles, Solo, Nia, Max, Aria, Sync, Nova, Cree, and Data, but pointed at
        # AR_SCOUT_TOOLS / _execute_ar_scout_tool instead. Runs the non-streaming
        # messages.create with tools, executes each emitted tool_use against
        # ar_scout_service, feeds tool_result back, and repeats (capped at
        # AR_SCOUT_MAX_TOOL_ITERS) until Scout returns a final text answer. The final
        # text is then streamed out sentence-by-sentence through the SAME TTS pipeline
        # the default path uses, so the call UI behaves identically aside from the
        # `actions` event. Every other agent never reaches here.
        full_text                  = ""
        actions_taken              = []
        ar_scout_crm_not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _ar_scout_producer():
            nonlocal full_text, actions_taken, ar_scout_crm_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(AR_SCOUT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=AR_SCOUT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, acnc = await _execute_ar_scout_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if acnc:
                                ar_scout_crm_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_ar_scout_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Scout took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                       "actions",
                "actions_taken":              actions_taken,
                "ar_scout_crm_not_connected": ar_scout_crm_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_producer_connect():
        # Producer-Connect-only path (Beat): the SAME Anthropic tool_use loop as
        # Marcus, Lex, Jade, Ray, Cleo, Finn, Victor, Nadia, Zara, Kai, Luna, Diego,
        # Ray B, Miles, Solo, Nia, Max, Aria, Sync, Nova, Cree, Data, and Scout, but
        # pointed at PRODUCER_CONNECT_TOOLS / _execute_producer_connect_tool instead.
        # Runs the non-streaming messages.create with tools, executes each emitted
        # tool_use against producer_connect_service, feeds tool_result back, and repeats
        # (capped at PRODUCER_CONNECT_MAX_TOOL_ITERS) until Beat returns a final text
        # answer. The final text is then streamed out sentence-by-sentence through the
        # SAME TTS pipeline the default path uses, so the call UI behaves identically
        # aside from the `actions` event. Every other agent never reaches here.
        full_text                       = ""
        actions_taken                   = []
        producer_network_not_connected  = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _producer_connect_producer():
            nonlocal full_text, actions_taken, producer_network_not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(PRODUCER_CONNECT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=PRODUCER_CONNECT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, pnnc = await _execute_producer_connect_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if pnnc:
                                producer_network_not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                # Stream the final answer through the shared sentence/TTS pipeline.
                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_producer_connect_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        # Surface the actions Beat took so the call UI can render them. Wrapped so a
        # serialization hiccup can never break the main stream.
        try:
            yield sse({
                "type":                            "actions",
                "actions_taken":                   actions_taken,
                "producer_network_not_connected":  producer_network_not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_pr_agent():
        # pr-agent-only path: the SAME Anthropic tool_use loop as Lex, but pointed at
        # PR_AGENT_TOOLS / _execute_pr_agent_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against pr_service, feeds
        # tool_result back, and repeats (capped at PR_AGENT_MAX_TOOL_ITERS) until
        # Quinn returns a final text answer, then streams it through the SAME
        # TTS pipeline the default path uses. Every other agent never reaches here.
        full_text     = ""
        actions_taken = []
        not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _pr_agent_producer():
            nonlocal full_text, actions_taken, not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(PR_AGENT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=PR_AGENT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, nc = await _execute_pr_agent_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if nc:
                                not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_pr_agent_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        try:
            yield sse({
                "type":          "actions",
                "actions_taken": actions_taken,
                "not_connected": not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_booking_agent():
        # booking-agent-only path: the SAME Anthropic tool_use loop as Lex, but pointed at
        # BOOKING_AGENT_TOOLS / _execute_booking_agent_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against booking_service, feeds
        # tool_result back, and repeats (capped at BOOKING_AGENT_MAX_TOOL_ITERS) until
        # Avery returns a final text answer, then streams it through the SAME
        # TTS pipeline the default path uses. Every other agent never reaches here.
        full_text     = ""
        actions_taken = []
        not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _booking_agent_producer():
            nonlocal full_text, actions_taken, not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(BOOKING_AGENT_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=BOOKING_AGENT_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, nc = await _execute_booking_agent_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if nc:
                                not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_booking_agent_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        try:
            yield sse({
                "type":          "actions",
                "actions_taken": actions_taken,
                "not_connected": not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_social_manager():
        # social-manager-only path: the SAME Anthropic tool_use loop as Lex, but pointed at
        # SOCIAL_MANAGER_TOOLS / _execute_social_manager_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against social_service, feeds
        # tool_result back, and repeats (capped at SOCIAL_MANAGER_MAX_TOOL_ITERS) until
        # Riley returns a final text answer, then streams it through the SAME
        # TTS pipeline the default path uses. Every other agent never reaches here.
        full_text     = ""
        actions_taken = []
        not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _social_manager_producer():
            nonlocal full_text, actions_taken, not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(SOCIAL_MANAGER_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=SOCIAL_MANAGER_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, nc = await _execute_social_manager_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if nc:
                                not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_social_manager_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        try:
            yield sse({
                "type":          "actions",
                "actions_taken": actions_taken,
                "not_connected": not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    async def generate_release_strategist():
        # release-strategist-only path: the SAME Anthropic tool_use loop as Lex, but pointed at
        # RELEASE_STRATEGIST_TOOLS / _execute_release_strategist_tool instead. Runs the non-streaming messages.create
        # with tools, executes each emitted tool_use against release_service, feeds
        # tool_result back, and repeats (capped at RELEASE_STRATEGIST_MAX_TOOL_ITERS) until
        # Sage returns a final text answer, then streams it through the SAME
        # TTS pipeline the default path uses. Every other agent never reaches here.
        full_text     = ""
        actions_taken = []
        not_connected = False

        tts_in  = asyncio.Queue()
        evt_out = asyncio.Queue()

        async def _release_strategist_producer():
            nonlocal full_text, actions_taken, not_connected
            try:
                loop_messages = list(messages)
                final_text    = ""
                last_text     = ""
                for _ in range(RELEASE_STRATEGIST_MAX_TOOL_ITERS):
                    resp = await async_client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_blocks,
                        messages=loop_messages,
                        tools=RELEASE_STRATEGIST_TOOLS,
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    blocks    = list(getattr(resp, "content", []) or [])
                    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
                    text_parts = [getattr(b, "text", "") for b in blocks
                                  if getattr(b, "type", None) == "text"]
                    last_text = "".join(text_parts).strip() or last_text

                    if getattr(resp, "stop_reason", None) == "tool_use" and tool_uses:
                        loop_messages.append({"role": "assistant", "content": blocks})
                        results_content = []
                        for tu in tool_uses:
                            result, summary, nc = await _execute_release_strategist_tool(
                                tu.name, getattr(tu, "input", {}) or {}, artist_id,
                            )
                            if nc:
                                not_connected = True
                            actions_taken.append({
                                "tool":   tu.name,
                                "input":  summary["input"],
                                "result": summary["result"],
                            })
                            results_content.append({
                                "type":        "tool_result",
                                "tool_use_id": tu.id,
                                "content":     json.dumps(result),
                            })
                        loop_messages.append({"role": "user", "content": results_content})
                        continue

                    final_text = "".join(text_parts).strip()
                    break

                if not final_text:
                    final_text = last_text or "I've taken the actions I can on that for now."
                full_text = final_text

                buf       = final_text
                route_cut = False
                while True:
                    sentence, buf = split_sentence(buf)
                    if not sentence:
                        break
                    await evt_out.put(("text", sentence))
                    if do_tts:
                        await tts_in.put(sentence)
                    if detect_routing(sentence):
                        route_cut = True
                        break
                if not route_cut:
                    remainder = buf.strip()
                    if remainder:
                        await evt_out.put(("text", remainder))
                        if do_tts:
                            await tts_in.put(remainder)
            except Exception as e:
                await evt_out.put(("error", str(e)))
            finally:
                await tts_in.put(None)

        async def _tts_worker():
            task_q = asyncio.Queue()
            first  = True

            async def _submit():
                nonlocal first
                while True:
                    sentence = await tts_in.get()
                    if sentence is None:
                        await task_q.put(None)
                        return
                    if first:
                        await evt_out.put(("status", "Generating voice…"))
                        first = False
                    task = asyncio.create_task(tts(sentence, voice))
                    await task_q.put(task)

            asyncio.create_task(_submit())
            while True:
                item = await task_q.get()
                if item is None:
                    break
                audio_bytes = await item
                if audio_bytes:
                    await evt_out.put(("audio", audio_bytes))
            await evt_out.put(None)

        asyncio.create_task(_release_strategist_producer())
        if do_tts:
            asyncio.create_task(_tts_worker())
        else:
            async def _no_tts_closer():
                while True:
                    s = await tts_in.get()
                    if s is None:
                        break
                await evt_out.put(None)
            asyncio.create_task(_no_tts_closer())

        while True:
            item = await evt_out.get()
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                yield sse({"type": "text", "text": data})
            elif evt_type == "audio":
                yield sse({"type": "audio", "audio": base64.b64encode(data).decode()})
            elif evt_type == "status":
                yield sse({"type": "status", "text": data})
            elif evt_type == "error":
                yield sse({"type": "error", "message": data})
                break

        route = detect_routing(full_text)
        if route:
            slug = route["name"].lower().replace(" ", "-")
            yield sse({
                "type":        "route",
                "agent_id":    route["id"],
                "agent_name":  route["name"],
                "agent_title": route["title"],
                "agent_voice": route["voice"],
                "agent_slug":  slug,
            })
        if experts_event:
            yield sse(experts_event)
        try:
            yield sse({
                "type":          "actions",
                "actions_taken": actions_taken,
                "not_connected": not_connected,
            })
        except Exception:
            pass
        yield sse({"type": "done", "full_text": full_text})

        if full_text and message != "__greet__":
            asyncio.create_task(_save_exchange(artist_id, agent_id, message, full_text))

    # Gate: Marcus (puppet-master), Lex (lex-cipher), Jade (fund-phantom), Ray
    # (rights-pulse), Cleo (border-royalty), Finn (mech-ledger), Victor
    # (vault-keeper), Nadia (ledger-lock), Zara (signal-blaster), Kai
    # (grid-prophet), Luna (vision-forge), Diego (design-studio), Ray B
    # (venue-hawk), Miles (tour-commander), Solo (airwave), Nia (brand-connect),
    # Max (merch-empire), Aria (fan-builder), Sync (sync-agent), Nova
    # (global-scout), and Cree (creative-director) each take their own tool_use
    # path; every other agent takes the unchanged streaming `generate()` path above.
    # M3 consolidation: agents migrated onto the shared helper are looked up here
    # first. An agent absent from the registry keeps its inline generator below.
    _registry_entry = STREAM_AGENT_REGISTRY.get(agent_id)
    if _registry_entry is not None:
        _reg_tools, _reg_execute_tool, _reg_max_iters, _reg_nck = _registry_entry
        def _stream_gen():
            return stream_tool_use_agent(
                tools=_reg_tools,
                execute_tool=_reg_execute_tool,
                max_iters=_reg_max_iters,
                not_connected_key=_reg_nck,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                system_blocks=system_blocks,
                voice=voice,
                do_tts=do_tts,
                artist_id=artist_id,
                agent_id=agent_id,
                message=message,
                experts_event=experts_event,
            )
    elif agent_id == "puppet-master":
        _stream_gen = generate_marcus
    elif agent_id == "lex-cipher":
        _stream_gen = generate_lex_cipher
    elif agent_id == "fund-phantom":
        _stream_gen = generate_fund_phantom
    elif agent_id == "rights-pulse":
        _stream_gen = generate_rights_pulse
    elif agent_id == "border-royalty":
        _stream_gen = generate_border_royalty
    elif agent_id == "mech-ledger":
        _stream_gen = generate_mech_ledger
    elif agent_id == "vault-keeper":
        _stream_gen = generate_vault_keeper
    elif agent_id == "ledger-lock":
        _stream_gen = generate_ledger_lock
    elif agent_id == "signal-blaster":
        _stream_gen = generate_signal_blaster
    elif agent_id == "grid-prophet":
        _stream_gen = generate_grid_prophet
    elif agent_id == "vision-forge":
        _stream_gen = generate_vision_forge
    elif agent_id == "design-studio":
        _stream_gen = generate_design_studio
    elif agent_id == "venue-hawk":
        _stream_gen = generate_venue_hawk
    elif agent_id == "tour-commander":
        _stream_gen = generate_tour_commander
    elif agent_id == "airwave":
        _stream_gen = generate_airwave
    elif agent_id == "brand-connect":
        _stream_gen = generate_brand_connect
    elif agent_id == "merch-empire":
        _stream_gen = generate_merch_empire
    elif agent_id == "fan-builder":
        _stream_gen = generate_fan_builder
    elif agent_id == "sync-agent":
        _stream_gen = generate_sync_agent
    elif agent_id == "global-scout":
        _stream_gen = generate_global_scout
    elif agent_id == "creative-director":
        _stream_gen = generate_creative_director
    elif agent_id == "data-oracle":
        _stream_gen = generate_data_oracle
    elif agent_id == "ar-scout":
        _stream_gen = generate_ar_scout
    elif agent_id == "producer-connect":
        _stream_gen = generate_producer_connect
    elif agent_id == "pr-agent":
        _stream_gen = generate_pr_agent
    elif agent_id == "booking-agent":
        _stream_gen = generate_booking_agent
    elif agent_id == "social-manager":
        _stream_gen = generate_social_manager
    elif agent_id == "release-strategist":
        _stream_gen = generate_release_strategist
    else:
        _stream_gen = generate
    return StreamingResponse(
        _stream_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/api/tts")
async def tts_endpoint(text: str, voice: str = "am_michael"):
    audio_bytes = await tts(text, voice)
    if audio_bytes:
        return Response(content=audio_bytes, media_type="audio/wav")
    return JSONResponse({"error": "TTS unavailable"}, status_code=503)

_TTS_MAX_TEXT_CHARS = int(os.environ.get("TTS_MAX_TEXT_CHARS", "4000"))
_TTS_MAX_CALL_ID_CHARS = 128
_TTS_CANCEL_TTL_SECONDS = float(os.environ.get("TTS_CANCEL_TTL_SECONDS", "300"))
_TTS_MAX_CANCELLED_CALLS = max(1, int(os.environ.get("TTS_MAX_CANCELLED_CALLS", "1000")))
_cancelled_calls: dict[str, float] = {}


def _purge_expired_tts_cancellations():
    now = time.monotonic()
    expired = [call_id for call_id, expires_at in _cancelled_calls.items() if expires_at <= now]
    for call_id in expired:
        _cancelled_calls.pop(call_id, None)


def _validate_tts_call_id(call_id: str, *, required: bool = False):
    if required and not call_id.strip():
        raise HTTPException(status_code=422, detail="call_id is required")
    if len(call_id) > _TTS_MAX_CALL_ID_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"call_id exceeds {_TTS_MAX_CALL_ID_CHARS} character limit",
        )

class TtsSynthRequest(BaseModel):
    text:    str
    voice:   str = "am_onyx"
    call_id: str = ""

class TtsCancelRequest(BaseModel):
    call_id: str

@app.post("/api/tts/cancel")
async def tts_cancel(req: TtsCancelRequest):
    """Mark a call as ended so any in-flight /api/tts/synth for that call returns null."""
    _validate_tts_call_id(req.call_id, required=True)
    _purge_expired_tts_cancellations()
    if req.call_id not in _cancelled_calls and len(_cancelled_calls) >= _TTS_MAX_CANCELLED_CALLS:
        oldest_call_id = min(_cancelled_calls, key=_cancelled_calls.get)
        _cancelled_calls.pop(oldest_call_id, None)
    _cancelled_calls[req.call_id] = time.monotonic() + _TTS_CANCEL_TTL_SECONDS
    return {"cancelled": req.call_id}

@app.post("/api/tts/synth")
async def tts_synth(req: TtsSynthRequest):
    """Synthesize text → base64 WAV. Used by app to bypass SSE buffering."""
    _validate_tts_call_id(req.call_id)
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    if len(text) > _TTS_MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"text exceeds {_TTS_MAX_TEXT_CHARS} character limit",
        )
    if not req.voice.strip() or len(req.voice) > 100:
        raise HTTPException(status_code=422, detail="voice must be between 1 and 100 characters")
    _purge_expired_tts_cancellations()
    if req.call_id and req.call_id in _cancelled_calls:
        _cancelled_calls.pop(req.call_id, None)
        return JSONResponse({"audio": None, "cancelled": True}, status_code=200)
    _tts_last_error.clear()
    audio_bytes = await tts(text, req.voice)
    # Check again — call may have ended while synthesis was running
    if req.call_id and req.call_id in _cancelled_calls:
        _cancelled_calls.pop(req.call_id, None)
        return JSONResponse({"audio": None, "cancelled": True}, status_code=200)
    if audio_bytes:
        return {"audio": base64.b64encode(audio_bytes).decode()}
    detail = _tts_last_error.get("detail", "unknown")
    return JSONResponse({"audio": None, "error": "TTS unavailable", "detail": detail}, status_code=503)

@app.get("/api/tts/status")
async def tts_status():
    """Returns whether TTS is ready. True if Kokoro loaded OR ElevenLabs key present."""
    kokoro_ready = _kokoro_available is True
    el_ready     = bool(ELEVENLABS_API_KEY)
    return {"ready": kokoro_ready or el_ready, "engine": "kokoro" if kokoro_ready else ("elevenlabs" if el_ready else "none")}

@app.get("/api/health")
async def api_health():
    tts_engine = "kokoro" if get_kokoro() else ("elevenlabs" if ELEVENLABS_API_KEY else "none")
    return {"status": "ok", "version": "2.2.1", "tts": tts_engine, "agents": len(AGENTS)}

app.mount("/static", StaticFiles(directory=str(_BASE / "static"), html=True), name="static")


# ── Artist save ────────────────────────────────────────────────────────────────
from pydantic import BaseModel

class ArtistProfile(BaseModel):
    artist_id: str
    name: str
    country: str = ""
    genres: list[str] = []
    monthly_listeners: str = ""
    tier: str = "Gold"
    onboarded: bool = False
    bio: str = ""
    photo: Optional[str] = None

@app.post("/api/artist/save")
async def save_artist(profile: ArtistProfile, request: Request):
    try:
        scoped_artist_id = _require_artist_scope(request, profile.artist_id)
        existing = load_artist(scoped_artist_id)

        existing.update({
            "artist_id": scoped_artist_id,
            "artist_name": profile.name,
            "country": profile.country,
            "genres": profile.genres,
            "monthly_listeners": profile.monthly_listeners,
            "tier": profile.tier,
            "onboarded": profile.onboarded,
            "bio": profile.bio,
            "photo": profile.photo,
        })

        _save_artist_file(scoped_artist_id, existing)
        print(f"[ARTIST] Saved profile for {profile.name} ({scoped_artist_id})")
        return {"status": "ok", "artist_id": scoped_artist_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ── Billing upgrade ────────────────────────────────────────────────────────────
class BillingUpgrade(BaseModel):
    artist_id: str
    tier: str

@app.post("/api/billing/upgrade")
async def billing_upgrade(payload: BillingUpgrade):
    try:
        if payload.tier not in ("Starter", "Gold", "Platinum", "Diamond"):
            raise HTTPException(status_code=400, detail="Invalid tier")

        existing = load_artist(payload.artist_id)
        existing["artist_id"] = payload.artist_id
        existing["tier"] = payload.tier
        _save_artist_file(payload.artist_id, existing)
        print(f"[BILLING] {payload.artist_id} upgraded to {payload.tier}")
        return {"status": "ok", "tier": payload.tier}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Conversation history read ──────────────────────────────────────────────────
@app.get("/api/history")
async def get_history(artist_id: str, agent_id: str, request: Request):
    scoped_artist_id = _require_artist_scope(request, artist_id)
    try:
        if not DB_PATH.exists():
            return {"history": []}
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM messages "
            "WHERE artist_id=? AND agent_id=? ORDER BY id ASC LIMIT 40",
            (scoped_artist_id, agent_id),
        )
        rows = cur.fetchall()
        conn.close()
        return {"history": [{"role": row[0], "content": row[1]} for row in rows]}
    except Exception as e:
        print(f"[HISTORY] error: {e}")
        raise HTTPException(status_code=503, detail="Conversation history unavailable")



# ── Artist lookup by name ──────────────────────────────────────────────────────
@app.get("/api/artist/lookup")
async def lookup_artist(name: str, request: Request):
    """Return only the authenticated artist's own profile."""
    try:
        if identity_configured():
            artist_id = _authenticated_artist_id(request)
            profile = load_artist(artist_id)
            if profile.get("artist_name", "").lower() != name.lower().strip():
                return {"found": False}
            profiles = [profile]
        elif DATABASE_URL:
            profiles = _pg_all()
        else:
            profiles = _sqlite_all_artists()

        target = name.lower().strip()
        for profile in profiles:
            if profile.get("artist_name", "").lower() == target:
                return {
                    "found": True,
                    "artist_id": profile.get("artist_id"),
                    "name": profile.get("artist_name"),
                    "tier": profile.get("tier", "Gold"),
                    "genres": profile.get("genres", []),
                    "country": profile.get("country", ""),
                    "monthly_listeners": profile.get("monthly_listeners", ""),
                    "bio": profile.get("bio", ""),
                    "photo": profile.get("photo", None),
                    "onboarded": profile.get("onboarded", False),
                }
        return {"found": False}
    except HTTPException:
        raise
    except Exception as e:
        return {"found": False, "error": str(e)}



# ── SMS OTP auth ───────────────────────────────────────────────────────────────
# In-memory store: { normalized_phone: { "otp": "123456", "expires": float } }
_otp_store: dict = {}
OTP_EXPIRY_SECONDS = 600  # 10 minutes


def _normalize_phone(raw: str) -> str:
    """Strip everything except digits, then prepend +."""
    digits = re.sub(r'\D', '', raw.strip())
    return '+' + digits


def _clean_otp_store():
    now = time.time()
    expired = [k for k, v in _otp_store.items() if v["expires"] < now]
    for k in expired:
        del _otp_store[k]


class SendOtpRequest(BaseModel):
    phone: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str


@app.post("/api/auth/send-otp")
async def send_otp(payload: SendOtpRequest):
    """Send a 6-digit OTP via Twilio SMS. Requires TWILIO_* env vars."""
    try:
        _clean_otp_store()

        phone = _normalize_phone(payload.phone)
        if len(phone) < 8:
            raise HTTPException(status_code=400, detail="Invalid phone number")

        # R-17 dev bypass: skip Twilio and store a fixed dev code
        if SMS_OTP_DEV_BYPASS:
            dev_otp = "000000"
            _otp_store[phone] = {"otp": dev_otp, "expires": time.time() + OTP_EXPIRY_SECONDS}
            log.warning("boot_warning", extra={
                "event":  "boot_warning",
                "key":    "SMS_OTP_DEV_BYPASS",
                "detail": f"dev OTP 000000 stored for ...{phone[-4:]} without SMS send",
            })
            return {"status": "ok", "message": "Dev bypass — code is 000000"}

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_PHONE_NUMBER")

        if not all([account_sid, auth_token, from_number]):
            raise HTTPException(status_code=503, detail="SMS service not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER")

        # Strip whitespace — Railway env vars can have trailing newlines/spaces
        auth_token  = (auth_token  or "").strip()
        account_sid = (account_sid or "").strip()
        from_number = (from_number or "").strip()

        # Validate auth token format BEFORE storing OTP (R-17 fix: was after)
        if not (auth_token and len(auth_token) == 32 and re.fullmatch(r"[0-9a-f]+", auth_token.lower())):
            raise HTTPException(
                status_code=503,
                detail=f"SMS not configured — TWILIO_AUTH_TOKEN must be exactly 32 lowercase hex characters (got {len(auth_token)} chars). Set the correct token in Railway env vars."
            )

        otp = str(secrets.randbelow(1000000)).zfill(6)
        _otp_store[phone] = {"otp": otp, "expires": time.time() + OTP_EXPIRY_SECONDS}

        from twilio.rest import Client as TwilioClient
        twilio = TwilioClient(account_sid, auth_token)
        twilio.messages.create(
            body=f"Your PLMKR code is {otp}. Valid for 10 minutes.",
            from_=from_number,
            to=phone,
        )

        print(f"[OTP] Sent to ...{phone[-4:]}")
        return {"status": "ok", "message": "Code sent"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[OTP] Send error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/verify-otp")
async def verify_otp(payload: VerifyOtpRequest):
    """Verify OTP and issue the canonical signed artist session."""
    phone = _normalize_phone(payload.phone)
    entry = _otp_store.get(phone)

    if not entry:
        return {
            "valid": False,
            "reason": "No code found for this number. Please request a new code.",
        }

    if time.time() > entry["expires"]:
        del _otp_store[phone]
        return {"valid": False, "reason": "Code expired. Please request a new one."}

    if entry["otp"] != payload.code.strip():
        return {"valid": False, "reason": "Incorrect code. Try again."}

    del _otp_store[phone]

    # Preserve old local behavior until the two identity secrets are configured.
    if not identity_configured():
        return {"valid": True}

    fingerprint = phone_fingerprint(phone)
    matching_profile = None

    profiles = _pg_all() if DATABASE_URL else _sqlite_all_artists()
    for profile in profiles:
        if secrets.compare_digest(
            str(profile.get("identity_phone_hash", "")),
            fingerprint,
        ):
            matching_profile = profile
            break

    if matching_profile:
        artist_id = matching_profile["artist_id"]
        profile = matching_profile
        returning_artist = bool(profile.get("onboarded"))
    else:
        artist_id = new_artist_id()
        profile = {
            "artist_id": artist_id,
            "identity_phone_hash": fingerprint,
            "phone_verified_at": int(time.time()),
            "onboarded": False,
        }
        _save_artist_file(artist_id, profile)
        returning_artist = False

    session = issue_session(artist_id)
    return {
        "valid": True,
        "artist_id": artist_id,
        "returning_artist": returning_artist,
        "profile": {
            "artist_id": artist_id,
            "name": profile.get("artist_name", ""),
            "tier": profile.get("tier", "Gold"),
            "genres": profile.get("genres", []),
            "country": profile.get("country", ""),
            "monthly_listeners": profile.get("monthly_listeners", ""),
            "bio": profile.get("bio", ""),
            "photo": profile.get("photo"),
            "onboarded": bool(profile.get("onboarded")),
        },
        **session,
    }



# ── Push notifications ─────────────────────────────────────────────────────────
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


class RegisterTokenRequest(BaseModel):
    artist_id: str
    push_token: str


class SendNotificationRequest(BaseModel):
    artist_id: str
    title: str
    body: str
    agent_id: str = ""
    data: dict = {}


def _load_artist_file(artist_id: str) -> tuple:
    """Return (artist_id, data). artist_id is passed through as the key for _save_artist_file."""
    if DATABASE_URL:
        return artist_id, _pg_get(artist_id)
    return artist_id, _sqlite_get_artist(artist_id)


def _save_artist_file(path_or_id, data: dict):
    if DATABASE_URL:
        _pg_put(data["artist_id"], data)
        return
    _sqlite_put_artist(data["artist_id"], data)


@app.post("/api/notifications/register")
async def register_push_token(payload: RegisterTokenRequest):
    """Save Expo push token to the artist's JSON file."""
    try:
        path, data = _load_artist_file(payload.artist_id)
        data["push_token"] = payload.push_token
        _save_artist_file(path, data)
        print(f"[PUSH] Registered token for {payload.artist_id}")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/notifications/send")
async def send_notification(payload: SendNotificationRequest):
    """Send a push notification to an artist via Expo push API and store in history."""
    try:
        path, data = _load_artist_file(payload.artist_id)
        push_token = data.get("push_token", "")

        result = {"status": "ok", "delivered": False, "artist_id": payload.artist_id}

        if push_token:
            message = {
                "to":    push_token,
                "title": payload.title,
                "body":  payload.body,
                "sound": "default",
                "data":  payload.data or {},
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    EXPO_PUSH_URL,
                    json=message,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
            resp_data  = resp.json()
            data_field = resp_data.get("data", {})
            # Single send returns dict; batch returns list — handle both
            if isinstance(data_field, list):
                expo_status = data_field[0].get("status", "unknown") if data_field else "unknown"
            else:
                expo_status = data_field.get("status", "unknown")
            result["delivered"] = expo_status == "ok"
            result["expo_status"] = expo_status
            print(f"[PUSH] Sent to {payload.artist_id}: {expo_status}")
        else:
            result["note"] = "No push token registered for this artist"
            print(f"[PUSH] No token for {payload.artist_id} — notification stored only")

        # Always append to notification history regardless of delivery
        notif_entry = {
            "id":       hashlib.md5(f"{payload.artist_id}{payload.title}{time.time()}".encode()).hexdigest()[:12],
            "type":     "agent" if payload.agent_id else "system",
            "agent_id": payload.agent_id,
            "title":    payload.title,
            "body":     payload.body,
            "sent_at":  time.time(),
            "read":     False,
        }
        data.setdefault("notifications", []).insert(0, notif_entry)
        # Keep last 100 notifications
        data["notifications"] = data["notifications"][:100]
        _save_artist_file(path, data)

        return result

    except Exception as e:
        print(f"[PUSH] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/notifications/{artist_id}")
async def get_notifications(artist_id: str):
    """Return the artist's notification history from their JSON file."""
    try:
        _, data = _load_artist_file(artist_id)
        return {"notifications": data.get("notifications", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Stripe billing ─────────────────────────────────────────────────────────────
import stripe as stripe_lib


STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_DEV_ALLOW_UNSIGNED = os.environ.get("STRIPE_DEV_ALLOW_UNSIGNED", "").lower() == "true"
STRIPE_AVAILABLE         = bool(STRIPE_SECRET_KEY)
APP_BASE_URL             = os.environ.get("APP_BASE_URL") or None   # R-11: None if unset
_RAILWAY_ENVIRONMENT    = os.environ.get("RAILWAY_ENVIRONMENT", "")

# Refuse to start if the dev bypass is active in a Railway production environment.
# STRIPE_DEV_ALLOW_UNSIGNED skips webhook signature verification — safe in local dev,
# catastrophic if left on in production (accepts forged events from anyone).
_on_railway = bool(_RAILWAY_ENVIRONMENT)

# R-11: hard-fail in production if APP_BASE_URL is unset; fall back in dev.
if APP_BASE_URL is None:
    if _on_railway:
        print("=" * 60)
        print("FATAL: APP_BASE_URL is not set on Railway.")
        print("  OAuth redirect URIs, billing links, and audio URLs require")
        print("  a public HTTPS base URL. Set APP_BASE_URL in Railway Variables:")
        print("  e.g. https://your-service.up.railway.app")
        print("=" * 60)
        import sys
        sys.exit(1)
    else:
        APP_BASE_URL = "http://localhost:8000"
        log.warning("boot_warning", extra={
            "event":  "boot_warning",
            "key":    "APP_BASE_URL",
            "detail": "unset — falling back to http://localhost:8000 (set before deploying to Railway)",
        })

if _on_railway and STRIPE_DEV_ALLOW_UNSIGNED:
    print("=" * 60)
    print("FATAL: STRIPE_DEV_ALLOW_UNSIGNED=true detected on Railway.")
    print(f"  RAILWAY_ENVIRONMENT={_RAILWAY_ENVIRONMENT!r}")
    print("  This bypasses webhook signature verification in production.")
    print("  Unset STRIPE_DEV_ALLOW_UNSIGNED before deploying.")
    print("=" * 60)
    import sys
    sys.exit(1)
if STRIPE_AVAILABLE:
    stripe_lib.api_key = STRIPE_SECRET_KEY

# R-17: SMS_OTP_DEV_BYPASS allows send-otp to succeed in local dev without real Twilio.
SMS_OTP_DEV_BYPASS = os.environ.get("SMS_OTP_DEV_BYPASS", "").lower() == "true"
if _on_railway and SMS_OTP_DEV_BYPASS:
    print("=" * 60)
    print("FATAL: SMS_OTP_DEV_BYPASS=true detected on Railway.")
    print(f"  RAILWAY_ENVIRONMENT={_RAILWAY_ENVIRONMENT!r}")
    print("  This bypasses SMS verification in production.")
    print("  Unset SMS_OTP_DEV_BYPASS before deploying.")
    print("=" * 60)
    import sys
    sys.exit(1)
if SMS_OTP_DEV_BYPASS:
    log.warning("boot_warning", extra={
        "event":  "boot_warning",
        "key":    "SMS_OTP_DEV_BYPASS",
        "detail": "OTP bypass active — send-otp stores dev code without calling Twilio",
    })

if not STRIPE_WEBHOOK_SECRET and not STRIPE_DEV_ALLOW_UNSIGNED:
    print("[STRIPE] WARNING: STRIPE_WEBHOOK_SECRET not set and STRIPE_DEV_ALLOW_UNSIGNED not enabled — "
          "webhook endpoint will reject all incoming events with HTTP 400")


def _verify_stripe_event(body: bytes, sig_header: str, webhook_secret: str, dev_allow_unsigned: bool):
    """Verify a Stripe webhook event. Raises HTTPException on failure.

    Production: webhook_secret must be set — unsigned events are rejected.
    Dev only:   set STRIPE_DEV_ALLOW_UNSIGNED=true to accept unsigned events.
    """
    if webhook_secret:
        return stripe_lib.Webhook.construct_event(body, sig_header, webhook_secret)
    if dev_allow_unsigned:
        print("[STRIPE] WARNING: accepting unsigned webhook event — STRIPE_DEV_ALLOW_UNSIGNED=true")
        return stripe_lib.Event.construct_from(json.loads(body), stripe_lib.api_key)
    raise HTTPException(
        status_code=400,
        detail="Webhook signature verification required — set STRIPE_WEBHOOK_SECRET",
    )

TIER_PRICES = {
    "Starter":  2900,    # $29.00 in cents
    "Gold":     4900,    # $49.00
    "Platinum": 9900,    # $99.00
    "Diamond":  19900,   # $199.00
}

# Pre-created Stripe Price IDs (recurring monthly, test mode)
PRICE_IDS = {
    "Starter":  "price_1TC7wsKcfMmE6BHPtCwDbkWD",
    "Gold":     "price_1TC7xHKcfMmE6BHPqa0aU1f2",
    "Platinum": "price_1TC7y0KcfMmE6BHPEzJ55j7E",
    "Diamond":  "price_1TC7ySKcfMmE6BHPvwN08fki",
}

class CheckoutRequest(BaseModel):
    artist_id: str
    tier: str

def _add_billing_history(data: dict, tier: str, amount: float):
    entry = {
        "id":     f"stripe_{int(time.time())}",
        "date":   time.strftime("%b %-d, %Y"),
        "amount": amount,
        "tier":   tier,
        "status": "paid",
    }
    data.setdefault("billing_history", []).insert(0, entry)
    data["billing_history"] = data["billing_history"][:50]

@app.post("/api/billing/create-checkout")
async def create_checkout(payload: CheckoutRequest):
    if not STRIPE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Billing unavailable — STRIPE_SECRET_KEY not set")
    if payload.tier not in PRICE_IDS:
        raise HTTPException(status_code=400, detail="Invalid tier")
    try:
        session = stripe_lib.checkout.Session.create(
            mode="subscription",
            line_items=[{
                "price": PRICE_IDS[payload.tier],
                "quantity": 1,
            }],
            client_reference_id=payload.artist_id,
            metadata={"artist_id": payload.artist_id, "tier": payload.tier},
            success_url=f"{APP_BASE_URL}/static/billing-success.html",
            cancel_url=f"{APP_BASE_URL}/static/billing-cancel.html",
        )
        print(f"[STRIPE] checkout session created for {payload.artist_id} → {payload.tier}")
        return {"url": session.url, "session_id": session.id}
    except stripe_lib.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e.user_message or e))

@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    if not STRIPE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Billing unavailable")
    body       = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:

        event = _verify_stripe_event(body, sig_header, STRIPE_WEBHOOK_SECRET, STRIPE_DEV_ALLOW_UNSIGNED)


    except stripe_lib.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    etype = event["type"]
    data  = event["data"]["object"]
    print(f"[STRIPE] webhook: {etype}")

    if etype == "checkout.session.completed":
        artist_id = data.get("client_reference_id") or data.get("metadata", {}).get("artist_id", "")
        tier      = data.get("metadata", {}).get("tier", "")
        if artist_id and tier and tier in TIER_PRICES:
            path, adata = _load_artist_file(artist_id)
            adata["tier"]                = tier
            adata["subscription_id"]     = data.get("subscription", "")
            adata["subscription_status"] = "active"
            _add_billing_history(adata, tier, TIER_PRICES[tier] / 100)
            _save_artist_file(path, adata)
            print(f"[STRIPE] {artist_id} → {tier} active")

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data.get("id", "")
        status = data.get("status", "")
        if DATABASE_URL:
            all_artists = _pg_all()
        else:
            all_artists = _sqlite_all_artists()
        for adata in all_artists:
            try:
                if adata.get("subscription_id") == sub_id:
                    if etype == "customer.subscription.deleted" or status == "canceled":
                        adata["subscription_status"] = "canceled"
                        adata["tier"] = "Gold"
                    else:
                        adata["subscription_status"] = status
                    _save_artist_file(adata["artist_id"], adata)
                    print(f"[STRIPE] sub {sub_id} → {status}")
                    break
            except Exception:
                pass

    return {"received": True}

@app.get("/api/billing/history")
async def get_billing_history(artist_id: str):
    try:
        _, data = _load_artist_file(artist_id)
        return {"history": data.get("billing_history", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── D-ID talking avatar ────────────────────────────────────────────────────────
import uuid as _uuid_mod
import numpy as _np

D_ID_API_KEY   = os.environ.get("D_ID_API_KEY", "")
D_ID_AVAILABLE = bool(D_ID_API_KEY)
TEMP_AUDIO_DIR = Path(os.environ.get("TEMP_AUDIO_DIR", _BASE / "static/temp_audio"))
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Cache: agent_id → D-ID image URL (uploaded once, reused)
_did_image_cache: dict = {}

def _did_auth() -> str:
    """D-ID Basic auth header value: base64(api_key:)"""
    return "Basic " + base64.b64encode(f"{D_ID_API_KEY}:".encode()).decode()

async def _ensure_did_image(agent_id: str) -> str:
    """Upload agent photo to D-ID once; return their CDN URL."""
    if agent_id in _did_image_cache:
        return _did_image_cache[agent_id]

    img_path = _BASE / "static" / "agents" / f"{agent_id}.jpg"
    if not img_path.exists():
        # Agent has no photo — use APP_BASE_URL served image (may not be public)
        url = f"{APP_BASE_URL}/static/agents/{agent_id}.jpg"
        _did_image_cache[agent_id] = url
        return url

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.d-id.com/images",
                headers={"Authorization": _did_auth()},
                files={"image": (f"{agent_id}.jpg", img_path.read_bytes(), "image/jpeg")},
            )
        if resp.status_code in (200, 201):
            url = resp.json().get("url", "")
            _did_image_cache[agent_id] = url
            print(f"[D-ID] uploaded image for {agent_id}: {url}")
            return url
    except Exception as e:
        print(f"[D-ID] image upload failed for {agent_id}: {e}")

    # Fallback: direct URL (works only if APP_BASE_URL is publicly reachable)
    url = f"{APP_BASE_URL}/static/agents/{agent_id}.jpg"
    _did_image_cache[agent_id] = url
    return url

def _concat_wav_chunks(chunks: list) -> bytes:
    """Concatenate list of base64-encoded WAV chunks into a single WAV."""
    import soundfile as sf
    all_samples = []
    sample_rate = 24000  # Kokoro default
    for b64_chunk in chunks:
        raw = base64.b64decode(b64_chunk)
        buf = io.BytesIO(raw)
        try:
            samples, sr = sf.read(buf)
            sample_rate = sr
            if samples.ndim > 1:
                samples = samples.mean(axis=1)  # stereo → mono
            all_samples.append(samples.astype(_np.float32))
        except Exception:
            pass
    if not all_samples:
        return b""
    combined = _np.concatenate(all_samples)
    out = io.BytesIO()
    sf.write(out, combined, sample_rate, format="WAV")
    return out.getvalue()

class AvatarTalkRequest(BaseModel):
    agent_id: str
    audio_chunks: list  # list of base64 WAV strings

@app.post("/api/avatar/talk")
async def avatar_talk(payload: AvatarTalkRequest):
    if not D_ID_AVAILABLE:
        raise HTTPException(status_code=503, detail="D-ID not configured — set D_ID_API_KEY")
    if not payload.audio_chunks:
        raise HTTPException(status_code=400, detail="No audio chunks provided")

    # Concatenate WAV chunks and save to temp file
    loop = asyncio.get_event_loop()
    audio_bytes = await loop.run_in_executor(None, _concat_wav_chunks, payload.audio_chunks)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Could not decode audio chunks")

    audio_id   = str(_uuid_mod.uuid4())[:12]
    audio_path = TEMP_AUDIO_DIR / f"{audio_id}.wav"
    audio_path.write_bytes(audio_bytes)
    audio_url  = f"{APP_BASE_URL}/static/temp_audio/{audio_id}.wav"

    # Ensure agent image is uploaded to D-ID
    source_url = await _ensure_did_image(payload.agent_id)

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            # Create talk
            create_resp = await client.post(
                "https://api.d-id.com/talks",
                headers={"Authorization": _did_auth(), "Content-Type": "application/json"},
                json={
                    "source_url": source_url,
                    "script": {
                        "type": "audio",
                        "audio_url": audio_url,
                    },
                    "config": {
                        "fluent": True,
                        "pad_audio": 0.0,
                        "stitch": True,
                    },
                },
            )
            if create_resp.status_code not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail=f"D-ID create error {create_resp.status_code}: {create_resp.text[:300]}",
                )
            talk_id = create_resp.json().get("id", "")
            print(f"[D-ID] talk {talk_id} created for agent={payload.agent_id}")

            # Poll until done (max 60 s)
            for attempt in range(60):
                await asyncio.sleep(1)
                poll = await client.get(
                    f"https://api.d-id.com/talks/{talk_id}",
                    headers={"Authorization": _did_auth()},
                )
                data   = poll.json()
                status = data.get("status", "")
                if status == "done":
                    video_url = data.get("result_url", "")
                    print(f"[D-ID] talk {talk_id} ready in {attempt+1}s → {video_url}")
                    try:
                        audio_path.unlink()
                    except Exception:
                        pass
                    return {"video_url": video_url, "talk_id": talk_id}
                elif status in ("error", "rejected"):
                    raise HTTPException(
                        status_code=502,
                        detail=f"D-ID failed: {data.get('error', {}).get('description', status)}",
                    )

            raise HTTPException(status_code=504, detail="D-ID timeout — talk not ready in 60s")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"D-ID error: {e}")
    finally:
        # Clean up temp audio on any error path
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception:
            pass

@app.get("/api/avatar/status")
async def avatar_status():
    """Check if D-ID avatar feature is available."""
    return {"available": D_ID_AVAILABLE}


# ── A&R Deep Assessment ────────────────────────────────────────────────────────

class ArScoutArtistInput(BaseModel):
    """Artist-level fields for the assessment. Name is taken from release/track
    metadata, NOT from the Playmaker account profile, to prevent account-name
    vs. credited-artist mismatches."""
    name:             str
    genre:            str
    stage:            str   = "emerging"   # emerging|developing|established|superstar
    territory:        str   = "unknown"
    monthly_listeners: Optional[int]   = None
    save_rate:        Optional[float]  = None  # 0.0–1.0
    release_count:    int   = 0
    manager:          Optional[str]    = None
    label:            Optional[str]    = None

class ArScoutTrackInput(BaseModel):
    title:            str
    bpm:              Optional[float]  = None
    duration_sec:     Optional[float]  = None
    lufs:             Optional[float]  = None
    intro_length_sec: Optional[float]  = None
    genre:            Optional[str]    = None
    features:         list[str]        = []
    release_date:     Optional[str]    = None  # YYYY-MM-DD if already released

class ArScoutAssessRequest(BaseModel):
    artist_id:       str         = ""   # Playmaker account id (for context; identity from artist field)
    artist:          ArScoutArtistInput
    track:           ArScoutTrackInput
    evaluation_stage: str        = "watching"  # watching|approach|due_diligence|internal_pitch
    additional_notes: str        = ""

_AR_SCOUT_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist": None,   # filled at runtime
    "track": None,    # filled at runtime
    "evaluation_stage": None,
    "assessment": {
        "pillars": {
            "music_quality":        {"grade": "B+", "numeric": 8.0, "confidence": "PARTIAL",
                                     "evaluable": ["emotional_impact (JUDGED)", "hook_speed (JUDGED)", "vocal_performance (JUDGED)"],
                                     "not_evaluable": ["production_quality — no audio analysis; confidence capped at PARTIAL",
                                                       "dynamic_progression — no audio analysis",
                                                       "chorus_lift — no audio analysis"]},
            "artist_identity":      {"grade": "B",  "numeric": 7.0, "confidence": "PARTIAL",
                                     "evaluable": ["authenticity (JUDGED)", "uniqueness (JUDGED)", "storytelling (JUDGED)"],
                                     "not_evaluable": ["interview_personality — no live/interview material",
                                                       "fan_connection — no platform analytics provided"]},
            "audience_market":      {"grade": "C+", "numeric": 6.0, "confidence": "LOW",
                                     "evaluable": ["release_count (SOURCED)"],
                                     "not_evaluable": ["save_rate — not provided",
                                                       "monthly_listener_growth — no trajectory data",
                                                       "repeat_listening — no analytics provided"]},
            "execution_team":       {"grade": "C",  "numeric": 5.0, "confidence": "PARTIAL",
                                     "evaluable": ["release_consistency (SOURCED)"],
                                     "not_evaluable": ["manager_quality — manager not identified",
                                                       "responsiveness — NOT EVALUABLE without direct contact",
                                                       "financial_discipline — NOT EVALUABLE without direct relationship"]},
            "commercial_opportunity":{"grade": "B-", "numeric": 6.5, "confidence": "PARTIAL",
                                      "evaluable": ["playlist (JUDGED)", "sync (JUDGED)"],
                                      "not_evaluable": ["radio — format fit requires measured audio data",
                                                        "international — territory data not provided"]}
        },
        "composite": {
            "value": 6.7,
            "formula": "(8.0×0.30) + (7.0×0.25) + (6.0×0.20) + (5.0×0.15) + (6.5×0.10)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked evaluations in feedback/outcomes/"
        },
        "hard_gates": {
            "gate1_production_floor": "CLEAR — production not evaluable without audio, but no hard-fail signal present",
            "gate2_identity_floor":   "CLEAR — identity signals present and evaluable",
            "gate3_no_commercial_lane":"CLEAR — at least one commercial pathway identifiable",
            "gate4_execution_floor":  "CLEAR — execution signals present; team gaps noted"
        },
        "verdict": "DEVELOP",
        "verdict_rationale": "Composite 6.7 falls in 6.5–7.9 range. Trajectory data is NOT EVALUABLE (no analytics provided), defaulting to DEVELOP split. Execution & Team grade C — team gaps require active development investment before pipeline advancement.",
        "trajectory": {
            "monthly_listener_growth": "NOT EVALUABLE — no analytics provided",
            "save_rate_trend":         "NOT EVALUABLE — no release history provided",
            "repeat_listening":        "NOT EVALUABLE",
            "release_consistency":     "Neutral — release count noted but cadence not evaluable from input",
            "trajectory_grade":        "Weak (data absent — defaults to DEVELOP split)"
        },
        "unfair_advantage": {
            "identified": [],
            "assessment": "None identified from available materials. Direct working-relationship evidence would be required to assess elite work ethic or exceptional live show."
        },
        "career_ceiling": {
            "tier": "National",
            "confidence": "Low",
            "reason": "Genre and stage suggest national ceiling is achievable; insufficient trajectory data to support International claim. Confidence LOW — capped by NOT EVALUABLE audience signals.",
            "catalyst_for_next_tier": "Documented positive save-rate trend across 2+ releases + at least one editorial playlist add would move confidence to Medium for International ceiling."
        },
        "risk_assessment": {
            "execution":       {"level": "Medium", "reason": "No manager identified; release cadence history not evaluable from input."},
            "financial":       {"level": "Medium", "reason": "Investment case realistic at National ceiling; recoupment plausible with consistent release cadence."},
            "reputation":      {"level": "Low",    "reason": "No known controversies or brand risks identified from available materials."},
            "team":            {"level": "High",   "reason": "Manager not identified; team qualification for development phase NOT EVALUABLE."},
            "legal":           {"level": "Medium", "reason": "IP ownership and deal encumbrances not confirmed from available materials."},
            "burnout":         {"level": "Low",    "reason": "No evidence of creative or personal overextension from available materials."},
            "ai_displacement": {"level": "Medium", "reason": "Genre-specific differentiation not yet fully established; identity moat requires further development."},
            "trend_dependency":{"level": "Medium", "reason": "Genre trajectory not assessed — insufficient market data in input."}
        },
        "five_year_test": {
            "question": "If this artist debuts today, could they still matter five years from now after trends change, algorithms change, and AI-generated music becomes ubiquitous?",
            "answer":   "CONDITIONAL — the identity signals present are promising but insufficiently documented. If the artist establishes an ownable sonic and narrative identity before the first major release (identity clarity test from artist-development.md), and executes a consistent waterfall release cadence, the foundation for a 5-year career is buildable. Specific condition: artist must be able to articulate their identity in one sentence without genre clichés before proceeding to Phase 2. Until that condition clears, the five-year case rests on potential, not demonstrated differentiation."
        },
        "owner_input_pending": "Identity and Execution assessment based on available materials only — direct relationship signals (responsiveness, financial discipline, professionalism) NOT EVALUABLE at this stage.",
        "remediation_priorities": [
            "Identify and engage a manager with demonstrated development-to-breakthrough track record — Hard Gate 4 risk zone until team is in place.",
            "Provide platform analytics (Spotify for Artists / Apple Music) for save-rate, monthly listener trajectory, and repeat-listening data — three pillars currently at LOW or PARTIAL confidence.",
            "Run the identity clarity test: can the artist articulate who they are in one sentence without genre clichés? If not, identity development precedes release.",
        ]
    },
    "model": "claude-sonnet-4-6",
    "mock_note": "AR_SCOUT_MOCK_MODE=true — this is a canned assessment. Set AR_SCOUT_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}

def _render_ar_scorecard_html(assessment: dict, artist: dict, track: dict) -> str:
    """Render an A&R assessment dict as a standalone HTML scorecard page."""
    pillar_color = {
        "A+": "#16a34a", "A": "#22c55e", "A-": "#4ade80",
        "B+": "#84cc16", "B": "#a3e635", "B-": "#d9f99d",
        "C+": "#facc15", "C": "#fbbf24", "C-": "#f59e0b",
        "D+": "#f97316", "D": "#ef4444", "D-": "#dc2626",
        "F":  "#991b1b",
    }
    verdict_color = {
        "PASS": "#991b1b", "WATCH": "#b45309", "DEVELOP": "#1d4ed8",
        "PURSUE": "#7c3aed", "GREENLIGHT": "#15803d", "SIGN IMMEDIATELY": "#065f46",
    }

    pillars      = assessment.get("pillars", {})
    composite    = assessment.get("composite", {})
    verdict      = assessment.get("verdict", "UNKNOWN")
    ceiling      = assessment.get("career_ceiling", {})
    risks        = assessment.get("risk_assessment", {})
    fyt          = assessment.get("five_year_test", {})
    trajectory   = assessment.get("trajectory", {})
    unfair_adv   = assessment.get("unfair_advantage", {})
    remediation  = assessment.get("remediation_priorities", [])
    hard_gates   = assessment.get("hard_gates", {})

    pillar_rows = ""
    pillar_names = {
        "music_quality": "Music Quality (0.30)",
        "artist_identity": "Artist Identity & Brand (0.25)",
        "audience_market": "Audience & Market (0.20)",
        "execution_team": "Execution & Team (0.15)",
        "commercial_opportunity": "Commercial Opportunity (0.10)",
    }
    for key, label in pillar_names.items():
        p = pillars.get(key, {})
        grade = p.get("grade", "?")
        color = pillar_color.get(grade, "#6b7280")
        conf  = p.get("confidence", "?")
        not_ev = "; ".join(p.get("not_evaluable", [])[:2]) or "—"
        pillar_rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:600;">{label}</td>
          <td style="padding:8px 12px;text-align:center;">
            <span style="background:{color};color:#fff;padding:3px 10px;border-radius:4px;font-weight:700;">{grade}</span>
          </td>
          <td style="padding:8px 12px;font-size:0.85em;color:#6b7280;">{conf}</td>
          <td style="padding:8px 12px;font-size:0.82em;color:#9ca3af;">{not_ev}</td>
        </tr>"""

    gate_rows = ""
    gate_labels = {
        "gate1_production_floor": "Gate 1 — Production Floor",
        "gate2_identity_floor":   "Gate 2 — Identity Floor",
        "gate3_no_commercial_lane":"Gate 3 — No Commercial Lane",
        "gate4_execution_floor":  "Gate 4 — Execution & Team Floor",
    }
    for k, label in gate_labels.items():
        v = hard_gates.get(k, "NOT ASSESSED")
        triggered = "TRIGGERED" in v.upper()
        color = "#991b1b" if triggered else "#15803d"
        badge = "TRIGGERED" if triggered else "CLEAR"
        gate_rows += f"""
        <tr>
          <td style="padding:6px 12px;font-size:0.9em;">{label}</td>
          <td style="padding:6px 12px;">
            <span style="background:{color};color:#fff;padding:2px 8px;border-radius:3px;font-size:0.8em;font-weight:700;">{badge}</span>
          </td>
          <td style="padding:6px 12px;font-size:0.82em;color:#6b7280;">{v[:80]}{'…' if len(v)>80 else ''}</td>
        </tr>"""

    risk_rows = ""
    risk_level_color = {"Low": "#15803d", "Medium": "#b45309", "High": "#991b1b"}
    for cat, data in risks.items():
        level  = data.get("level", "?")
        reason = data.get("reason", "")
        color  = risk_level_color.get(level, "#6b7280")
        risk_rows += f"""
        <tr>
          <td style="padding:6px 12px;font-size:0.9em;text-transform:capitalize;">{cat.replace('_',' ')}</td>
          <td style="padding:6px 12px;">
            <span style="background:{color};color:#fff;padding:2px 8px;border-radius:3px;font-size:0.8em;font-weight:700;">{level}</span>
          </td>
          <td style="padding:6px 12px;font-size:0.82em;color:#6b7280;">{reason}</td>
        </tr>"""

    remediation_items = "".join(
        f'<li style="margin-bottom:8px;">{item}</li>'
        for item in remediation
    )

    v_color = verdict_color.get(verdict, "#374151")
    comp_val = composite.get("value", "?")
    comp_label = composite.get("label", "")

    unfair_list = unfair_adv.get("identified", [])
    unfair_note = unfair_adv.get("assessment", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PLMKR A&R Assessment — {artist.get('name','?')}</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px;}}
  .card{{background:#1e293b;border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;}}
  h1{{font-size:1.6em;margin:0 0 4px;}}
  h2{{font-size:1.1em;color:#94a3b8;margin:0 0 16px;font-weight:500;}}
  h3{{font-size:1em;color:#64748b;margin:0 0 12px;text-transform:uppercase;letter-spacing:.05em;}}
  table{{width:100%;border-collapse:collapse;}}
  tr:nth-child(even){{background:rgba(255,255,255,.03);}}
  .verdict{{font-size:2em;font-weight:800;padding:8px 20px;border-radius:8px;display:inline-block;color:#fff;}}
  .comp{{font-size:1.4em;font-weight:700;}}
  .provisional{{font-size:0.75em;color:#94a3b8;font-weight:400;}}
  .badge{{display:inline-block;padding:3px 10px;border-radius:4px;font-size:0.8em;font-weight:700;}}
  ul{{margin:0;padding-left:20px;color:#cbd5e1;}}
  p{{color:#cbd5e1;line-height:1.6;margin:0;}}
  .meta{{color:#64748b;font-size:0.85em;}}
</style>
</head>
<body>
<div style="max-width:900px;margin:0 auto;">

<div class="card">
  <h1>PLMKR A&R Assessment</h1>
  <h2>Signing Evaluation Memo — {artist.get('name','?')}</h2>
  <div class="meta">Track: "{track.get('title','?')}" &nbsp;|&nbsp; Stage: {artist.get('stage','?')} &nbsp;|&nbsp; Genre: {artist.get('genre','?')} &nbsp;|&nbsp; Territory: {artist.get('territory','?')}</div>
</div>

<div class="card" style="display:flex;gap:32px;align-items:center;">
  <div>
    <h3>Verdict</h3>
    <div class="verdict" style="background:{v_color};">{verdict}</div>
  </div>
  <div>
    <h3>Provisional Composite</h3>
    <div class="comp">{comp_val}</div>
    <div class="provisional">{comp_label} — unlock condition: ≥30 outcome-checked evaluations in feedback/outcomes/</div>
  </div>
  <div>
    <h3>Career Ceiling</h3>
    <div style="font-size:1.3em;font-weight:700;">{ceiling.get('tier','?')}</div>
    <div class="meta">Confidence: {ceiling.get('confidence','?')}</div>
  </div>
</div>

<div class="card">
  <h3>Pillar Scores (Five-Pillar Model)</h3>
  <table>
    <thead><tr style="color:#64748b;font-size:0.8em;text-transform:uppercase;">
      <th style="text-align:left;padding:6px 12px;">Pillar</th>
      <th style="padding:6px 12px;">Grade</th>
      <th style="text-align:left;padding:6px 12px;">Confidence</th>
      <th style="text-align:left;padding:6px 12px;">Key NOT EVALUABLE items</th>
    </tr></thead>
    <tbody>{pillar_rows}</tbody>
  </table>
</div>

<div class="card">
  <h3>Hard Gates</h3>
  <table>
    <thead><tr style="color:#64748b;font-size:0.8em;text-transform:uppercase;">
      <th style="text-align:left;padding:6px 12px;">Gate</th>
      <th style="padding:6px 12px;">Status</th>
      <th style="text-align:left;padding:6px 12px;">Note</th>
    </tr></thead>
    <tbody>{gate_rows}</tbody>
  </table>
</div>

<div class="card">
  <h3>Trajectory Assessment</h3>
  <table>
    {''.join(f'<tr><td style="padding:6px 12px;color:#94a3b8;font-size:0.9em;">{k.replace("_"," ").title()}</td><td style="padding:6px 12px;font-size:0.9em;">{v}</td></tr>' for k,v in trajectory.items())}
  </table>
</div>

<div class="card">
  <h3>Unfair Advantage Assessment</h3>
  <p>{unfair_note}</p>
  {'<ul>' + ''.join(f'<li>{a}</li>' for a in unfair_list) + '</ul>' if unfair_list else ''}
</div>

<div class="card">
  <h3>Risk Assessment</h3>
  <table>
    <thead><tr style="color:#64748b;font-size:0.8em;text-transform:uppercase;">
      <th style="text-align:left;padding:6px 12px;">Category</th>
      <th style="padding:6px 12px;">Level</th>
      <th style="text-align:left;padding:6px 12px;">Reason</th>
    </tr></thead>
    <tbody>{risk_rows}</tbody>
  </table>
</div>

<div class="card">
  <h3>Five-Year Test</h3>
  <p style="font-style:italic;color:#94a3b8;margin-bottom:12px;">{fyt.get('question','')}</p>
  <p>{fyt.get('answer','')}</p>
</div>

<div class="card">
  <h3>Career Ceiling Rationale</h3>
  <p><strong style="color:#e2e8f0;">{ceiling.get('tier','?')}</strong> — {ceiling.get('reason','')}</p>
  {f'<p style="margin-top:10px;color:#64748b;font-size:0.88em;"><strong>Catalyst for next tier:</strong> {ceiling.get("catalyst_for_next_tier","—")}</p>' if ceiling.get("catalyst_for_next_tier") else ''}
</div>

<div class="card">
  <h3>Remediation Priorities</h3>
  <ol>{remediation_items}</ol>
</div>

<div class="card" style="text-align:center;color:#475569;font-size:0.8em;">
  PLMKR A&R Assessment &nbsp;|&nbsp; Rubric v1.0 — Five-Pillar Model &nbsp;|&nbsp; Composite: PROVISIONAL (unlock ≥30 evaluated outcomes)
</div>

</div>
</body>
</html>"""


@app.get("/api/agents/ar-scout/assess/demo", tags=["ar-scout"])
async def ar_scout_assess_demo():
    """
    Render a mocked A&R scored assessment as a standalone HTML scorecard.
    Demonstrates the pilot end-to-end without any live API calls.
    """
    sample_artist = {
        "name": "Jordan Voss", "genre": "R&B / Soul", "stage": "emerging",
        "territory": "Canada", "monthly_listeners": 28000, "save_rate": 0.062,
        "release_count": 4, "manager": None, "label": None,
    }
    sample_track = {
        "title": "Still Waters", "bpm": 88.0, "duration_sec": 213.0,
        "lufs": None, "intro_length_sec": 14.0,
        "genre": None, "features": [], "release_date": None,
    }
    import main as _m
    mock_result = dict(_m._AR_SCOUT_MOCK_ASSESSMENT)
    mock_result["artist"] = sample_artist
    mock_result["track"]  = sample_track

    html = _render_ar_scorecard_html(mock_result["assessment"], sample_artist, sample_track)
    return HTMLResponse(content=html)


@app.post("/api/agents/ar-scout/assess", tags=["ar-scout"])
async def ar_scout_assess(req: ArScoutAssessRequest):
    """
    Deep A&R assessment for a PLMKR artist + track.

    Artist identity is bound from the request payload (artist.name from release/
    track metadata), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When AR_SCOUT_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set AR_SCOUT_MOCK_MODE=false with a
    valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if AR_SCOUT_MOCK_MODE:
        result = dict(_AR_SCOUT_MOCK_ASSESSMENT)
        result["artist"] = req.artist.model_dump()
        result["track"]  = req.track.model_dump()
        result["evaluation_stage"] = req.evaluation_stage
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set AR_SCOUT_MOCK_MODE=true to use mock mode.")

    system_prompt = build_ar_scout_system_prompt(skills_dir=SKILLS_DIR)

    # Build the structured user prompt with every field explicitly labeled
    artist = req.artist
    track  = req.track

    user_prompt = f"""You are performing a PLMKR A&R deep assessment. Use the scoring rubric, song evaluation framework, and output templates from your knowledge base to produce a complete Signing Evaluation Memo.

EVALUATION STAGE: {req.evaluation_stage}

ARTIST PROFILE (from release/track metadata — use this for artist identity, not any account profile name):
- Credited artist name: {artist.name}
- Primary genre: {artist.genre}
- Career stage: {artist.stage}
- Primary territory: {artist.territory}
- Monthly listeners: {artist.monthly_listeners if artist.monthly_listeners is not None else 'NOT PROVIDED — mark as NOT EVALUABLE'}
- Save rate (most recent release): {f'{artist.save_rate:.1%}' if artist.save_rate is not None else 'NOT PROVIDED — mark as NOT EVALUABLE'}
- Total release count: {artist.release_count}
- Manager: {artist.manager or 'NOT IDENTIFIED — flag as Hard Gate 4 risk'}
- Label: {artist.label or 'Independent / not identified'}

TRACK DETAILS:
- Track title: {track.title}
- BPM: {track.bpm if track.bpm is not None else 'NOT PROVIDED — mark Hook Speed and DSP Friendliness as NOT EVALUABLE from measured data'}
- Duration: {f'{track.duration_sec:.0f}s ({track.duration_sec/60:.1f} min)' if track.duration_sec is not None else 'NOT PROVIDED'}
- Integrated LUFS: {track.lufs if track.lufs is not None else 'NOT PROVIDED — mark Production Quality as NOT EVALUABLE from measured data; confidence cap applies'}
- Intro length to hook/vocal: {f'{track.intro_length_sec:.0f}s' if track.intro_length_sec is not None else 'NOT PROVIDED — skip risk assessment requires this; mark as NOT EVALUABLE'}
- Track genre (if different from artist genre): {track.genre or 'same as artist genre'}
- Features / collaborators: {', '.join(track.features) if track.features else 'none'}
- Release date: {track.release_date or 'unreleased'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST PROFILE above (credited artist name), not any other name or account reference.
2. For every sub-signal, explicitly state whether it is MEASURED, SOURCED, or JUDGED, and whether it is EVALUABLE or NOT EVALUABLE based on the data provided above.
3. If a measured audio field (LUFS, BPM, intro length) is NOT PROVIDED, mark it NOT EVALUABLE — do not estimate or infer it.
4. Apply the Anti-Fake-Precision mechanics from the scoring rubric: no probability percentages, no fabricated comparables, no invented unfair advantages.
5. Do not use any retention/threshold language as sourced data claims — frame all benchmarks as industry convention (Tier B estimates), never as verified data.
6. Produce the complete 8-element Signing Evaluation Memo format from the output template.
7. End with a JSON-compatible structured scorecard block labelled STRUCTURED_SCORECARD_JSON containing: verdict, composite_value, pillar_grades (dict), career_ceiling, and top_3_remediation_priorities (list).
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"A&R assessment failed: {str(e)}")

    return {
        "status":           "ok",
        "mock":             False,
        "artist":           req.artist.model_dump(),
        "track":            req.track.model_dump(),
        "evaluation_stage": req.evaluation_stage,
        "assessment_text":  assessment_text,
        "model":            MODEL_SONNET,
    }


# ── GRID-PROPHET — Marketing Assessment ───────────────────────────────────────

class GridProphetArtistInput(BaseModel):
    """Artist-level fields for the marketing assessment. Name is taken from
    release/track metadata, NOT from the Playmaker account profile."""
    name:             str
    genre:            str
    stage:            str   = "emerging"   # emerging|developing|established|superstar
    territory:        str   = "unknown"
    monthly_listeners: Optional[int]   = None
    social_following:  Optional[int]   = None   # total cross-platform
    save_rate:         Optional[float] = None   # 0.0–1.0 on most recent release
    release_count:     int   = 0
    has_email_list:    bool  = False
    email_list_size:   Optional[int]   = None
    has_merch:         bool  = False
    prior_editorial_placements: int = 0

class GridProphetCampaignInput(BaseModel):
    release_title:       str
    release_date:        Optional[str]   = None   # YYYY-MM-DD or None if unreleased
    campaign_budget_usd: Optional[float] = None
    campaign_window_weeks: int = 12
    primary_platforms:   list[str] = []   # e.g. ["tiktok", "instagram", "spotify"]
    has_tour_dates:      bool = False
    tour_territory:      Optional[str]   = None
    is_catalog_campaign: bool = False

class GridProphetAssessRequest(BaseModel):
    artist_id:        str   = ""
    artist:           GridProphetArtistInput
    campaign:         GridProphetCampaignInput
    additional_notes: str   = ""

_GRID_PROPHET_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist": None,    # filled at runtime
    "campaign": None,  # filled at runtime
    "assessment": {
        "dimensions": {
            "virality": {
                "score": 6.0, "weight": 0.15, "confidence": "PARTIAL",
                "evidence": "Genre has documented TikTok sound track record at this scale. Hook structure is excerpt-friendly per track profile.",
                "not_evaluable": ["pre-release organic spread — no social listening data provided",
                                  "UGC velocity — no comparable track history provided"]
            },
            "ugc_potential": {
                "score": 5.0, "weight": 0.12, "confidence": "PARTIAL",
                "evidence": "Genre supports participatory format (singalong, lip-sync). Audio excerpt-worthy based on genre profile.",
                "not_evaluable": ["prior UGC on artist catalog — no analytics provided",
                                  "challenge/movement vector — no release concept details provided"]
            },
            "platform_fit": {
                "score": 5.5, "weight": 0.18, "confidence": "LOW",
                "evidence": "Platform list provided; genre indicates plausible fit on primary platforms.",
                "not_evaluable": ["posting cadence — no social analytics provided",
                                  "engagement rate vs. cohort benchmark — no platform analytics provided",
                                  "algorithm growth state — cannot assess without recent post performance data"]
            },
            "editorial_readiness": {
                "score": 4.0, "weight": 0.13, "confidence": "PARTIAL",
                "evidence": "Release date provided; pitch window can be assessed once EPK status confirmed.",
                "not_evaluable": ["metadata completeness — distributor QC not confirmed",
                                  "EPK status — bio, press photo, embed player not confirmed",
                                  "editorial contact — not provided"]
            },
            "touring_synergy": {
                "score": 3.0, "weight": 0.03, "confidence": "LOW",
                "evidence": "No tour dates confirmed in campaign input.",
                "not_evaluable": ["live activity within campaign window — not provided"]
            },
            "merch_d2c": {
                "score": 3.0, "weight": 0.10, "confidence": "PARTIAL",
                "evidence": "Merch status and email list provided at binary level; size and conversion history not evaluable.",
                "not_evaluable": ["email list open rate — not provided",
                                  "D2C purchase history — not provided",
                                  "physical format plans — not provided"]
            },
            "brand_partnership": {
                "score": 4.0, "weight": 0.07, "confidence": "LOW",
                "evidence": "Monthly listener range indicates brand interest threshold may not yet be met at sub-100K scale.",
                "not_evaluable": ["brand-safety record — no public controversy check data provided",
                                  "prior brand work — not provided",
                                  "category openness — not provided"]
            },
            "fan_ltv": {
                "score": 4.0, "weight": 0.22, "confidence": "PARTIAL",
                "evidence": "Release count and email list presence are structural LTV signals. Save rate provided as proxy for depth of listen.",
                "not_evaluable": ["ticket conversion history — no live event data provided",
                                  "catalog re-listen behavior — no streaming analytics provided",
                                  "community platform (Discord/Patreon) — not provided"]
            }
        },
        "composite": {
            "value": 4.6,
            "formula": "(6.0×0.15) + (5.0×0.12) + (5.5×0.18) + (4.0×0.13) + (3.0×0.03) + (3.0×0.10) + (4.0×0.07) + (4.0×0.22)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked campaign evaluations in feedback/outcomes/"
        },
        "band": "Amber",
        "band_meaning": "Limited campaign — address gap dimensions before full spend; partial rollout",
        "hard_gates": {
            "editorial_gate":    "CONDITIONAL — Editorial Readiness scored 4. Metadata and EPK must be confirmed clean before any editorial pitch submission. Score of 1 would block pitch.",
            "paid_spend_gate":   "CLEAR — Platform Fit scored 5.5 (above gate threshold of ≤2). Paid spend can proceed with caution; recommend small-scale test first.",
            "brand_pitch_gate":  "BLOCKED — Brand Partnership Potential scored 4 (≤2 threshold not triggered at 4; gate not active). However, audience size NOT EVALUABLE — confirm monthly listeners ≥50K before pursuing brand outreach."
        },
        "campaign_priorities": [
            "Confirm EPK completeness (bio, press photo, metadata) — this is a hard gate for editorial and blocks Spotify pitch if any field is missing.",
            "Obtain platform analytics (Spotify for Artists, TikTok/Instagram analytics) — Platform Fit is the highest-weighted gap dimension; cannot score confidently without posting cadence and engagement rate data.",
            "Build or grow email list before launch — Fan LTV potential is the highest-weight dimension (0.22); email list is the single highest-LTV fan acquisition channel available to this artist at this stage."
        ],
        "channel_mix_recommendation": {
            "organic_social": {
                "allocation": "50%", "rationale": "Primary platform algorithm building. TikTok/Reels cadence must be established before paid amplification.",
                "measurement": "Organic engagement rate, completion rate, follower growth WoW"
            },
            "editorial_pitch": {
                "allocation": "0% budget (relationship-earned)", "rationale": "Highest-value non-paid channel. Priority: Spotify editorial 7+ days pre-release.",
                "measurement": "Editorial add within 72h of release"
            },
            "email_sms": {
                "allocation": "10%", "rationale": "Highest LTV-per-dollar channel for artists with existing list. Pre-save push in Phase 2.",
                "measurement": "Pre-save click rate, email open rate, D2C conversion rate"
            },
            "paid_social": {
                "allocation": "40%", "rationale": "Retargeting warm audiences (video viewers ≥50%, profile visitors, prior savers). Cold prospecting only at 10–15% of paid budget until CPA is proven.",
                "measurement": "Cost-per-pre-save, cost-per-follower, warm vs. cold audience CPM ratio"
            }
        },
        "confidence_cap": "Composite confidence capped at LOW: Platform Fit and Fan LTV scored PARTIAL or LOW — platform analytics and conversion history not provided. Assessment cannot be confidently scored above Amber band without these inputs.",
        "next_best_action": "Provide Spotify for Artists analytics (monthly listener count, save rate, completion rate) and social posting cadence data — these two inputs would move Platform Fit and Fan LTV from PARTIAL/LOW to PARTIAL/MEDIUM and may shift the composite band from Amber to Yellow."
    },
    "model": "claude-sonnet-4-6",
    "mock_note": "GRID_PROPHET_MOCK_MODE=true — this is a canned assessment. Set GRID_PROPHET_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/grid-prophet/assess", tags=["grid-prophet"])
async def grid_prophet_assess(req: GridProphetAssessRequest):
    """
    Marketing readiness assessment for a PLMKR artist + campaign.

    Artist identity is bound from the request payload (artist.name from release/
    track metadata), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When GRID_PROPHET_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set GRID_PROPHET_MOCK_MODE=false with a
    valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if GRID_PROPHET_MOCK_MODE:
        result = dict(_GRID_PROPHET_MOCK_ASSESSMENT)
        result["artist"]   = req.artist.model_dump()
        result["campaign"] = req.campaign.model_dump()
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set GRID_PROPHET_MOCK_MODE=true to use mock mode.")

    system_prompt = build_grid_prophet_system_prompt(skills_dir=SKILLS_DIR)

    artist   = req.artist
    campaign = req.campaign

    user_prompt = f"""You are performing a PLMKR marketing readiness assessment. Use the scoring rubric, campaign architecture, channel economics, and output templates from your knowledge base to produce a complete Campaign Plan assessment.

ARTIST PROFILE (from release/track metadata — use this for artist identity, not any account profile name):
- Credited artist name: {artist.name}
- Primary genre: {artist.genre}
- Career stage: {artist.stage}
- Primary territory: {artist.territory}
- Monthly listeners: {artist.monthly_listeners if artist.monthly_listeners is not None else 'NOT PROVIDED — mark as NOT EVALUABLE'}
- Social following (cross-platform): {artist.social_following if artist.social_following is not None else 'NOT PROVIDED'}
- Save rate (most recent release): {f'{artist.save_rate:.1%}' if artist.save_rate is not None else 'NOT PROVIDED — mark as NOT EVALUABLE'}
- Total release count: {artist.release_count}
- Email list: {'YES — size: ' + str(artist.email_list_size) if artist.has_email_list else 'NO — flag as LTV gap'}
- Merch available: {'YES' if artist.has_merch else 'NO'}
- Prior editorial placements: {artist.prior_editorial_placements}

CAMPAIGN DETAILS:
- Release title: {campaign.release_title}
- Release date: {campaign.release_date or 'unreleased / TBD'}
- Campaign budget: {f'${campaign.campaign_budget_usd:,.0f}' if campaign.campaign_budget_usd is not None else 'NOT PROVIDED'}
- Campaign window: {campaign.campaign_window_weeks} weeks
- Primary platforms: {', '.join(campaign.primary_platforms) if campaign.primary_platforms else 'NOT PROVIDED'}
- Tour dates within campaign window: {'YES — territory: ' + (campaign.tour_territory or 'unspecified') if campaign.has_tour_dates else 'NO'}
- Catalog campaign (vs. new release): {'YES' if campaign.is_catalog_campaign else 'NO'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST PROFILE above (credited artist name), not any other name or account reference.
2. Score all 8 rubric dimensions. For each, explicitly state whether evidence is OBSERVED, TOLD, or INFERRED, and whether the dimension is EVALUABLE or NOT EVALUABLE from the data above.
3. If a required data field is NOT PROVIDED, mark it NOT EVALUABLE — do not estimate or infer it.
4. Apply the scoring rubric composite formula. Label the composite PROVISIONAL with the unlock condition.
5. State all hard gates (Editorial Gate, Paid Spend Gate, Brand Pitch Gate) — CLEAR or BLOCKED with reason.
6. Recommend a channel mix with stated mechanism and measurement method for each channel.
7. Name the top 3 campaign priorities and the single next best action (24–72h).
8. Do not use probability percentage language for any projection. Label all projections ESTIMATE with the comparable that produced them.
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Marketing assessment failed: {str(e)}")

    return {
        "status":        "ok",
        "mock":          False,
        "artist":        req.artist.model_dump(),
        "campaign":      req.campaign.model_dump(),
        "assessment_text": assessment_text,
        "model":         MODEL_SONNET,
    }


# ── BRAND-CONNECT — Brand Partnership Deal Quality Assessment ──────────────────

class BrandConnectArtistInput(BaseModel):
    """Artist-level fields for the brand partnership assessment. Name is taken
    from release/track metadata, NOT from the Playmaker account profile."""
    name:              str
    genre:             str
    stage:             str   = "emerging"   # emerging|developing|established|superstar
    territory:         str   = "unknown"
    tier:              str   = "GOLD"       # GOLD|PLATINUM|DIAMOND
    monthly_listeners: Optional[int]   = None
    social_following:  Optional[int]   = None   # total cross-platform
    engagement_rate:   Optional[float] = None   # 0.0–1.0

class BrandConnectDealInput(BaseModel):
    deal_type:              str   = "paid_social"  # product_gifting|paid_social|ambassador|tour_sponsorship|equity
    partner_category:       str   = "consumer_brand"
    exclusivity_category:   Optional[str]  = None
    term_months:            Optional[int]  = None
    has_kill_fee:           Optional[bool] = None
    upfront_payment_pct:    Optional[float] = None   # 0.0–1.0; fraction paid at signing
    diligence_status:       str   = "none"   # none|partial|full
    prior_artist_deals:     Optional[int]  = None   # brand's documented prior artist partnerships
    has_morality_clause:    Optional[bool] = None
    morality_clause_scoped: Optional[bool] = None   # True if specific triggers defined

class BrandConnectAssessRequest(BaseModel):
    artist_id:        str   = ""
    artist:           BrandConnectArtistInput
    deal:             BrandConnectDealInput
    additional_notes: str   = ""

_BRAND_CONNECT_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist": None,   # filled at runtime
    "deal":   None,   # filled at runtime
    "assessment": {
        "dimensions": {
            "strategic_value": {
                "score": 5.5, "weight": 0.18, "confidence": "PARTIAL",
                "evidence": "Deal type indicates audience access potential in artist's genre. Leverage-transfer test requires confirmation of brand's consumer demographic overlap with artist's listener base.",
                "not_evaluable": ["confirmed audience demographic overlap — brand consumer data not provided",
                                  "co-marketing spend commitment — not confirmed as contractually committed vs. aspirational"]
            },
            "economic_value": {
                "score": 5.0, "weight": 0.16, "confidence": "LOW",
                "evidence": "Deal type provided; specific consideration not provided. Payment schedule status unconfirmed.",
                "not_evaluable": ["total consideration amount — not provided",
                                  "production cost obligations — not assessed",
                                  "kill fee amount — not confirmed at this stage"]
            },
            "partner_quality": {
                "score": 3.0, "weight": 0.14, "confidence": "LOW",
                "evidence": "Diligence status: none. Prior artist deal count not confirmed. Proceeding without adequate credibility evidence — scored INFERRED at baseline.",
                "not_evaluable": ["brand payment track record — diligence not conducted",
                                  "creative approval process timeline — not assessed",
                                  "brand organizational alignment — not confirmed"]
            },
            "deal_structure": {
                "score": 4.0, "weight": 0.14, "confidence": "LOW",
                "evidence": "Term not confirmed. Category exclusivity scope not defined. Kill fee status not provided.",
                "not_evaluable": ["exclusivity category scope — not confirmed",
                                  "creative approval rounds and SLA — not assessed",
                                  "IP ownership of content created — not confirmed",
                                  "artist exit right — not confirmed"]
            },
            "risk_exposure": {
                "score": 4.0, "weight": 0.14, "confidence": "PARTIAL",
                "evidence": "Morality clause status not confirmed. No fatal exposure identified from available information, but morality clause assessment is incomplete.",
                "not_evaluable": ["morality clause trigger specificity — not assessed",
                                  "brand reputation controversy check — not conducted",
                                  "category exclusivity vs. alternative revenue impact — not assessed"]
            },
            "execution_feasibility": {
                "score": 5.0, "weight": 0.10, "confidence": "LOW",
                "evidence": "Deal type (paid social) indicates standard content delivery scope. Specific deliverable requirements and production timeline not confirmed.",
                "not_evaluable": ["production bandwidth assessment — not provided",
                                  "content calendar conflict with existing commitments — not assessed"]
            },
            "opportunity_cost": {
                "score": 3.0, "weight": 0.08, "confidence": "LOW",
                "evidence": "Category exclusivity scope not confirmed. Alternative brand partnerships in same category not identified or assessed. BATNA not established.",
                "not_evaluable": ["alternative brand opportunities in same category — not assessed",
                                  "exclusivity cost vs. best foreclosed alternative — not quantified"]
            },
            "reversibility": {
                "score": 4.0, "weight": 0.06, "confidence": "LOW",
                "evidence": "Kill fee and exit right status not confirmed. Term length not confirmed.",
                "not_evaluable": ["artist-exercisable exit right — not confirmed",
                                  "kill fee structure — not confirmed",
                                  "renewal terms — not confirmed"]
            }
        },
        "composite": {
            "value": 4.3,
            "formula": "(5.5×0.18) + (5.0×0.16) + (3.0×0.14) + (4.0×0.14) + (4.0×0.14) + (5.0×0.10) + (3.0×0.08) + (4.0×0.06)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked brand partnership evaluations in feedback/outcomes/"
        },
        "band": "Amber",
        "band_meaning": "Material gaps require resolution before committing — deal structure, partner diligence, and opportunity cost are all under-assessed at this stage",
        "hard_gates": {
            "partner_quality_gate": "CONDITIONAL — Partner Quality & Credibility scored 3 (INFERRED — diligence not conducted). Hard gate threshold is score 1 (disqualifying partner confirmed). Current score reflects absence of evidence, not negative evidence. Conduct partner diligence before commitment.",
            "risk_exposure_gate":   "CONDITIONAL — Risk & Downside Exposure scored 4. Hard gate threshold is score 1 (fatal, unmitigated exposure confirmed). Morality clause specificity must be confirmed before commitment. No fatal exposure identified from available information."
        },
        "deal_priorities": [
            "Conduct partner due diligence — confirm brand's prior artist partnership track record, payment reliability, and creative approval process timeline before committing. Partner Quality is the most consequential unassessed dimension.",
            "Obtain deal term sheet — category exclusivity scope, kill fee structure, artist exit right, and creative approval SLAs are all required for a complete DQS assessment. Current structure scoring is LOW confidence on these dimensions.",
            "Assess opportunity cost — identify alternative brand partnerships available in the same category within the next 6 months and confirm whether this deal's exclusivity provisions foreclose deals of comparable or greater value."
        ],
        "structural_flags": [
            "Kill fee status unconfirmed — require before any commitment. A paid brand deal without a kill fee places all cancellation risk on the artist.",
            "Morality clause scope not assessed — require specific trigger language for LEX-CIPHER review before commitment.",
            "Category exclusivity scope not defined — require specific category definition. Over-broad exclusivity is a Dim-5 risk and a Dim-7 opportunity cost driver."
        ],
        "lex_cipher_routing": "Route to LEX-CIPHER when: (1) deal term sheet is available for review, (2) morality clause language is confirmed, (3) IP ownership provisions on content created for the deal are defined. Do not commit before LEX-CIPHER review.",
        "confidence_cap": "Composite confidence capped at LOW: Partner Quality (Dim-3) scored INFERRED — no diligence conducted. Deal Structure (Dim-4) and Risk Exposure (Dim-5) scored LOW — deal documents not yet reviewed. Assessment cannot be confidently scored above Amber band without these inputs.",
        "next_best_action": "Obtain the brand's deal term sheet and conduct a reference check with at least one prior artist partnership — these two inputs would move Partner Quality and Deal Structure from LOW to PARTIAL and would confirm or clear the Risk Exposure gate."
    },
    "model": "claude-sonnet-4-6",
    "mock_note": "BRAND_CONNECT_MOCK_MODE=true — this is a canned assessment. Set BRAND_CONNECT_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/brand-connect/assess", tags=["brand-connect"])
async def brand_connect_assess(req: BrandConnectAssessRequest):
    """
    Brand partnership deal quality assessment for a PLMKR artist + proposed deal.

    Artist identity is bound from the request payload (artist.name from release/
    track metadata), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When BRAND_CONNECT_MOCK_MODE=true (default), returns a canned scored
    assessment without calling the Anthropic API. Set BRAND_CONNECT_MOCK_MODE=false
    with a valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if BRAND_CONNECT_MOCK_MODE:
        result = dict(_BRAND_CONNECT_MOCK_ASSESSMENT)
        result["artist"] = req.artist.model_dump()
        result["deal"]   = req.deal.model_dump()
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set BRAND_CONNECT_MOCK_MODE=true to use mock mode.")

    system_prompt = build_brand_connect_system_prompt(skills_dir=SKILLS_DIR)

    artist = req.artist
    deal   = req.deal

    user_prompt = f"""You are performing a PLMKR brand partnership deal quality assessment. Use the DQS scoring rubric, partnership strategy frameworks, deal economics, deal structure standards, and output templates from your knowledge base to produce a complete Deal Quality Assessment Memo.

ARTIST PROFILE (from release/track metadata — use this for artist identity):
- Credited artist name: {artist.name}
- Primary genre: {artist.genre}
- Career stage: {artist.stage}
- Primary territory: {artist.territory}
- Artist tier: {artist.tier}
- Monthly listeners: {artist.monthly_listeners if artist.monthly_listeners is not None else 'NOT PROVIDED — mark as NOT EVALUABLE'}
- Social following (cross-platform): {artist.social_following if artist.social_following is not None else 'NOT PROVIDED'}
- Engagement rate: {f'{artist.engagement_rate:.1%}' if artist.engagement_rate is not None else 'NOT PROVIDED'}

PROPOSED DEAL DETAILS:
- Deal type: {deal.deal_type}
- Partner category: {deal.partner_category}
- Category exclusivity: {deal.exclusivity_category or 'NOT CONFIRMED'}
- Term: {f'{deal.term_months} months' if deal.term_months is not None else 'NOT CONFIRMED'}
- Kill fee confirmed: {deal.has_kill_fee if deal.has_kill_fee is not None else 'NOT CONFIRMED'}
- Upfront payment %: {f'{deal.upfront_payment_pct:.0%}' if deal.upfront_payment_pct is not None else 'NOT CONFIRMED'}
- Diligence status: {deal.diligence_status}
- Prior artist partnerships (brand's documented history): {deal.prior_artist_deals if deal.prior_artist_deals is not None else 'NOT CONFIRMED'}
- Morality clause present: {deal.has_morality_clause if deal.has_morality_clause is not None else 'NOT CONFIRMED'}
- Morality clause specifically scoped: {deal.morality_clause_scoped if deal.morality_clause_scoped is not None else 'NOT CONFIRMED'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST PROFILE above, not any other name or account reference.
2. Score all 8 DQS dimensions independently. For each, state evidence type (observed / told / inferred), confidence (high / medium / low), and whether the dimension is EVALUABLE from the data above.
3. If a required data field is NOT CONFIRMED or NOT PROVIDED, mark it NOT EVALUABLE — do not estimate or infer it.
4. Apply the DQS composite formula. Label the composite PROVISIONAL with the unlock condition.
5. State both hard gate statuses (Partner Quality Gate, Risk & Exposure Gate) — CLEAR or CONDITIONAL or TRIGGERED with specific reason.
6. Name the top 3 deal priorities (actions the artist should take before committing).
7. Identify any structural flags (terms requiring LEX-CIPHER review before commitment).
8. State the single next best action (24–72h).
9. Do not use probability percentage language for any projection. Label all projections ESTIMATE with the comparable that produced them.
10. Dim-1 (Strategic Value) and Dim-2 (Economic Value) must be scored and reported independently — never collapse them into a single "deal value" judgment.
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Brand partnership assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist":          req.artist.model_dump(),
        "deal":            req.deal.model_dump(),
        "assessment_text": assessment_text,
        "model":           MODEL_SONNET,
    }


# ── SYNC-AGENT — Sync Licensing Assessment ────────────────────────────────────

class SyncAgentTrackInput(BaseModel):
    """Track-level fields for the sync licensing assessment."""
    title:               str
    genre:               str
    clearance_status:    str   = "UNKNOWN"   # CLEARED|CLEARABLE|PENDING|BLOCKED|UNKNOWN
    is_one_stop:         bool  = False
    has_stems:           bool  = False
    has_clean_version:   bool  = True
    duration_sec:        Optional[float] = None
    bpm:                 Optional[float] = None
    has_samples:         bool  = False
    has_explicit_lyrics: bool  = False

class SyncAgentBriefInput(BaseModel):
    """Brief-level fields describing the sync opportunity."""
    project_type:         str          = "unknown"   # film|tv|ad|trailer|game|web|unknown
    scene_description:    str          = ""
    budget_range:         Optional[str] = None        # e.g., "$5k-$15k" or None
    deadline_days:        Optional[int] = None
    territory:            str          = "worldwide"
    reference_tracks:     list[str]    = []
    lyric_restrictions:   list[str]    = []
    exclusivity_required: bool         = False
    buyer_class:          str          = "unknown"   # indie|mid-tier|major|streaming|ad-agency|unknown

class SyncAgentAssessRequest(BaseModel):
    artist_id:        str   = ""
    artist_name:      str
    artist_territory: str   = "unknown"
    track:            SyncAgentTrackInput
    brief:            SyncAgentBriefInput
    additional_notes: str   = ""

_SYNC_AGENT_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,  # filled at runtime
    "track": None,        # filled at runtime
    "brief": None,        # filled at runtime
    "assessment": {
        "dimensions": {
            "brief_fit": {
                "score": 4, "weight": 0.40, "confidence": "PARTIAL",
                "rationale": "Track genre and energy profile consistent with the brief's emotional function. Reference tracks share up-tempo, cinematic qualities with this catalog entry. One defensible deviation: era may differ from references by ~5 years.",
                "not_evaluable": [
                    "lyric theme alignment — no lyric content provided",
                    "specific reference track shared properties — references not yet decoded"
                ]
            },
            "clearance_complexity": {
                "score": 5, "weight": 0.25, "confidence": "HIGH",
                "rationale": "One-stop confirmed: artist controls both master and publishing. No samples or uncleared interpolations flagged. Status: CLEARED — instant yes available.",
                "not_evaluable": []
            },
            "turnaround_feasibility": {
                "score": 4, "weight": 0.20, "confidence": "HIGH",
                "rationale": "One-stop catalog can confirm same-day. Deadline of 14 days met with ≥50% buffer. Structurally eligible for this buyer class.",
                "not_evaluable": []
            },
            "fee_tier": {
                "score": 3, "weight": 0.15, "confidence": "LOW",
                "rationale": "Budget range sits within the standard band for this use category per indicative estimates. Standard negotiation — no strategic exception required.",
                "not_evaluable": [
                    "buyer budget ceiling — no exact figure provided",
                    "comparable closed deals in this category — real comparables pending"
                ]
            }
        },
        "composite": {
            "value": 82,
            "formula": "(4×0.40×20) + (5×0.25×20) + (4×0.20×20) + (3×0.15×20)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked sync assessments in feedback/outcomes/"
        },
        "hard_gates": {
            "clearance_unknown_gate": "CLEAR — clearance status CLEARED; chain fully papered",
            "turnaround_gate":        "CLEAR — turnaround feasibility scored 4; deadline met with ≥50% buffer",
            "brief_fit_gate":         "CLEAR — brief fit scored 4; above the do-not-pitch floor of ≤2"
        },
        "verdict": "PITCH",
        "pitch_rationale": "All three hard gates clear. Composite 82/100 (PROVISIONAL). One-stop clears the clearance risk entirely; brief-fit at 4 gives a defensible one-line fit case. Fee tier confidence is LOW pending exact budget confirmation — quote inside stated budget range or open at top of comparable-supported band.",
        "next_action": "Prepare pitch email within 24h. Lead with clearance status and one-line fit case referencing the scene's emotional function. Include stems availability note. Subject line should carry the answer."
    },
    "mock_note": "SYNC_AGENT_MOCK_MODE=true — this is a canned assessment. Set SYNC_AGENT_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/sync-agent/assess", tags=["sync-agent"])
async def sync_agent_assess(req: SyncAgentAssessRequest):
    """
    Sync licensing brief-fit assessment for a PLMKR artist + track + brief.

    Artist identity is bound from the request payload (artist_name from
    track/release metadata), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When SYNC_AGENT_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set SYNC_AGENT_MOCK_MODE=false with a
    valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if SYNC_AGENT_MOCK_MODE:
        result = dict(_SYNC_AGENT_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["track"]       = req.track.model_dump()
        result["brief"]       = req.brief.model_dump()
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set SYNC_AGENT_MOCK_MODE=true to use mock mode.")

    system_prompt = build_sync_agent_system_prompt(skills_dir=SKILLS_DIR)

    track = req.track
    brief = req.brief

    user_prompt = f"""You are performing a PLMKR sync licensing assessment. Use the scoring rubric, buyer psychology, clearance workflow, deal logic, and output templates from your knowledge base to produce a complete Brief-Fit Scorecard.

ARTIST PROFILE (use this for artist identity — not any account name):
- Credited artist name: {req.artist_name}
- Primary territory: {req.artist_territory}

TRACK DETAILS:
- Title: {track.title}
- Genre: {track.genre}
- Clearance status: {track.clearance_status}
- One-stop (artist controls both master and publishing): {'YES' if track.is_one_stop else 'NO — publishing side requires separate clearance'}
- Stems available: {'YES' if track.has_stems else 'NO'}
- Clean version available: {'YES' if track.has_clean_version else 'NO'}
- Duration: {f'{track.duration_sec:.0f}s' if track.duration_sec is not None else 'NOT PROVIDED'}
- BPM: {f'{track.bpm:.0f}' if track.bpm is not None else 'NOT PROVIDED'}
- Contains samples: {'YES — recursive chain applies; status cannot exceed CLEARABLE until sample chain confirmed' if track.has_samples else 'NO'}
- Explicit lyrics: {'YES — clean version required for most TV/ad; confirm availability' if track.has_explicit_lyrics else 'NO'}

BRIEF DETAILS:
- Project type: {brief.project_type}
- Scene / emotional function: {brief.scene_description or 'NOT PROVIDED — mark brief fit as NOT EVALUABLE from scene function'}
- Budget range: {brief.budget_range or 'NOT PROVIDED — fee tier scored LOW confidence'}
- Deadline (days from now): {brief.deadline_days if brief.deadline_days is not None else 'NOT PROVIDED — turnaround feasibility scored LOW confidence'}
- Territory: {brief.territory}
- Reference tracks: {', '.join(brief.reference_tracks) if brief.reference_tracks else 'NOT PROVIDED'}
- Lyric restrictions: {', '.join(brief.lyric_restrictions) if brief.lyric_restrictions else 'None stated'}
- Exclusivity required: {'YES' if brief.exclusivity_required else 'NO'}
- Buyer class: {brief.buyer_class}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST PROFILE above.
2. Score all 4 rubric dimensions. For each: score (1–5), weight, rationale (1–3 sentences), confidence (HIGH/PARTIAL/LOW), and NOT EVALUABLE items named.
3. If clearance_status is UNKNOWN: cap composite at 40 and flag the gate.
4. Apply the composite formula: Σ(weight × score × 20) → 0–100. Label PROVISIONAL with unlock condition.
5. State all three hard gates: CLEAR or TRIGGERED with reason.
6. Issue verdict: PITCH (≥60, all gates clear, brief_fit ≥ 3) / HOLD / PASS.
7. State ALTERNATIVES if HOLD or PASS (what condition changes the verdict).
8. State the single NEXT BEST ACTION (24–48h).
9. Log as a falsifiable prediction with outcome check date (90 days default).
10. Do not use probability percentage language.
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Sync licensing assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "track":           req.track.model_dump(),
        "brief":           req.brief.model_dump(),
        "assessment_text": assessment_text,
        "model":           MODEL_SONNET,
    }


# ── LEX-CIPHER — Legal Deal / Document Quality Assessment ─────────────────────

class LexCipherAgreementInput(BaseModel):
    """Structured description of the agreement under review.

    The agent reviews what is PRESENT and flags what is ABSENT — absence of a
    required provision is itself evaluable as a gap. These flags describe the
    document's structure; they are not legal conclusions.
    """
    agreement_type:               str  = "unknown"   # recording|publishing|management|sync|brand|performance|nda|other
    parties_described:            str  = ""
    governing_territory:          str  = "unknown"
    # Dimension 1 — Rights Grant Clarity
    has_rights_grant:             bool = False
    rights_grant_defined:         bool = False        # territory/term/media/exclusivity specified
    reserved_rights_stated:       bool = False
    # Dimension 2 — Compensation Structure
    compensation_defined:         bool = False
    royalty_base:                 Optional[str] = None  # SRLP|PPD|net_receipts|gross|flat_fee|None
    deductions_defined:           bool = False
    accounting_period_defined:    bool = False
    # Dimension 3 — Recoupment / Cost Transparency
    recoupable_costs_enumerated:  Optional[bool] = None
    cross_collateralization:      Optional[bool] = None
    # Dimension 4 — Exit / Reversion
    has_termination_for_cause:    bool = False
    has_reversion_clause:         bool = False
    perpetual_term:               Optional[bool] = None
    # Dimension 5 — Audit Rights
    has_audit_rights:             bool = False
    audit_window_months:          Optional[int] = None
    # Dimension 6 — Warranties & Representations
    has_ip_ownership_warranty:    bool = False
    has_indemnification:          bool = False
    # Dimension 7 — Dispute Resolution
    governing_law_stated:         bool = False
    dispute_forum_defined:        bool = False
    # Dimension 8 — Red-Flag Clauses
    red_flag_clauses:             list[str] = []      # named clauses present, e.g. "open-ended recoupable costs"

class LexCipherAssessRequest(BaseModel):
    artist_id:        str = ""
    artist_name:      str
    artist_territory: str = "unknown"
    agreement:        LexCipherAgreementInput
    additional_notes: str = ""

_LEX_CIPHER_COUNSEL_FOOTER = (
    "Route to qualified entertainment counsel for execution, negotiation, and legal opinion."
)

_LEX_CIPHER_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,   # filled at runtime
    "agreement": None,     # filled at runtime
    "domain_constraint": "Drafts and flags — never advises. Qualified counsel signs off.",
    "assessment": {
        "agreement_type": None,  # filled at runtime
        "dimensions": {
            "rights_grant_clarity": {
                "grade": "B", "numeric": 8.0, "weight": 0.18, "confidence": "PARTIAL",
                "rationale": "Rights grant present with territory, term, and media defined; exclusivity stated. Reserved rights are not separately enumerated — one element requires interpretation but is resolvable from context.",
                "sub_signals": "territory SOURCED · term SOURCED · media SOURCED · exclusivity SOURCED · reserved rights ABSENT",
                "not_evaluable": ["full rights-scope language — only structural flags supplied, not the clause text"]
            },
            "compensation_structure": {
                "grade": "B-", "numeric": 7.5, "weight": 0.16, "confidence": "PARTIAL",
                "rationale": "Compensation type and royalty base defined; accounting period present. Deduction categories are not fully enumerated, leaving the effective rate partly indeterminate without the deduction schedule.",
                "sub_signals": "payment type SOURCED · royalty base SOURCED · deductions AMBIGUOUS · accounting period SOURCED",
                "not_evaluable": ["effective rate — meaningless without the full deduction schedule"]
            },
            "recoupment_transparency": {
                "grade": "C", "numeric": 6.0, "weight": 0.14, "confidence": "PARTIAL",
                "rationale": "Recoupable-cost categories are partially defined; cross-collateralization is present without a stated scope limit. Document this — a cross-collateralization clause without scope nets balances across projects.",
                "sub_signals": "recoupable costs AMBIGUOUS · cross-collateralization SOURCED (scope ABSENT)",
                "not_evaluable": []
            },
            "exit_reversion": {
                "grade": "B", "numeric": 8.0, "weight": 0.13, "confidence": "HIGH",
                "rationale": "Termination for cause is defined and a reversion clause is present with stated trigger conditions. Term is not perpetual.",
                "sub_signals": "termination for cause SOURCED · reversion trigger SOURCED · perpetual term ABSENT",
                "not_evaluable": []
            },
            "audit_rights": {
                "grade": "B-", "numeric": 7.5, "weight": 0.13, "confidence": "HIGH",
                "rationale": "Audit rights present with a window of at least 12 months from statement date by industry convention; scope and cost allocation not fully specified.",
                "sub_signals": "audit clause SOURCED · window SOURCED · scope AMBIGUOUS · cost allocation ABSENT",
                "not_evaluable": []
            },
            "warranties_representations": {
                "grade": "B-", "numeric": 7.5, "weight": 0.12, "confidence": "PARTIAL",
                "rationale": "IP-ownership warranty and an indemnification provision are present. Indemnification proportionality (cap vs. unlimited) and survival are not specified in the supplied flags.",
                "sub_signals": "ownership warranty SOURCED · indemnification SOURCED · proportionality AMBIGUOUS · survival ABSENT",
                "not_evaluable": ["indemnification cap — not described in the structured input"]
            },
            "dispute_resolution": {
                "grade": "C+", "numeric": 6.5, "weight": 0.08, "confidence": "PARTIAL",
                "rationale": "Governing law is stated; forum/jurisdiction is defined. Cost allocation and confidentiality of proceedings are not addressed in the supplied structure.",
                "sub_signals": "governing law SOURCED · forum SOURCED · cost allocation ABSENT · confidentiality ABSENT",
                "not_evaluable": []
            },
            "red_flag_absence": {
                "grade": "B", "numeric": 8.0, "weight": 0.06, "confidence": "HIGH",
                "rationale": "No high-severity red-flag clauses identified in the supplied list. Cross-collateralization without scope limit is documented under recoupment but is not an HG-4 trigger on its own.",
                "sub_signals": "no HG-4 red-flag clauses named",
                "not_evaluable": []
            }
        },
        "hard_gates": {
            "rights_grant_gate":  "CLEAR — rights grant present and defined (HG-1 not triggered)",
            "audit_rights_gate":  "CLEAR — audit rights present with window ≥ 12 months from statement (HG-2 not triggered)",
            "ip_ownership_gate":  "CLEAR — IP-ownership warranty present; chain of title established within stated context (HG-3 not triggered)",
            "red_flag_gate":      "CLEAR — no HG-4 red-flag clause present"
        },
        "composite": {
            "value": 7.4,
            "formula": "(8.0×0.18)+(7.5×0.16)+(6.0×0.14)+(8.0×0.13)+(7.5×0.13)+(7.5×0.12)+(6.5×0.08)+(8.0×0.06)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked deal evaluations in feedback/outcomes/"
        },
        "risk_classification": "NOTABLE_GAPS",
        "red_flags": [
            {
                "clause": "Cross-collateralization without stated scope limit",
                "what_it_does": "Nets recoupable balances across projects so a profitable project pays down another's deficit.",
                "severity": "HIGH",
                "routing": "Identify the deficiency for qualified counsel; route the negotiation position to the management function. Not prescribed here."
            }
        ],
        "not_evaluable": [
            "Full clause text — this assessment is built from structured presence/absence flags, not the agreement itself. A definitive review requires the executed document."
        ],
        "next_best_action": "Provide the executed agreement text for a clause-level review. The single highest-value documentation step is confirming the deduction schedule and the cross-collateralization scope; route any negotiation of those terms to the management function plus qualified counsel.",
        "counsel_footer": _LEX_CIPHER_COUNSEL_FOOTER
    },
    "mock_note": "LEX_CIPHER_MOCK_MODE=true — this is a canned assessment. Set LEX_CIPHER_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/lex-cipher/assess", tags=["lex-cipher"])
async def lex_cipher_assess(req: LexCipherAssessRequest):
    """
    Legal deal / document quality assessment for a PLMKR artist's agreement.

    The agent DRAFTS and FLAGS — it never advises, never gives a legal opinion,
    and never recommends a negotiation position. It scores the eight-dimension
    Deal/Document Quality Rubric, states the four hard gates, classifies legal
    risk severity, and routes execution to qualified entertainment counsel.

    Artist identity is bound from the request payload (artist_name from the
    deal/release context), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When LEX_CIPHER_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set LEX_CIPHER_MOCK_MODE=false with a
    valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if LEX_CIPHER_MOCK_MODE:
        result = dict(_LEX_CIPHER_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["agreement"]   = req.agreement.model_dump()
        # deep-copy the assessment block so per-request fields don't mutate the template
        assessment = dict(result["assessment"])
        assessment["agreement_type"] = req.agreement.agreement_type
        result["assessment"] = assessment
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set LEX_CIPHER_MOCK_MODE=true to use mock mode.")

    system_prompt = build_lex_cipher_system_prompt(skills_dir=SKILLS_DIR)

    ag = req.agreement

    user_prompt = f"""You are performing a PLMKR legal deal/document quality assessment. You DRAFT and FLAG — you never advise, never give a legal opinion, and never recommend a negotiation position. Use the eight-dimension Deal/Document Quality Rubric, the hard gates, and the Contract/Deal Review template from your knowledge base.

ARTIST IDENTITY (use this — not any account name):
- Credited artist name: {req.artist_name}
- Artist territory: {req.artist_territory}

AGREEMENT UNDER REVIEW (structured presence/absence flags — the full clause text is NOT supplied):
- Agreement type: {ag.agreement_type}
- Parties: {ag.parties_described or 'NOT DESCRIBED'}
- Governing territory: {ag.governing_territory}
- Rights grant present: {'YES' if ag.has_rights_grant else 'NO — HG-1 candidate'}
- Rights grant defined (territory/term/media/exclusivity): {'YES' if ag.rights_grant_defined else 'NO'}
- Reserved rights stated: {'YES' if ag.reserved_rights_stated else 'NO'}
- Compensation defined: {'YES' if ag.compensation_defined else 'NO'}
- Royalty base: {ag.royalty_base or 'NOT STATED — effective rate not determinable'}
- Deductions defined: {'YES' if ag.deductions_defined else 'NO'}
- Accounting period defined: {'YES' if ag.accounting_period_defined else 'NO'}
- Recoupable costs enumerated: {ag.recoupable_costs_enumerated if ag.recoupable_costs_enumerated is not None else 'NOT STATED'}
- Cross-collateralization present: {ag.cross_collateralization if ag.cross_collateralization is not None else 'NOT STATED'}
- Termination for cause present: {'YES' if ag.has_termination_for_cause else 'NO'}
- Reversion clause present: {'YES' if ag.has_reversion_clause else 'NO'}
- Perpetual term: {ag.perpetual_term if ag.perpetual_term is not None else 'NOT STATED'}
- Audit rights present: {'YES' if ag.has_audit_rights else 'NO — HG-2 candidate'}
- Audit window (months from statement): {ag.audit_window_months if ag.audit_window_months is not None else 'NOT STATED'}
- IP-ownership warranty present: {'YES' if ag.has_ip_ownership_warranty else 'NO — HG-3 candidate'}
- Indemnification present: {'YES' if ag.has_indemnification else 'NO'}
- Governing law stated: {'YES' if ag.governing_law_stated else 'NO'}
- Dispute forum defined: {'YES' if ag.dispute_forum_defined else 'NO'}
- Named red-flag clauses present: {', '.join(ag.red_flag_clauses) if ag.red_flag_clauses else 'None named'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST IDENTITY above.
2. Score all 8 rubric dimensions. For each: letter grade, numeric equivalent, weight, sub-signal classification (SOURCED/ABSENT/AMBIGUOUS/NOT EVALUABLE), confidence, and a 1–3 sentence rationale describing what the clause does — not what position to take.
3. State all four hard gates (HG-1 Rights Grant, HG-2 Audit Rights, HG-3 IP Ownership, HG-4 Red-Flag Clause): CLEAR or TRIGGERED with reason. An absent rights grant, absent audit rights, audit window shorter than 12 months from statement, unestablished IP ownership, or any high-severity red-flag clause triggers the corresponding gate.
4. Compute the PROVISIONAL COMPOSITE per the rubric formula. Label it PROVISIONAL and state the unlock condition. Treat any retention/threshold figure as industry convention, not a recommendation.
5. Classify legal-risk severity descriptively (LOW_RISK / NOTABLE_GAPS / SIGNIFICANT_GAPS / MATERIALLY_DEFICIENT / CRITICALLY_DEFICIENT). This labels severity — it is NOT a recommendation to sign or walk away.
6. List red-flag clauses present: clause, what it does, severity, and routing. Name the deficiency only — never prescribe a target term or position.
7. Mark NOT EVALUABLE items (the full clause text is not supplied) and name the minimum data required.
8. State a single NEXT BEST ACTION focused on documentation/counsel — route all negotiation to the management function plus qualified counsel.
9. Do not state any rate, advance, or fee as "market standard" without a Tier A/B source.
10. End with the counsel footer: "{_LEX_CIPHER_COUNSEL_FOOTER}"
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Legal assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "agreement":       req.agreement.model_dump(),
        "assessment_text": assessment_text,
        "counsel_footer":  _LEX_CIPHER_COUNSEL_FOOTER,
        "model":           MODEL_SONNET,
    }


# ── TOUR-COMMANDER — Tour Campaign Quality Assessment ─────────────────────────

class TourCampaignInput(BaseModel):
    """Structured description of the tour campaign under review.

    The agent scores what is PRESENT and flags what is ABSENT — absence of a
    required planning step (a break-even model, a confirmed anchor, a production
    advance) is itself evaluable as a gap. These flags describe the campaign's
    planning state; they are not predictions of box-office outcome.
    """
    campaign_name:                    str  = ""
    tour_type:                        str  = "headline"   # headline|support|festival_run|one_off|international
    markets_count:                    int  = 0
    has_international_dates:           bool = False
    # Dimension 1 — Routing Logic
    anchor_dates_confirmed:           bool = False
    routing_efficiency_ratio:         Optional[float] = None   # show days / total tour days
    dead_legs_analyzed:               bool = False
    # Dimension 2 — Financial Model Integrity
    break_even_model_exists:          bool = False
    modeled_before_routing:           bool = False
    sensitivity_scenarios_modeled:    bool = False            # 70/85/100% capacity
    # Dimension 3 — Offer Evaluation Quality
    offers_evaluated_against_framework: bool = False
    split_points_analyzed:            Optional[bool] = None
    red_flag_clauses_identified:      bool = False
    # Dimension 4 — Production Readiness
    rider_complete:                   bool = False
    production_advance_completed:     bool = False
    advance_lead_weeks:               Optional[int] = None
    # Dimension 5 — Ticketing Strategy
    price_points_market_analyzed:     bool = False
    tier_structure_defined:           bool = False            # GA + mid + VIP
    secondary_market_monitored:       bool = False
    # Dimension 6 — International Readiness
    work_permits_confirmed:           Optional[bool] = None
    withholding_modeled:              Optional[bool] = None
    territory_streaming_signal:       Optional[bool] = None
    # Dimension 7 — Merch Planning
    merch_planned:                    bool = False
    hall_fees_documented:             bool = False
    # Dimension 8 — Settlement Process
    settlement_review_process:        bool = False
    red_flag_clauses:                 list[str] = []          # named red-flag clauses present in offers

class TourCommanderAssessRequest(BaseModel):
    artist_id:        str = ""
    artist_name:      str
    artist_territory: str = "unknown"
    campaign:         TourCampaignInput
    additional_notes: str = ""

_TOUR_COMMANDER_ADVISORY_FOOTER = (
    "Operational assessment only — not legal, tax, or immigration advice. "
    "Route contracts and work permits to qualified counsel; route detailed "
    "financial modeling to the finance function."
)

_TOUR_COMMANDER_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,   # filled at runtime
    "campaign": None,      # filled at runtime
    "operating_principle": "Model first, route second, commit third. Scores planning state — not box-office outcome.",
    "assessment": {
        "tour_type": None,  # filled at runtime
        "dimensions": {
            "routing_logic": {
                "grade": "B", "numeric": 3.0, "weight": 0.20, "confidence": "MEDIUM",
                "rationale": "Anchor-first protocol evident and dead legs analyzed; routing efficiency ratio sits in the adequate band rather than the efficient band. Sequence is defensible but carries some geography-driven fill dates.",
                "sub_signals": "anchors confirmed SOURCED · dead legs analyzed SOURCED · efficiency ratio MEASURED (adequate band) · market sequence JUDGED",
                "not_evaluable": []
            },
            "financial_model_integrity": {
                "grade": "A-", "numeric": 3.7, "weight": 0.20, "confidence": "HIGH",
                "rationale": "A complete break-even model exists and was built before routing was locked, with 70/85/100% sensitivity scenarios. One expense category is estimated without a firm basis.",
                "sub_signals": "break-even model SOURCED · modeled before routing SOURCED · sensitivity scenarios SOURCED · one expense estimate JUDGED",
                "not_evaluable": []
            },
            "offer_evaluation_quality": {
                "grade": "B", "numeric": 3.0, "weight": 0.15, "confidence": "MEDIUM",
                "rationale": "Offers evaluated against the framework with red-flag clauses identified; split-point reachability checked on most but not all backend offers. No below-threshold offer accepted without rationale.",
                "sub_signals": "framework applied SOURCED · red-flags identified SOURCED · split-point analysis AMBIGUOUS (partial)",
                "not_evaluable": []
            },
            "production_readiness": {
                "grade": "B+", "numeric": 3.3, "weight": 0.15, "confidence": "HIGH",
                "rationale": "Rider complete and appropriate to deal level; production advance completed with adequate lead time. Minor discrepancies expected to resolve before show week.",
                "sub_signals": "rider complete SOURCED · advance completed SOURCED · advance lead MEASURED (≥2 weeks)",
                "not_evaluable": []
            },
            "ticketing_strategy": {
                "grade": "B-", "numeric": 2.7, "weight": 0.12, "confidence": "MEDIUM",
                "rationale": "Tier structure defined and secondary market monitored, but pricing is applied with limited market-by-market analysis. Accessible GA floor maintained.",
                "sub_signals": "tier structure SOURCED · secondary monitored SOURCED · market-by-market price analysis AMBIGUOUS",
                "not_evaluable": []
            },
            "international_readiness": {
                "grade": "B-", "numeric": 2.7, "weight": 0.08, "confidence": "MEDIUM",
                "rationale": "International leg has a confirmed work-permit pathway and a territory streaming signal; withholding tax is not yet fully modeled in the P&L. Treat permit and tax specifics as routed to counsel.",
                "sub_signals": "work permit pathway SOURCED · territory streaming signal SOURCED · withholding modeling AMBIGUOUS",
                "not_evaluable": ["definitive withholding rates and treaty relief — route to the finance function and qualified tax counsel"]
            },
            "merch_planning": {
                "grade": "B", "numeric": 3.0, "weight": 0.06, "confidence": "MEDIUM",
                "rationale": "Merch planned and on track for tour start; hall fees documented for most venues. Per-market inventory plan is partial.",
                "sub_signals": "merch planned SOURCED · hall fees documented SOURCED (most venues) · per-market inventory JUDGED",
                "not_evaluable": []
            },
            "settlement_process": {
                "grade": "B", "numeric": 3.0, "weight": 0.04, "confidence": "MEDIUM",
                "rationale": "A line-by-line settlement review process is in place against the deal memo with a dispute protocol. Tour-wide tracking is being maintained.",
                "sub_signals": "review process SOURCED · dispute protocol SOURCED · tour-wide tracking JUDGED",
                "not_evaluable": []
            }
        },
        "hard_gates": {
            "break_even_gate":       "CLEAR — break-even model built before routing commitment (HG-1 not triggered)",
            "anchor_dates_gate":     "CLEAR — top anchor markets confirmed before secondary routing fill (HG-2 not triggered)",
            "work_permit_gate":      "CLEAR — work-permit pathway confirmed for the international leg (HG-3 not triggered)",
            "production_advance_gate": "CLEAR — production advance completed ≥2 weeks before every show (HG-4 not triggered)"
        },
        "composite": {
            "value": 3.1,
            "formula": "(3.0×0.20)+(3.7×0.20)+(3.0×0.15)+(3.3×0.15)+(2.7×0.12)+(2.7×0.08)+(3.0×0.06)+(3.0×0.04)",
            "scale": "0.0–4.3 (letter-grade numeric)",
            "label": "PROVISIONAL",
            "band": "Adequate campaign — gaps that reduce efficiency or upside",
            "unlock_condition": "≥30 outcome-checked tour-campaign evaluations"
        },
        "risk_classification": "NOTABLE_GAPS",
        "red_flags": [],
        "action_profile": [
            {"priority": "PRIORITY", "item": "Tighten the routing efficiency ratio into the efficient band by reviewing geography-driven fill dates against the distance-vs-revenue test."},
            {"priority": "OPTIMIZE", "item": "Add market-by-market price analysis before on-sale; the tier structure is set but pricing is not yet market-calibrated."},
            {"priority": "OPTIMIZE", "item": "Complete withholding-tax modeling for the international leg with the finance function before the P&L is treated as final."}
        ],
        "not_evaluable": [
            "Box-office outcome — this assessment scores the campaign's planning state from structured flags, not realized ticket sales. Settlement data closes the loop after the shows."
        ],
        "next_best_action": "Run the distance-vs-revenue test on every geography-driven fill date to lift the routing efficiency ratio, then finalize withholding-tax modeling on the international leg before locking the P&L. Route any work-permit and contract specifics to qualified counsel.",
        "advisory_footer": _TOUR_COMMANDER_ADVISORY_FOOTER
    },
    "mock_note": "TOUR_COMMANDER_MOCK_MODE=true — this is a canned assessment. Set TOUR_COMMANDER_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/tour-commander/assess", tags=["tour-commander"])
async def tour_commander_assess(req: TourCommanderAssessRequest):
    """
    Tour campaign quality assessment for a PLMKR artist's touring plan.

    The agent scores the eight-dimension Tour Campaign Quality Rubric (routing
    logic, financial-model integrity, offer-evaluation quality, production
    readiness, ticketing strategy, international readiness, merch planning,
    settlement process), states the four hard gates, computes a PROVISIONAL
    composite, and classifies campaign risk severity. It scores PLANNING STATE —
    not box-office outcome — and routes contracts, permits, and detailed
    financial modeling to the appropriate functions and qualified counsel.

    Artist identity is bound from the request payload (artist_name from the
    touring context), NOT from the Playmaker account profile, to prevent
    account-name vs. credited-artist mismatches.

    When TOUR_COMMANDER_MOCK_MODE=true (default), returns a canned scored
    assessment without calling the Anthropic API. Set TOUR_COMMANDER_MOCK_MODE=false
    with a valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if TOUR_COMMANDER_MOCK_MODE:
        result = dict(_TOUR_COMMANDER_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["campaign"]    = req.campaign.model_dump()
        # shallow-copy the assessment block so per-request fields don't mutate the template
        assessment = dict(result["assessment"])
        assessment["tour_type"] = req.campaign.tour_type
        result["assessment"] = assessment
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set TOUR_COMMANDER_MOCK_MODE=true to use mock mode.")

    system_prompt = build_tour_commander_system_prompt(skills_dir=SKILLS_DIR)

    c = req.campaign

    intl_line = (
        "International dates in scope — HG-3 applies."
        if c.has_international_dates else
        "No international dates — HG-3 NOT APPLICABLE; Dimension 6 NOT APPLICABLE."
    )

    user_prompt = f"""You are performing a PLMKR tour campaign quality assessment. You score PLANNING STATE — not box-office outcome. Use the eight-dimension Tour Campaign Quality Rubric, the four hard gates, and the Tour Routing Assessment template from your knowledge base.

ARTIST IDENTITY (use this — not any account name):
- Credited artist name: {req.artist_name}
- Artist territory: {req.artist_territory}

CAMPAIGN UNDER REVIEW (structured planning-state flags — not realized sales):
- Campaign name: {c.campaign_name or 'NOT NAMED'}
- Tour type: {c.tour_type}
- Markets in scope: {c.markets_count}
- {intl_line}
- Anchor dates confirmed before fill: {'YES' if c.anchor_dates_confirmed else 'NO — HG-2 candidate'}
- Routing efficiency ratio (show days ÷ total days): {c.routing_efficiency_ratio if c.routing_efficiency_ratio is not None else 'NOT STATED'}
- Dead legs analyzed: {'YES' if c.dead_legs_analyzed else 'NO'}
- Break-even model exists: {'YES' if c.break_even_model_exists else 'NO — HG-1 candidate'}
- Break-even modeled before routing lock: {'YES' if c.modeled_before_routing else 'NO — HG-1 candidate'}
- Sensitivity scenarios (70/85/100%) modeled: {'YES' if c.sensitivity_scenarios_modeled else 'NO'}
- Offers evaluated against the framework: {'YES' if c.offers_evaluated_against_framework else 'NO'}
- Split points analyzed: {c.split_points_analyzed if c.split_points_analyzed is not None else 'NOT STATED'}
- Red-flag clauses identified: {'YES' if c.red_flag_clauses_identified else 'NO'}
- Rider complete: {'YES' if c.rider_complete else 'NO'}
- Production advance completed: {'YES' if c.production_advance_completed else 'NO — HG-4 candidate'}
- Production advance lead (weeks before show): {c.advance_lead_weeks if c.advance_lead_weeks is not None else 'NOT STATED'}
- Price points set with market analysis: {'YES' if c.price_points_market_analyzed else 'NO'}
- Tier structure (GA/mid/VIP) defined: {'YES' if c.tier_structure_defined else 'NO'}
- Secondary market monitored: {'YES' if c.secondary_market_monitored else 'NO'}
- Work permits confirmed (international): {c.work_permits_confirmed if c.work_permits_confirmed is not None else 'NOT STATED'}
- Withholding tax modeled: {c.withholding_modeled if c.withholding_modeled is not None else 'NOT STATED'}
- Territory streaming signal: {c.territory_streaming_signal if c.territory_streaming_signal is not None else 'NOT STATED'}
- Merch planned: {'YES' if c.merch_planned else 'NO'}
- Hall fees documented: {'YES' if c.hall_fees_documented else 'NO'}
- Settlement review process in place: {'YES' if c.settlement_review_process else 'NO'}
- Named red-flag clauses present: {', '.join(c.red_flag_clauses) if c.red_flag_clauses else 'None named'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST IDENTITY above.
2. Score all 8 rubric dimensions. For each: letter grade, numeric equivalent, weight, sub-signal classification (MEASURED/SOURCED/JUDGED/NOT EVALUABLE), confidence band (HIGH/MEDIUM/LOW with reasons), and a 1–3 sentence rationale describing the planning state — not a box-office prediction. If the campaign has no international dates, mark Dimension 6 NOT APPLICABLE.
3. State all four hard gates (HG-1 Break-Even Before Commitment, HG-2 Anchor Before Fill, HG-3 Work-Permit Pathway, HG-4 Production Advance): CLEAR / TRIGGERED / NOT APPLICABLE with reason.
4. Compute the PROVISIONAL COMPOSITE on the 0.0–4.3 letter-grade numeric scale per the rubric formula. Label it PROVISIONAL and state the unlock condition. Treat any threshold figure as industry convention, not a recommendation.
5. Classify campaign risk severity descriptively (LOW_RISK / NOTABLE_GAPS / SIGNIFICANT_GAPS / HIGH_FINANCIAL_RISK / CRITICAL_RISK). This labels severity — it is NOT a recommendation to proceed, hold, or cancel.
6. List named red-flag clauses present: clause, what it does, and routing. Name the deficiency only.
7. Mark NOT EVALUABLE items and name the minimum data required. Never quote a guarantee, production cost, or P&L figure without a Tier A/B source.
8. Provide an Action Profile (IMMEDIATE / PRIORITY / OPTIMIZE / MAINTAIN) and a single NEXT BEST ACTION. Route contracts and permits to qualified counsel and detailed modeling to the finance function.
9. End with the advisory footer: "{_TOUR_COMMANDER_ADVISORY_FOOTER}"
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Tour assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "campaign":        req.campaign.model_dump(),
        "assessment_text": assessment_text,
        "advisory_footer": _TOUR_COMMANDER_ADVISORY_FOOTER,
        "model":           MODEL_SONNET,
    }


# ── INK-AND-AIR — Catalog Health (Publishing & Rights) Assessment ─────────────

class CatalogRightsInput(BaseModel):
    """Structured description of the catalog / work under review.

    The agent scores what is PRESENT and flags what is ABSENT — absence of a
    required step (a registration, an executed split sheet, a filed claim) is
    itself evaluable as a gap. These flags describe the catalog's rights state;
    they are not a valuation and not a legal opinion.
    """
    catalog_name:                      str  = ""
    work_count:                        int  = 0
    has_international_usage:            bool = False
    # D1 — Registration Completeness
    pro_registration_complete:         bool = False
    mechanical_registration_complete:  bool = False
    new_works_registered_before_release: bool = False
    # D2 — Collection Coverage
    active_income_streams:             list[str] = []   # performance|mechanical|sync|neighboring|ugc
    ugc_monetization_active:           bool = False
    neighboring_rights_registered:     bool = False
    # D3 — Royalty Recovery Readiness
    mlc_unmatched_claims_filed:        Optional[bool] = None
    black_box_claim_strategy:          bool = False
    # D4 — Identifier Completeness
    iswc_coverage_complete:            bool = False
    ipi_registered_all_parties:        bool = False
    active_revenue_without_identifiers: bool = False     # HG-1 trigger
    # D5 — Ownership Clarity
    split_sheets_executed:             bool = False
    chain_of_title_documented:         bool = False
    active_ownership_dispute:          bool = False       # HG-2 trigger (when formally filed)
    # D6 — Licensing Readiness
    one_stop_clearance:                Optional[bool] = None
    sync_rep_in_place:                 bool = False
    master_rights_clear:               bool = False
    # D7 — Territorial Coverage
    subpublishing_in_major_territories: bool = False
    territories_collecting_count:      int  = 0
    # D8 — Metadata Quality
    statement_matching_rate:           Optional[float] = None  # 0.0–1.0
    # D9 — Audit Status
    audit_window_status:               str  = "unknown"   # within|approaching|expired|unknown
    last_audit_within_window:          bool = False
    # D10 — Legal Exposure
    active_litigation:                 bool = False        # HG-4 trigger
    uncleared_sample_exposure:         Optional[bool] = None

class InkAndAirAssessRequest(BaseModel):
    artist_id:        str = ""
    artist_name:      str
    artist_territory: str = "unknown"
    catalog:          CatalogRightsInput
    additional_notes: str = ""

_INK_AND_AIR_ADVISORY_FOOTER = (
    "This is a publishing rights-infrastructure assessment — not a legal opinion, "
    "a financial valuation, or a sync-pitch strategy. Route deal execution and notice "
    "filing to qualified entertainment counsel, royalty modeling to the Finance/Royalties "
    "function, and sync pursuit to the Sync agent."
)

_INK_AND_AIR_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,   # filled at runtime
    "catalog": None,       # filled at runtime
    "domain_constraint": "Maximizes legitimate royalty capture and rights protection. Identifies and explains; never advises on negotiation, never gives a legal opinion.",
    "assessment": {
        "catalog_name": None,  # filled at runtime
        "dimensions": {
            "registration_completeness": {
                "grade": "B", "numeric": 7.0, "weight": 0.15, "confidence": "PARTIAL",
                "rationale": "PRO registration is present across the home territory and new works are registered before release; mechanical registration shows documented timing gaps on part of the catalog tail. Registration is prospective, not yet a fully closed loop.",
                "sub_signals": "PRO coverage SOURCED · mechanical coverage AMBIGUOUS · new-works lead time SOURCED · black-box-from-gaps NOT EVALUABLE",
                "not_evaluable": ["exact PRO/mechanical coverage percentages — requires a society-portal export, not supplied"]
            },
            "collection_coverage": {
                "grade": "B-", "numeric": 6.5, "weight": 0.15, "confidence": "PARTIAL",
                "rationale": "Performance and mechanical are collecting and at least one UGC platform is monetized; neighboring-rights infrastructure is initiated but not complete across all applicable territories. Gaps are identified with a remediation path.",
                "sub_signals": "performance SOURCED · mechanical SOURCED · UGC SOURCED · neighboring rights AMBIGUOUS",
                "not_evaluable": []
            },
            "royalty_recovery_readiness": {
                "grade": "C", "numeric": 5.0, "weight": 0.13, "confidence": "PARTIAL",
                "rationale": "Recovery mechanisms are largely unworked: MLC unmatched-pool claims are not confirmed filed and there is no active society black-box claim program. This is quantifiable value currently uncaptured — document it as a recovery opportunity.",
                "sub_signals": "MLC unmatched claims AMBIGUOUS · black-box strategy ABSENT · retroactive recovery NOT EVALUABLE",
                "not_evaluable": ["recoverable value — requires statement history and a dated registration-gap range; estimate only where evidence supports it"]
            },
            "identifier_completeness": {
                "grade": "B", "numeric": 7.0, "weight": 0.12, "confidence": "HIGH",
                "rationale": "ISWC coverage and IPI registration for all writers and publishers are in place; no active-revenue works lack identifiers (HG-1 not triggered). Some ISRC linkage gaps remain on older catalog.",
                "sub_signals": "ISWC SOURCED · IPI all parties SOURCED · active-revenue-without-identifiers ABSENT",
                "not_evaluable": []
            },
            "ownership_clarity": {
                "grade": "B+", "numeric": 8.0, "weight": 0.12, "confidence": "HIGH",
                "rationale": "Co-written works carry executed split sheets and chain of title is documented; no formally filed ownership dispute is on record (HG-2 not triggered).",
                "sub_signals": "executed splits SOURCED · chain of title SOURCED · active dispute ABSENT",
                "not_evaluable": []
            },
            "licensing_readiness": {
                "grade": "C+", "numeric": 6.0, "weight": 0.12, "confidence": "PARTIAL",
                "rationale": "Master rights are clear but one-stop clearance is not confirmed and no dedicated sync representative is in place; licensing is possible but happens by ad hoc negotiation. No HG-2 dispute caps this dimension.",
                "sub_signals": "master rights SOURCED · one-stop status AMBIGUOUS · sync rep ABSENT",
                "not_evaluable": ["turnaround capability — not described in the structured input"]
            },
            "territorial_coverage": {
                "grade": "C", "numeric": 5.0, "weight": 0.08, "confidence": "PARTIAL",
                "rationale": "Home territory plus a few majors are collecting via sub-publishing; no direct affiliations in non-Western markets and reciprocal coverage is unverified against streaming territory data.",
                "sub_signals": "sub-pub in majors SOURCED · territory count MEASURED · usage-vs-revenue overlap NOT EVALUABLE",
                "not_evaluable": ["usage-vs-revenue territory overlap — requires DSP territory streaming data"]
            },
            "metadata_quality": {
                "grade": "B-", "numeric": 6.5, "weight": 0.07, "confidence": "PARTIAL",
                "rationale": "Statement matching rate is in the mid-range with minor discrepancies under reconciliation; ISWC is mostly linked in the content-management system.",
                "sub_signals": "matching rate MEASURED · share consistency AMBIGUOUS · ISWC-in-CMS SOURCED",
                "not_evaluable": []
            },
            "audit_status": {
                "grade": "B", "numeric": 7.0, "weight": 0.04, "confidence": "HIGH",
                "rationale": "The contractual audit window is not expired and a statement-review practice is in place (HG-3 not triggered). New-revenue-stream monitoring is reactive rather than systematic.",
                "sub_signals": "audit window SOURCED (not expired) · review practice SOURCED · new-stream monitoring JUDGED",
                "not_evaluable": []
            },
            "legal_exposure": {
                "grade": "A-", "numeric": 8.5, "weight": 0.02, "confidence": "PARTIAL",
                "rationale": "No active infringement litigation names the catalog (HG-4 not triggered) and termination windows are tracked. Uncleared sample exposure is not confirmed either way and is flagged for documentation.",
                "sub_signals": "active litigation ABSENT · termination windows SOURCED · uncleared samples AMBIGUOUS · AI exposure CONTESTED (no settled law)",
                "not_evaluable": ["uncleared sample exposure — not confirmed in the structured input"]
            }
        },
        "hard_gates": {
            "identifier_gate":       "CLEAR — no active-revenue works without ISWC and PRO registration (HG-1 not triggered)",
            "ownership_dispute_gate":"CLEAR — no formally filed ownership dispute on record (HG-2 not triggered)",
            "audit_window_gate":     "CLEAR — contractual audit window not expired (HG-3 not triggered)",
            "litigation_gate":       "CLEAR — no active infringement litigation naming the catalog (HG-4 not triggered)"
        },
        "composite": {
            "value": 6.5,
            "formula": "(7.0×0.15)+(6.5×0.15)+(5.0×0.13)+(7.0×0.12)+(8.0×0.12)+(6.0×0.12)+(5.0×0.08)+(6.5×0.07)+(7.0×0.04)+(8.5×0.02)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked catalog evaluations",
            "note": "PROVISIONAL — not calibrated; not comparable across catalogs. Secondary to the per-dimension grades."
        },
        "risk_classification": "NOTABLE_GAPS",
        "asset_recovery_frame": "If this catalog went to sale, licensing, or audit today, due diligence would discount it primarily on royalty-recovery readiness (unworked unmatched/black-box claims) and territorial coverage (unverified reciprocal collection), with ownership clarity and legal exposure as genuine strengths. The largest near-term upside is filing the recovery claims and verifying foreign collection.",
        "action_profile": {
            "immediate": [],
            "priority": [
                {
                    "dimension": "royalty_recovery_readiness",
                    "gap": "MLC unmatched-pool claims not confirmed filed; no active black-box claim program.",
                    "action": "File MLC unmatched-pool claims (US) and stand up a society black-box claim program; track retroactive recovery windows.",
                    "estimated_recovery": "NOT ESTIMABLE without statement history and a dated registration-gap range — qualitative recoverability only."
                }
            ],
            "optimize": [
                {
                    "dimension": "licensing_readiness",
                    "gap": "One-stop clearance not confirmed; no dedicated sync representative.",
                    "action": "Confirm one-stop clearance authority and establish a rate card / single sync contact; route active sync pursuit to the Sync agent."
                },
                {
                    "dimension": "territorial_coverage",
                    "gap": "Reciprocal coverage unverified against streaming territory data (sub-threshold weight, still improvable).",
                    "action": "Map DSP territory streaming data against statement territories; confirm sub-publisher registration in the top revenue territories."
                }
            ],
            "maintain": [
                "registration_completeness", "collection_coverage", "identifier_completeness",
                "ownership_clarity", "metadata_quality", "audit_status", "legal_exposure"
            ]
        },
        "not_evaluable": [
            "Exact coverage percentages, recoverable value, and territory overlap — this assessment is built from structured presence/absence flags, not from society-portal exports, statements, or DSP territory data. A definitive evaluation requires those records."
        ],
        "next_best_action": "File the MLC unmatched-pool claims and stand up the black-box recovery program — the single highest-value documentation step. Provide 3-year statement history and a society-portal export to convert the PARTIAL-confidence dimensions to a sourced grade. Route any deal negotiation to qualified counsel and royalty modeling to the Finance/Royalties function.",
        "advisory_footer": _INK_AND_AIR_ADVISORY_FOOTER
    },
    "mock_note": "INK_AND_AIR_MOCK_MODE=true — this is a canned assessment. Set INK_AND_AIR_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/ink-and-air/assess", tags=["ink-and-air"])
async def ink_and_air_assess(req: InkAndAirAssessRequest):
    """
    Catalog Health (publishing & rights) assessment for a PLMKR artist's catalog.

    The agent scores the ten-dimension Catalog Health Rubric, states the four hard
    gates, answers the Asset-Recovery Frame, classifies rights-infrastructure risk
    severity, produces a prioritized Action Profile, and routes execution out. It
    identifies and explains — it never advises on negotiation, never gives a legal
    opinion, and never builds a financial valuation.

    Artist identity is bound from the request payload (artist_name from the catalog
    context), NOT from the Playmaker account profile, to prevent account-name vs.
    credited-artist mismatches.

    When INK_AND_AIR_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set INK_AND_AIR_MOCK_MODE=false with a valid
    ANTHROPIC_API_KEY to run a live assessment.
    """
    if INK_AND_AIR_MOCK_MODE:
        result = dict(_INK_AND_AIR_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["catalog"]     = req.catalog.model_dump()
        # shallow-copy the assessment block so per-request fields don't mutate the template
        assessment = dict(result["assessment"])
        assessment["catalog_name"] = req.catalog.catalog_name
        result["assessment"] = assessment
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set INK_AND_AIR_MOCK_MODE=true to use mock mode.")

    system_prompt = build_ink_and_air_system_prompt(skills_dir=SKILLS_DIR)

    cat = req.catalog

    user_prompt = f"""You are performing a PLMKR Catalog Health assessment (publishing & rights). You IDENTIFY and EXPLAIN — you never advise on negotiation, never give a legal opinion, and never build a financial valuation. Use the ten-dimension Catalog Health Rubric, the four hard gates, the Asset-Recovery Frame, and the Catalog Health Evaluation template from your knowledge base.

ARTIST IDENTITY (use this — not any account name):
- Credited artist name: {req.artist_name}
- Artist territory: {req.artist_territory}

CATALOG UNDER REVIEW (structured presence/absence flags — full statements/portal exports are NOT supplied):
- Catalog name: {cat.catalog_name or 'NOT NAMED'}
- Work count: {cat.work_count}
- Has international usage: {'YES' if cat.has_international_usage else 'NO'}
- PRO registration complete: {'YES' if cat.pro_registration_complete else 'NO'}
- Mechanical registration complete: {'YES' if cat.mechanical_registration_complete else 'NO'}
- New works registered before release: {'YES' if cat.new_works_registered_before_release else 'NO'}
- Active income streams: {', '.join(cat.active_income_streams) if cat.active_income_streams else 'None reported'}
- UGC monetization active: {'YES' if cat.ugc_monetization_active else 'NO'}
- Neighboring rights registered: {'YES' if cat.neighboring_rights_registered else 'NO'}
- MLC unmatched-pool claims filed: {cat.mlc_unmatched_claims_filed if cat.mlc_unmatched_claims_filed is not None else 'NOT STATED'}
- Black-box claim strategy in place: {'YES' if cat.black_box_claim_strategy else 'NO'}
- ISWC coverage complete: {'YES' if cat.iswc_coverage_complete else 'NO'}
- IPI registered for all parties: {'YES' if cat.ipi_registered_all_parties else 'NO'}
- Active revenue without identifiers: {'YES — HG-1 candidate' if cat.active_revenue_without_identifiers else 'NO'}
- Split sheets executed: {'YES' if cat.split_sheets_executed else 'NO'}
- Chain of title documented: {'YES' if cat.chain_of_title_documented else 'NO'}
- Active ownership dispute (formally filed): {'YES — HG-2 candidate' if cat.active_ownership_dispute else 'NO'}
- One-stop clearance: {cat.one_stop_clearance if cat.one_stop_clearance is not None else 'NOT STATED'}
- Sync representative in place: {'YES' if cat.sync_rep_in_place else 'NO'}
- Master rights clear: {'YES' if cat.master_rights_clear else 'NO'}
- Sub-publishing in major territories: {'YES' if cat.subpublishing_in_major_territories else 'NO'}
- Territories collecting (count): {cat.territories_collecting_count}
- Statement matching rate: {f'{cat.statement_matching_rate:.0%}' if cat.statement_matching_rate is not None else 'NOT STATED'}
- Audit window status: {cat.audit_window_status}
- Last audit within window: {'YES' if cat.last_audit_within_window else 'NO'}
- Active litigation naming the catalog: {'YES — HG-4 candidate' if cat.active_litigation else 'NO'}
- Uncleared sample exposure: {cat.uncleared_sample_exposure if cat.uncleared_sample_exposure is not None else 'NOT STATED'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST IDENTITY above.
2. Score all 10 rubric dimensions. For each: letter grade, numeric equivalent, weight, sub-signal classification (MEASURED/SOURCED/JUDGED/AMBIGUOUS/ABSENT/NOT EVALUABLE), confidence (HIGH/MEDIUM/LOW/PARTIAL with reason), and a 1–3 sentence rationale describing the economic consequence of the current state — not a negotiation position.
3. State all four hard gates (HG-1 Active Revenue Without Identifiers, HG-2 Unresolved Ownership Dispute, HG-3 Expired Audit Window, HG-4 Active Litigation): CLEAR or TRIGGERED with reason. HG-2 requires a formally filed dispute; a disclosed-but-unfiled dispute is CLEAR but must still be surfaced as a flagged risk.
4. Compute the PROVISIONAL COMPOSITE per the rubric formula. Label it PROVISIONAL and state the unlock condition. Treat any retention/threshold figure as industry convention, not a recommendation.
5. Answer the Asset-Recovery Frame explicitly with evidence from the dimension grades.
6. Classify rights-infrastructure risk severity descriptively (LOW_RISK / NOTABLE_GAPS / SIGNIFICANT_GAPS / MATERIALLY_DEFICIENT / CRITICALLY_DEFICIENT). This labels severity — it is NOT a recommendation to sign, sell, or walk away.
7. Produce an Action Profile (IMMEDIATE / PRIORITY / OPTIMIZE / MAINTAIN), with recoverability estimates ONLY where evidence supports them (labeled ESTIMATE with basis, or NOT ESTIMABLE).
8. Mark NOT EVALUABLE items and name the minimum data required.
9. Do not state any rate, advance, fee, or multiple as "market standard" without a Tier A/B source. Treat AI training-rights questions as contested unless settled.
10. End with the advisory footer: "{_INK_AND_AIR_ADVISORY_FOOTER}"
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Publishing assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "catalog":         req.catalog.model_dump(),
        "assessment_text": assessment_text,
        "advisory_footer": _INK_AND_AIR_ADVISORY_FOOTER,
        "model":           MODEL_SONNET,
    }


# ── ROYALTY-DOCTOR — Royalty Recovery Audit ──────────────────────────────────────

class RoyaltyCatalogInput(BaseModel):
    """Structured description of the catalog under recovery review.

    The agent scores the catalog's RECOVERY STATE — how completely it is collecting
    income it has already earned, and how ready it is to recover what it is not.
    Absence of a required step (a registration, a filed claim, a verification
    practice) is itself evaluable as a leak point. These flags are not a valuation
    and not a legal opinion.
    """
    catalog_name:                      str  = ""
    work_count:                        int  = 0
    has_international_usage:            bool = False
    # HG-1 inputs — minimum data set for an evaluable recovery audit
    statement_data_available:          bool = False
    registration_export_available:     bool = False
    # D1 — Registration Integrity
    pro_registration_complete:         bool = False
    mechanical_registration_complete:  bool = False
    neighboring_rights_registered:     bool = False
    identifiers_complete:              bool = False   # ISWC / ISRC / IPI on earning works
    active_revenue_without_identifiers: bool = False
    # D2 — Statement Verification
    statements_verified_against_dsp:   bool = False
    anomaly_review_practiced:          bool = False
    reserve_deductions_reviewed:       bool = False
    # D3 — Black-Box Recovery Readiness
    unmatched_pool_claims_filed:       Optional[bool] = None
    black_box_program_active:          bool = False
    proof_of_ownership_ready:          bool = False
    # D4 — Pipeline Coverage
    active_income_streams:             list[str] = []   # master|performance|mechanical|neighboring|sync_backend
    # D5 — Audit Readiness
    audit_window_status:               str  = "unknown"   # within|approaching|expired|unknown
    statement_history_retained:        bool = False
    soft_audit_practice:               bool = False
    # D6 — Collection-Timing Discipline
    lag_vs_missing_tracked:            bool = False
    # D7 — Recovery Documentation
    chain_of_title_documented:         bool = False
    split_sheets_executed:             bool = False

class RoyaltyDoctorAssessRequest(BaseModel):
    artist_id:        str = ""
    artist_name:      str
    artist_territory: str = "unknown"
    catalog:          RoyaltyCatalogInput
    additional_notes: str = ""

_ROYALTY_DOCTOR_ADVISORY_FOOTER = (
    "This is a royalty-recovery analysis — it identifies and documents where income "
    "is uncollected or underpaid and builds the recovery case. It is not a legal "
    "opinion, a contract interpretation, or a catalog valuation. Route formal audit "
    "demands and any legal action to qualified entertainment counsel, deal and "
    "valuation modeling to the Finance/Royalties function, copyright-law and "
    "publishing-deal questions to the Publishing function, and sync pursuit to the "
    "Sync function."
)

_ROYALTY_DOCTOR_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,   # filled at runtime
    "catalog": None,       # filled at runtime
    "domain_constraint": "Maximizes legitimate cash capture. Identifies, documents, and quantifies-where-evidence-supports; rules out normal pipeline lag before claiming loss; never fabricates a recoverable figure; never renders a legal opinion.",
    "assessment": {
        "catalog_name": None,  # filled at runtime
        "dimensions": {
            "registration_integrity": {
                "grade": "B-", "numeric": 6.5, "weight": 0.20, "confidence": "PARTIAL",
                "rationale": "PRO registration is complete across the home territory, but mechanical registration shows documented gaps on the catalog tail and neighboring rights are not registered with the applicable collectors. Each gap means earned income that cannot be matched to its owner.",
                "sub_signals": "PRO coverage SOURCED · mechanical coverage AMBIGUOUS · neighboring-rights registration ABSENT · identifier coverage SOURCED",
                "not_evaluable": ["exact PRO/mechanical coverage percentages — requires a society-portal export, not supplied"]
            },
            "statement_verification": {
                "grade": "C+", "numeric": 6.0, "weight": 0.18, "confidence": "PARTIAL",
                "rationale": "Statements receive spot checks but no systematic cross-reference against DSP dashboard data and no full twelve-category anomaly review. The reserve and deduction sections are not routinely reconciled — a common location for silent underpayment.",
                "sub_signals": "DSP cross-reference JUDGED (intermittent) · anomaly checklist JUDGED (not systematic) · reserve/deduction review ABSENT",
                "not_evaluable": []
            },
            "black_box_recovery_readiness": {
                "grade": "C", "numeric": 5.0, "weight": 0.16, "confidence": "PARTIAL",
                "rationale": "Recovery mechanisms are largely unworked: unmatched-pool claims are not confirmed filed and there is no active black-box claim program with the holding collectors. This is quantifiable value currently uncaptured and aging toward redistribution — document it as the primary recovery opportunity.",
                "sub_signals": "unmatched-pool claims AMBIGUOUS · black-box program ABSENT · proof-of-ownership package JUDGED (partial)",
                "not_evaluable": ["recoverable value — requires distribution history and a dated registration-gap range; NOT ESTIMABLE on the supplied flags"]
            },
            "pipeline_coverage": {
                "grade": "B", "numeric": 7.0, "weight": 0.14, "confidence": "PARTIAL",
                "rationale": "Master, performance, and mechanical streams are flowing; neighboring rights and sync backend are not confirmed collecting where international usage exists. The same plays are generating income on streams that are not all being captured.",
                "sub_signals": "master SOURCED · performance SOURCED · mechanical SOURCED · neighboring/sync-backend AMBIGUOUS",
                "not_evaluable": []
            },
            "audit_readiness": {
                "grade": "B", "numeric": 7.0, "weight": 0.12, "confidence": "HIGH",
                "rationale": "The contractual audit window is within range (not expired — HG-4 not triggered) and statement history is retained for the recoverable period. A soft-audit / written-inquiry practice exists but is used reactively rather than as a standing discipline.",
                "sub_signals": "audit window SOURCED (within) · statement history SOURCED · soft-audit practice JUDGED",
                "not_evaluable": []
            },
            "collection_timing_discipline": {
                "grade": "B+", "numeric": 8.0, "weight": 0.10, "confidence": "HIGH",
                "rationale": "The team distinguishes income in normal pipeline transit from income that is genuinely stuck or underpaid, and maps the expected lag per stream before flagging a loss. This keeps recovery claims credible and avoids premature audit demands (HG-3 discipline intact).",
                "sub_signals": "per-stream lag mapping JUDGED · usage-vs-receipt comparison JUDGED · in-transit/stuck/underpaid classification JUDGED",
                "not_evaluable": []
            },
            "recovery_documentation": {
                "grade": "C+", "numeric": 6.0, "weight": 0.10, "confidence": "PARTIAL",
                "rationale": "Chain of title is documented, but split sheets are not executed across all co-written works, leaving ownership ambiguity that would weaken a claim. The evidentiary chain is partial — sufficient for some claims, not yet for all.",
                "sub_signals": "chain of title SOURCED · executed splits AMBIGUOUS · identifier/registration records JUDGED (partial)",
                "not_evaluable": []
            }
        },
        "hard_gates": {
            "data_sufficiency_gate": "CLEAR — statement data and a registration export are available; the audit is evaluable (HG-1 not triggered)",
            "fabrication_gate":      "CLEAR — no recoverable figure is stated without statement history and a dated gap range (HG-2 not triggered)",
            "lag_diagnosis_gate":    "CLEAR — questioned streams are classified IN TRANSIT / STUCK / UNDERPAID before any loss claim (HG-3 not triggered)",
            "audit_window_gate":     "CLEAR — the contractual audit window is not expired for the period in question (HG-4 not triggered)"
        },
        "composite": {
            "value": 6.4,
            "formula": "(6.5×0.20)+(6.0×0.18)+(5.0×0.16)+(7.0×0.14)+(7.0×0.12)+(8.0×0.10)+(6.0×0.10)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked recovery audits",
            "note": "PROVISIONAL — not calibrated; not comparable across catalogs. Secondary to the per-dimension grades."
        },
        "recovery_posture": "NOTABLE_LEAKAGE",
        "leak_map": [
            {
                "leak": "Unmatched-pool / black-box mechanicals",
                "evidence": "Unmatched-pool claims not confirmed filed; no active claim program (D3 = C). Registration tail gaps (D1 = B-) feed the unmatched pool.",
                "recoverable": "NOT ESTIMABLE — requires distribution history and a dated registration-gap range. Time-sensitive: unmatched mechanicals are subject to market-share redistribution after a retention period (industry convention)."
            },
            {
                "leak": "Uncollected neighboring rights",
                "evidence": "Neighboring rights not registered with applicable collectors (D1) and not confirmed flowing (D4), despite international usage.",
                "recoverable": "NOT ESTIMABLE on the supplied flags — register, then file the claim with proof of ownership."
            },
            {
                "leak": "Silent statement underpayment (reserve / deductions)",
                "evidence": "Reserve and deduction sections not routinely reconciled (D2 = C+); no systematic DSP cross-reference.",
                "recoverable": "NOT ESTIMABLE without the statements themselves — surfaces through a soft audit citing specific line items."
            }
        ],
        "recovery_plan": {
            "immediate": [],
            "priority": [
                {
                    "dimension": "black_box_recovery_readiness",
                    "gap": "Unmatched-pool claims not filed; no black-box claim program; recoverable money aging toward redistribution.",
                    "action": "Close the mechanical/neighboring registration tail gaps, then file the unmatched-pool and historical claims with proof of ownership; track each retroactive recovery window against its deadline.",
                    "estimated_recovery": "NOT ESTIMABLE without distribution history and a dated registration-gap range — qualitative recoverability only."
                },
                {
                    "dimension": "registration_integrity",
                    "gap": "Mechanical tail gaps and unregistered neighboring rights are causing earned income to go unmatched.",
                    "action": "Complete mechanical registration on the catalog tail and register neighboring rights with the applicable collectors — this stops the leak prospectively and unlocks the retroactive claims."
                }
            ],
            "optimize": [
                {
                    "dimension": "statement_verification",
                    "gap": "Statements spot-checked, not systematically verified; reserve/deduction sections not reconciled.",
                    "action": "Stand up a standing statement-verification practice: DSP cross-reference plus the twelve-category anomaly checklist plus a reserve/deduction reconciliation on every statement; raise anomalies via a documented soft audit before any formal demand."
                },
                {
                    "dimension": "recovery_documentation",
                    "gap": "Split sheets not executed across all co-written works.",
                    "action": "Execute split sheets on the remaining co-written works to complete the evidentiary chain needed to file claims."
                }
            ],
            "maintain": [
                "pipeline_coverage", "audit_readiness", "collection_timing_discipline"
            ]
        },
        "not_evaluable": [
            "Recoverable dollar amounts, exact coverage percentages, and per-stream income — this audit is built from structured presence/absence flags, not from statements, distribution history, or society-portal exports. A definitive recovery figure requires those records plus a dated registration-gap or anomaly range."
        ],
        "next_best_action": "File the unmatched-pool / black-box claims and close the mechanical and neighboring-rights registration tail gaps — the single highest-value, time-sensitive recovery step. Supply 3-year statement history and a society-portal export to convert the PARTIAL-confidence dimensions to sourced grades and to make recoverable amounts estimable. Route any formal audit demand to qualified counsel.",
        "advisory_footer": _ROYALTY_DOCTOR_ADVISORY_FOOTER
    },
    "mock_note": "ROYALTY_DOCTOR_MOCK_MODE=true — this is a canned assessment. Set ROYALTY_DOCTOR_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/royalty-doctor/assess", tags=["royalty-doctor"])
async def royalty_doctor_assess(req: RoyaltyDoctorAssessRequest):
    """
    Royalty Recovery Audit for a PLMKR artist's catalog.

    The agent scores the seven-dimension Royalty Recovery Readiness Rubric, states
    the four hard gates, classifies recovery posture (leakage severity), produces a
    Leak Map and a prioritized Recovery Plan, and routes execution out. It IDENTIFIES,
    DOCUMENTS, and QUANTIFIES-WHERE-EVIDENCE-SUPPORTS — it rules out normal pipeline
    lag before claiming loss, never fabricates a recoverable figure, and never renders
    a legal opinion.

    Artist identity is bound from the request payload (artist_name from the catalog
    context), NOT from the Playmaker account profile, to prevent account-name vs.
    credited-artist mismatches.

    When ROYALTY_DOCTOR_MOCK_MODE=true (default), returns a canned scored assessment
    without calling the Anthropic API. Set ROYALTY_DOCTOR_MOCK_MODE=false with a valid
    ANTHROPIC_API_KEY to run a live assessment.
    """
    if ROYALTY_DOCTOR_MOCK_MODE:
        result = dict(_ROYALTY_DOCTOR_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["catalog"]     = req.catalog.model_dump()
        # shallow-copy the assessment block so per-request fields don't mutate the template
        assessment = dict(result["assessment"])
        assessment["catalog_name"] = req.catalog.catalog_name
        result["assessment"] = assessment
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set ROYALTY_DOCTOR_MOCK_MODE=true to use mock mode.")

    system_prompt = build_royalty_doctor_system_prompt(skills_dir=SKILLS_DIR)

    cat = req.catalog

    user_prompt = f"""You are performing a PLMKR Royalty Recovery Audit. You IDENTIFY, DOCUMENT, and QUANTIFY-WHERE-EVIDENCE-SUPPORTS where income is uncollected or underpaid. You rule out normal pipeline lag before claiming any loss, you never fabricate a recoverable figure, and you never render a legal opinion. Use the seven-dimension Royalty Recovery Readiness Rubric, the four hard gates, the Pipeline-First Diagnosis, and the Royalty Recovery Audit template from your knowledge base.

ARTIST IDENTITY (use this — not any account name):
- Credited artist name: {req.artist_name}
- Artist territory: {req.artist_territory}

CATALOG UNDER REVIEW (structured presence/absence flags — full statements/portal exports are NOT supplied unless the data-availability flags say so):
- Catalog name: {cat.catalog_name or 'NOT NAMED'}
- Work count: {cat.work_count}
- Has international usage: {'YES' if cat.has_international_usage else 'NO'}
- Statement data available: {'YES' if cat.statement_data_available else 'NO'}
- Registration export available: {'YES' if cat.registration_export_available else 'NO'}
- PRO registration complete: {'YES' if cat.pro_registration_complete else 'NO'}
- Mechanical registration complete: {'YES' if cat.mechanical_registration_complete else 'NO'}
- Neighboring rights registered: {'YES' if cat.neighboring_rights_registered else 'NO'}
- Identifiers complete (ISWC/ISRC/IPI): {'YES' if cat.identifiers_complete else 'NO'}
- Active revenue without identifiers: {'YES' if cat.active_revenue_without_identifiers else 'NO'}
- Statements verified against DSP data: {'YES' if cat.statements_verified_against_dsp else 'NO'}
- Anomaly review practiced: {'YES' if cat.anomaly_review_practiced else 'NO'}
- Reserve/deductions reviewed: {'YES' if cat.reserve_deductions_reviewed else 'NO'}
- Unmatched-pool claims filed: {cat.unmatched_pool_claims_filed if cat.unmatched_pool_claims_filed is not None else 'NOT STATED'}
- Black-box claim program active: {'YES' if cat.black_box_program_active else 'NO'}
- Proof of ownership ready: {'YES' if cat.proof_of_ownership_ready else 'NO'}
- Active income streams: {', '.join(cat.active_income_streams) if cat.active_income_streams else 'None reported'}
- Audit window status: {cat.audit_window_status}
- Statement history retained: {'YES' if cat.statement_history_retained else 'NO'}
- Soft-audit practice in place: {'YES' if cat.soft_audit_practice else 'NO'}
- Lag-vs-missing tracked: {'YES' if cat.lag_vs_missing_tracked else 'NO'}
- Chain of title documented: {'YES' if cat.chain_of_title_documented else 'NO'}
- Split sheets executed: {'YES' if cat.split_sheets_executed else 'NO'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST IDENTITY above.
2. Score all 7 rubric dimensions. For each: letter grade, numeric equivalent, weight, sub-signal classification (MEASURED/SOURCED/JUDGED/AMBIGUOUS/ABSENT/NOT EVALUABLE), confidence (HIGH/MEDIUM/LOW/PARTIAL with reason), and a 1–3 sentence rationale describing the recovery consequence of the current state.
3. State all four hard gates: HG-1 No statement/registration data (NOT EVALUABLE if both data flags are NO), HG-2 Fabricated recovery figure, HG-3 Lag misdiagnosed as underpayment, HG-4 Expired audit window (TIME-CRITICAL if the window is expired). CLEAR or TRIGGERED with reason. If HG-1 triggers, STOP and return NOT EVALUABLE naming the minimum data required.
4. Compute the PROVISIONAL COMPOSITE per the rubric formula. Label it PROVISIONAL and state the unlock condition. Treat any retention/threshold figure as industry convention, not a recommendation.
5. Classify recovery posture descriptively (FULLY_COLLECTING / MINOR_LEAKAGE / NOTABLE_LEAKAGE / SIGNIFICANT_LEAKAGE / SEVERE_LEAKAGE). This labels leakage severity — it is NOT a recommendation to sign, sell, or litigate.
6. Produce a Leak Map (where money is most likely going missing, ranked, each with its evidence basis) and a four-tier Recovery Plan (IMMEDIATE / PRIORITY / OPTIMIZE / MAINTAIN), with recoverable amounts labeled ESTIMATE (with basis) ONLY where evidence supports them, otherwise NOT ESTIMABLE.
7. Apply the Pipeline-First Diagnosis: classify any questioned stream IN TRANSIT / STUCK / UNDERPAID before asserting a loss.
8. Mark NOT EVALUABLE items and name the minimum data required.
9. Do not state any rate, mechanical rate, society retention, or multiple as "market standard" without a Tier A/B source.
10. End with the advisory footer: "{_ROYALTY_DOCTOR_ADVISORY_FOOTER}"
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Royalty recovery assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "catalog":         req.catalog.model_dump(),
        "assessment_text": assessment_text,
        "advisory_footer": _ROYALTY_DOCTOR_ADVISORY_FOOTER,
        "model":           MODEL_SONNET,
    }


# ── PRODUCER-CONNECT — Production Readiness assessment ────────────────────────────

class ProductionProjectInput(BaseModel):
    """Structured description of the recording project under readiness review.

    Beat scores the project's PRODUCTION READINESS — how ready it is to reach a
    release-ready, spec-compliant master on schedule and on budget — across eight
    dimensions. Absence of a required step (a signed brief, a deal memo, a
    contingency reserve, an embedded identifier) is itself evaluable. These flags
    are a readiness assessment, not a contract, a legal opinion, or a sync valuation.
    """
    project_name:                       str  = ""
    project_scope:                      str  = "single"   # single|ep|album
    target_format:                      str  = "streaming"
    # D1 — Creative Direction Clarity
    reference_tracks_count:             int  = 0
    creative_brief_signed:              bool = False
    sonic_target_measurable:            bool = False
    # D2 — Producer/Team Fit
    producer_selected:                  bool = False
    team_credits_genre_matched:         bool = False
    rates_confirmed_in_writing:         bool = False
    full_team_locked:                   bool = False
    # D3 — Song/Arrangement Readiness
    demo_exists:                        bool = False
    structure_locked:                   bool = False
    composition_locked:                 bool = False
    # D4 — Technical Quality & Standards (hard gate)
    mix_stage:                          str  = "not_started"  # not_started|in_progress|mixed|mastered
    measured_loudness_in_spec:          Optional[bool] = None
    true_peak_in_spec:                  Optional[bool] = None
    format_conforms:                    Optional[bool] = None
    stems_complete:                     Optional[bool] = None
    qc_bounced:                         bool = False           # Dim-4 hard gate trigger
    # D5 — Budget Discipline
    budget_documented:                  bool = False
    phase_allocated:                    bool = False
    contingency_reserved:               bool = False
    actuals_reconciled:                 bool = False
    # D6 — Delivery & QC Completeness (hard gate)
    stereo_master_present:              Optional[bool] = None
    isrc_assigned:                      Optional[bool] = None
    core_metadata_complete:             Optional[bool] = None
    credits_finalized:                  Optional[bool] = None
    # D7 — Schedule Feasibility
    schedule_exists:                    bool = False
    team_availability_confirmed:        bool = False
    release_date_buffer_weeks:          Optional[int]  = None
    # D8 — Deal & Ownership Structure
    deal_memos_executed:                bool = False
    master_ownership_assigned:          bool = False
    wfh_confirmed_session_players:      Optional[bool] = None
    cowrite_flagged:                    Optional[bool] = None

class BankConsultRequest(BaseModel):
    """Request body for the shared knowledge-bank consult route."""
    agent: str
    query: str


class ProducerConnectAssessRequest(BaseModel):
    artist_id:        str = ""
    artist_name:      str
    artist_territory: str = "unknown"
    project:          ProductionProjectInput
    additional_notes: str = ""

_PRODUCER_CONNECT_ADVISORY_FOOTER = (
    "This is a production-readiness and casting assessment — not a contract, a legal "
    "opinion, a song/talent verdict, or a sync valuation. Route deal papering and "
    "clause interpretation to qualified counsel, composition splits and co-writes to "
    "the Publishing function, placement pricing to the Sync function, and song/talent "
    "selection to A&R. Beat identifies, scopes, casts, and QC-checks — it does not "
    "negotiate, paper, or place."
)

_PRODUCER_CONNECT_MOCK_ASSESSMENT = {
    "status": "ok",
    "mock": True,
    "artist_name": None,   # filled at runtime
    "project": None,       # filled at runtime
    "operating_principle": "The master is the asset. Every call is scored against a release-ready, spec-compliant, creatively-realized recording — quality, time, and budget tradeoffs named, never pretended free.",
    "assessment": {
        "project_name": None,  # filled at runtime
        "dimensions": {
            "creative_direction_clarity": {
                "grade": "B", "numeric": 7.0, "weight": 0.18, "confidence": "PARTIAL",
                "rationale": "References are cited and a measurable sonic target is described, but the written brief is not yet signed by both artist and producer. Direction is consistent enough to track against; the sign-off gap leaves room for mid-session re-alignment.",
                "evidence": "references SOURCED · measurable target SOURCED · brief sign-off ABSENT",
                "assumption": "If a signed brief and a documented demo A/B were confirmed, this moves toward 9."
            },
            "producer_team_fit": {
                "grade": "C+", "numeric": 6.0, "weight": 0.16, "confidence": "PARTIAL",
                "rationale": "Producer is selected on genre-matching credits and the rate is within range, but rates are not yet confirmed in writing and the mixer/mastering engineer are identified rather than locked. The team is preliminary, not confirmed.",
                "evidence": "producer credits SOURCED · rate-in-range SOURCED · rates-in-writing ABSENT · full team locked ABSENT",
                "assumption": "If signed deal memos for all key roles were reviewed, this moves toward 9 (and lifts Dim-8)."
            },
            "song_arrangement_readiness": {
                "grade": "B", "numeric": 7.0, "weight": 0.14, "confidence": "HIGH",
                "rationale": "Demo exists at the target tempo, structure is locked, and composition is effectively complete; minor refinements possible but no structural changes expected in tracking.",
                "evidence": "demo SOURCED · structure locked SOURCED · composition locked SOURCED",
                "assumption": "If a producer-delivered pre-pro arrangement map were confirmed, this moves toward 10."
            },
            "technical_quality_standards": {
                "grade": "C", "numeric": 5.0, "weight": 0.14, "confidence": "PARTIAL",
                "rationale": "Mix is in progress and measurable, not yet mastered; loudness/true-peak/format are not yet measured against the current platform spec. In progress and on track — only a measured spec failure or a QC bounce would trigger the NOT DELIVERABLE gate (it is not triggered here).",
                "evidence": "mix in progress JUDGED · measured loudness NOT EVALUABLE (not yet mastered) · qc bounce ABSENT",
                "not_evaluable": ["measured integrated loudness and true-peak — requires a mastered file and a compliant meter, not supplied"],
                "assumption": "Once mastered and metered to the current platform spec, this moves to 7+ (or triggers the gate if it fails)."
            },
            "budget_discipline": {
                "grade": "C+", "numeric": 6.0, "weight": 0.12, "confidence": "PARTIAL",
                "rationale": "A phase-allocated budget with a ≥10% contingency line exists and is owner-approved, but actuals are not yet reconciled at phase close and not every cost item is signed-deal-memo-backed. Documented but not yet fully tracked.",
                "evidence": "phase-allocated budget SOURCED · contingency SOURCED · actuals reconciled ABSENT",
                "assumption": "If phase-close reconciliations and signed memos for every line were confirmed, this moves toward 9."
            },
            "delivery_qc_completeness": {
                "grade": "C", "numeric": 5.0, "weight": 0.12, "confidence": "PARTIAL",
                "rationale": "Package is assembling — stereo master and identifier path are in place and metadata is drafting — but stems are not yet verified against the full distributor spec and credits are not yet artist-approved in writing. No required element is confirmed missing, so the RELEASE BLOCKED gate is not triggered.",
                "evidence": "stereo master path SOURCED · ISRC path SOURCED · stems-verified ABSENT · credits finalized ABSENT",
                "assumption": "If a verified full package (stems summing to master, metadata through QC, credits approved) were confirmed, this moves toward 10."
            },
            "schedule_feasibility": {
                "grade": "C+", "numeric": 6.0, "weight": 0.08, "confidence": "PARTIAL",
                "rationale": "A phase-by-phase schedule exists with key availability confirmed and a release-date buffer, but the mastering slot and ≥2-week distributor buffer are not yet both locked. Achievable with 1–2 named risks.",
                "evidence": "schedule SOURCED · availability SOURCED · ≥2-week delivery buffer AMBIGUOUS",
                "assumption": "If a fully signed-off calendar with the mastering slot booked were confirmed, this moves toward 9."
            },
            "deal_ownership_structure": {
                "grade": "C", "numeric": 5.0, "weight": 0.06, "confidence": "PARTIAL",
                "rationale": "Master ownership is assigned in writing and deal memos exist for key roles, but session-player work-for-hire is not yet confirmed across the board and any co-write implication is not yet flagged to Publishing. A well-assembled team can still be legally exposed until every memo is signed.",
                "evidence": "master ownership SOURCED · key-role memos SOURCED · session-player WFH AMBIGUOUS · co-write flag ABSENT",
                "assumption": "If all session-player WFH clauses were signed and any co-write referred to Publishing, this moves toward 9."
            }
        },
        "hard_gates": {
            "technical_quality_gate": "CLEAR — mix in progress and measurable; no measured spec failure and no QC bounce (Dim-4 not at 1). NOT DELIVERABLE is not triggered. Re-check at mastering against the current platform spec.",
            "delivery_qc_gate":       "CLEAR — no required package element confirmed missing (stereo master, stems, identifier, core metadata, credits) (Dim-6 not at 1). RELEASE BLOCKED is not triggered."
        },
        "composite": {
            "value": 6.0,
            "formula": "(7.0×0.18)+(6.0×0.16)+(7.0×0.14)+(5.0×0.14)+(6.0×0.12)+(5.0×0.12)+(6.0×0.08)+(5.0×0.06)",
            "label": "PROVISIONAL",
            "unlock_condition": "≥30 outcome-checked production evaluations",
            "note": "PROVISIONAL — not calibrated; not comparable across projects. Secondary to the per-dimension grades. Thresholds and bands are industry convention, not a go/no-go verdict."
        },
        "readiness_band": "YELLOW_PROCEED_WITH_NAMED_GAPS",
        "action_profile": {
            "immediate": [
                {
                    "dimension": "creative_direction_clarity",
                    "gap": "Written creative brief not yet signed by both artist and producer.",
                    "action": "Get the brief — ≥3 annotated references, measurable sonic target — countersigned by artist and producer before booking any further studio dates. The cheapest insurance in the budget."
                }
            ],
            "priority": [
                {
                    "dimension": "producer_team_fit",
                    "gap": "Rates not confirmed in writing; mixer/mastering engineer not locked.",
                    "action": "Send and countersign deal memos for all key roles (rate, scope, revision rounds, ownership consequence) — this also lifts Dim-8 from a told to a sourced basis."
                },
                {
                    "dimension": "delivery_qc_completeness",
                    "gap": "Stems not verified against full distributor spec; credits not artist-approved.",
                    "action": "Confirm stems sum to the master and match the naming spec; finalize and get written artist approval of credits before the mastering handoff."
                }
            ],
            "optimize": [
                {
                    "dimension": "budget_discipline",
                    "gap": "Actuals not yet reconciled at phase close; not all lines signed-memo-backed.",
                    "action": "Stand up phase-close reconciliation (invoices + open commitments) and convert ESTIMATE lines to signed deal memos."
                },
                {
                    "dimension": "deal_ownership_structure",
                    "gap": "Session-player work-for-hire not confirmed across the board; co-write implication not yet routed.",
                    "action": "Sign WFH clauses for every session player before tracking; if any producer contribution crosses into composition, flag it and refer the split to the Publishing function."
                }
            ],
            "maintain": [
                "song_arrangement_readiness", "technical_quality_standards", "schedule_feasibility"
            ]
        },
        "not_evaluable": [
            "Measured loudness/true-peak/format conformance and final credits — this assessment is built from structured readiness flags, not from a mastered file, a meter output, a distributor QC receipt, or signed deal memos. A release-ready verdict requires those records, and any platform spec or rate is industry convention until verified against current data."
        ],
        "next_best_action": "Lock the signed creative brief, then countersign deal memos for all key roles — the two highest-leverage documentation steps. Re-run Dim-4 against a mastered, metered file at the current platform spec before any release-ready claim. Route contract papering to counsel, composition splits to Publishing, and placement pricing to Sync.",
        "advisory_footer": _PRODUCER_CONNECT_ADVISORY_FOOTER
    },
    "mock_note": "PRODUCER_CONNECT_MOCK_MODE=true — this is a canned assessment. Set PRODUCER_CONNECT_MOCK_MODE=false with a valid ANTHROPIC_API_KEY to run a live assessment."
}


@app.post("/api/agents/producer-connect/assess", tags=["producer-connect"])
async def producer_connect_assess(req: ProducerConnectAssessRequest):
    """
    Production Readiness assessment for a PLMKR artist's recording project.

    Beat (Production Specialist) scores the eight-dimension Production Readiness
    Rubric, states the two hard gates (Dim-4 Technical Quality → NOT DELIVERABLE,
    Dim-6 Delivery & QC → RELEASE BLOCKED), computes a PROVISIONAL composite and
    output band, produces a prioritized Action Profile, and routes execution out.
    Beat identifies, scopes, casts, and QC-checks — it never negotiates a contract,
    prices a placement, splits a composition, or picks the song.

    Artist identity is bound from the request payload (artist_name), NOT from the
    Playmaker account profile, to prevent account-name vs. credited-artist mismatches.

    When PRODUCER_CONNECT_MOCK_MODE=true (default), returns a canned scored
    assessment without calling the Anthropic API. Set PRODUCER_CONNECT_MOCK_MODE=false
    with a valid ANTHROPIC_API_KEY to run a live assessment.
    """
    if PRODUCER_CONNECT_MOCK_MODE:
        result = dict(_PRODUCER_CONNECT_MOCK_ASSESSMENT)
        result["artist_name"] = req.artist_name
        result["project"]     = req.project.model_dump()
        # shallow-copy the assessment block so per-request fields don't mutate the template
        assessment = dict(result["assessment"])
        assessment["project_name"] = req.project.project_name
        result["assessment"] = assessment
        return result

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="AI unavailable: ANTHROPIC_API_KEY not configured. "
                                   "Set PRODUCER_CONNECT_MOCK_MODE=true to use mock mode.")

    system_prompt = build_producer_connect_system_prompt(skills_dir=SKILLS_DIR)

    p = req.project

    user_prompt = f"""You are performing a PLMKR Production Readiness assessment as Beat, Production Specialist. You IDENTIFY, SCOPE, CAST, and QC-CHECK — you never negotiate a contract, price a placement, split a composition, or pick the song/talent. Use the eight-dimension Production Readiness Rubric, the two hard gates, the output bands, and the Production Readiness Scorecard template from your knowledge base.

ARTIST IDENTITY (use this — not any account name):
- Credited artist name: {req.artist_name}
- Artist territory: {req.artist_territory}

PROJECT UNDER REVIEW (structured readiness flags — a mastered file, meter output, distributor QC receipt, and signed deal memos are NOT supplied):
- Project name: {p.project_name or 'NOT NAMED'}
- Scope: {p.project_scope}
- Target format: {p.target_format}
- Reference tracks cited (count): {p.reference_tracks_count}
- Creative brief signed by both parties: {'YES' if p.creative_brief_signed else 'NO'}
- Sonic target measurable (genre/sub-genre/era/BPM/palette/dynamic): {'YES' if p.sonic_target_measurable else 'NO'}
- Producer selected: {'YES' if p.producer_selected else 'NO'}
- Team credits genre-matched: {'YES' if p.team_credits_genre_matched else 'NO'}
- Rates confirmed in writing: {'YES' if p.rates_confirmed_in_writing else 'NO'}
- Full team locked: {'YES' if p.full_team_locked else 'NO'}
- Demo exists: {'YES' if p.demo_exists else 'NO'}
- Song structure locked: {'YES' if p.structure_locked else 'NO'}
- Composition/lyrics locked: {'YES' if p.composition_locked else 'NO'}
- Mix stage: {p.mix_stage}
- Measured loudness in spec: {p.measured_loudness_in_spec if p.measured_loudness_in_spec is not None else 'NOT MEASURED'}
- True-peak in spec: {p.true_peak_in_spec if p.true_peak_in_spec is not None else 'NOT MEASURED'}
- Delivery format conforms: {p.format_conforms if p.format_conforms is not None else 'NOT STATED'}
- Stems complete and verified: {p.stems_complete if p.stems_complete is not None else 'NOT STATED'}
- QC bounced by distributor/mastering: {'YES — Dim-4 hard gate candidate' if p.qc_bounced else 'NO'}
- Budget documented: {'YES' if p.budget_documented else 'NO'}
- Budget phase-allocated: {'YES' if p.phase_allocated else 'NO'}
- Contingency reserved (>=10%): {'YES' if p.contingency_reserved else 'NO'}
- Actuals reconciled at phase close: {'YES' if p.actuals_reconciled else 'NO'}
- Stereo master present: {p.stereo_master_present if p.stereo_master_present is not None else 'NOT STATED'}
- ISRC assigned/embedded: {p.isrc_assigned if p.isrc_assigned is not None else 'NOT STATED'}
- Core metadata complete: {p.core_metadata_complete if p.core_metadata_complete is not None else 'NOT STATED'}
- Credits finalized (artist-approved in writing): {p.credits_finalized if p.credits_finalized is not None else 'NOT STATED'}
- Production schedule exists: {'YES' if p.schedule_exists else 'NO'}
- Team availability confirmed: {'YES' if p.team_availability_confirmed else 'NO'}
- Release-date buffer (weeks after delivery): {p.release_date_buffer_weeks if p.release_date_buffer_weeks is not None else 'NOT STATED'}
- Deal memos executed for key roles: {'YES' if p.deal_memos_executed else 'NO'}
- Master ownership assigned in writing: {'YES' if p.master_ownership_assigned else 'NO'}
- Session-player work-for-hire confirmed: {p.wfh_confirmed_session_players if p.wfh_confirmed_session_players is not None else 'NOT STATED'}
- Co-write implication flagged to Publishing: {p.cowrite_flagged if p.cowrite_flagged is not None else 'NOT STATED'}

ADDITIONAL CONTEXT:
{req.additional_notes or 'None provided.'}

INSTRUCTIONS:
1. Use ONLY the artist name from the ARTIST IDENTITY above.
2. Score all 8 rubric dimensions. For each: letter grade, numeric equivalent, weight, an evidence classification (MEASURED/SOURCED/JUDGED/AMBIGUOUS/ABSENT/NOT EVALUABLE), confidence (HIGH/MEDIUM/LOW/PARTIAL with reason), a 1–3 sentence rationale describing the readiness consequence of the current state, and the assumption that, if wrong, would move the score by >=1. Score each dimension independently; told-absence of infrastructure is evidence (scored on its anchor), while a missing track record on a first/early project defaults to 3 labeled INFERRED and is never scored below 3 on absence alone.
3. State both hard gates — Technical Quality Gate (Dim-4 = 1 → NOT DELIVERABLE) and Delivery & QC Gate (Dim-6 = 1 → RELEASE BLOCKED): CLEAR or TRIGGERED with reason. Only a measured spec failure or a QC bounce triggers Dim-4; only a confirmed-missing required package element triggers Dim-6.
4. Compute the COMPOSITE per the rubric formula and assign the output band (GREEN/YELLOW/AMBER/RED). Label the composite PROVISIONAL and state the unlock condition. If either gate is TRIGGERED, state the gate verdict first — it overrides the band for the blocked verdict.
5. Never state a loudness or true-peak value as a measurement unless a meter output is supplied — otherwise label it TARGET. Treat any platform-specific spec or any rate as industry convention / ESTIMATE — NOT QUOTABLE until verified against current data.
6. Produce an Action Profile (IMMEDIATE / PRIORITY / OPTIMIZE / MAINTAIN) keyed by dimension with concrete next steps.
7. Mark NOT EVALUABLE items and name the minimum record required.
8. If any producer contribution crosses into composition (a co-write), flag it and refer it to the Publishing function — do not resolve the split here.
9. End with the advisory footer: "{_PRODUCER_CONNECT_ADVISORY_FOOTER}"
"""

    try:
        resp = await async_client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        assessment_text = resp.content[0].text
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"Production readiness assessment failed: {str(e)}")

    return {
        "status":          "ok",
        "mock":            False,
        "artist_name":     req.artist_name,
        "project":         req.project.model_dump(),
        "assessment_text": assessment_text,
        "advisory_footer": _PRODUCER_CONNECT_ADVISORY_FOOTER,
        "model":           MODEL_SONNET,
    }


@app.post("/api/bank/consult", tags=["knowledge-bank"])
async def bank_consult(req: BankConsultRequest):
    """
    Shared knowledge-bank consultation — lets ANY agent (paired or unpaired) pull
    assembled expert knowledge from ANY of the 9 domains.

    Paired agents (the 9 with a home domain) get their home domain by default plus
    any query keyword matches; unpaired agents (e.g. merch-empire) get pure query
    matches. The bank is PURELY deterministic — no Anthropic/LLM call is made.

    When BANK_CONSULT_MOCK_MODE=true (default), the route returns the deterministic
    consult result directly. The flag exists only to mirror the assess-route shape;
    there is no live-LLM branch because retrieval needs none.
    """
    if BANK_CONSULT_MOCK_MODE:
        result = _bank_consult_for_agent(req.agent, req.query)
        return {
            "status":      "ok",
            "mock":        True,
            "agent":       result["agent"],
            "home_domain": result["home_domain"],
            "domains":     result["domains"],
            "knowledge":   result["knowledge"],
        }

    # Retrieval is deterministic; a non-mock path still serves the same result.
    result = _bank_consult_for_agent(req.agent, req.query)
    return {
        "status":      "ok",
        "mock":        False,
        "agent":       result["agent"],
        "home_domain": result["home_domain"],
        "domains":     result["domains"],
        "knowledge":   result["knowledge"],
    }
