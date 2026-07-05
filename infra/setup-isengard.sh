#!/usr/bin/env bash
# Oppsett av nyhetsbrief på isengard. Kjøres som root, i to faser:
#
#   sudo bash setup-isengard.sh phase1   # bruker + deploy-nøkkel (skriver ut pubkey)
#   (pubkey registreres i GitHub-repoet fra atlantis: gh repo deploy-key add)
#   sudo bash setup-isengard.sh phase2   # kloner, venv, systemd, starter timer
#
# Idempotent — trygt å kjøre om igjen.
set -euo pipefail

REPO_HTTPS="https://github.com/zedd80/nyhetsbrief.git"
REPO_SSH="git@github.com:zedd80/nyhetsbrief.git"
BASE=/var/lib/newsbrief
RUN_AS=(sudo -u newsbrief)

phase1() {
  apt-get update -qq
  apt-get install -y --no-install-recommends git python3-venv

  id newsbrief &>/dev/null || useradd --system --create-home \
    --home-dir "$BASE" --shell /usr/sbin/nologin newsbrief
  install -d -o newsbrief -g newsbrief -m 700 "$BASE/.ssh"
  install -d -o newsbrief -g newsbrief "$BASE/state"

  if [[ ! -f $BASE/.ssh/id_ed25519 ]]; then
    "${RUN_AS[@]}" ssh-keygen -t ed25519 -N "" -C "newsbrief@isengard" \
      -f "$BASE/.ssh/id_ed25519"
  fi
  "${RUN_AS[@]}" bash -c "ssh-keyscan github.com 2>/dev/null > $BASE/.ssh/known_hosts"

  # Offentlig nøkkel er ikke hemmelig — legges lesbart i /tmp så den kan
  # hentes over ssh uten root (registreres i GitHub fra atlantis).
  install -m 644 "$BASE/.ssh/id_ed25519.pub" /tmp/newsbrief-deploy-key.pub

  echo
  echo "=== DEPLOY-NØKKEL (også i /tmp/newsbrief-deploy-key.pub) ==="
  cat "$BASE/.ssh/id_ed25519.pub"
  echo "=== Kjør deretter: sudo bash setup-isengard.sh phase2 ==="
}

phase2() {
  # Kode (lesetilgang holder — anonym https)
  if [[ ! -d $BASE/app/.git ]]; then
    "${RUN_AS[@]}" git clone --quiet "$REPO_HTTPS" "$BASE/app"
  fi

  # Publiseringsklone (trenger push — ssh med deploy-nøkkelen)
  if [[ ! -d $BASE/pages/.git ]]; then
    "${RUN_AS[@]}" git clone --quiet --branch pages --single-branch \
      "$REPO_SSH" "$BASE/pages"
  fi
  "${RUN_AS[@]}" git -C "$BASE/pages" config user.name "newsbrief"
  "${RUN_AS[@]}" git -C "$BASE/pages" config user.email "newsbrief@isengard.invalid"
  "${RUN_AS[@]}" git -C "$BASE/pages" config core.sshCommand \
    "ssh -i $BASE/.ssh/id_ed25519 -o IdentitiesOnly=yes"

  if [[ ! -d $BASE/venv ]]; then
    "${RUN_AS[@]}" python3 -m venv "$BASE/venv"
  fi
  "${RUN_AS[@]}" "$BASE/venv/bin/pip" install --quiet --upgrade feedparser

  cp "$BASE/app/systemd/newsbrief.service" "$BASE/app/systemd/newsbrief.timer" \
     /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now newsbrief.timer

  echo "=== Ferdig. Første kjøring: sudo systemctl start newsbrief.service"
  echo "=== Logg: journalctl -u newsbrief -f"
}

case "${1:-}" in
  phase1) phase1 ;;
  phase2) phase2 ;;
  *) echo "Bruk: sudo bash $0 phase1|phase2"; exit 1 ;;
esac
