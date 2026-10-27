# Ludvig Operator Stats 🎯

A Rainbow Six Siege stats tracker with a Discord bot. It watches a player (Ludvig, by default) and:

- **Counts every match he plays** and posts it in Discord: win/loss, kills/deaths, RP gained or lost, and which operators he played.
- Tracks **rank, RP, peak rank, K/D, wins/losses/abandons** for every playlist (Ranked, Quick Match, Unranked, Dual Front, events).
- Tracks **per-operator stats**: rounds, win %, K/D, headshot %, opening kills/deaths, clutches, aces, multi-kills and time played, with the operator icons from [r6operators](https://r6operators.marcopixel.eu/).
- Groups matches into **play sessions** and posts a summary when he stops ("6 matches, 4W 2L, K/D 1.37, +64 RP… 💪 Solid.").
- Keeps a **history**: matches per day, K/D per day and RP over time, as charts.
- Has **"Ludvig complained" / "Ludvig argued" buttons** under its messages. Press one and a pop-up asks what he complained or argued about, who or what got the blame, and the exact quote. The bot counts them and shows the totals, top topics, who got blamed most, and whether it happens after wins or losses.
- Includes a **terminal program** (`r6tracker`) that does everything without Discord too, including CSV export.

## How it works (and what it can't do)

Neither Ubisoft nor anyone else offers a public match-history API for Siege. What *is* available are running totals, like "34 ranked wins this season". So the tracker checks every few minutes and compares the numbers. When wins + losses + abandons go up, that's a new match, and the changes in kills, deaths and RP show how it went.

What that means in practice:

- Only matches played **while the tracker is running** get counted. Run it on something that stays on: [Google Cloud](#run-it-on-google-cloud-247), a PC, a Raspberry Pi, or any server with Docker.
- If two matches finish between two checks, they show up as one update ("2 matches, 1W 1L"). With the default 5-minute interval that's rare, because a Siege match lasts longer than that.
- Operator stats come from a separate source that sometimes updates a few minutes after the match. The bot keeps checking for 30 minutes after each match and posts the operators when they show up.
- These APIs are unofficial. If Ubisoft changes something, a fix may be needed.

### Where the stats come from

| Source | What it gives | What you need |
|---|---|---|
| **Ubisoft's own API** | rank, RP, peak, K/D, W/L/abandons per playlist, level, playtime, commendations, battle pass level | A Ubisoft account for the bot to log in with. Use a **spare account without 2-step verification**. Your password is only sent to Ubisoft; the bot caches the session ticket, never the password. |
| **[Arenyze R6 API](https://r6.arenyze.com)** | per-operator stats (Ubisoft's API stopped serving these), and rank/K/D as a fallback | A free API key from r6.arenyze.com |

Set up at least one. Set up both to get everything.

## Setup

You need **Python 3.10 or newer**.

### 1. Install

```bash
git clone https://github.com/erikxvi/ludvigoperatorstats.git
cd ludvigoperatorstats
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e .
```

### 2. Create the Discord bot

1. Go to <https://discord.com/developers/applications>, click **New Application** and give it a name.
2. Open **Bot**, click **Reset Token** and copy the token. It's the `DISCORD_TOKEN`.
3. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`.
   - Bot permissions: View Channels, Send Messages, Embed Links, Attach Files.
   - Open the generated URL and add the bot to your server.

No privileged intents are needed.

### 3. Configure

```bash
cp .env.example .env
```

Fill in `.env`: the Discord token, your Ubisoft spare-account login and/or the Arenyze API key, and `TRACK_PLAYERS=Ludvig` (use his exact Ubisoft name). The file explains every setting. Set `DISCORD_GUILD_ID` to your server's ID to make the slash commands show up instantly. To get the ID, turn on Developer Mode in Discord, then right-click the server and choose **Copy Server ID**.

### 4. Run

```bash
r6tracker bot
```

In Discord, go to the channel where match updates should be posted and run **`/setchannel`**. That's it. The first check saves a baseline, and every match after that gets posted.

### Run it on Google Cloud (24/7)

The bot has to stay online to catch every match, so it runs best on a server. `deploy/gcp/deploy.sh` sets everything up on a small Google Cloud VM with one command. It creates the VM, uploads the bot and your `.env`, installs it as a service that restarts by itself, and keeps the database on the VM's disk.

1. Create a project at <https://console.cloud.google.com> and turn on billing. Google needs a card even for the free tier.
2. Open **Cloud Shell**: the `>_` button at the top right of the console. It's a terminal in your browser with `gcloud` already logged in.
3. In Cloud Shell, get the code and create your `.env`:
   ```bash
   git clone https://github.com/erikxvi/ludvigoperatorstats.git
   cd ludvigoperatorstats
   cp .env.example .env
   nano .env            # fill it in, then Ctrl+O, Enter, Ctrl+X
   gcloud config set project YOUR_PROJECT_ID
   ```
   If the repo is private, use the Cloud Shell editor's upload button (⋮ → Upload) instead of `git clone`.
4. Deploy:
   ```bash
   bash deploy/gcp/deploy.sh
   ```
   The first run takes a few minutes. Run the same command again any time you change the code or `.env`; the tracked history is kept.

Useful commands, from Cloud Shell:

```bash
gcloud compute ssh r6tracker --zone us-central1-a -- sudo journalctl -u r6tracker -f   # live logs
gcloud compute ssh r6tracker --zone us-central1-a -- r6tracker stats                  # terminal program on the server
gcloud compute ssh r6tracker --zone us-central1-a -- sudo systemctl restart r6tracker # restart
gcloud compute instances delete r6tracker --zone us-central1-a                        # remove everything
```

**Cost:** the VM is an `e2-micro` with a 30 GB standard disk in `us-central1`. That fits Google's [Always Free tier](https://docs.cloud.google.com/free/docs/free-cloud-features#compute), which covers one e2-micro VM in us-west1, us-central1 or us-east1 plus 30 GB of standard disk. Google may still charge a few dollars a month for the VM's public IP address; check **Billing** in the console after a few days. To use another zone or machine, set `ZONE=…` or `MACHINE=…` before the command, e.g. `ZONE=us-east1-b bash deploy/gcp/deploy.sh`.

Cloud Run and similar "serverless" products aren't a good fit. They shut down idle containers and lose local files, but the bot needs a permanent Discord connection and keeps its database on disk.

### Docker (optional, for any other server)

```bash
docker compose up -d --build
```

The database is kept in `./data`.

## Discord commands

| Command | What it shows |
|---|---|
| `/stats [player]` | Overview: level, playtime, rank, every playlist's W/L and K/D, today's matches, top operators |
| `/ranked [player]` | Rank, RP, peak, W/L, abandons, K/D, kills per match |
| `/operators [player] [season] [playlist] [side] [sort]` | Operator table + bar chart with icons. Season: `current`, `all` or e.g. `Y11S2`. Sort by rounds, K/D, win %, kills or headshot % |
| `/operator <name> [player] [season]` | Everything about one operator |
| `/today [player]` | Today's matches, W/L, K/D, RP and operators |
| `/session [player]` | The current or last play session |
| `/history [player] [days]` | Chart: matches per day, K/D per day, RP over time |
| `/matches [player] [count]` | The latest detected matches |
| `/complaints [player]` | The complaint & argument tally |
| `/refresh [player]` | Check for new matches right now |
| `/players` | Who's tracked and when they were last checked |
| `/track <name> [platform]` · `/untrack <name>` · `/setchannel [channel]` | Admin commands (need **Manage Server**) |

Most messages about a player come with three buttons:

- **😤 Ludvig complained**: opens a pop-up asking *what did Ludvig complain about?*, *who or what got the blame?* (teammates, enemies/cheaters, Ubisoft servers, the map, operators/guns, his own play, …) and *the exact quote* (optional). The bot posts it with a running count ("Complaint #23, 5 today, right after a 💀 Ranked LOSS").
- **🗣️ Ludvig argued**: same, for arguments.
- **📊 Tally**: shows (only to you) the totals, top topics, who got blamed most, whether it happens after wins or losses, the latest quotes, and who logs the most.

The buttons keep working after the bot restarts.

## Terminal program

Everything also works from the terminal (`r6tracker --help`):

```bash
r6tracker add Ludvig                 # start tracking (add --platform psn / xbox for console)
r6tracker stats                      # check for new matches, then show stats
r6tracker track                      # keep checking and print new matches (no Discord needed)
r6tracker operators --sort kd        # operator table (--season all, --playlist ranked, --side defender, --chart ops.png)
r6tracker operator jager             # one operator in detail
r6tracker today                      # today + the current/last session
r6tracker history --days 30 --chart history.png
r6tracker matches -n 30
r6tracker complain "the map" --category map --details "Who designed this?"
r6tracker complaints
r6tracker export --out export        # matches.csv, operators_played.csv, complaints.csv
```

`python -m r6tracker …` works too.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests include a fake Ubisoft + Arenyze server (`tests/fake_api.py`), so the login flow, ticket refresh, match detection, the CLI and the complaint pop-up are all tested without real accounts.

Project layout:

```
r6tracker/
  sources/     Ubisoft + Arenyze API clients and JSON parsing
  fetcher.py   combines the sources into one snapshot
  tracker.py   match detection (diffing snapshots), sessions, daily stats
  storage.py   SQLite: players, snapshots, matches, operator rounds, complaints
  charts.py    matplotlib charts
  bot/         Discord bot: slash commands, embeds, complain/argue buttons
  cli.py       terminal program
  data/        operator metadata + icons (r6operators, MIT)
deploy/gcp/    one-command Google Cloud deployment (VM + systemd service)
```

## Credits

- Operator icons and metadata: [r6operators](https://github.com/marcopixel/r6operators) by Marco Vockner (MIT). See `r6tracker/data/ICONS_LICENSE.txt`.
- Ubisoft API details: [siegeapi](https://github.com/CNDRD/siegeapi). Operator stats: [Arenyze / r6-data.js](https://github.com/mazeor9/r6-data.js).
- Not affiliated with Ubisoft. Rainbow Six Siege is a trademark of Ubisoft Entertainment.
