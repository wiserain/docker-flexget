# docker-flexget

Docker image for running [flexget](http://flexget.com/)

Container features are

- [lsiobase/alpine](https://github.com/linuxserver/docker-baseimage-alpine)
- pre-installed dependencies for plugins
  - telegram
  - cfscraper
  - convert_magnet
  - decompress
  - transmission
  - deluge
  - irc

## Usage

### docker run

```bash
docker run -d \
    --name=<container name> \
    -p 5050:5050 \
    -v <path for data files>:/data \
    -v <path for config files>:/config \
    -e FG_WEBUI_PASSWD=<desired password> \
    -e FG_LOG_LEVEL=info \
    -e FG_LOG_FILE=flexget.log \
    -e PUID=<UID for user> \
    -e PGID=<GID for user> \
    -e TZ=<timezone> \
    wiserain/flexget
```
### docker-compose

```yml
version: "3"
services:
  flexget:
    image: wiserain/flexget
    container_name: <container name>
    environment:
      - FG_WEBUI_PASSWD=<desired password>
      - FG_LOG_LEVEL=info
      - FG_LOG_FILE=flexget.log
      - PUID=<UID for user>
      - PGID=<GID for user>
    ports :
      - 5050:5050
    volumes:
      - <path for data files>:/data
      - <path for config files>:/config
    restart: on-failure:5 
 ```

Most importantly, secure webui using ```FG_WEBUI_PASSWD```.

### Additional packages

If there are additional packages you may want to install, create bash script with any name under ```/config/custom-cont-init.d```, for example,

```bash
#!/usr/bin/with-contenv bash
apk add -q --no-cache <alpine pkgs>
pip install <python pkgs>
```

Then, it will run every container start.

### Custom plugins

You can install custom plugins by ```FG_PLUGINS="{plugin_name_1} {plugin_name_2}"``` whose value is a space-separated list of plugin names. Currently available ones are

- ```write_magnet```: Mostly same as built-in ```convert_magnet``` but expect better performance and improved error handling, which is compatible with a version of libtorrent containerized in this image.
