#!/usr/bin/env bash
# Kjøres av newsbrief.service på isengard hvert 30. min (05–23, ellers
# hver time). Henter evt. ny kode/konfig, genererer siden og publiserer.
set -euo pipefail

BASE=/var/lib/newsbrief
APP=$BASE/app
PAGES=$BASE/pages

# Plukk opp endringer i feeds.toml/kode uten manuelle steg. GitHub nede
# er ikke fatalt — kjør videre med eksisterende kode.
git -C "$APP" pull --ff-only --quiet \
  || echo "ADVARSEL: git pull feilet – kjører med eksisterende kode"

"$BASE/venv/bin/python" "$APP/generate.py" \
  --config "$APP/feeds.toml" \
  --state "$BASE/state" \
  --out "$PAGES/index.html"

# Publiser: pages-branchen holdes på nøyaktig én commit (amend + force)
# så repoet ikke vokser med ~17k commits i året.
cd "$PAGES"
git add -A
git commit --quiet --amend --reset-author -m "publish"
git push --quiet --force origin pages
echo "Publisert $(date -Is)"
