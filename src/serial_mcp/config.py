from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/serial-mcp/config.yaml")
DEFAULT_SOCKET_PATH = Path("~/.config/serial-mcp/server.sock")
DEFAULT_GRACE_PERIOD = 5.0
DEFAULT_BAUDRATE = 115200


@dataclasses.dataclass(frozen=True)
class ServerConfig:
    socket: Path | None = None
    address: str | None = None
    grace_period: float = DEFAULT_GRACE_PERIOD

    @property
    def is_uds(self) -> bool:
        return self.address is None

    @property
    def uds_path(self) -> Path:
        assert self.is_uds
        return self.socket if self.socket else DEFAULT_SOCKET_PATH

    @property
    def host(self) -> str:
        assert not self.is_uds
        assert self.address is not None
        host, _, _ = self.address.rpartition(":")
        return host or "127.0.0.1"

    @property
    def port(self) -> int:
        assert not self.is_uds
        assert self.address is not None
        _, _, port_str = self.address.rpartition(":")
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
