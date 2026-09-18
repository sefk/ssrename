import subprocess

from ssrename import backends
from ssrename.backends import FmBackend
from ssrename.config import Config, FmBackendConfig


def test_fm_passes_extra_args(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "github pull request diff\n", "")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    cfg = Config(max_image_px=0, fm=FmBackendConfig(extra_args=["--greedy", "--tool", "ocr"]))
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    assert FmBackend(cfg).describe(image) == "github pull request diff"
    argv = seen[-1]
    assert argv[:2] == ["fm", "respond"]
    assert argv[-3:] == ["--greedy", "--tool", "ocr"]
    assert "--model" not in argv
