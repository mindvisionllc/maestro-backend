#!/usr/bin/env python3
"""
tts_latency_probe.py — VOICE_DIAGNOSIS.md Pass 3, section 1 (MEASURE).

Synthesizes fixed-length strings through the SAME Kokoro code path the app
uses in production: a real HTTP POST to this repo's own /api/tts/synth
endpoint (identical route CallScreen.js's fetchTtsAudio() and
voice_probe.py's stage (c) call), against a locally running backend.

For each target length in CHAR_LENGTHS, does N_ITERATIONS synth calls using
DISTINCT text per call (same length, different content) — reusing identical
text would hit /api/tts/synth's on-disk audio cache (main.py:734-737,
keyed on md5(voice:speed:text)) after the first call, silently turning every
later "synth" into a cache read and hiding the real per-call variance this
probe exists to measure.

No production code is touched by this script. Read-only against the server.

Usage:
  python3 scripts/tts_latency_probe.py                       # default 5 iters/length
  python3 scripts/tts_latency_probe.py --base-url http://127.0.0.1:8000
  python3 scripts/tts_latency_probe.py --iterations 5 --voice am_onyx
"""
import argparse
import base64
import io
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import httpx

CHAR_LENGTHS = [100, 250, 500, 1000, 2000]
N_ITERATIONS = 5
TEST_ARTIST_ID = "tts-latency-probe-artist"

# A pool of natural, spoken-register music-business sentences (the same
# register real Marcus replies use) — long enough in aggregate to build any
# of the target lengths by rotating through it per iteration, so no two
# iterations at the same length share identical text.
_SENTENCE_POOL = [
    "Let's talk about your next move before you lock anything in.",
    "The offer looks solid on paper, but the routing between cities is going to eat three days you don't have.",
    "I'd push back on the exclusivity clause — you don't want to be boxed out of streaming syncs for eighteen months.",
    "Your streaming numbers are trending up in three markets, and that's exactly where we should focus the next single.",
    "Before you sign anything, get the promoter to confirm the guarantee in writing, not just a verbal handshake.",
    "The merch margins on that vendor are thin — I'd renegotiate before the tour, not during it.",
    "Radio's a slow burn right now, but the playlist pickups are doing real work for your reach.",
    "You've got leverage here because two other venues want the date — use it.",
    "Let's not overcommit the fall calendar until the mixing is actually finished and mastered.",
    "The sync licensing inquiry is real money, but read the buyout terms twice before you say yes.",
    "Your last release cycle taught us the fans respond best to behind-the-scenes content, not polished trailers.",
    "I'd rather you play the smaller room and sell it out than half-fill the bigger one.",
    "The label's advance sounds generous until you look at the recoupment schedule line by line.",
    "Keep the setlist tight — twelve songs, no filler, and end on the one that's charting.",
    "We should lock the support act this week before another agent scoops the slot.",
]


def _make_text(target_len: int, variant: int) -> str:
    """Build a string near target_len characters, rotating through the pool
    so each (length, variant) pair gets distinct content."""
    parts = []
    total = 0
    i = variant  # offset the starting sentence per variant
    while total < target_len:
        s = _SENTENCE_POOL[i % len(_SENTENCE_POOL)]
        parts.append(s)
        total += len(s) + 1
        i += 1
    text = " ".join(parts)
    return text[:target_len].rsplit(" ", 1)[0] if len(text) > target_len else text


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate) if rate else 0.0


