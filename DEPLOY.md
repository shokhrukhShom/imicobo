# Deploying iMicobo to Hetzner

One-time setup, then updates are two commands. Total cost ≈ €4/mo + your domain.

---

## 1. Push the code to GitHub (from your Mac)

From your project folder:

```bash
git init
git add .
git commit -m "iMicobo MVP"
```

The included `.gitignore` keeps your `.env` (API key) and `yt_cache.json` out of the repo — good.

Create the repo and push (easiest with the GitHub CLI):

```bash
gh repo create imicobo --private --source=. --push
```

No `gh`? Create an empty repo on github.com, then:

```bash
git remote add origin https://github.com/YOURNAME/imicobo.git
git branch -M main
git push -u origin main
```

---

## 2. Create the server

On **Hetzner Cloud** → *Add Server*:
- Image: **Ubuntu 24.04**
- Type: smallest shared vCPU (e.g. **CAX11**, ~€3.79/mo) is plenty
- Add your SSH key

Note the server's **public IP**.

---

## 3. Point your domain at it

At your domain registrar for **imicobo.com**, add DNS records:

| Type | Name | Value            |
|------|------|------------------|
| A    | @    | YOUR_SERVER_IP   |
| A    | www  | YOUR_SERVER_IP   |

DNS can take a few minutes to propagate. HTTPS won't issue until this resolves.

---

## 4. Set up the server

SSH in:

```bash
ssh root@YOUR_SERVER_IP
```

Install Python, git, and Caddy:

```bash
apt update && apt install -y python3 git debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Basic firewall (allow SSH + web only; keeps port 8080 private):

```bash
apt install -y ufw
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
```

---

## 5. Deploy the app

Create a dedicated user and clone the repo:

```bash
useradd -r -s /usr/sbin/nologin imicobo
git clone https://github.com/YOURNAME/imicobo.git /opt/imicobo
```

Create the `.env` with your key (this file stays only on the server):

```bash
cp /opt/imicobo/.env.example /opt/imicobo/.env
nano /opt/imicobo/.env       # paste your real YT_API_KEY, save
chown -R imicobo:imicobo /opt/imicobo
```

Install and start the service:

```bash
cp /opt/imicobo/imicobo.service /etc/systemd/system/imicobo.service
systemctl daemon-reload
systemctl enable --now imicobo
systemctl status imicobo        # should say "active (running)"
```

---

## 6. Turn on HTTPS

```bash
cp /opt/imicobo/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

Caddy fetches a free certificate automatically. Open **https://imicobo.com** — you should see the app, and the mic will work because it's now HTTPS.

Quick check that covers work:
```
https://imicobo.com/art?t=Bohemian%20Rhapsody&a=Queen
```
should return `{"url": "https://...mzstatic.com/..."}`.

---

## 7. Turn on Google sign-in

Sign-in lets a paying customer restore their pass on any device by signing in
with the same Google email. It uses Google's ID-token flow, so **no client
secret is needed** and there are no redirect URIs to configure — just two things
in the [Google Cloud Console](https://console.cloud.google.com/apis/credentials):

1. **Authorized JavaScript origins** — on your OAuth 2.0 Client ID, add:
   - `https://imicobo.com`
   - `https://www.imicobo.com`

2. **Publish the consent screen** — OAuth consent screen → **Publish app**
   (move it from "Testing" to "In production"). The app only requests basic
   profile/email, which are non-sensitive scopes, so this needs **no Google
   review**. While it's still in "Testing", only accounts you add as test users
   can sign in.

The Client ID is already baked into the app; override it with `GOOGLE_CLIENT_ID`
in `.env` only if you make a new one. Nothing else to deploy — the button and the
`/api/auth/google` verifier are already in the build.

> The client secret from your earlier screenshot isn't used by this flow, but
> rotate it anyway (it was shown in a screenshot) — Console → that credential →
> *Reset secret*.

---

## 8. Turn on payments (Stripe)

The paywall is free-tier until you add Stripe keys — until then `/api/checkout`
politely returns "Payments aren't set up yet" and everyone gets the 5 free songs
a day. To accept money:

**a) Fill in the secrets in `/opt/imicobo/.env`:**

```bash
nano /opt/imicobo/.env
```

```
APP_SECRET=<a long random string>        # python3 -c "import secrets; print(secrets.token_urlsafe(48))"
STRIPE_SECRET_KEY=sk_test_...            # test key while sandboxing; sk_live_... for real money
STRIPE_WEBHOOK_SECRET=whsec_...          # from step (b) below
```

The two price IDs already default to your sandbox products inside `relay.py`.
When you switch to live mode, add your **live** price IDs too:

```
STRIPE_PRICE_DAY=price_...
STRIPE_PRICE_QUARTER=price_...
```

**b) Create the webhook** in Stripe → *Developers → Webhooks → Add endpoint*:
- Endpoint URL: **https://imicobo.com/api/stripe/webhook**
- Event to send: **checkout.session.completed**
- Copy the endpoint's **Signing secret** (`whsec_...`) into `STRIPE_WEBHOOK_SECRET` above.

**c) Restart:**

```bash
chown imicobo:imicobo /opt/imicobo/.env
systemctl restart imicobo
```

**How it fits together:** a buyer clicks a pass → `/api/checkout` creates a Stripe
Checkout session → they pay on Stripe's hosted page → Stripe calls your webhook →
the server records the pass against their email and sets a signed login cookie when
they return. The screen checks `/api/me` to unlock unlimited singing. The database
(`imicobo.db`, created automatically next to the app) is the source of truth; the
cookie just remembers who they are on that device.

> **Security:** never paste these secrets into chat, the code, or git — they live
> only in `.env` on the server. If a Stripe key or your Google client secret was
> ever shown in a screenshot or message, rotate it in the Stripe/Google dashboard
> before going live.

Test the whole flow in Stripe **test mode** first (card `4242 4242 4242 4242`,
any future expiry/CVC) before flipping to live keys.

---

## Updating later (the everyday workflow)

On your Mac: edit → `git commit` → `git push`.

On the server:

```bash
cd /opt/imicobo && git pull && systemctl restart imicobo
```

Two commands, done. (Caddy only needs reloading if you change the `Caddyfile`.)

---

## Handy commands

```bash
journalctl -u imicobo -f      # live app logs
systemctl restart imicobo     # restart after a change
systemctl status caddy        # check the web server / HTTPS
```
