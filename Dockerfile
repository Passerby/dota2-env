# syntax=docker/dockerfile:1
# Headless runtime for dota2-env: what the Linux Dota 2 client links against, SteamCMD to download the client, and
# the Python environment. The client itself (76 GB) is not in the image: it is mounted at /opt/dota2. See docs/DOCKER.md.

ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG APT_MIRROR
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG STEAMCMD_URL=https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz
# The owner of the mounted Dota install on the host, so that what SteamCMD downloads into it stays theirs.
ARG UID=1000
ARG GID=1000

# lib32gcc-s1 is for the 32-bit SteamCMD; libx11-6 through libthai0 are what the ffmpeg and pango libraries bundled
# with the client link against; procps gives the environment pkill and pgrep.
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s#//archive.ubuntu.com#//$APT_MIRROR#; s#//security.ubuntu.com#//$APT_MIRROR#" /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl lib32gcc-s1 procps python3 python3-venv \
        libx11-6 libdrm2 libva2 libvdpau1 libharfbuzz0b libfribidi0 libglib2.0-0t64 libthai0 \
 && rm -rf /var/lib/apt/lists/* \
 && userdel --remove ubuntu \
 && groupadd --gid "$GID" dota \
 && useradd --create-home --uid "$UID" --gid "$GID" dota \
 && mkdir -p /opt/steamcmd /opt/dota2 /opt/venv \
 && chown dota:dota /opt/steamcmd /opt/dota2 /opt/venv

USER dota
# SteamCMD keeps its login and caches in ~/Steam; the client looks for steamclient.so in ~/.steam/sdk64.
RUN curl -fsSL "$STEAMCMD_URL" | tar -xz -C /opt/steamcmd \
 && /opt/steamcmd/steamcmd.sh +quit \
 && mkdir -p ~/.steam \
 && ln -s /opt/steamcmd/linux64 ~/.steam/sdk64

COPY --chown=dota:dota . /opt/dota2-env
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -e "/opt/dota2-env[llm]"

ENV PATH=/opt/venv/bin:/opt/steamcmd:$PATH \
    DOTA_GAME_PATH=/opt/dota2/game \
    LANG=C.UTF-8
WORKDIR /opt/dota2-env
