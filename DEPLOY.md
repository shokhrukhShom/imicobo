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
