#!/usr/bin/env bash
# Runs ON the Google Cloud VM (as root, via deploy.sh). Installs or updates the bot as a systemd service.
# Expects /tmp/r6tracker.tgz (the code) and /tmp/r6tracker.env (your .env) to have been copied over.
set -euo pipefail

APP=/opt/r6tracker
SVC=r6tracker

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv >/dev/null

echo "==> Setting up $APP"
id "$SVC" >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin "$SVC"
mkdir -p "$APP/data"
rm -rf "$APP/app"
mkdir -p "$APP/app"
tar -xzf /tmp/r6tracker.tgz -C "$APP/app"
install -m 600 /tmp/r6tracker.env "$APP/.env"
rm -f /tmp/r6tracker.tgz /tmp/r6tracker.env
rm -f "$APP/data/ubisoft_session.json"  # left over from versions that logged in to Ubisoft

echo "==> Installing the bot (takes a minute the first time)"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$APP/app"
chown -R "$SVC:$SVC" "$APP"

echo "==> Installing the service"
cat > /etc/systemd/system/$SVC.service <<EOF
[Unit]
Description=Rainbow Six Siege stats tracker + Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC
WorkingDirectory=$APP
Environment=DATA_DIR=$APP/data
ExecStart=$APP/venv/bin/r6tracker --env $APP/.env bot
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

# `r6tracker ...` on the VM runs the terminal program against the bot's database.
cat > /usr/local/bin/r6tracker <<EOF
#!/bin/sh
cd $APP && exec sudo -u $SVC env DATA_DIR=$APP/data $APP/venv/bin/r6tracker --env $APP/.env "\$@"
EOF
chmod 755 /usr/local/bin/r6tracker

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1
systemctl restart "$SVC"
sleep 5
if systemctl is-active --quiet "$SVC"; then
  echo "==> The bot is running."
else
  echo "==> The bot didn't start. Last log lines:"
  journalctl -u "$SVC" -n 30 --no-pager
  exit 1
fi
