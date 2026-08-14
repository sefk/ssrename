from pathlib import Path

from ssrename.config import DEFAULT_CONFIG_TEXT, load_config, write_default_config


def test_defaults_when_file_is_absent(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.backend_kind == "openai"
    assert cfg.watch_dir == Path("~/Desktop/screenshots").expanduser()


def test_shipped_default_config_parses(tmp_path):
    path = write_default_config(tmp_path / "config.toml")
    assert path.read_text() == DEFAULT_CONFIG_TEXT
    cfg = load_config(path)
    assert cfg.backend_kind == "openai"
    assert cfg.max_words == 5
    assert cfg.max_image_px == 1600
    assert cfg.openai.extra_body == {"reasoning_effort": "none"}
    assert "2-5 words" in cfg.instructions


def test_write_default_config_does_not_clobber(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# mine\n")
    write_default_config(path)
    assert path.read_text() == "# mine\n"
    write_default_config(path, force=True)
    assert path.read_text() == DEFAULT_CONFIG_TEXT


def test_partial_config_keeps_other_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[general]\nwatch_dir = "~/Pictures/shots"\n\n'
        '[backend]\nkind = "fm"\n\n[backend.fm]\nmodel = "pcc"\n'
    )
    cfg = load_config(path)
    assert cfg.watch_dir == Path("~/Pictures/shots").expanduser()
    assert cfg.backend_kind == "fm"
    assert cfg.fm.model == "pcc"
    assert cfg.max_words == 5
    assert cfg.openai.base_url == "http://localhost:1234/v1"
