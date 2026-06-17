# clip.cool

**clip.cool is a GIF/meme hosting platform** built to be faster and cheaper than Giphy / Tenor /
Imgur. The core idea: **never serve a GIF as a GIF** — ingest anything, transcode to looping video
(AV1 / VP9 / H.264), and serve `<video autoplay loop muted playsinline>`. That alone makes it
faster and cheaper than half the field; perceptual dedup and search-by-spoken/burned-in-text are the
differentiators on top.

This repository is a **fresh app built on the infrastructure scaffolding developed for
[`keygrip`](../keygrip)**. It **replaces keygrip's web footprint** on the `vent.dog` + `vent.dog2`
pair, while the existing **chat app at `chat.vent.dog`** (Matrix/dendrite + LiveKit + the Go `vent`
server) keeps running beside it on the shared platform.

> **Status — scaffolding ported, app not yet built.** The Ansible infra and the deploy tooling are
> in place and the app-tier has been renamed `keygrip → clip`, but `app/` is still keygrip's Django
> CMS awaiting the rewrite into the clip.cool media app, and the public apex (`clip.cool`) edge
> cutover is still pending. See [`docs/migration-from-keygrip.md`](./docs/migration-from-keygrip.md)
> for exactly what's done and what remains.

## Repository layout

| Path | What it is |
|---|---|
| [`app/`](./app) | The Django application. **Currently keygrip's CMS code — to be gutted into the clip.cool media app.** |
| [`ansible/`](./ansible) | Infra-as-code for the `vent.dog` pair. All runs go through [`./ac`](./ansible/README.md), a pinned control container (ADR 0007). |
| [`docs/`](./docs) | [`architecture.md`](./docs/architecture.md) (the media pipeline + stack) and [`migration-from-keygrip.md`](./docs/migration-from-keygrip.md) (ported / renamed / remaining). |
| [`bin/secrets`](./bin) | Helper for the SOPS-encrypted secret stores. |
| [`bin/whereami`](./bin) | Prints which environment a shell is in; wired to the prompt via `.claude/settings.json`. |

## Documentation map

- [`CLAUDE.md`](./CLAUDE.md) — the working agreement: conventions, locked decisions, infra layout,
  and the two-layer `keygrip → clip` rename model. The most complete single reference.
- [`docs/architecture.md`](./docs/architecture.md) — what clip.cool is and the media pipeline
  (ingest → transcode → deliver), the stack, and how it maps to the inherited infra.
- [`docs/migration-from-keygrip.md`](./docs/migration-from-keygrip.md) — what was brought over,
  renamed, deliberately kept, and what's left to do.

## Operating the infra

Everything runs through `./ac` (the pinned control container) — never call a host
`ansible-playbook` directly:

```sh
cd ansible
./ac ansible-playbook playbooks/clip-web.yml      # the app tier (replaces keygrip-web)
./ac ansible-playbook playbooks/postgres-ha.yml   # shared HA Postgres
./ac ansible-playbook playbooks/keycloak-realm.yml # shared Keycloak realm (clip + chat clients)
```

See [`ansible/README.md`](./ansible/README.md). Secrets are SOPS-encrypted (age) in git, decrypted to
tmpfs at deploy — never commit a plaintext secret. Edit them with [`bin/secrets`](./bin) (ADR 0001).

## Relationship to keygrip

clip.cool and the chat app share one platform on the `vent.dog` pair, so the rename was **surgical**:
the app tier (`clip_web` role, the `clip` DB, `clip-*` OIDC clients) is clip's, but the shared
platform — the Keycloak `keygrip` realm, the `keygrip-pgha` Postgres cluster, the `keygrip-edge`
Docker network, the `vent-keygrip*` tailscale hosts, the `/opt/keygrip` install paths — keeps its
keygrip names because the chat app depends on it. Full detail in `CLAUDE.md` and
`docs/migration-from-keygrip.md`.
