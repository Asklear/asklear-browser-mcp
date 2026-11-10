"""Tests for the local browser pairing credential."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from asklear_browser_mcp.secure_channel import pairing_key


def test_pairing_key_is_private_and_stable_under_concurrent_creation(tmp_path):
    with ThreadPoolExecutor(max_workers=16) as pool:
        values = list(pool.map(lambda _: pairing_key(root=tmp_path), range(64)))
    assert len(set(values)) == 1
    assert len(values[0]) == 64
    assert pairing_key(root=tmp_path) == values[0]
    if os.name != "nt":
        assert (tmp_path / "extension-pairing-key").stat().st_mode & 0o777 == 0o600


def test_pairing_key_does_not_require_hard_links(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("hard links unavailable")

    monkeypatch.setattr(os, "link", unavailable)
    assert len(pairing_key(root=tmp_path)) == 64


def test_pairing_key_waits_for_concurrent_initialization(tmp_path):
    key_path = tmp_path / "extension-pairing-key"
    key_path.write_text("a" * 32, encoding="ascii")
    key_path.chmod(0o600)

    def complete_write():
        time.sleep(0.05)
        key_path.write_text("a" * 64, encoding="ascii")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(complete_write)
        assert pairing_key(root=tmp_path) == "a" * 64
        future.result()


def test_pairing_key_rejects_invalid_existing_data(tmp_path):
    key_path = tmp_path / "extension-pairing-key"
    key_path.write_text("invalid", encoding="ascii")
    key_path.chmod(0o600)
    with pytest.raises(ValueError, match="credentials are invalid"):
        pairing_key(root=tmp_path)
    assert key_path.read_text(encoding="ascii") == "invalid"


def test_pairing_key_normalizes_the_configured_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ASKLEAR_BROWSER_CONNECTOR_HOME", f"  {tmp_path}  ")
    assert pairing_key() == pairing_key(root=tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and mode checks")
def test_pairing_key_rejects_a_symlink_or_public_file(tmp_path):
    target = tmp_path / "target"
    target.write_text("a" * 64, encoding="ascii")
    key_path = tmp_path / "extension-pairing-key"
    key_path.symlink_to(target)
    with pytest.raises(OSError):
        pairing_key(root=tmp_path)
    key_path.unlink()
    key_path.write_text("a" * 64, encoding="ascii")
    key_path.chmod(0o644)
    with pytest.raises(ValueError):
        pairing_key(root=tmp_path)
