#!/usr/bin/env python3
"""Nyhetsbrief: genererer én statisk HTML-side med nyhetstitler fra RSS.

Konsumenten er en LLM (Claude Chat) som henter siden med enkel GET —
derfor ingen JavaScript, tidsstempler i klartekst og eksplisitt feilstatus
øverst på siden. Se README.md for drift.
"""

import argparse
import difflib
import html
import json
import os
import re
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
FRESH_HOURS = 24  # eldre saker prefikses [48h] — briefen bruker 24t-vindu
DEFAULT_MAX_ITEMS = 25
TITLE_SIM = 0.9  # tittel-likhet som regnes som duplikat innenfor én kilde
SIZE_BUDGET = 200_000  # bytes
MAX_CACHED_ITEMS = 200  # per feed, begrenser state-fila
FETCH_TIMEOUT = 25  # sekunder
DESC_MAX_WORDS = 40
DESC_MAX_CHARS = 280  # ordgrense biter ikke på språk uten mellomrom (NHK)
BASE_URL = "https://zedd80.github.io/nyhetsbrief/"
DATED_KEEP_DAYS = 10

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
DATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.html$")
NRK_ID_RE = re.compile(r"1\.\d{7,}")
NON_WORD_RE = re.compile(r"[\W\d_]+")


def clean_summary(raw: str, title: str) -> str:
    """Feedens eget sammendrag (ingress) — HTML strippet og trunkert.
    Kun feed-innhold, aldri artikkelkropp (jf. opphavsrettsvalget i spek)."""
    if not raw:
        return ""
    text = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", raw))).strip()
    if text.casefold() == title.casefold():  # bare tittelen om igjen
        return ""
    words = text.split()
    if len(words) > DESC_MAX_WORDS:
        text = " ".join(words[:DESC_MAX_WORDS]) + " …"
    if len(text) > DESC_MAX_CHARS:
        text = text[:DESC_MAX_CHARS].rstrip() + " …"
    return text


def url_key(link: str) -> tuple:
    """Dedup-nøkkel for en lenke. Query og fragment strippes helt (utm_*,
    at_medium o.l.); NRK-artikler identifiseres på den stabile IDen
    «1.NNNNNNNN» alene — samme sak republiseres under ulike seksjonsstier
    (/nyheter/, /norge/, /buskerud/, …)."""
    low = link.casefold()
    if "nrk.no" in low:
        m = NRK_ID_RE.search(low)
        if m:
            return ("nrk", m.group(0))
    return ("url", low.split("#")[0].split("?")[0].rstrip("/"))


def norm_title(title: str) -> str:
    """Tegnsetting og siffer fjernes («12.000» == «12 000»), whitespace
    kollapses — grunnlag for tittel-likhet innenfor én kilde."""
    return WS_RE.sub(" ", NON_WORD_RE.sub(" ", title.casefold())).strip()


def dedupe_source(items: list) -> list:
    """Duplikater innenfor én kilde: samme URL-nøkkel eller ≥ TITLE_SIM
    tittel-likhet. items er nyeste-først, så første forekomst (nyeste
    tidsstempel) beholdes; ingress arves fra eldre variant ved behov."""
    out, key_idx, titles = [], {}, []
    for it in items:
        key = url_key(it["link"])
        idx = key_idx.get(key)
        nt = norm_title(it["title"])
        if idx is None:
            for i, prev in enumerate(titles):
                if difflib.SequenceMatcher(None, nt, prev).ratio() >= TITLE_SIM:
                    idx = i
                    break
        if idx is not None:
            if not out[idx].get("desc") and it.get("desc"):
                out[idx]["desc"] = it["desc"]
            continue
        key_idx[key] = len(out)
        titles.append(nt)
        out.append(dict(it))
    return out


