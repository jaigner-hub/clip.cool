# Keygrip — Django app (`keygrip-web`)

Minimal Django app that authenticates against the Keycloak **keygrip** realm via OIDC
(`mozilla-django-oidc`). Proves the full chain: Google → Keycloak → app → roles.

> Phase-0 scaffold. ASGI per ADR 0004 (Gunicorn + Uvicorn for prod); local dev uses runserver.
> Settings split under `keygrip/settings/`. No local passwords — OIDC only.

## Run locally
```bash
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill OIDC_RP_CLIENT_SECRET:
echo "OIDC_RP_CLIENT_SECRET=$(../bin/secrets get dev vault_keygrip_web_client_secret)" >> .env

python manage.py migrate
python manage.py runserver 8000
```
Open <http://localhost:8000/> → you're redirected to Keycloak → "Sign in with Google" →
back to the app, which shows your user + composite-expanded roles.

`http://localhost:8000/*` is already a Valid Redirect URI on the `keygrip-web` client.

## Run the task queue locally (Procrastinate, ADR 0008)

Default dev uses SQLite, where the queue is absent (Procrastinate is Postgres-only). To run the
worker end-to-end on your machine, bring up the dev Postgres container and switch `DEV_DB`:

```bash
docker compose -f compose.dev.yml up -d        # Postgres on localhost:5433
export DEV_DB=postgres DB_HOST=localhost DB_PORT=5433 \
       DB_NAME=keygrip DB_USER=keygrip DB_PASSWORD=keygrip
python manage.py migrate
python manage.py procrastinate worker --queues default,batch,workflow,aeo   # shell 1
python manage.py runserver                                                   # shell 2
```

**Monitor** at <http://localhost:8000/admin/>: under *Procrastinate*, deferred/running **jobs**
(`procrastinatejob/`) and live **worker** heartbeat/liveness (`procrastinateworker/`). Defer a test job:
`python manage.py shell -c "from web.tasks import health_check; health_check.defer(note='hi')"`.

## Parallel instances (`./mc`)

To run **several isolated copies** of the app at once — one per branch/worktree — use the `./mc`
tool at the repo root. Each instance is a git worktree at `../keygrip-<name>` plus its own Docker
Compose project (Postgres + webapp + worker) on auto-allocated non-clashing ports, with the
worktree's `app/` mounted for **live reload** and a fresh, empty, migrated DB.

```bash
./mc demo init                   # worktree off main + build + up + migrate; prints the URL
./mc demo logs [webapp|worker|appdb]
./mc demo manage createsuperuser # run any manage.py command in the container
./mc demo url                    # this instance's http://localhost:<port>/
./mc list                        # all running instances + URLs
./mc demo destroy                # tear down containers + volumes + the worktree
```

`mc` gives each instance a web port in **8010–8099** (DB in 5510–5599); all of those localhost
ports are registered as `keygrip-web` redirect URIs (`ansible/roles/keycloak_realm`), so **OIDC
login works on every instance** (the same ports are registered for the `keygrip-api-docs` Swagger
client too). Login uses the real dev Keycloak (`id.vent.dog`); `mc` reads the client secret from the
SOPS dev vault for you. The **JSON API works locally too, bearer tokens included** —
`web/api_auth.py` fetches Keycloak's JWKS with a non-urllib `User-Agent`, so the Cloudflare-fronted
endpoint doesn't 403 the validation. **Creating API credentials also works if you're on the
tailnet** — the Keycloak admin API is Cloudflare-Access-gated on the public host, so `mc` reaches it
over Tailscale (it injects the `kc-admin` secret and resolves the tailnet host for you). Minting
creates real `kg-*` clients in the dev realm. Run `./mc help` for the full command list.

## Layout
```
app/
  manage.py
  requirements.txt
  keygrip/            # project: settings split, urls, asgi/wsgi
    settings/{base,dev}.py
  web/                # first app: home view + OIDC role-mapping backend
    auth.py           # maps the realm `roles` claim -> Django groups + is_staff/superuser
    views.py
  templates/home.html
```

## Prod (later)
Containerize (Gunicorn + Uvicorn workers, ADR 0004), deploy via Ansible, front with a
Cloudflare Tunnel hostname; add that hostname to `keygrip-web` redirect URIs and swap sqlite
for the managed Postgres.
