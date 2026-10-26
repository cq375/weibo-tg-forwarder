from pathlib import Path

from weibo_tg.config import load_config


def test_loads_default_targets_and_environment(monkeypatch):
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")

    config = load_config(Path(__file__).parents[1] / "config.example.yml")

    assert config.weibo.uids == ("1000000001", "1000000002")
    assert config.weibo.cookie == "SUB=test"
    assert config.telegram.channel_id == "-100123"
    assert config.storage.database.is_absolute()
    assert config.storage.cookie_file is not None
    assert config.service.cookie_refresh_enabled is True
    assert config.telegram.notifications_use_channel is True


def test_persisted_cookie_has_priority_over_environment(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "weibo_cookie.txt").write_text("SUB=persisted", encoding="utf-8")
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
weibo:
  targets:
    - uid: "1000000001"
telegram: {}
storage:
  cookie_file: "./data/weibo_cookie.txt"
service: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=environment")

    config = load_config(config_file)

    assert config.weibo.cookie == "SUB=persisted"


def test_douyin_section_defaults_and_paths(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
douyin:
  targets:
    - uid: "abc"
      sec_uid: "MS4wLjABAAAAtarget"
telegram: {}
storage: {}
service: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOUYIN_COOKIE", "ttwid=backup")

    config = load_config(config_file)

    assert config.weibo is None
    assert config.douyin is not None
    assert config.douyin.targets == (("abc", "MS4wLjABAAAAtarget"),)
    assert config.douyin.cookie == "ttwid=backup"
    assert config.douyin.poll_interval_seconds == 120
    assert config.storage.douyin_database == (
        tmp_path / "data/douyin-state.sqlite3"
    ).resolve()


def test_douyin_cookie_file_has_priority(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "douyin_cookie.txt").write_text("ttwid=persisted", encoding="utf-8")
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
douyin:
  targets:
    - uid: "abc"
      sec_uid: "MS4wLjABAAAAtarget"
telegram: {}
storage: {}
service: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOUYIN_COOKIE", "ttwid=environment")

    config = load_config(config_file)

    assert config.douyin is not None
    assert config.douyin.cookie == "ttwid=persisted"


def test_weibo_command_rejects_douyin_only_config(tmp_path):
    from weibo_tg.cli import _require_weibo
    from weibo_tg.config import ConfigError
    import pytest

    config_file = tmp_path / "config.yml"
    config_file.write_text(
        """
douyin:
  targets:
    - uid: "abc"
      sec_uid: "MS4wLjABAAAAtarget"
telegram: {}
storage: {}
service: {}
""",
        encoding="utf-8",
    )
    config = load_config(config_file)

    with pytest.raises(ConfigError, match="weibo.targets"):
        _require_weibo(config)
