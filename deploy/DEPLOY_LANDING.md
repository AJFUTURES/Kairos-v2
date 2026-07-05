# Always-Up Landing Page — your-domain.example on Cloudflare Pages

**Goal:** `https://your-domain.example` is **always available** to visitors (a static site on Cloudflare Pages, hosted by Cloudflare — independent of your Mac). The **bot** — dashboard + TradingView webhook — lives on **`https://app.your-domain.example`**, served through the Cloudflare Tunnel and therefore up only while the bot is running. That's the intended split: the public site is always on; the dashboard *is* the bot.

```
                         ┌─ your-domain.example ───────────► Cloudflare Pages  (site/, always up)
visitor / TradingView ───┤
                         └─ app.your-domain.example ───────► Cloudflare Tunnel ─► 127.0.0.1:8000 (bot, only when running)
```

The repo changes are already done (see the bottom of this file). The steps below are the **Cloudflare + TradingView** actions you must do once, in this order.

---

## 1. Deploy the landing page to Cloudflare Pages

The deploy folder is **`site/`** (`index.html` + `logo.png` + `favicon.png`).

**Cloudflare dashboard → Workers & Pages → Create → Pages → Upload assets**
- Project name: `kairos-landing` (anything).
- Drag in the **contents of `site/`** (the `index.html`, `logo.png`, `favicon.png` — not the folder itself).
- Deploy. You'll get a `*.pages.dev` URL — open it to confirm the landing page renders.

*(CLI alternative, if you prefer: `npx wrangler pages deploy site --project-name kairos-landing`.)*

## 2. Point your-domain.example (+ www) at the Pages project

**Pages project → Custom domains → Set up a custom domain**
- Add `your-domain.example`.
- Add `www.your-domain.example`.

Cloudflare will create/replace the DNS records for the apex and www to point at Pages. If it warns that an existing record (the old tunnel CNAME for the apex/www) conflicts, **let it replace them** — that old record is exactly what we're moving off the tunnel.

## 3. Route app.your-domain.example to the tunnel

On the Mac (the tunnel `config.yml` ingress is already updated to `app.your-domain.example`):

```bash
cloudflared tunnel route dns kairos app.your-domain.example
```

This creates the `app` CNAME pointing at the tunnel. Then restart the tunnel agent so it picks up the new ingress:

```bash
launchctl unload ~/Library/LaunchAgents/com.kairos.cloudflared.plist
launchctl load -w ~/Library/LaunchAgents/com.kairos.cloudflared.plist
```

## 4. Re-point the TradingView webhook (no need to recreate alerts)

For **each** alert: open it → **Notifications** tab → change the **Webhook URL** to:

```
https://app.your-domain.example/webhook
```

The alert message/condition stays exactly the same — only the URL changes. Save. (Repeat per chart since there's one alert per instrument.)

## 5. Verify

- With the **bot OFF**: `https://your-domain.example` still loads the landing page ✅. `https://app.your-domain.example/dashboard` is down (expected).
- Start the bot (`./start.sh`), then `https://app.your-domain.example/dashboard?token=…` loads, and a TradingView test alert reaches `/webhook`.

---

## What changed in the repo

- **`site/`** — new Cloudflare Pages deploy folder: `index.html` (the landing page, with its Dashboard links pointed at `https://app.your-domain.example/dashboard`), plus `logo.png` / `favicon.png`. This is the single source of truth for the landing markup.
- **`web/landing.html`** — removed (its content moved to `site/index.html`). The bot still serves the same file at its own `/` as a harmless fallback (`_LANDING_PATH` → `site/index.html` in `main.py`).
- **`web/login.html`** — the "← back to your-domain.example" link now points to the public site absolutely.
- **`~/.cloudflared/config.yml`** — ingress changed from `your-domain.example` / `www` to **`app.your-domain.example`**.
- **`start.sh`** — prints the public site (Pages) and the bot's `app.` webhook/dashboard URLs.

## To update the landing page later

Edit `site/index.html`, then re-deploy `site/` to Pages (re-upload, or `npx wrangler pages deploy site --project-name kairos-landing`). No bot restart needed for the public site; the bot picks up the same file on its next request.
