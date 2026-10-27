#!/usr/bin/env bash
# Deploys the bot to a small Google Cloud VM and keeps it running 24/7.
#
# Run it from the repository folder, in Google Cloud Shell (or anywhere with `gcloud` logged in):
#     bash deploy/gcp/deploy.sh
# Running it again uploads the current code and .env and restarts the bot. The database is kept.
#
# Settings (optional environment variables):
#     PROJECT   Google Cloud project id   (default: gcloud's current project)
#     INSTANCE  VM name                   (default: r6tracker)
#     ZONE      VM zone                   (default: us-central1-a; free tier: us-west1, us-central1, us-east1)
#     MACHINE   machine type              (default: e2-micro, the free-tier one)
set -euo pipefail

cd "$(dirname "$0")/../.."
INSTANCE="${INSTANCE:-r6tracker}"
ZONE="${ZONE:-us-central1-a}"
MACHINE="${MACHINE:-e2-micro}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"

if [ ! -f .env ]; then
  echo "No .env file here. Create it first:  cp .env.example .env  and fill it in." >&2
  exit 1
fi
if ! grep -qE '^DISCORD_TOKEN=.+' .env; then
  echo ".env has no DISCORD_TOKEN, so the bot couldn't log in. Fill it in first." >&2
  exit 1
fi
if [ -z "$PROJECT" ]; then
  echo "No Google Cloud project selected. Run:  gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

G=(--project "$PROJECT" --zone "$ZONE")

echo "==> Project $PROJECT, VM $INSTANCE in $ZONE"
gcloud services enable compute.googleapis.com --project "$PROJECT"

if ! gcloud compute instances describe "$INSTANCE" "${G[@]}" >/dev/null 2>&1; then
  echo "==> Creating the VM ($MACHINE, Debian 12, 30 GB standard disk)"
  gcloud compute instances create "$INSTANCE" "${G[@]}" \
    --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size 30GB --boot-disk-type pd-standard \
    --labels app=r6tracker
  echo "==> Waiting for the VM to boot"
  for _ in $(seq 1 30); do
    gcloud compute ssh "$INSTANCE" "${G[@]}" --command true --quiet >/dev/null 2>&1 && break
    sleep 10
  done
fi

echo "==> Uploading the code"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# "./data" etc. are anchored on purpose: a bare "data" would also drop r6tracker/data (icons, operator list).
tar -czf "$TMP/r6tracker.tgz" --exclude=./.git --exclude=./.venv --exclude=./venv --exclude=./data \
  --exclude=./export --exclude=./.env --exclude=__pycache__ --exclude=.pytest_cache .
cp .env "$TMP/r6tracker.env"
gcloud compute scp "$TMP/r6tracker.tgz" "$TMP/r6tracker.env" deploy/gcp/install.sh "$INSTANCE":/tmp/ "${G[@]}" --quiet

echo "==> Installing on the VM"
gcloud compute ssh "$INSTANCE" "${G[@]}" --quiet --command "sudo bash /tmp/install.sh && rm -f /tmp/install.sh"

cat <<EOF

Done! The bot runs on the VM and restarts by itself after crashes and reboots.

  Live logs:        gcloud compute ssh $INSTANCE --zone $ZONE -- sudo journalctl -u r6tracker -f
  Terminal program: gcloud compute ssh $INSTANCE --zone $ZONE -- r6tracker stats
  Restart:          gcloud compute ssh $INSTANCE --zone $ZONE -- sudo systemctl restart r6tracker
  Update:           bash deploy/gcp/deploy.sh   (after changing code or .env)
EOF
