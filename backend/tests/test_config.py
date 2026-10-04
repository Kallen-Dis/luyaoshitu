""".env 读取的容错：BOM、引号、行尾注释都是常见写法，不能让后端读错或启动崩溃。"""

import os

from app.config import _load_env_file


def test_env_file_tolerates_bom_quotes_and_inline_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(
        "\ufeffLYS_TEST_AK=abc123\n"
        'LYS_TEST_QUOTED="with space"\n'
        "LYS_TEST_SINGLE='x'\n"
        "LYS_TEST_QPS=8               # 实测地理编码 12 并发安全\n"
        "# LYS_TEST_COMMENTED=1\n".encode()
    )
    for key in ("LYS_TEST_AK", "LYS_TEST_QUOTED", "LYS_TEST_SINGLE", "LYS_TEST_QPS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LYS_TEST_COMMENTED", raising=False)

    _load_env_file(env)

    assert os.environ["LYS_TEST_AK"] == "abc123"  # BOM 不会粘到键名上
    assert os.environ["LYS_TEST_QUOTED"] == "with space"
    assert os.environ["LYS_TEST_SINGLE"] == "x"
    assert int(os.environ["LYS_TEST_QPS"]) == 8  # README 示例带行尾注释也能解析
    assert "LYS_TEST_COMMENTED" not in os.environ


def test_existing_environment_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LYS_TEST_OVERRIDE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("LYS_TEST_OVERRIDE", "from_env")
    _load_env_file(env)
    assert os.environ["LYS_TEST_OVERRIDE"] == "from_env"
