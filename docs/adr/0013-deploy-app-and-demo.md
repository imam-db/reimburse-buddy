# 0013. Deploy: the app and the demo side by side, linked by "Coba demo"

- Status: Accepted
- Date: 2026-11-07

## Context

The project needs a public deployment for the hackathon showcase at the domain `reimbursebuddy.my.id`. Two
audiences will visit it:

- **judges and visitors**, who should be able to try every role in one click, without an account;
- **the real app**, with email and password sign-in, production checks (`APP_ENV=prod`) and the production Neon
  branch.

One process cannot serve both:

- **Reset demo moves the epoch for the whole app.** With demo and real data in one database, a judge pressing it
  would hide every real claim.
- **ADR 0012 forbids the profile picker in production.** `auth.startup_check` refuses to start with
  `REIMBURSE_DEMO_PROFILES` set and `APP_ENV=prod`. Merging the two would mean removing that guard.

There is no deployment tooling in the repo yet: no Dockerfile, service file, proxy config or deploy guide. The app
is light enough for one small VPS. It needs no GPU and no PyTorch; OCR (RapidOCR) and embeddings (fastembed) are
small ONNX models. The Python environments take about 0.5 GB, and the databases stay on Neon.

## Decision

**Run two instances of the same code on one VPS, one per subdomain, and link them with a "Coba demo" button. Each
instance keeps its own database branch, environment file and LLM budget.**

### 1. Layout

| Host | Instance | Env | Neon branch | Sign-in |
|---|---|---|---|---|
| `reimbursebuddy.my.id` | app | `APP_ENV=prod` | `production` | email + password |
| `demo.reimbursebuddy.my.id` | demo | `APP_ENV` unset, `REIMBURSE_DEMO_PROFILES=1` | `demo` | profile picker (ADR 0012) |

- Both run as **systemd services** from one checkout, as an unprivileged user, on `127.0.0.1`:
  - `reimburse-app` on port 8000;
  - `reimburse-demo` on port 8001.
- Each service reads its own env file, `/etc/reimburse-buddy/{app,demo}.env`, owned by root and mode `600`. Env
  files are never in the repo.
- Both services set `REIMBURSE_PLAN_STORE` and `REIMBURSE_WEB_UPLOADS` to separate directories, so uploads and
  file state never mix.
- **Caddy** terminates HTTPS for both hosts (automatic certificates) and reverse-proxies to the ports. Only ports
  22, 80 and 443 are open.
- Each instance sets its own `LLM_DAILY_BUDGET_USD` and token prices. **This is required for the demo too**: it is
  public, and outside production an unset budget means no cap.

### 2. "Coba demo" link

- A new optional variable, `REIMBURSE_DEMO_URL`, holds an absolute `https://` URL.
- When it is set and the login card is shown (the gate is on and the picker is off), the card adds a secondary
  button **"Coba demo →"** linking to that URL. Its note reads *"Data sintetis, tanpa kata sandi."*
- The value is validated at startup. It must be `https://`, or `http://` for `localhost`/`127.0.0.1`, and must
  have a host. Anything else stops startup with `AuthConfigError`, so the link can never point to a script URL.
- It is a plain link. No session, cookie or account crosses between the instances.
- Unset, the login card is exactly what it is today.

### 3. Demo banner

- With the picker on, every page shows a thin banner **"Mode demo · data sintetis · bisa di-reset kapan saja"**.
  The banner shows only with the picker on, which never happens in production.
- This tells visitors that anything they enter may disappear after Reset demo.

### 4. Deploy package (`deploy/`)

- `deploy/reimburse-app.service` and `deploy/reimburse-demo.service`: systemd units with
  `Restart=on-failure`, `EnvironmentFile=`, the uvicorn command with `--group rapidocr`, and basic hardening
  (`NoNewPrivileges`, `ProtectSystem=strict` with explicit `ReadWritePaths` for the state and upload
  directories).
- `deploy/Caddyfile`: the two hosts.
- `deploy/env.app.example` and `deploy/env.demo.example`: every variable with a comment and no secret values.
- `deploy/setup.sh`: an idempotent script for a fresh Ubuntu 22.04/24.04 VPS. It installs `uv` and Caddy,
  creates the user and directories, syncs both environments (`uv sync --group rapidocr`,
  `uv --directory agent sync --extra postgres`), and installs the units. It **never** writes env values; it
  stops and says which env files are missing.
- `docs/deploy.md`: VPS spec, DNS records, steps, the production database steps (migrations 001–008 and
  `checkpoint_setup` as the owner role, `seed_users`, `audit_selftest`), updates (`git pull`, sync, restart), and
  checks after deploy (`/healthz`, one upload, one agent run per instance).

### 5. VPS

- Minimum **2 vCPU, 4 GB RAM, 20 GB SSD**, Ubuntu 22.04/24.04, plus 2 GB swap.
- 4 GB because two web processes, an agent subprocess and OCR can run at once.
- Tencent Cloud Lighthouse fits the hackathon; any provider with this spec works.

## Consequences

- Judges get one-click access from the real app's login page. The real app keeps every production guard, and
  ADR 0012 stays intact.
- Two databases, two env files and two budgets have to be maintained. An update restarts both services.
- Memory roughly doubles compared with one instance, so the VPS minimum is 4 GB, not 2 GB.
- **Not included:**
  - Docker images and CI deploys (one VPS does not need them);
  - monitoring beyond `/healthz` and `journalctl`;
  - automatic backups (Neon keeps history);
  - a scheduled Reset demo.
