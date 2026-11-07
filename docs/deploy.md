# Deploying to a VPS (the app and the demo side by side)

Decision and reasons: [ADR 0013](adr/0013-deploy-app-and-demo.md). Two instances of the same code run on one VPS:

| Host | systemd unit | Port | Env file | Neon branch | Sign-in |
|---|---|---|---|---|---|
| `reimbursebuddy.my.id` | `reimburse-app` | 8000 | `/etc/reimburse-buddy/app.env` | `production` | email + password (`APP_ENV=prod`) |
| `demo.reimbursebuddy.my.id` | `reimburse-demo` | 8001 | `/etc/reimburse-buddy/demo.env` | `demo` | profile picker ([ADR 0012](adr/0012-demo-profiles.md)) |

Caddy terminates HTTPS and proxies to the two ports. The files are in [deploy/](../deploy/).

**Privilege separation.** The code and both virtualenvs under `/opt/reimburse-buddy` are owned by `root`; the
services can read and run them but never change them (so a compromised service cannot rewrite `setup.sh` or code
that root later runs). Each instance runs as its own system user, `reimburse-app` and `reimburse-demo`, and can
write only its own state root, `/var/lib/reimburse-buddy/app` or `/var/lib/reimburse-buddy/demo` (mode 750), so a
compromised public demo cannot read the real app's uploads or state. The managed Python lives in the root-owned
`/opt/uv-python`.

## 1. The VPS

- Ubuntu 22.04 or 24.04, at least **2 vCPU, 4 GB RAM, 20 GB SSD**, plus **2 GB swap** (two web processes, an agent
  subprocess and OCR can run at once). Tencent Cloud Lighthouse fits; any provider with this spec works.
