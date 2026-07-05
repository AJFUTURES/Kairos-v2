> ⛔ **DO NOT USE THIS FOR A TOPSTEP ACCOUNT.** Topstep's Terms of Use require all
> trading to originate from your **personal device** and explicitly prohibit **VPS /
> cloud / remote servers** (and VPNs/proxies). Running KAIROS on Oracle Cloud risks
> **account suspension/closure and profit forfeiture**.
> Source: https://help.topstep.com/en/articles/11187768-topstepx-api-access
> Compliant alternative: run it on an always-on **personal device at home** (old
> laptop / mini-PC / Raspberry Pi) on your home internet — same setup as below, just
> a home Linux box instead of a cloud VM. Ask and I'll convert this runbook.

# KAIROS → Oracle Cloud (Always Free) — deployment runbook

Move KAIROS off your laptop onto a free, always-on Oracle Cloud VM. Cost: **$0/month**.
Your public URL, webhook, and dashboard keep working **unchanged** — we run your
existing Cloudflare tunnel from the server instead of the Mac, so there are **no DNS
changes** and **no inbound firewall rules to open** (the webhook arrives over the
tunnel's *outbound* connection).

End state:
- `kairos-bot.service`  — the bot, auto-restart on crash + on reboot.
- `kairos-tunnel.service` — the Cloudflare tunnel (`your-domain.example` → `127.0.0.1:8000`).
- Laptop can be shut off.

> ⚠️ **Never run two bots on one TopstepX account.** Follow the cutover in Step 9
> exactly: stop the laptop's services *before* starting the server's.

---

## 1 · Create the free VM (Oracle Cloud console)

1. Sign up at <https://www.oracle.com/cloud/free/> (needs a card for identity check;
   Always-Free resources are never charged).
   - **Home region:** pick a **US region** (e.g. *US East (Ashburn)*) at signup for the
     lowest latency to the broker. *(If your account is already in India, that's fine
     too — it still works, just slightly higher order latency than US. Not worth making
     a new account over.)*
2. **Compute → Instances → Create instance:**
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** *Change shape* → **Ampere (ARM)** `VM.Standard.A1.Flex`, 1 OCPU / 6 GB
     (Always-Free). If it says "out of capacity", use **`VM.Standard.E2.1.Micro`**
     (AMD x86, 1 GB) — always available and plenty for this bot.
   - **SSH keys:** *Generate a key pair* (download the private key) **or** paste your
     own public key. You'll SSH in as user **`ubuntu`**.
   - Leave networking default. **Create.** Note the **public IP** when it's running.

> The bot needs only *outbound* internet, so you do **not** need to add any ingress
> security-list rule. (This is why Oracle's usual inbound-firewall pain doesn't apply.)

---

## 2 · Connect

```bash
# from your Mac — use the private key for the instance
ssh -i /path/to/your-key ubuntu@<SERVER_IP>
```

Set the arch variable for later (paste once on the server):
```bash
ARCH=$(dpkg --print-architecture)   # amd64 (E2 micro) or arm64 (A1)
echo "arch is $ARCH"
```

---

## 3 · Install system packages (on the server)

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip rsync

# cloudflared (matches the VM arch automatically)
curl -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared --version        # sanity check
mkdir -p ~/.cloudflared
```

---

## 4 · Copy the bot + secrets + tunnel creds **from your Mac**

Run these in a **new terminal on your Mac** (not on the server). They push the app
(minus the platform-specific venv and bulky logs), your `.env`, your saved state, and
the three Cloudflare tunnel files.

```bash
SRV=ubuntu@<SERVER_IP>          # <-- edit
KDIR="/Users/<your-mac-username>/Desktop/AJ Ventures/KAIROS"

# 4a. the app (keeps .env + bot_state.json; skips venv/__pycache__/logs/wrangler/git)
rsync -av \
  --exclude venv --exclude __pycache__ --exclude '.git' --exclude '.wrangler' \
  --exclude '*.log' --exclude 'trade_logs.txt' \
  "$KDIR/" "$SRV:~/KAIROS/"

