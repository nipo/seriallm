from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import yaml

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _config_dir = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "seriallm"
else:
    _config_dir = Path("~/.config/seriallm")

DEFAULT_CONFIG_PATH = _config_dir / "config.yaml"
DEFAULT_SOCKET_PATH = _config_dir / "server.sock"
DEFAULT_TCP_ADDRESS = "127.0.0.1:18808"
DEFAULT_GRACE_PERIOD = 5.0
DEFAULT_BAUDRATE = 115200


@dataclasses.dataclass(frozen=True)
class ServerConfig:
    socket: Path | None = None
    address: str | None = None
    grace_period: float = DEFAULT_GRACE_PERIOD

    @property
    def is_uds(self) -> bool:
        if self.address is not None:
            return False
        if self.socket is not None:
            return True
        # No explicit config — platform default
        return not _IS_WINDOWS

    @property
    def uds_path(self) -> Path:
        return self.socket if self.socket else DEFAULT_SOCKET_PATH

    @property
    def effective_address(self) -> str:
        """Return the TCP address, falling back to the default on Windows."""
        if self.address is not None:
            return self.address
        return DEFAULT_TCP_ADDRESS

    @property
    def host(self) -> str:
        host, _, _ = self.effective_address.rpartition(":")
        return host or "127.0.0.1"

    @property
    def port(self) -> int:
        _, _, port_str = self.effective_address.rpartition(":")
        return int(port_str)


@dataclasses.dataclass(frozen=True)
class ProfileConfig:
    baudrate: int = DEFAULT_BAUDRATE


@dataclasses.dataclass(frozen=True)
class AliasConfig:
    url: str
    profile: str | None = None


@dataclasses.dataclass(frozen=True)
class Config:
    server: ServerConfig = dataclasses.field(default_factory=ServerConfig)
    aliases: dict[str, AliasConfig] = dataclasses.field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = dataclasses.field(default_factory=dict)
    config_path: Path | None = None

    def resolve_target(self, target: str) -> tuple[str, int]:
        """Resolve a target (alias name or raw URL) to (url, baudrate)."""
        if target in self.aliases:
            alias = self.aliases[target]
            profile_name = alias.profile or "default"
            profile = self.profiles.get(profile_name, ProfileConfig())
            return alias.url, profile.baudrate
        # Not an alias — treat as raw URL, use default profile
        default_profile = self.profiles.get("default", ProfileConfig())
        return target, default_profile.baudrate


def load_config(path: Path | None = None) -> Config:
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not config_path.exists():
        return Config()

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        return Config()

    # Parse server section
    raw_server = raw.get("server", {})
    server = ServerConfig(
        socket=Path(raw_server["socket"]).expanduser() if "socket" in raw_server else None,
        address=raw_server.get("address"),
        grace_period=float(raw_server.get("grace_period", DEFAULT_GRACE_PERIOD)),
    )

    # Parse profiles section
    profiles: dict[str, ProfileConfig] = {}
    for name, raw_profile in raw.get("profile", {}).items():
        profiles[name] = ProfileConfig(
            baudrate=int(raw_profile.get("baudrate", DEFAULT_BAUDRATE)),
        )

    # Parse aliases section
    aliases: dict[str, AliasConfig] = {}
    for name, raw_alias in raw.get("alias", {}).items():
        aliases[name] = AliasConfig(
            url=raw_alias["url"],
            profile=raw_alias.get("profile"),
        )

    return Config(server=server, aliases=aliases, profiles=profiles, config_path=config_path)
