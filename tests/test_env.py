"""Tests for .env auto-loading (websync.env)."""

from __future__ import annotations

import os

from websync.env import load_env


def test_load_env_reads_dotenv_from_cwd(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEBSYNC_TEST_DOTENV_VALUE=from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBSYNC_TEST_DOTENV_VALUE", raising=False)

    loaded = load_env()
    try:
        assert loaded == env_file
        assert os.environ["WEBSYNC_TEST_DOTENV_VALUE"] == "from_dotenv"
    finally:
        monkeypatch.delenv("WEBSYNC_TEST_DOTENV_VALUE", raising=False)


def test_load_env_walks_up_to_find_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("WEBSYNC_TEST_DOTENV_VALUE=parent\n")
    child = tmp_path / "sub"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("WEBSYNC_TEST_DOTENV_VALUE", raising=False)

    loaded = load_env()
    try:
        assert loaded == tmp_path / ".env"
        assert os.environ["WEBSYNC_TEST_DOTENV_VALUE"] == "parent"
    finally:
        monkeypatch.delenv("WEBSYNC_TEST_DOTENV_VALUE", raising=False)


def test_load_env_never_overrides_real_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("WEBSYNC_TEST_DOTENV_VALUE=from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBSYNC_TEST_DOTENV_VALUE", "from_environment")

    loaded = load_env()
    try:
        assert loaded == tmp_path / ".env"
        assert os.environ["WEBSYNC_TEST_DOTENV_VALUE"] == "from_environment"
    finally:
        monkeypatch.delenv("WEBSYNC_TEST_DOTENV_VALUE", raising=False)


def test_load_env_returns_none_without_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_env() is None
