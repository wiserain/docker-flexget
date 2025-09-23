from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urlparse

from loguru import logger

from flexget import plugin
from flexget.event import event
from flexget.utils.pathscrub import pathscrub
from flexget.utils.tools import parse_timedelta

try:
    import libtorrent as lt  # pylint: disable=import-error
except ImportError:
    lt = None

logger = logger.bind(name="write_magnet")


class TorrentInfo:
    """A helper class to obtain torrent metadata without download using libtorrent"""

    def __init__(self, **opts):
        """
        Initializes the TorrentInfo instance with default or custom settings.

        Reference:
        https://www.libtorrent.org/reference-Settings.html#settings-pack
        https://libtorrent.org/reference-Add_Torrent.html#add_torrent_params
        """
        self.settings: Dict[str, Any] = {
            "user_agent": "libtorrent/" + lt.version,
            "listen_interfaces": "0.0.0.0:6881",
            "outgoing_interfaces": "",
            "connections_limit": 400,
            "enable_dht": False,
            "use_dht_as_fallback": False,
            "dht_bootstrap_nodes": "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881,",
            "enable_lsd": False,
            "enable_upnp": True,
            "enable_natpmp": True,
            "alert_mask": lt.alert.category_t.all_categories,
            "announce_to_all_tiers": True,
            "announce_to_all_trackers": True,
            "auto_manage_interval": 5,
            "auto_scrape_interval": 0,
            "auto_scrape_min_interval": 0,
            "max_failcount": 1,
            "aio_threads": 8,
            "checking_mem_usage": 2048,
        }  # settings pack for session
        self.trackers: List[str] = []  # for add_torrent_params
        self.save_path: str = "."  # for add_torrent_params
        self.timeout: float = 30.0  # for retrieving metadata

        self.parse_opts(**opts)

        # placeholders
        self.info_hash: Optional[str] = None
        self.uri: Optional[str] = None  # magnet uri
        self.lt_info: Optional[lt.torrent_info] = None
        self.lt_status: Optional[lt.torrent_status] = None
        self.elapsed_time: Optional[float] = None  # only when it is retrieved

    def parse_opts(self, **opts):
        self.save_path = opts.pop("save_path", self.save_path)
        self.trackers = opts.pop("trackers", self.trackers)
        self.timeout = opts.pop("timeout", self.timeout)
        if http_proxy := opts.pop("http_proxy", None):
            proxy_url = urlparse(http_proxy)
            self.settings.update(
                {
                    "proxy_username": proxy_url.username,
                    "proxy_password": proxy_url.password,
                    "proxy_hostname": proxy_url.hostname,
                    "proxy_port": proxy_url.port,
                    "proxy_type": (
                        lt.proxy_type_t.http_pw if proxy_url.username and proxy_url.password else lt.proxy_type_t.http
                    ),
                    "force_proxy": True,
                    "anonymous_mode": True,
                }
            )
        self.settings.update(opts)

    @classmethod
    def from_torrent_file(cls, torrent_file: Union[bytes, str, Path], **opts):
        """
        Returns an instance of TorrentInfo object from a .torrent file.

        Data conversion flow:
        torrent_file >> lt_dict >> lt_info
        """
        if isinstance(torrent_file, (str, Path)):
            file_path = Path(torrent_file)
            if not file_path.is_file():
                raise FileNotFoundError(f"File not found: {torrent_file}")

            with open(file_path, "rb") as f:
                data = f.read()
        elif isinstance(torrent_file, bytes):
            data = torrent_file
        else:
            raise TypeError("torrent_file must be bytes, str, or a Path object.")

        lt_dict = lt.bdecode(data)
        lt_info = lt.torrent_info(lt_dict)

        _t = cls(**opts)
        _t.info_hash = str(lt_info.info_hash())
        _t.uri = lt.make_magnet_uri(lt_info)
        _t.lt_info = lt_info
        return _t

    @classmethod
    def from_magnet_uri(cls, uri: str, **opts):
        """
        Returns a TorrentInfo instance prepared to retrieve metadata from a magnet URI.

        Reference:
        https://www.libtorrent.org/reference-Core.html#parse_magnet_uri()
        https://www.libtorrent.org/reference-Add_Torrent.html#add_torrent_params
        """
        _t = cls(**opts)
        _t.info_hash = str(lt.parse_magnet_uri(uri))
        _t.uri = uri
        return _t

    #
    # retrieve metadata
    #

    def add_torrent_params(self):
        if not self.uri:
            raise ValueError("Uri is not available.")

        atp = lt.parse_magnet_uri(self.uri)  # add_torrent_params

        # https://gist.github.com/francoism90/4db9efa5af546d831ca47208e58f3364
        atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
        atp.flags |= lt.torrent_flags.duplicate_is_error | lt.torrent_flags.auto_managed | lt.torrent_flags.upload_mode

        # add trackers if not available in atp
        if len(atp.trackers) == 0:
            atp.trackers = self.trackers
        atp.save_path = self.save_path
        return atp

    @staticmethod
    def _retrieve(handle: lt.torrent_handle, timeout: float = 30.0) -> Tuple:
        """
        A helper method to retrieve torrent_info from a handle.
        """
        stime = time.time()
        until = stime + timeout

        if not handle or not handle.is_valid():
            raise ValueError("Invalid torrent handle provided.")

        poll_inverval = 0.5
        logger.debug("Retrieving metadata for handle: %s", handle.info_hash())

        while (now := time.time()) < until:
            status = handle.status()

            if status.has_metadata:
                etime = now - stime
                logger.debug("Successfully retrieved metadata after %f seconds.", etime)
                _info = handle.torrent_file()
                return _info, status, etime

            time.sleep(poll_inverval)

        raise TimeoutError(f"Metadata retrieval timed out after {timeout} seconds.")

    def retrieve(self, **opts):
        """
        Retrieves torrent metadata by creating a libtorrent session.

        Reference:
        https://www.libtorrent.org/reference-Session.html#session
        """
        self.parse_opts(**opts)

        # session
        sess = lt.session(self.settings)

        sess.add_extension("ut_metadata")
        sess.add_extension("ut_pex")
        sess.add_extension("metadata_transfer")

        # add_torrent_params
        atp = self.add_torrent_params()

        # handle
        h: lt.torrent_handle = None
        try:
            h = sess.add_torrent(atp)

            if self.settings["enable_dht"]:
                h.force_dht_announce()
            _info, _status, etime = self._retrieve(h, timeout=self.timeout)
        finally:
            if h:
                sess.remove_torrent(h, True)

        # Note
        # When metadata is retrieved from tracker,
        # lt.torrent_info has empty fields for:
        # - creation_date = 0
        # - creator = ''

        self.lt_info = _info
        self.lt_status = _status
        self.elapsed_time = etime
        return self

    #
    # output / export
    #

    @staticmethod
    def _info2dict(obj: lt.torrent_info) -> dict:
        # This is obtained by [a for a in dir(lt_info) if not a.startswith("__")]
        attrs = [
            # "collections",  # list
            "comment",  # str
            "creation_date",  # int
            "creator",  # str
            "files",  # lt.file_storage
            "info_hash",  # lt.sha1_hash
            # "info_hashes",  # lt.info_hash_t
            "is_i2p",  # bool
            "is_valid",  # bool
            "name",  # str
            # "nodes",  # list
            "num_files",  # int
            "num_pieces",  # int
            # "orig_files",  # lt.file_storage
            "piece_length",  # int
            "priv",  # bool
            # "similar_torrents",  # list
            # "ssl_cert",  # str
            "total_size",  # int
            "trackers",  # list[lt.announce_entry]
            # "web_seeds",  # list
        ]
        return {attr: a() if callable(a) else a for attr in attrs if (a := getattr(obj, attr, None))}

    @staticmethod
    def _fs2dict(fs: lt.file_storage) -> dict:
        """
        from libtorrent file_storage to python dictionary object.
        """
        attrs = [
            # ("absolute_path", "file_absolute_path"),  # bool
            # ("flags", "file_flags"),  # int
            # ("index_at_offset", "file_index_at_offset"),  # int
            # ("index_at_piece", "file_index_at_piece"),  # int
            ("name", "file_name"),  # str
            ("offset", "file_offset"),  # int
            ("path", "file_path"),  # str
            ("size", "file_size"),  # int
            # ("flag_executable", "flag_executable"),  # int
            # ("flag_hidden", "flag_hidden"),  # int
            # ("flag_pad_file", "flag_pad_file"),  # int
            # ("flag_symlink", "flag_symlink"),  # int
            # ("hash", "hash"),  # lt.sha1_hash
            # ("piece_index_at_file", "piece_index_at_file"),  # int
            # ("piece_size", "piece_size"),  # int
            # ("root", "root"),  # lt.sha256_hash
            # ("symlink", "symlink"),  # str
        ]
        return [
            {key: a(i) if callable(a) else a for key, attr in attrs if (a := getattr(fs, attr, None))}
            for i in range(fs.num_files())
        ]

    @staticmethod
    def status2dict(_status: lt.torrent_status) -> dict:
        attrs = [
            "num_seeds",  # int
            "num_peers",  # int
            "num_complete",  # int
            "num_incomplete",  # int
        ]
        return {attr: a() if callable(a) else a for attr in attrs if (a := getattr(_status, attr, None))}

    @staticmethod
    def size_fmt(num: int, suffix: str = "B") -> str:
        # Size in Windows https://superuser.com/a/938259
        for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
            if abs(num) < 1000.0:
                return f"{num:3.1f} {unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f} Y{suffix}"

    def to_dict(self) -> Dict:
        """
        from libtorrent torrent_info to python dictionary object.

        Reference:
        https://www.libtorrent.org/reference-Torrent_Info.html#torrent_info
        """
        if not self.lt_info:
            raise ValueError("Metadata not yet retrieved. Call retrieve() first.")

        _dict = self._info2dict(self.lt_info)
        _dict["total_size_fmt"] = self.size_fmt(_dict["total_size"])
        _dict["files"] = self._fs2dict(_dict["files"])
        _dict["files"] = [{**f, "size_fmt": self.size_fmt(f["size"])} for f in _dict["files"]]
        _dict["info_hash"] = str(_dict["info_hash"])
        _dict["trackers"] = [t.url for t in _dict["trackers"]]
        _dict["magnet_uri"] = self.uri or lt.make_magnet_uri(self.lt_info)

        # only when the metadata is retrieved
        if self.lt_status:
            _dict.update(self.status2dict(self.lt_status))
        if self.elapsed_time:
            _dict["elapsed_time"] = self.elapsed_time
        return _dict

    def to_file(self) -> Tuple[bytes, str]:
        """
        Creates a .torrent file and a scrubbed filename.
        """
        if not self.lt_info:
            raise ValueError("Torrent data not available.")

        # create torrent object and generate file stream
        torrent = lt.create_torrent(self.lt_info)
        if not self.lt_info.creator():
            torrent.set_creator(f"libtorrent v{lt.version}")  # signature
        _dict = torrent.generate()

        _file = lt.bencode(_dict)
        _name = pathscrub(self.lt_info.name(), os="windows", filename=True)
        return _file, _name


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

            trackers_from = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all_ip.txt"
            self.trackers = [x for x in requests.get(trackers_from, timeout=5).text.splitlines() if x]
        except Exception as e:
            logger.debug("Failed to get trackers from {}: {}", trackers_from, str(e))
            self.trackers = []

    def prepare_config(self, config):
        if not isinstance(config, dict):
            config = {}
        config.setdefault("timeout", "30 seconds")
        config.setdefault("force", False)
        config.setdefault("use_dht", True)
        config.setdefault("http_proxy", "")
        return config

    @plugin.priority(plugin.PRIORITY_FIRST)
    def on_task_start(self, _task, config):
        if config is False:
            return
        if not lt:
            raise plugin.DependencyError("write_magnet", "libtorrent", "libtorrent package required", logger)

    @plugin.priority(130)
    def on_task_download(self, task, config):
        if config is False:
            return
        config = self.prepare_config(config)
        # Create the conversion target directory
        converted_path = task.manager.config_base / "converted"

        timeout = parse_timedelta(config["timeout"]).total_seconds()

        if not converted_path.is_dir():
            converted_path.mkdir()

        for entry in task.accepted:
            if entry["url"].startswith("magnet:"):
                entry.setdefault("urls", [entry["url"]])
                try:
                    logger.info("Converting entry {} magnet URI to a torrent file", entry["title"])
                    ti = TorrentInfo.from_magnet_uri(
                        entry["url"],
                        trackers=self.trackers,
                        save_path=str(converted_path),
                        enable_dht=config["use_dht"],
                        http_proxy=config["http_proxy"],
                        timeout=timeout,
                    )
                    _file, _name = ti.to_file()
                    torrent_path = converted_path / (_name + ".torrent")
                    with torrent_path.open("wb") as f:
                        f.write(_file)
                    logger.debug("Torrent file wrote to {}", torrent_path)

                except (plugin.PluginError, TypeError) as e:
                    logger.error("Unable to convert Magnet URI for entry {}: {}", entry["title"], e)
                    if config["force"]:
                        entry.fail("Magnet URI conversion failed")
                    continue
                # Windows paths need an extra / prepended to them for url
                if not torrent_file.startswith("/"):
                    torrent_file = "/" + torrent_file
                entry["url"] = torrent_file
                entry["file"] = torrent_file
                # make sure it's first in the list because of how download plugin works
                entry["urls"].insert(0, f"file://{quote(torrent_file)}")

                # TODO: could be populate extra fields from torrent_info
                torrent_info = ti.to_dict()
                if "content_size" not in entry.keys():
                    entry["content_size"] = round(torrent_info["total_size"] / 1024**2)
                entry["seeders"] = torrent_info["num_complete"]
                entry["leechers"] = torrent_info["num_incomplete"]


@event("plugin.register")
def register_plugin():
    plugin.register(ConvertMagnet, "write_magnet", api_ver=2)


# Expected working with libtorrent version 1.2.19 2.0.11
