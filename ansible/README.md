# Ansible — infra config-as-code

Config management for the new Keygrip platform. **Ansible + SOPS + Docker** on **Ubuntu 24.04**
(ADR 0006). Ansible configures the *host*; the apps run as *containers*.

> ⚠️ **Never target the old prod boxes.** This inventory contains only the new-platform infra
> dev box (`vent.dog`). `keygrip-prod` / `keygrip-staging` run the OLD zrag app and are off-limits.

## Layout
```
ansible/
  ansible.cfg              # inventory path, ssh settings
  requirements.yml         # collections (community.general/docker/sops)
  inventory.yml            # dev: vent.dog  (staging/prod added post-funding)
                           # NOTE: must sit next to group_vars/ or group_vars won't load
  group_vars/
    all.yml                # non-secret defaults (all hosts)
    dev/
      vars.yml             # dev non-secret vars
      secrets.sops.yml     # dev secrets — SOPS-encrypted (age)
  playbooks/
    bootstrap.yml          # baseline + deploy user + Docker CE
  roles/                   # (added as services land: keycloak, cloudflared, tailscale, ...)
```

## Always run via the pinned control container (ADR 0007)
All Ansible runs go through `./ac`, a wrapper around a pinned image (`ansible-core` + collections
+ `sops`) so versions are identical on every laptop and in CI. It builds the image on first use
and mounts the repo, your SSH keys (ro), and your age key (ro, for `community.sops`). The target
Python is pinned via `ansible_python_interpreter` so modules never run on an unexpected interpreter.

```bash
cd ansible
./ac ansible --version                          # builds the image on first run
./ac ansible-playbook playbooks/bootstrap.yml   # once vent.dog is up on Ubuntu 24.04
```

> Running Ansible directly off your host still works, but then you own the version drift —
> prefer `./ac`.

> ⚠️ **Deploy the app stack from the main checkout, not a git worktree.** `keygrip-web.yml`
> archives the app source with `git archive HEAD:app`, and `./ac` only mounts the repo dir as
> `/work`. In a **worktree**, `.git` is a *file* pointing at the parent repo's gitdir (which isn't
> mounted), so the archive fails: `fatal: not a git repository: …/.git/worktrees/<name>`. Run
> `keygrip-web.yml` from the primary checkout (`/home/enum/Projects/keygrip`). Other playbooks
> (e.g. `observability.yml`) copy role files, not `git archive`, so they're fine from any worktree.

## Secrets (SOPS + age)
Secrets are SOPS-encrypted with an **age** key, per environment (ADR 0001). Creation rules live
in the repo-root `.sops.yaml`. Private keys are **never committed** — locally at
`~/.config/sops/age/keys.txt`, and (per ADR 0003) backed up in Vaultwarden + offline.

```bash
# edit/encrypt the dev secrets (uses .sops.yaml to pick the recipient)
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt
sops ansible/group_vars/dev/secrets.sops.yml
```

Ansible decrypts these at runtime via `community.sops` (not `ansible-vault`). Add a new env by
generating its age key, adding the recipient to `.sops.yaml`, and creating
`group_vars/<env>/secrets.sops.yml`.
