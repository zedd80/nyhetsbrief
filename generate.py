#!/usr/bin/env python3
"""Nyhetsbrief: genererer én statisk HTML-side med nyhetstitler fra RSS.

Konsumenten er en LLM (Claude Chat) som henter siden med enkel GET —
derfor ingen JavaScript, tidsstempler i klartekst og eksplisitt feilstatus
øverst på siden. Se README.md for drift.
"""

import argparse
import html
import json
import os
import socket
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser

USER_AGENT = "nyhetsbrief/1.0 (+https://github.com/zedd80/nyhetsbrief; pmabell@proton.me)"
OSLO = ZoneInfo("Europe/Oslo")
WINDOW_HOURS = 48
DEFAULT_MAX_ITEMS = 25
SIZE_BUDGET = 200_000  # bytes
MAX_CACHED_ITEMS = 200  # per feed, begrenser state-fila
FETCH_TIMEOUT = 25  # sekunder


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path, help="feeds.toml")
    p.add_argument("--state", required=True, type=Path, help="state-katalog")
    p.add_argument("--out", required=True, type=Path, help="index.html")
    return p.parse_args()


def load_state(state_dir: Path) -> dict:
    f = state_dir / "feeds.json"
    if f.exists():
        return json.loads(f.read_text())
    return {}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "feeds.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    os.replace(tmp, state_dir / "feeds.json")


def entry_timestamp(entry, cached_ts_by_link: dict, now_utc: datetime) -> datetime:
    """Publiseringstid i UTC. Mangler feeden dato: behold tidligere sett
    tidspunkt for samme lenke, ellers 'nå' (første gang vi ser saken)."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    cached = cached_ts_by_link.get(entry.get("link", ""))
    if cached:
        return datetime.fromisoformat(cached)
    return now_utc


def fetch_feed(cfg: dict, st: dict, now_utc: datetime):
    """Henter én feed. Returnerer (ok, items | None, loggmelding).
    items=None ved 304 (uendret) — behold eksisterende cache."""
    t0 = time.monotonic()
    try:
        d = feedparser.parse(
            cfg["url"],
            agent=USER_AGENT,
            etag=st.get("etag"),
            modified=st.get("modified"),
        )
    except Exception as e:  # feedparser skal sluke det meste, men vær robust
        return False, None, f"exception: {e}"
    dur = time.monotonic() - t0
    status = d.get("status")

    if status == 304:
        return True, None, f"304 uendret ({dur:.1f}s)"
    if status is not None and status >= 400:
        return False, None, f"HTTP {status} ({dur:.1f}s)"
    if status is None or (d.bozo and not d.entries):
        err = d.get("bozo_exception") or "ukjent feil"
        return False, None, f"{err} ({dur:.1f}s)"

    cached_ts = {it["link"]: it["ts"] for it in st.get("items", [])}
    suffix = cfg.get("strip_suffix")
    items = []
    for e in d.entries:
        title = e.get("title", "").strip()
        link = e.get("link", "").strip()
        if not title or not link:
            continue
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)].rstrip(" -–")
        ts = entry_timestamp(e, cached_ts, now_utc)
        items.append({"title": title, "link": link,
                      "ts": ts.astimezone(timezone.utc).isoformat()})

    # Slå sammen med cache: utvider 48-timersvinduet utover feedens eget
    # vindu (viktig for feeder med kort horisont, f.eks. «siste nytt»).
    seen = {it["link"] for it in items}
    items += [old for old in st.get("items", []) if old["link"] not in seen]
    items.sort(key=lambda it: it["ts"], reverse=True)

    st["etag"] = d.get("etag")
    st["modified"] = d.get("modified")
    return True, items[:MAX_CACHED_ITEMS], f"{len(d.entries)} i feed ({dur:.1f}s)"


def fmt_oslo(ts_utc_iso: str) -> str:
    return datetime.fromisoformat(ts_utc_iso).astimezone(OSLO).isoformat(timespec="minutes")


def render(feed_cfgs: list, state: dict, now_oslo: datetime, global_cap) -> str:
    esc = html.escape
    cutoff = (now_oslo - timedelta(hours=WINDOW_HOURS)).astimezone(timezone.utc).isoformat()

    status_lines = []
    sections = []
    n_ok = 0
    for cfg in feed_cfgs:
        name = cfg["name"]
        st = state.get(name, {})
        failed = bool(st.get("last_error"))
        if failed:
            last_ok = st.get("last_success")
            last_ok_txt = fmt_oslo(last_ok) if last_ok else "aldri"
            status_lines.append(
                f"⚠ {esc(name)}: henting feilet {fmt_oslo(st['last_error_ts'])} "
                f"({esc(str(st['last_error']))}), siste vellykkede henting: {last_ok_txt}"
            )
        else:
            n_ok += 1

        cap = cfg.get("max", DEFAULT_MAX_ITEMS)
        if global_cap:
            cap = min(cap, global_cap)
        items = [it for it in st.get("items", []) if it["ts"] >= cutoff][:cap]

        header = esc(name) + (" ⚠ (viser sist vellykkede henting)" if failed else "")
        lines = [f"<h2>{header} — {len(items)} saker siste {WINDOW_HOURS} t</h2>", "<ul>"]
        for it in items:
            url = esc(it["link"], quote=True)
            lines.append(
                f'<li>[{esc(name)}] {fmt_oslo(it["ts"])} — {esc(it["title"])} — '
                f'<a href="{url}">{url}</a></li>'
            )
        lines.append("</ul>")
        sections.append("\n".join(lines))

    if status_lines:
        status_html = "\n".join(f"<p>{line}</p>" for line in status_lines)
    else:
        status_html = f"<p>STATUS: alle {n_ok} kilder OK</p>"

    return f"""<!doctype html>
<html lang="no">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex, nofollow">
<title>Nyhetsbrief</title>
</head>
<body>
<h1>Nyhetsbrief</h1>
<p>Generert: {now_oslo.isoformat(timespec="seconds")} (Europe/Oslo)</p>
{status_html}
<p>Format per sak: [KILDE] ISO-tidsstempel — Tittel — URL. Kun saker fra
siste {WINDOW_HOURS} timer. Kilder med ⚠ over feilet ved siste henting.</p>
{chr(10).join(sections)}
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    socket.setdefaulttimeout(FETCH_TIMEOUT)

    with open(args.config, "rb") as f:
        feed_cfgs = tomllib.load(f)["feeds"]
    state = load_state(args.state)
    now_utc = datetime.now(timezone.utc)
    now_oslo = now_utc.astimezone(OSLO)
    now_iso = now_utc.isoformat()

    for cfg in feed_cfgs:
        st = state.setdefault(cfg["name"], {})
        ok, items, msg = fetch_feed(cfg, st, now_utc)
        if ok:
            if items is not None:
                st["items"] = items
            st["last_success"] = now_iso
            st["last_error"] = None
            st["last_error_ts"] = None
            print(f"OK   {cfg['name']}: {msg}")
        else:
            st["last_error"] = msg
            st["last_error_ts"] = now_iso
            print(f"FEIL {cfg['name']}: {msg}")

    # Krymp per-kilde-tak til siden er under budsjett (LLM-fetchere har
    # størrelsesgrenser).
    for cap in (None, 15, 10, 5):
        page = render(feed_cfgs, state, now_oslo, cap)
        size = len(page.encode())
        if size <= SIZE_BUDGET:
            break
    print(f"Side: {size} bytes (tak: {cap or 'per-feed'})")

    save_state(args.state, state)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(page)
    os.replace(tmp, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
