ARG ALPINE_VER=3.13
ARG LIBTORRENT_VER=latest

FROM wiserain/libtorrent:${LIBTORRENT_VER}-alpine${ALPINE_VER}-py3 AS libtorrent
FROM ghcr.io/linuxserver/baseimage-alpine:${ALPINE_VER}
LABEL maintainer "wiserain"

ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

RUN \
    echo "**** install frolvlad/alpine-python3 ****" && \
	apk add --no-cache python3 && \
	if [[ ! -e /usr/bin/python ]]; then ln -sf /usr/bin/python3 /usr/bin/python; fi && \
	python3 -m ensurepip && \
	rm -r /usr/lib/python*/ensurepip && \
	pip3 install --no-cache --upgrade pip setuptools wheel && \
	if [ ! -e /usr/bin/pip ]; then ln -s pip3 /usr/bin/pip; fi && \
	echo "**** install dependencies for plugin: telegram ****" && \
	apk add --no-cache --virtual=build-deps gcc python3-dev libffi-dev musl-dev openssl-dev && \
	pip install --upgrade python-telegram-bot==12.8 PySocks && \
	echo "**** install dependencies for plugin: cfscraper ****" && \
	apk add --no-cache --virtual=build-deps g++ gcc python3-dev libffi-dev openssl-dev && \
	pip install --upgrade cloudscraper && \
	echo "**** install dependencies for plugin: convert_magnet ****" && \
	apk add --no-cache boost-python3 libstdc++ && \
	echo "**** install dependencies for plugin: decompress ****" && \
	apk add --no-cache unrar && \
	pip install --upgrade \
		rarfile && \
	echo "**** install dependencies for plugin: transmission-rpc ****" && \
	apk add --no-cache --virtual=build-deps build-base python3-dev && \
	pip install --upgrade transmission-rpc && \
	echo "**** install dependencies for plugin: misc ****" && \
	pip install --upgrade \
		deluge-client \
		irc_bot && \
	echo "**** install flexget ****" && \
	apk add --no-cache --virtual=build-deps gcc libxml2-dev libxslt-dev libc-dev python3-dev jpeg-dev && \
	pip install --upgrade --force-reinstall \
		flexget && \
	apk del --purge --no-cache build-deps && \
	apk add --no-cache libxml2 libxslt jpeg && \
	echo "**** system configurations ****" && \
	apk --no-cache add bash bash-completion tzdata && \
	echo "**** cleanup ****" && \
	rm -rf \
		/tmp/* \
		/root/.cache

# copy libtorrent libs
COPY --from=libtorrent /libtorrent-build/usr/lib/ /usr/lib/

# copy local files
COPY root/ /

# add default volumes
VOLUME /config /data
WORKDIR /config

# HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 CMD [ "cd /config && if [[ $(flexget daemon status | tail -n1 | grep running | wc -l) == "1" ]]; then exit 0; else exit 1; fi" ]

# expose port for flexget webui
EXPOSE 5050 5050/tcp
