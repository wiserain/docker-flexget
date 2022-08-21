ARG ALPINE_VER
ARG LIBTORRENT_VER=latest

FROM ghcr.io/by275/libtorrent:${LIBTORRENT_VER}-alpine${ALPINE_VER} AS libtorrent
FROM ghcr.io/linuxserver/baseimage-alpine:${ALPINE_VER} AS base

RUN \
    echo "**** install frolvlad/alpine-python3 ****" && \
    apk add --no-cache python3 && \
    if [ ! -e /usr/bin/python ]; then ln -sf /usr/bin/python3 /usr/bin/python; fi && \
    python3 -m ensurepip && \
    rm -r /usr/lib/python*/ensurepip && \
    pip3 install --no-cache --upgrade pip setuptools wheel && \
    if [ ! -e /usr/bin/pip ]; then ln -s pip3 /usr/bin/pip; fi && \
    echo "**** cleanup ****" && \
    rm -rf \
        /tmp/* \
        /root/.cache

# 
# BUILD
# 
FROM alpine:${ALPINE_VER} AS unrar

ARG ALPINE_VER

RUN \
    apk add --no-cache \
        alpine-sdk \
        bash \
        shadow \
        sudo \
        su-exec \
        openssl

RUN \
    useradd -m -s /bin/bash \
        -p $(openssl passwd -1 abc) abc && \
    echo "abc    ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    addgroup abc abuild && \
    su-exec abc:abuild abuild-keygen -ain && \
    mkdir -p /var/cache/distfiles && \
    chmod a+w /var/cache/distfiles

RUN git clone https://github.com/alpinelinux/aports /home/abc/aports -b ${ALPINE_VER}-stable --depth=1 && \
    chown -R abc:abuild /home/abc

RUN su-exec abc:abuild env APKBUILD=/home/abc/aports/non-free/unrar/APKBUILD abuild -r
RUN mkdir /unrar-build && find /home/abc/packages -name *.apk -type f -exec tar xf {} -C /unrar-build \;


FROM base AS builder

COPY requirements.txt /tmp/

RUN \
    echo "**** install build dependencies ****" && \
    apk add --no-cache \
        build-base \
        python3-dev \
        musl-dev \
        libffi-dev \
        openssl-dev \
        libxml2-dev \
        libxslt-dev \
        libc-dev \
        jpeg-dev \
        linux-headers && \
    pip install -r /tmp/requirements.txt --root /bar --no-warn-script-location

# copy libtorrent libs
COPY --from=libtorrent /libtorrent-build/usr/lib/ /bar/usr/lib/

# copy unrar
COPY --from=unrar /unrar-build/usr/bin/ /bar/usr/bin/

ADD https://raw.githubusercontent.com/by275/docker-base/main/_/etc/cont-init.d/install-pkg /bar/etc/cont-init.d/15-install-pkg

# copy local files
COPY root/ /bar/

# 
# RELEASE
# 
FROM base
LABEL maintainer="wiserain"
LABEL org.opencontainers.image.source https://github.com/wiserain/docker-flexget

ENV \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    FIX_DIR_OWNERSHIP_CONFIG=1 \
    FIX_DIR_OWNERSHIP_DATA=1

COPY --from=builder /bar/ /

RUN \
    echo "**** install runtime dependencies ****" && \
    apk add --no-cache \
        `# libtorrent` \
        boost-python3 libstdc++ \
        `# lxml` \
        libxml2 libxslt \
        `# others` \
        jpeg \
        `# system` \
        bash bash-completion findutils tzdata && \
    echo "**** cleanup ****" && \
    rm -rf \
        /tmp/* \
        /root/.cache

# add default volumes
VOLUME /config /data

# expose port for flexget webui
EXPOSE 5050 5050/tcp