# 4b. the Cloudflare named-tunnel credentials (3 files)
rsync -av \
  "/Users/<your-mac-username>/.cloudflared/<YOUR-TUNNEL-UUID>.json" \
  "/Users/<your-mac-username>/.cloudflared/cert.pem" \
  "/Users/<your-mac-username>/.cloudflared/config.yml" \
  "$SRV:~/.cloudflared/"
```

---

## 5 · Point the tunnel config at the server paths (on the server)

`config.yml` still references the Mac's `/Users/<your-mac-username>/...` path. Fix it + lock perms:

```bash
sed -i 's#/Users/<your-mac-username>/.cloudflared#/home/ubuntu/.cloudflared#' ~/.cloudflared/config.yml
chmod 600 ~/.cloudflared/cert.pem ~/.cloudflared/<YOUR-TUNNEL-UUID>.json
chmod 600 ~/KAIROS/.env
cat ~/.cloudflared/config.yml    # confirm credentials-file now says /home/ubuntu/...
```

---

## 6 · Build the venv + install deps (on the server)

```bash
cd ~/KAIROS
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

---

## 7 · Install the two services (on the server)

```bash
sudo cp ~/KAIROS/deploy/kairos-bot.service ~/KAIROS/deploy/kairos-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
```
*(Do NOT start them yet — first stop the laptop in Step 9 to avoid two live bots.)*

---

## 8 · Pre-flight (optional, safe — does not trade)

You can start just the **tunnel** later; for now verify the bot boots and connects by
running it once in the foreground (Ctrl+C to stop). This connects to the broker read-only
on startup but won't act unless TradingView fires — keep the laptop bot the live one until
you're ready to cut over, OR pause the laptop bot from its dashboard during this test.

```bash
cd ~/KAIROS && ./venv/bin/python main.py
# look for: SignalR User/Market hub ... CONNECTED, then Ctrl+C
```

---

## 9 · Cutover — laptop OFF, server ON  ⚠️ order matters

**On the Mac**, stop both laptop services so nothing trades or serves the tunnel:
```bash
launchctl bootout gui/$(id -u)/com.kairos.bot 2>/dev/null
launchctl unload -w ~/Library/LaunchAgents/com.kairos.cloudflared.plist 2>/dev/null
pkill -f main.py; pkill -f cloudflared        # belt + suspenders
```
(And don't double-click `kairos.command` anymore.)

**On the server**, start the tunnel, then the bot:
```bash
sudo systemctl enable --now kairos-tunnel.service
sudo systemctl enable --now kairos-bot.service
```

---

## 10 · Verify

```bash
systemctl status kairos-tunnel kairos-bot --no-pager
journalctl -u kairos-bot -f          # watch for "SignalR ... CONNECTED"; Ctrl+C to exit
curl -s http://127.0.0.1:8000/health # -> {"status":"ok"}
```
Then from your browser:
- `https://your-domain.example`  → landing page loads.
- `https://your-domain.example/dashboard?token=<DASHBOARD_TOKEN>` → control panel, ACTIVE, both
  SignalR dots green.
- Fire a **Test BUY** from the dashboard (logs only, no real order) to confirm the path.

You can now shut the laptop. The bot restarts itself on crash and on VM reboot.

---

## 11 · Day-to-day

```bash
# after editing main.py (pull/scp the new file up, then):
sudo systemctl restart kairos-bot

# logs
journalctl -u kairos-bot -f
journalctl -u kairos-tunnel -f
tail -f ~/KAIROS/kairos.log

# stop / start
sudo systemctl stop kairos-bot          # tunnel stays up
sudo systemctl start kairos-bot
```

**Web edits without restart:** thanks to the per-request page loading, editing
`web/landing.html` / `login.html` / `dashboard.html` on the server shows on browser
refresh — no `systemctl restart` needed.

**Won't get reclaimed:** Oracle only reclaims *idle* Always-Free compute (very low CPU
+ network for 7 days). A 24/7 bot with constant broker traffic is never idle.

**Log growth (optional):** `trade_logs.txt` is append-only. If you want it capped, add a
logrotate rule — not urgent on the free VM's ~47 GB disk.
