# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""pw-link ENOENT from inside a distrobox: retry on the host (#181, SteamOS).

pw-link in the container prints "failed to link ports: No such file or
directory" (its own registry lacks the ports), while the same argv on the host
links fine. PipeWire tools may exit 0 with the error on stderr, so the tests
use both exit codes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from arctis_sound_manager import container, pw_utils

HOST = '/usr/bin/distrobox-host-exec'
ENOENT = b'failed to link ports: No such file or directory'


def _r(rc=0, err=b''):
    return SimpleNamespace(args=[], returncode=rc, stdout=b'', stderr=err)


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.delenv('PIPEWIRE_CONFIG_NAME', raising=False)
    pw_utils._host_link_works = False
    pw_utils._host_link_logged.clear()
    yield
    pw_utils._host_link_works = False
    pw_utils._host_link_logged.clear()


def _env(monkeypatch, in_container, host=HOST):
    monkeypatch.setattr(container, 'running_in_container', lambda: in_container)
    monkeypatch.setattr(container, 'host_exec',
                        lambda: ([host] if host else None) if in_container else [])


def _runner(monkeypatch, local, remote):
    calls = []

    def run(argv, **_k):
        calls.append(argv)
        return (remote if argv[0] == HOST else local)(argv)

    monkeypatch.setattr(pw_utils, '_pw_run', run)
    return calls


def test_container_enoent_retries_on_host(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    assert pw_utils._pw_link(280, 151).returncode == 0
    assert calls == [['pw-link', '280', '151'], [HOST, 'pw-link', '280', '151']]


def test_enoent_with_exit_zero_is_still_detected(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(0, ENOENT), lambda a: _r(0))
    assert pw_utils._pw_link(1, 2).returncode == 0
    assert calls[-1] == [HOST, 'pw-link', '1', '2']


def test_success_is_cached_so_next_link_goes_to_host_first(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    pw_utils._pw_link(1, 2)
    calls.clear()
    assert pw_utils._pw_link(3, 4).returncode == 0
    assert calls == [[HOST, 'pw-link', '3', '4']]


def test_unlink_uses_same_fallback(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    pw_utils._pw_link(1, 2, delete=True)
    assert calls[-1] == [HOST, 'pw-link', '-d', '1', '2']


def test_non_container_enoent_has_no_fallback(monkeypatch):
    _env(monkeypatch, False)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    assert pw_utils._pw_link(1, 2).returncode == 1
    assert calls == [['pw-link', '1', '2']]


def test_container_without_host_exec_has_no_fallback(monkeypatch):
    _env(monkeypatch, True, host=None)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    assert pw_utils._pw_link(1, 2).returncode == 1
    assert len(calls) == 1


def test_host_failure_returns_failure_without_looping(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT),
                    lambda a: _r(1, b'failed to link ports: Operation not permitted'))
    r = pw_utils._pw_link(1, 2)
    assert r.returncode == 1 and b'not permitted' in r.stderr
    assert len(calls) == 2
    assert pw_utils._host_link_works is False


def test_host_exit_zero_with_enoent_is_failure(monkeypatch):
    _env(monkeypatch, True)
    _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0, ENOENT))
    assert pw_utils._pw_link(1, 2).returncode != 0
    assert pw_utils._host_link_works is False


def test_file_exists_is_success_on_both_paths(monkeypatch):
    _env(monkeypatch, True)
    exists = b'failed to link ports: File exists'
    _runner(monkeypatch, lambda a: _r(1, exists), lambda a: _r(1, exists))
    assert pw_utils._pw_link(1, 2).returncode == 0
    pw_utils._host_link_works = True
    assert pw_utils._pw_link(1, 2).returncode == 0


def test_other_local_errors_do_not_trigger_fallback(monkeypatch):
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, b'Operation not permitted'),
                    lambda a: _r(0))
    assert pw_utils._pw_link(1, 2).returncode == 1
    assert len(calls) == 1


def test_warning_logged_once_per_pair(monkeypatch, caplog):
    _env(monkeypatch, True)
    _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    with caplog.at_level('DEBUG', logger='pw_utils'):
        pw_utils._pw_link(1, 2)
        pw_utils._host_link_works = False
        pw_utils._pw_link(1, 2)
    warns = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warns) == 1 and '#181' in warns[0].getMessage()


def test_host_retry_carries_the_wireplumber_exemption(monkeypatch):
    # distrobox-host-exec drops the environment; the #325 client config must
    # reach the host pw-link anyway, or it is hidden the Sonar nodes there too.
    monkeypatch.setenv('PIPEWIRE_CONFIG_NAME', '/home/deck/client-asm.conf')
    _env(monkeypatch, True)
    calls = _runner(monkeypatch, lambda a: _r(1, ENOENT), lambda a: _r(0))
    assert pw_utils._pw_link(280, 151).returncode == 0
    assert calls[1] == [HOST, 'env', 'PIPEWIRE_CONFIG_NAME=/home/deck/client-asm.conf',
                        'pw-link', '280', '151']