- Add swap if the image has none:

  ```bash
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ```
- No GPU is needed. The Python environments take about 0.5 GB; the databases stay on Neon.
- Open only ports 22, 80 and 443 (`setup.sh` configures `ufw`; also check the provider's firewall).

## 2. DNS

Create two **A** records at the domain's DNS, both pointing to the VPS's public IPv4 address:

| Type | Name | Value |
|---|---|---|
| A | `@` | VPS IP |
| A | `demo` | VPS IP |

Caddy can only get certificates once both names resolve to the server. Check with `dig +short reimbursebuddy.my.id`
and `dig +short demo.reimbursebuddy.my.id`.

## 3. Databases (before the first start)

Do this from your own machine (or on the VPS) with a checkout of the repo, **once per Neon branch**. The owner DSN
is only for these steps; it never goes into `app.env` or `demo.env`. The services start with the **application**
role (`reimburse_app`). Use Neon's direct (unpooled) endpoint. Repeat the steps for the `production` branch and the
`demo` branch; each branch has its own `reimburse_app` password.

1. **Migrations 001 to 008**, in numeric order, as the owner role (see the notes in the
   [README](../README.md#environment-variables) on why the order matters):

   ```bash
   for f in agent/migrations/00[1-8]_*.sql; do psql "$OWNER_DATABASE_URL" -f "$f"; done
   ```

   The `demo` branch was copied from `production` and already has 001 to 008. Re-applying is not needed there.
2. **Checkpoint tables** (plan graph), once per database, as the owner role. Safe to re-run:

   ```bash
   cd agent
   NEON_OWNER_DSN='postgresql://OWNER:PASSWORD@HOST/DB' uv run python -m reimburse_agent.office.checkpoint_setup
   ```
3. **Accounts**, as the owner role. Passwords are prompted with `getpass`, never passed as arguments.
   - `production`: real accounts, for example `uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --employee EMP-100 --role reviewer`
     (or `--all` to create everyone in `data/employees.json`).
   - `demo`: `--all --same-password`, then make EMP-101 finance for the self-approval scene:

     ```bash
     uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --all --same-password
     uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --employee EMP-101 --role finance
     ```

     The demo password is never used by visitors (they use the profile cards); keep it in a password manager.
4. **Audit log self-test**, with the **application** DSN (never the owner) in `REIMBURSE_AUDIT_DATABASE_URL`:

   ```bash
   cd agent
   uv sync --extra postgres
   uv run --extra postgres python -m reimburse_agent.audit_selftest
   ```

   Every line must say PASS (append and list roundtrip, duplicate `event_key`, UPDATE/DELETE/TRUNCATE denied,
   no unmatched executions, `prepare_threshold=None`). It exits non-zero otherwise.

## 4. Install

On the VPS, as root:

```bash
git clone https://github.com/imam-db/reimburse-buddy.git /opt/reimburse-buddy
/opt/reimburse-buddy/deploy/setup.sh
```

The first run installs `uv` and Caddy, creates the `reimburse-app` and `reimburse-demo` users and
`/var/lib/reimburse-buddy/{app,demo}`, makes `/opt/reimburse-buddy` root-owned, builds both Python environments
as root (`uv sync --group rapidocr` and `uv --directory agent sync --extra postgres`, with
`UV_PYTHON_INSTALL_DIR=/opt/uv-python`), installs the units and the Caddyfile, and opens the firewall. It never writes env values. If an env file is
missing it stops and lists it. Then:

1. Copy each example and fill in the values. Both need an LLM budget and prices: the demo is public, and outside
   production an unset budget means no cap.

   ```bash
   cp /opt/reimburse-buddy/deploy/env.app.example  /etc/reimburse-buddy/app.env
   cp /opt/reimburse-buddy/deploy/env.demo.example /etc/reimburse-buddy/demo.env
   $EDITOR /etc/reimburse-buddy/app.env /etc/reimburse-buddy/demo.env
   ```

   Use a different `REIMBURSE_DEMO_SESSION_KEY` in each file, generated with `openssl rand -hex 32` (production
   refuses to start with a `CHANGE_ME` placeholder or a key under 32 characters). `app.env` has `APP_ENV=prod` and no
   `REIMBURSE_DEMO_PROFILES`; `demo.env` has `REIMBURSE_DEMO_PROFILES=1` and no `APP_ENV`.
2. Run `/opt/reimburse-buddy/deploy/setup.sh` again. It fixes the file mode (`root:root`, `600`), enables both
   services and reloads Caddy. It is safe to run as often as needed.

## 5. Checks after deploy

```bash
systemctl status reimburse-app reimburse-demo --no-pager
curl -fsS http://127.0.0.1:8000/healthz && curl -fsS http://127.0.0.1:8001/healthz   # both answer: ok
curl -fsS https://reimbursebuddy.my.id/healthz && curl -fsS https://demo.reimbursebuddy.my.id/healthz
journalctl -u reimburse-app -u reimburse-demo -n 50 --no-pager
```

Then, in a browser, on **each** instance:

- **app**: `/login` shows the email form and the "Coba demo →" button; sign in as a real account; upload one
  receipt on a claim; run one agent investigation.
- **demo**: `/login` shows the profile picker and the banner "Mode demo · data sintetis · bisa di-reset kapan
  saja"; click a card; upload one receipt; run one agent investigation.

A startup that aborts shows its reason in `journalctl` (for example a missing `LLM_DAILY_BUDGET_USD` or a bad
`REIMBURSE_DEMO_URL`). The messages never contain a DSN.

## 6. Updating

```bash
cd /opt/reimburse-buddy
sudo git pull --ff-only                     # as root: the checkout is root-owned
sudo ./deploy/setup.sh                      # rebuilds both environments as root (with UV_PYTHON_INSTALL_DIR=/opt/uv-python) and refreshes the units
sudo systemctl restart reimburse-app reimburse-demo
```

Never run `uv sync` as a service user: the services run with `UV_NO_SYNC=1` and cannot write the virtualenvs. To
sync by hand, run it as root with `UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --frozen ...`, exactly as
`setup.sh` does.

If the release adds a migration, apply it to **both** Neon branches as the owner role before the restart. An
update restarts both services. Reset demo (`POST /admin/reset`) only exists on the demo instance; it is refused
in production.

### Migrating an existing VPS (one time, from the shared `reimburse` user)

Older installs ran both services as one `reimburse` user that owned the checkout and a single state directory.
To move to the separated layout:

```bash
sudo systemctl stop reimburse-app reimburse-demo
cd /opt/reimburse-buddy && sudo git pull --ff-only
sudo ./deploy/setup.sh        # creates reimburse-app / reimburse-demo, re-owns /opt/reimburse-buddy to root,
                              # rebuilds the environments as root; it leaves the old reimburse user alone
```

If `setup.sh` stops because an env file is missing, that is fine on an existing VPS only if the files exist; if the
old layout kept state in `/var/lib/reimburse-buddy/{app,demo}`, hand it to the new owners and start:

```bash
sudo chown -R reimburse-app:reimburse-app   /var/lib/reimburse-buddy/app
sudo chown -R reimburse-demo:reimburse-demo /var/lib/reimburse-buddy/demo
sudo chown -R root:root /opt/reimburse-buddy      # setup.sh already did this; repeat only if you changed anything
sudo systemctl start reimburse-app reimburse-demo
curl -fsS http://127.0.0.1:8000/healthz && curl -fsS http://127.0.0.1:8001/healthz
```

The old `/var/lib/reimburse-buddy/home` (uv cache and managed Python of the old user) is no longer used. Once both
healthz checks pass and the checks in section 5 are good, remove the old user and its leftovers:

```bash
sudo rm -rf /var/lib/reimburse-buddy/home
sudo userdel reimburse
```

## Not included

Docker images, CI deploys, monitoring beyond `/healthz` and `journalctl`, automatic backups (Neon keeps history)
and a scheduled Reset demo.
