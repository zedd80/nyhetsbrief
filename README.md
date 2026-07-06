# nyhetsbrief

Henter RSS-feeder på isengard hvert 30. minutt og publiserer én statisk
HTML-side til GitHub Pages, som Claude Chat leser til den daglige
nyhetsbriefingen: **https://zedd80.github.io/nyhetsbrief/**

Siden er designet for LLM-lesing: ingen JavaScript, tidsstempler i
klartekst (Europe/Oslo), eksplisitt feilstatus øverst, titler + lenker +
kort ingress fra feedens eget sammendragsfelt (maks ~40 ord; aldri
artikkelkropp — bevisst opphavsrettsvalg), maks ~200 KB. Blir siden for
stor, kuttes antall saker per kilde før ingressene ofres.

**Cache-omgåelse:** Claude Chats hente-verktøy cacher per URL-sti (query
strippes, TTL > 18 t), så index.html kan serveres døgngammel. Derfor skrives
også en datert kopi per dag (`YYYY-MM-DD.html`, Europe/Oslo) og en liten
pekerside `dagens.html` med lenker for i går t.o.m. +7 dager. Claude Chat
henter pekersiden (ufarlig om cachet — dagens lenke står der uansett) og
deretter dagens daterte URL, som aldri kan være cachet. Daterte filer eldre
enn 10 dager slettes automatisk.

## Drift (isengard)

- **Legge til/fjerne kilde:** rediger `feeds.toml` (GitHubs web-UI eller be
  Claude) — plukkes opp automatisk ved neste kjøring.
- **Logger:** `journalctl -u newsbrief` (én linje per feed per kjøring).
- **Kjør nå / restart:** `sudo systemctl start newsbrief.service`;
  timeren: `systemctl list-timers newsbrief.timer`.

## Arkitektur

```
isengard: newsbrief.timer (30 min 05–23, ellers hver time)
  └─ run.sh: git pull → generate.py → commit --amend + force-push «pages»
       └─ GitHub Pages serverer index.html
            └─ Claude Chat henter med GET
```

- `main`-branch: kode og konfig. `pages`-branch: generert side, holdes på
  én commit (amend + force-push).
- Feiler en feed, vises forrige vellykkede innhold med ⚠-markering
  (carry-forward) — kildesvikt er synlig for Claude, aldri stille utelatt.
- State (etag/last-modified, sakscache): `/var/lib/newsbrief/state/` —
  regenererbar, trenger ikke backup.
- Kjører som systembruker `newsbrief` (nologin), herdet systemd-unit.

## Oppsett fra bunnen

Se `infra/setup-isengard.sh` (to faser; deploy-nøkkel registreres med
`gh repo deploy-key add --allow-write` mellom fasene).
