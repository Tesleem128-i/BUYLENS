# Deploying BuyLens: Oracle Cloud Always Free VM + self-hosted Postgres

This gets you a genuinely free, always-on (no sleep, no cold starts) home for the
app: Flask running under Gunicorn, Nginx in front of it, and Postgres running on
the same box so there's no managed-DB pause/cold-start either.

Rough time: 30-45 minutes the first time.

---

## 0. Create the VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (card required for
   identity verification, but the Always Free resources never bill you).
2. Create a Compute Instance:
   - Image: **Canonical Ubuntu 22.04** (or newer LTS)
   - Shape: **VM.Standard.A1.Flex** (Ampere/ARM — the Always Free one). 2 OCPU /
     12 GB RAM is comfortably enough for this app; you get up to 4 OCPU / 24 GB
     free total across all your A1 instances.
   - Add your SSH public key when prompted (or let Oracle generate a keypair
     for you to download).
3. Under the instance's **VCN → Security List**, add ingress rules for:
   - Port `22` (SSH) — usually already open
   - Port `80` (HTTP)
   - Port `443` (HTTPS) — only needed if you add a domain + SSL later
4. Note the instance's **public IP address**.

SSH in:
```bash
ssh -i /path/to/your-key.pem ubuntu@<PUBLIC_IP>
```

---

## 1. System setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip postgresql postgresql-contrib nginx git ufw
```

Open the VM's own firewall (separate from Oracle's Security List above):
```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

---

## 2. Set up Postgres

```bash
sudo -u postgres psql
```
Inside the `psql` prompt:
```sql
CREATE DATABASE buylens;
CREATE USER buylens_user WITH PASSWORD 'choose-a-strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE buylens TO buylens_user;
ALTER DATABASE buylens OWNER TO buylens_user;
\q
```

Postgres on Ubuntu defaults to listening on `localhost` only and trusting local
connections — perfect here, since Flask and Postgres live on the same box and
never need to talk over the public internet. Nothing else to configure.

---

## 3. Get the app onto the server

Push your project to a GitHub repo (private is fine), then:
```bash
cd /home/ubuntu
git clone https://github.com/<you>/buylens.git
cd buylens
```
(No GitHub? `scp -i your-key.pem -r ./buylens ubuntu@<PUBLIC_IP>:/home/ubuntu/` works too.)

Make sure `requirements.txt` is in the project root — if you don't have one yet:
```
Flask==3.0.3
Flask-SQLAlchemy==3.1.1
SQLAlchemy==2.0.35
requests==2.32.3
python-dotenv==1.0.1
Werkzeug==3.0.4
gunicorn==22.0.0
psycopg2-binary==2.9.9
```

Create the virtual environment and install everything:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Environment variables

Create `/home/ubuntu/buylens/.env` (this is what `load_dotenv()` in `app.py`
reads — never commit this file to git):

```bash
SECRET_KEY=generate-a-long-random-string-here
DATABASE_URL=postgresql://buylens_user:choose-a-strong-password-here@localhost/buylens
GROQ_API_KEY=your-groq-key
BREVO_API_KEY=your-brevo-key
BREVO_SENDER_EMAIL=you@yourdomain.com
BREVO_SENDER_NAME=BUYLENS
PEXELS_API_KEY=your-pexels-key
```
Generate a strong `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Lock the file down:
```bash
chmod 600 .env
```

---

## 5. First run — create the tables

With the venv active and `.env` in place:
```bash
python3 -c "from app import app, db; app.app_context().push(); db.create_all()"
```
`app.py` already does this automatically on import (inside the
`with app.app_context(): db.create_all()` block near the top), so in practice
just starting the app once is enough — this manual step is only useful to
confirm the Postgres connection works *before* wiring up Gunicorn.

If this fails, it's almost always the `DATABASE_URL` — double check the
password and that `psycopg2-binary` installed cleanly.

---

## 6. Gunicorn as a systemd service (keeps it running forever, restarts on crash/reboot)

Create `/etc/systemd/system/buylens.service`:
```ini
[Unit]
Description=BuyLens Flask app (Gunicorn)
After=network.target postgresql.service

[Service]
User=ubuntu
Group=www-data
WorkingDirectory=/home/ubuntu/buylens
EnvironmentFile=/home/ubuntu/buylens/.env
ExecStart=/home/ubuntu/buylens/venv/bin/gunicorn --workers 3 --bind unix:/home/ubuntu/buylens/buylens.sock -m 007 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl start buylens
sudo systemctl enable buylens   # survives reboots
sudo systemctl status buylens   # confirm it's "active (running)"
```

Useful commands going forward:
```bash
sudo systemctl restart buylens     # after a code change
sudo journalctl -u buylens -f      # live logs
```

---

## 7. Nginx as the reverse proxy

Create `/etc/nginx/sites-available/buylens`:
```nginx
server {
    listen 80;
    server_name <PUBLIC_IP_OR_YOUR_DOMAIN>;

    location / {
        include proxy_params;
        proxy_pass http://unix:/home/ubuntu/buylens/buylens.sock;
    }

    location /static/ {
        alias /home/ubuntu/buylens/static/;
    }
}
```

Enable it:
```bash
sudo ln -s /etc/nginx/sites-available/buylens /etc/nginx/sites-enabled/
sudo nginx -t          # check syntax
sudo systemctl restart nginx
```

Visit `http://<PUBLIC_IP>` — you should see the BuyLens landing page.

---

## 8. (Optional) A real domain + HTTPS

If you point a domain's DNS `A` record at the VM's public IP:
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```
Certbot edits the Nginx config for you and auto-renews.

---

## 9. Deploying updates later

```bash
cd /home/ubuntu/buylens
git pull
source venv/bin/activate
pip install -r requirements.txt   # only needed if dependencies changed
sudo systemctl restart buylens
```

---

## 10. Bringing your existing SQLite data over (optional)

If you have a `buylens.db` with real accounts/wishlist data already in it and
want to keep it instead of starting fresh, the simplest path is `pgloader`:

```bash
sudo apt install -y pgloader
```
Create `migrate.load`:
```
LOAD DATABASE
     FROM sqlite:///home/ubuntu/buylens/buylens.db
     INTO postgresql://buylens_user:your-password@localhost/buylens
WITH include drop, create tables, create indexes, reset sequences
SET work_mem to '16MB', maintenance_work_mem to '512 MB';
```
```bash
pgloader migrate.load
```
Check row counts afterward (`psql buylens -c "select count(*) from \"user\";"`)
before deleting the old `.db` file. If this is a hackathon demo and the seeded
`demo@gmail.com` account is all you need, you can skip this section entirely —
`app.py` recreates that demo account automatically on first run.

---

## Quick sanity checklist before you call it done

- [ ] `sudo systemctl status buylens` → active (running)
- [ ] `sudo systemctl status postgresql` → active (running)
- [ ] `sudo systemctl status nginx` → active (running)
- [ ] Visiting `http://<PUBLIC_IP>` loads the landing page
- [ ] Signing up / logging in works (confirms Postgres connection)
- [ ] A search in the dashboard returns results (confirms `GROQ_API_KEY`)
- [ ] `sudo journalctl -u buylens -f` is clean (no repeating tracebacks) while
      you click around