def build_views(feed_cfgs: list, state: dict, now_oslo: datetime) -> dict:
    """Vindusfiltrert, deduplisert sakliste per kilde (ubegrenset — taket
    settes i render). Dedup på tvers av kilder gjøres KUN på NRK-ID:
    toppsaker og siste nytt er samme redaksjon, og første kilde i
    konfig-rekkefølgen vinner (toppsaker — salienssignalet bevares).
    Ellers aldri på tvers: at uavhengige kilder kjører samme sak er et
    signal konsumenten vil se."""
    cutoff = (now_oslo - timedelta(hours=WINDOW_HOURS)).astimezone(timezone.utc).isoformat()
    views = {}
    seen_nrk = set()
    for cfg in feed_cfgs:
        items = [it for it in state.get(cfg["name"], {}).get("items", [])
                 if it["ts"] >= cutoff]
        deduped = []
        for it in dedupe_source(items):
            key = url_key(it["link"])
            if key[0] == "nrk":
                if key[1] in seen_nrk:
                    continue
                seen_nrk.add(key[1])
            deduped.append(it)
        views[cfg["name"]] = deduped
    return views


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
        desc = ""
        if cfg.get("descriptions", True):
            desc = clean_summary(e.get("summary", ""), title)
        items.append({"title": title, "link": link, "desc": desc,
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


def render(feed_cfgs: list, state: dict, views: dict, now_oslo: datetime,
           global_cap, with_desc: bool = True) -> str:
    esc = html.escape
    fresh_cutoff = (now_oslo - timedelta(hours=FRESH_HOURS)).astimezone(timezone.utc).isoformat()

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
        items = views.get(name, [])[:cap]

        header = esc(name)
        if failed:
            header += " ⚠ (viser sist vellykkede henting)"
        elif not items:
            header += " ∅"
        lines = [f"<h2>{header} — {len(items)} saker siste {WINDOW_HOURS} t</h2>", "<ul>"]
        for it in items:
            url = esc(it["link"], quote=True)
            age = "[48h] " if it["ts"] < fresh_cutoff else ""
            line = (
                f'<li>{age}[{esc(name)}] {fmt_oslo(it["ts"])} — {esc(it["title"])} — '
                f'<a href="{url}">{url}</a>'
            )
            if with_desc and it.get("desc"):
                line += f'<br>↳ {esc(it["desc"])}'
            lines.append(line + "</li>")
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
<p>Format per sak: [KILDE] ISO-tidsstempel — Tittel — URL, eventuelt
etterfulgt av «↳ ingress» (feedens eget sammendrag). Kun saker fra siste
{WINDOW_HOURS} timer; saker eldre enn {FRESH_HOURS} timer er prefikset
[48h]. Kilder med ⚠ over feilet ved siste henting. ∅ betyr vellykket
henting, men ingen saker i tidsvinduet — normalt for lavvolum-kilder
(Rett24, NVE, Nordstrands Blad).</p>
{chr(10).join(sections)}
</body>
</html>
"""


def write_dated_copies(out_dir: Path, page: str, now_oslo: datetime) -> None:
    """Datert kopi av siden + pekerside (dagens.html), mot fetch-cache.

    Claude Chats hente-verktøy cacher per normalisert URL-sti (query
    strippes, TTL > 18 t), så index.html kan serveres døgngammel. Dagens
    daterte URL har aldri vært hentet før og kan derfor ikke være cachet.
    Pekersiden lister lenker for i går t.o.m. +7 dager — filnavnene er
    deterministiske, så en inntil en uke gammel cachet kopi av pekersiden
    inneholder fortsatt dagens gyldige lenke.
    """
    today = now_oslo.date()
    dated = out_dir / f"{today.isoformat()}.html"
    tmp = dated.with_suffix(".tmp")
    tmp.write_text(page)
    os.replace(tmp, dated)

    links = []
    for offset in range(-1, 8):
        d = (today + timedelta(days=offset)).isoformat()
        links.append(f'<li>{d}: <a href="{BASE_URL}{d}.html">{BASE_URL}{d}.html</a></li>')
    pointer = f"""<!doctype html>
<html lang="no">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex, nofollow">
<title>Nyhetsbrief — daterte utgaver</title>
</head>
<body>
<h1>Nyhetsbrief — daterte utgaver</h1>
<p>Hent utgaven for dagens dato (Europe/Oslo) — den oppdateres hvert 30.
minutt gjennom dagen. Bruk den daterte URL-en i stedet for index.html for å
omgå cache i hente-verktøy. Denne pekersiden kan trygt være en cachet kopi:
lenkene under dekker en uke fram i tid og er gyldige uansett. Filer for
framtidige datoer finnes først fra og med sin egen dato.</p>
<ul>
{chr(10).join(links)}
</ul>
</body>
</html>
"""
    tmp = out_dir / "dagens.tmp"
    tmp.write_text(pointer)
    os.replace(tmp, out_dir / "dagens.html")

    cutoff = (today - timedelta(days=DATED_KEEP_DAYS)).isoformat()
    for f in out_dir.iterdir():
        if DATED_RE.match(f.name) and f.name[:-5] < cutoff:
            f.unlink()


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

    views = build_views(feed_cfgs, state, now_oslo)

    # Krymp per-kilde-tak til siden er under budsjett (LLM-fetchere har
    # størrelsesgrenser). Antall saker kuttes før ingresser droppes.
    for cap, with_desc in ((None, True), (15, True), (10, True), (5, True),
                           (10, False), (5, False)):
        page = render(feed_cfgs, state, views, now_oslo, cap, with_desc)
        size = len(page.encode())
        if size <= SIZE_BUDGET:
            break
    print(f"Side: {size} bytes (tak: {cap or 'per-feed'}, ingress: {with_desc})")

    save_state(args.state, state)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(page)
    os.replace(tmp, args.out)
    write_dated_copies(args.out.parent, page, now_oslo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
