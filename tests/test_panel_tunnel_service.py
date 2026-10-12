"""Validate the declarative, local-only SSH service without starting it."""

import configparser
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parents[1] / "deploy/yam-ipc-panel-tunnel.service"


def sections():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(UNIT.read_text().replace("\\\n", " "))
    return parser


def test_only_loopback_forward_and_no_remote_command():
    command = shlex.split(sections()["Service"]["ExecStart"])
    assert command[:4] == ["/usr/bin/ssh", "-N", "-T", "-n"]
    assert command[-1] == "linux@192.168.250.2"
    assert command[command.index("-L") + 1] == "127.0.0.1:8876:127.0.0.1:8766"
    assert command.count("-L") == 1
    assert "-R" not in command and "-D" not in command and "-g" not in command


def test_both_hops_are_noninteractive_strict_and_do_not_need_agent():
    command = shlex.split(sections()["Service"]["ExecStart"])
    options = [command[i + 1] for i, item in enumerate(command) if item == "-o"]
    required = {
        "BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes",
        "IdentityAgent=none", "ForwardAgent=no", "ConnectTimeout=6", "ConnectionAttempts=1",
        "ServerAliveInterval=10", "ServerAliveCountMax=3", "ControlMaster=no", "ControlPath=none",
    }
    assert required <= set(options)
    assert "ExitOnForwardFailure=yes" in options
    assert command[command.index("-i") + 1] == "%h/.ssh/id_ed25519_condapi_yam"
    proxy = next(value.removeprefix("ProxyCommand=") for value in options if value.startswith("ProxyCommand="))
    proxy_command = shlex.split(proxy)
    proxy_options = {proxy_command[i + 1] for i, item in enumerate(proxy_command) if item == "-o"}
    assert required <= proxy_options
    # systemd consumes %% -> %; SSH expands the destination, not systemd's home/port specifiers.
    assert proxy_command[-3:] == ["-W", "%%h:%%p", "thor"]


def test_restart_and_lifecycle_are_independent_of_robot_services():
    unit = sections()
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert unit["Service"]["Restart"] == "always"
    assert unit["Service"]["RestartSec"] == "5"
    assert unit["Service"]["KillMode"] == "control-group"
    assert unit["Install"]["WantedBy"] == "default.target"
    for key in ("Requires", "Wants", "PartOf", "BindsTo", "OnFailure"):
        assert key not in unit["Unit"]
    for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"):
        assert key not in unit["Service"]


def test_systemd_parses_unit():
    executable = shutil.which("systemd-analyze")
    if executable is None:
        pytest.skip("systemd unit verifier unavailable")
    subprocess.run([executable, "--user", "verify", str(UNIT)], check=True, capture_output=True, text=True, timeout=10)
