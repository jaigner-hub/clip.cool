# Pinned Ansible control-node image — reproducible runs across laptops + CI (ADR 0007).
# Build context is the ansible/ dir:
#   docker build -t keygrip-ansible:local -f control/Dockerfile .
# Normally you don't build by hand — the ./ac wrapper builds it on first use.
FROM python:3.12-slim-bookworm

ARG SOPS_VERSION=3.8.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-client sshpass git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# sops binary — community.sops shells out to it for decrypt-at-runtime.
RUN curl -fsSL -o /usr/local/bin/sops \
      "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
 && chmod +x /usr/local/bin/sops

# Ansible — pin the minor; lock exact with `pip freeze` once the stack is stable.
RUN pip install --no-cache-dir "ansible-core~=2.18.0"

# Collections (pinned in requirements.yml).
COPY requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml

# Stage host-mounted SSH/age keys into root-owned copies (fixes ownership mismatch).
COPY control/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /work/ansible
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["ansible", "--version"]
