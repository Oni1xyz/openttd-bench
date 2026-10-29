# Build only in a disposable, credential-free VM. Linux/amd64 is the supported target.
# All game/agent tools are present before an episode starts; runtime bootstrap is disabled.
FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# Immutable signed Debian snapshot, not today's floating apt indexes.
RUN python -c "from pathlib import Path; Path('/etc/apt/sources.list.d/debian.sources').write_text('Types: deb\nURIs: https://snapshot.debian.org/archive/debian/20261022T000000Z/\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://snapshot.debian.org/archive/debian-security/20261022T000000Z/\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n')" \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 libglib2.0-0 procps util-linux xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && for i in 1 2 3 4; do useradd --create-home --shell /bin/bash agent$i; done
COPY image/requirements.lock /opt/openttd/requirements.lock
RUN python -m pip install --only-binary=:all: --require-hashes -r /opt/openttd/requirements.lock

COPY image/fetch_content.py image/install_node.py image/toolchain.json /opt/openttd/
RUN python /opt/openttd/install_node.py \
    && python /opt/openttd/fetch_content.py --cache /opt/openttd/cache --install /opt/openttd/install \
    && python -c "import sys; from pathlib import Path; sys.path.insert(0, '/opt/openttd'); import fetch_content; fetch_content.install_hogex(Path('/opt/openttd/cache'), Path('/opt/openttd/install'))"
ENV PATH=/opt/codex-cli/node_modules/.bin:/var/tmp/vf-node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY image/agent/package.json image/agent/package-lock.json /opt/codex-cli/
RUN npm ci --prefix /opt/codex-cli --ignore-scripts --omit=dev --no-audit --no-fund \
    && codex --version \
    && python -c "from pathlib import Path; Path('/opt/codex-cli/.ready').write_text('0.162.1\n')" \
    && chmod -R go-w /opt/codex-cli /var/tmp/vf-node

# Baked code for the offline smoke. The verified env payload replaces it per episode.
COPY image/ /opt/openttd/
RUN chmod -R go-rwx /opt/openttd/server /opt/openttd/game /opt/openttd/ai /opt/openttd/install /opt/openttd/cache \
    && chmod -R a+rX /opt/openttd/client \
    && dpkg-query -W > /opt/openttd/system-packages.txt
WORKDIR /work
CMD ["sleep", "infinity"]
