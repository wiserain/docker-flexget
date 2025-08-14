from __future__ import annotations

import time
from typing import TYPE_CHECKING
from datetime import datetime
from urllib.parse import urlparse, quote

from loguru import logger

from flexget import plugin
from flexget.event import event
from flexget.utils.pathscrub import pathscrub
from flexget.utils.tools import parse_timedelta

if TYPE_CHECKING:
    from pathlib import Path

logger = logger.bind(name='write_magnet')


class ConvertMagnet:
    """Convert magnet only entries to a torrent file"""

    schema = {
        "oneOf": [
            # Allow write_magnet: no form to turn off plugin altogether
            {"type": "boolean"},
            {
                "type": "object",
                "properties": {
                    "timeout": {"type": "string", "format": "interval"},
                    "force": {"type": "boolean"},
                    "num_try": {"type": "integer"},
                    "use_dht": {"type": "boolean"},
                    "http_proxy": {"type": "string", "format": "uri"},
                },
                "additionalProperties": False,
            },
        ]
    }

    def __init__(self):
        try:
            import requests
            trackers_from = 'https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all_ip.txt'
            self.trackers = [x for x in requests.get(trackers_from, timeout=5).text.splitlines() if x]
        except Exception as e:
            logger.debug('Failed to get trackers from {}: {}', trackers_from, str(e))
            self.trackers = []

    def _convert_lt_info(self, torrent_info):
        """from libtorrent torrent_info to python dictionary object"""
        import libtorrent as lt

        return {
            'name': torrent_info.name(),
            'num_files': torrent_info.num_files(),
            'total_size': torrent_info.total_size(),  # in byte
            'info_hash': str(torrent_info.info_hash()),  # original type: libtorrent.sha1_hash
            'num_pieces': torrent_info.num_pieces(),
            'creator': torrent_info.creator() or f"libtorrent v{lt.version}",
            'comment': torrent_info.comment(),
            'files': [{'path': file.path, 'size': file.size} for file in torrent_info.files()],
            'magnet_uri': lt.make_magnet_uri(torrent_info),
        }

    def _setup_lt_session(self, magnet_uri: str, dest_dir: Path, use_dht: bool, http_proxy: str):
        import libtorrent as lt

        # parameters
        try:
            params = lt.parse_magnet_uri(magnet_uri)
        except Exception as e:
            raise plugin.PluginError('Failed to parse the uri: {}', str(e))

        # prevent downloading
        # https://stackoverflow.com/q/45680113
        if isinstance(params, dict):
            params['flags'] |= lt.add_torrent_params_flags_t.flag_upload_mode
        else:
            params.flags |= lt.add_torrent_params_flags_t.flag_upload_mode

        lt_version = [int(v) for v in lt.version.split('.')]
        if [0, 16, 13, 0] < lt_version < [1, 1, 3, 0]:
            # for some reason the info_hash needs to be bytes but it's a struct called sha1_hash
            if isinstance(params, dict):
                params['info_hash'] = params['info_hash'].to_bytes()
            else:
                params.info_hash = params.info_hash.to_bytes()

        # add_trackers - currently always append
        try:
            if isinstance(params, dict):
                params['trackers'] += self.trackers
            else:
                params.trackers += self.trackers
        except Exception as e:
            logger.debug('Failed to add trackers: {}', str(e))

        params.save_path = str(dest_dir)

        # session from setting pack
        settings = {
            # basics
            # 'user_agent': 'libtorrent/' + lt.__version__,
            'listen_interfaces': '0.0.0.0:6881',
            # dht
            'enable_dht': use_dht,
            'use_dht_as_fallback': True,
            'dht_bootstrap_nodes': 'router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881,127.0.0.1:6881',
            'enable_lsd': False,
            'enable_upnp': True,
            'enable_natpmp': True,
            'announce_to_all_tiers': True,
            'announce_to_all_trackers': True,
            'aio_threads': 4*2,
            'checking_mem_usage': 1024*2,
        }
        if http_proxy:
            # TODO: TEST http_proxy
            proxy_url = urlparse(http_proxy)
            logger.debug(proxy_url)
            settings.update({
                'proxy_username': proxy_url.username,
                'proxy_password': proxy_url.password,
                'proxy_hostname': proxy_url.hostname,
                'proxy_port': proxy_url.port,
                'proxy_type': lt.proxy_type_t.http_pw if proxy_url.username and proxy_url.password else lt.proxy_type_t.http,
                'force_proxy': True,
                'anonymous_mode': True,
            })
        session = lt.session(settings)

        # session.add_extension('ut_metadata')
        # session.add_extension('ut_pex')
        # session.add_extension('metadata_transfer')

        return session, params

    def _get_metadata(self, handle, timeout: float, num_try: int):
        max_try = max(num_try, 1)
        for tryid in range(max_try):
            timeout_value = timeout
            logger.debug(f'Trying to get metadata ... {tryid+1}/{max_try}')
            while not handle.has_metadata():
                time.sleep(0.1)
                timeout_value -= 0.1
                if timeout_value <= 0:
                    break

            if handle.has_metadata():
                logger.debug('Metadata acquired after {}*{}+{:.1f} seconds', tryid, timeout, timeout - timeout_value)
                return handle.get_torrent_info()
        raise plugin.PluginError(f'Timed out after {max_try}*{timeout} seconds')

    def _get_peer_info(self, handle, timeout: float) -> dict:
        # start scraping
        timeout_value = timeout
        logger.debug('Trying to get peerinfo ... ')
        while handle.status(0).num_complete < 0:
            time.sleep(0.1)
            timeout_value -= 0.1
            if timeout_value <= 0:
                break

        if handle.status(0).num_complete >= 0:
            torrent_status = handle.status(0)
            logger.debug('Peerinfo acquired after {:.1f} seconds', timeout - timeout_value)
            return {
                'seeders': torrent_status.num_complete,
                'peers': torrent_status.num_incomplete,
                'total_wanted': torrent_status.total_wanted
            }
        raise plugin.PluginError(f'Timed out after {timeout} seconds')

    def magnet_to_torrent(
        self,
        magnet_uri: str,
        dest_dir: Path,
        timeout: float,
        num_try: int,
        use_dht: bool,
        http_proxy: str,
    ):
        import libtorrent as lt

        session, params = self._setup_lt_session(magnet_uri, dest_dir, use_dht, http_proxy)
        handle = None
        try:
            handle = session.add_torrent(params)

            if use_dht:
                handle.force_dht_announce()

            logger.debug('Acquiring torrent metadata for magnet {}', magnet_uri)

            lt_info = self._get_metadata(handle, timeout, num_try)

            # create torrent object
            torrent = lt.create_torrent(lt_info)
            torrent.set_creator(f"libtorrent v{lt.version}")    # signature
            torrent_dict = torrent.generate()

            torrent_info = self._convert_lt_info(lt_info)
            torrent_info.update({
                'trackers': params['trackers'] if isinstance(params, dict) else params.trackers,
                'creation_date': datetime.fromtimestamp(torrent_dict[b'creation date']).isoformat(),
            })
            peer_info = self._get_peer_info(handle, timeout)
            torrent_info.update(peer_info)
        finally:
            if session and handle:
                session.remove_torrent(handle, True)

        torrent_path = dest_dir / (lt_info.name() + '.torrent')
        torrent_path = Path(pathscrub(str(torrent_path)))
        with torrent_path.open('wb') as f:
            f.write(lt.bencode(torrent_dict))
        logger.debug('Torrent file wrote to {}', torrent_path)

        return str(torrent_path), torrent_info

    def prepare_config(self, config):
        if not isinstance(config, dict):
            config = {}
        config.setdefault('timeout', '15 seconds')
        config.setdefault('force', False)
        config.setdefault('num_try', 3)
        config.setdefault('use_dht', True)
        config.setdefault('http_proxy', '')
        return config

    @plugin.priority(plugin.PRIORITY_FIRST)
    def on_task_start(self, task, config):
        if config is False:
            return
        try:
            import libtorrent  # noqa
        except ImportError:
            raise plugin.DependencyError(
                'write_magnet', 'libtorrent', 'libtorrent package required', logger
            )

    @plugin.priority(130)
    def on_task_download(self, task, config):
        if config is False:
            return
        config = self.prepare_config(config)
        # Create the conversion target directory
        converted_path = task.manager.config_base / 'converted'

        timeout = parse_timedelta(config['timeout']).total_seconds()

        if not converted_path.is_dir():
            converted_path.mkdir()

        for entry in task.accepted:
            if entry['url'].startswith('magnet:'):
                entry.setdefault('urls', [entry['url']])
                try:
                    logger.info('Converting entry {} magnet URI to a torrent file', entry['title'])
                    torrent_file, torrent_info = self.magnet_to_torrent(
                        entry['url'], converted_path, timeout, config['num_try'], config['use_dht'], config['http_proxy']
                    )
                except (plugin.PluginError, TypeError) as e:
                    logger.error(
                        'Unable to convert Magnet URI for entry {}: {}', entry['title'], e
                    )
                    if config['force']:
                        entry.fail('Magnet URI conversion failed')
                    continue
                # Windows paths need an extra / prepended to them for url
                if not torrent_file.startswith('/'):
                    torrent_file = '/' + torrent_file
                entry['url'] = torrent_file
                entry['file'] = torrent_file
                # make sure it's first in the list because of how download plugin works
                entry['urls'].insert(0, f'file://{quote(torrent_file)}')

                # TODO: could be populate extra fields from torrent_info
                if "content_size" not in entry.keys():
                    entry["content_size"] = (
                        round(torrent_info['total_wanted'] / 1024 ** 2)
                    )
                entry['seeders'] = torrent_info['seeders']
                entry['leechers'] = torrent_info['peers']


@event('plugin.register')
def register_plugin():
    plugin.register(ConvertMagnet, 'write_magnet', api_ver=2)

# confirmed working with libtorrent version 1.1.5 1.1.13 1.2.5+
