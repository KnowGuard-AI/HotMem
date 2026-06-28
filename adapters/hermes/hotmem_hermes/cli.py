"""Hermes CLI commands for the HotMem provider.

Registered via ``register_cli(subparser)`` and gated to the active provider.
Commands appear under ``hermes hotmem <subcommand>``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hotmem.client import HotMemClient


def _status(args: argparse.Namespace) -> None:
    hermes_home = args.hermes_home or Path.home() / ".hermes"
    config_path = Path(hermes_home) / "hotmem.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    base_url = config.get("hotmem_url", "http://127.0.0.1:8711")
    client = HotMemClient(base_url)
    try:
        health = client.health()
        print(f"HotMem sidecar: {health['status']}")
        print(f"Memory count:  {health['memory_count']}")
        print(f"Uptime:        {health['uptime_s']}s")
        print(f"DB path:       {health['db_path']}")
    except Exception as err:
        print(f"HotMem sidecar unreachable at {base_url}: {err}")


def _config(args: argparse.Namespace) -> None:
    hermes_home = args.hermes_home or Path.home() / ".hermes"
    config_path = Path(hermes_home) / "hotmem.json"
    if config_path.exists():
        print(config_path.read_text())
    else:
        print(f"No config at {config_path} — run `hermes memory setup`.")


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes hotmem`` argparse tree."""
    subs = subparser.add_subparsers(dest="hotmem_command")
    p_status = subs.add_parser("status", help="Show HotMem sidecar health")
    p_status.add_argument("--hermes-home", default=None)
    p_config = subs.add_parser("config", help="Show active HotMem config")
    p_config.add_argument("--hermes-home", default=None)
    subparser.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> None:
    cmd = getattr(args, "hotmem_command", None)
    if cmd == "status":
        _status(args)
    elif cmd == "config":
        _config(args)
    else:
        print("Usage: hermes hotmem <status|config>")