def _assert_tts_healthy(client: "httpx.Client", base_url: str) -> dict:
    """VOICE_DIAGNOSIS.md §D2 (Pass 4): read before each length series via the
    C4 health fields (main.py's /api/tts/status) — never trust a series that
    started while the worker was dead or already suspiciously close to
    stalling; that would measure recovery/queue time, not synth time, the
    exact contamination this pass exists to rule out."""
    resp = client.get(f"{base_url}/api/tts/status")
    resp.raise_for_status()
    return resp.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--voice", default="am_onyx", help="Marcus's Kokoro voice ID")
    ap.add_argument("--iterations", type=int, default=N_ITERATIONS)
    ap.add_argument("--lengths", default=None,
                     help="Comma-separated char-length targets, e.g. 100,130,160,200,300,450 "
                          "(default: the module's own CHAR_LENGTHS)")
    ap.add_argument("--out", default=None,
                     help="Path for the raw-data JSON dump (default: docs/tts_latency_probe_raw.json)")
    args = ap.parse_args()
    lengths = [int(x) for x in args.lengths.split(",")] if args.lengths else CHAR_LENGTHS

    # Pass 4: /api/tts/synth is now identity-scoped (PLMKR restoration, same
    # day) — this script never talks to Twilio/SMS, it just needs a locally-
    # signed session so its own requests pass artist-scope auth. Read from
    # env, never hardcoded/logged: mint one with
    #   PLMKR_SESSION_SECRET=... PLMKR_IDENTITY_SECRET=... python3 -c \
    #     "import artist_identity as a; print(a.issue_session('tts-latency-probe-artist')['access_token'])"
    bearer = os.environ.get("PLMKR_PROBE_BEARER_TOKEN", "")
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    client = httpx.Client(timeout=60.0, headers=headers)
    print(f"tts_latency_probe.py against {args.base_url}  voice={args.voice}  "
          f"iterations/length={args.iterations}  lengths={lengths}\n")

    rows = []  # (chars, [ms...], [bytes...], [audio_s...])
    for target_len in lengths:
        # D2: assert health before the series; on a wedged/dead worker, wait
        # briefly for the supervisor's own kill+respawn (main.py §C) to
        # finish, then re-check once before giving up on this series.
        status = _assert_tts_healthy(client, args.base_url)
        if status.get("wedged") or not status.get("worker", {}).get("alive", True):
            print(f"  chars~{target_len}: TTS not healthy before this series "
                  f"({status}) — waiting 5s and re-checking once")
            time.sleep(5)
            status = _assert_tts_healthy(client, args.base_url)
            if status.get("wedged") or not status.get("worker", {}).get("alive", True):
                print(f"  chars~{target_len}: SKIPPED — still not healthy ({status})")
                continue
        ms_list, bytes_list, dur_list, chars_list = [], [], [], []
        for it in range(args.iterations):
            text = _make_text(target_len, it)
            call_id = f"latprobe-{target_len}-{it}-{int(time.time()*1000)}"
            t0 = time.monotonic()
            resp = client.post(
                f"{args.base_url}/api/tts/synth",
                json={"text": text, "voice": args.voice, "call_id": call_id,
                      "turn_id": call_id, "artist_id": TEST_ARTIST_ID},
            )
            ms = (time.monotonic() - t0) * 1000
            if resp.status_code != 200:
                print(f"  chars~{target_len} iter {it}: HTTP {resp.status_code} {resp.text[:200]}")
                continue
            body = resp.json() or {}
            b64 = body.get("audio")
            if not b64:
                print(f"  chars~{target_len} iter {it}: no audio — {body}")
                continue
            audio_bytes = base64.b64decode(b64)
            audio_s = _wav_duration_seconds(audio_bytes)
            ms_list.append(ms)
            bytes_list.append(len(audio_bytes))
            dur_list.append(audio_s)
            chars_list.append(len(text))
            print(f"  chars={len(text):5d} iter={it}  synth={ms:8.0f}ms  "
                  f"bytes={len(audio_bytes):7d}  audio={audio_s:5.2f}s")
        if ms_list:
            rows.append((target_len, chars_list, ms_list, bytes_list, dur_list))
        print()

    print("=" * 78)
    print(f"{'chars':>7} {'min ms':>9} {'median ms':>11} {'max ms':>9} "
          f"{'ms/char (median)':>18}")
    for target_len, chars_list, ms_list, bytes_list, dur_list in rows:
        actual_chars = statistics.median(chars_list)
        mn, md, mx = min(ms_list), statistics.median(ms_list), max(ms_list)
        ms_per_char = md / actual_chars if actual_chars else 0
        print(f"{actual_chars:7.0f} {mn:9.0f} {md:11.0f} {mx:9.0f} {ms_per_char:18.3f}")

    print()
    # Where does MEDIAN cross 25s, and where does MAX cross 25s?
    median_cross = None
    max_cross = None
    for target_len, chars_list, ms_list, bytes_list, dur_list in rows:
        actual_chars = statistics.median(chars_list)
        md, mx = statistics.median(ms_list), max(ms_list)
        if median_cross is None and md >= 25000:
            median_cross = actual_chars
        if max_cross is None and mx >= 25000:
            max_cross = actual_chars
    print(f"MEDIAN crosses 25000ms at ~{median_cross} chars" if median_cross
          else "MEDIAN never crosses 25000ms within tested range")
    print(f"MAX crosses 25000ms at ~{max_cross} chars" if max_cross
          else "MAX never crosses 25000ms within tested range")

    # Dump raw data as JSON for the diagnosis doc / downstream analysis.
    out = {
        "voice": args.voice,
        "iterations": args.iterations,
        "rows": [
            {
                "target_chars": t,
                "actual_chars": chars_list,
                "synth_ms": ms_list,
                "bytes": bytes_list,
                "audio_s": dur_list,
            }
            for t, chars_list, ms_list, bytes_list, dur_list in rows
        ],
    }
    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "docs" / "tts_latency_probe_raw.json"
    )
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nraw data written to {out_path}")


if __name__ == "__main__":
    main()
