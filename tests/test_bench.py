import json
import subprocess
from pathlib import Path

import pytest

from ssrename import backends, bench
from ssrename.backends import Backend, BackendError
from ssrename.config import Config


class FakeBackend(Backend):
    def __init__(self, answers: dict[str, str]):
        self.answers = answers

    def describe(self, image: Path) -> str:
        answer = self.answers[image.name]
        if answer.startswith("!"):
            raise BackendError(answer[1:])
        return answer

    def check(self) -> str:
        return "fake"


def test_mentions_whole_words_only():
    assert bench.mentions("datatalk-query-results", ["datatalk"])
    assert not bench.mentions("datalk-query-results", ["datatalk"])
    assert not bench.mentions("datatalks-query", ["datatalk"])
    assert bench.mentions("google-cloud-run-services", ["cloud run"])
    assert not bench.mentions("google-cloud-services", ["cloud run"])
    assert not bench.mentions("anything", ["", "  "])


def test_mentions_ignores_the_stopwords_slugify_drops():
    # slugify("out of state donors") gives "out-state-donors".
    assert bench.mentions("out-state-donors", ["out of state"])
    assert bench.mentions("apple-device-chart", ["Apple on-device"])


def _set(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_bytes(b"png")
    return tmp_path


def test_evaluate_scores_the_filename_not_the_raw_answer(tmp_path):
    set_dir = _set(tmp_path, "a.png")
    case = bench.Case("a.png", app=["datatalk"], topic=["query results"], reviewed=True)
    # Six words: the fifth-word cutoff keeps "query results" but the answer is
    # not well-formed, and the trailing period is punctuation.
    backend = FakeBackend({"a.png": "The DataTalk query results table view."})
    row = bench.evaluate(backend, case, set_dir, max_words=5)
    assert row["name"] == "datatalk-query-results-table-view"
    assert row["app"] and row["topic"] and row["passed"]
    assert not row["well_formed"]
    assert bench.mark(row) == "pass"


def test_evaluate_reports_which_check_failed(tmp_path):
    set_dir = _set(tmp_path, "a.png")
    case = bench.Case("a.png", app=["github"], topic=["issue"])
    row = bench.evaluate(FakeBackend({"a.png": "jira issue 1000"}), case, set_dir, 5)
    assert row["app"] is False and row["topic"] is True and row["passed"] is False
    assert row["well_formed"]
    assert bench.mark(row) == "FAIL app"


def test_evaluate_errors_count_as_misses(tmp_path):
    set_dir = _set(tmp_path, "a.png")
    case = bench.Case("a.png", app=["github"], topic=["issue"])
    row = bench.evaluate(FakeBackend({"a.png": "!context overflow"}), case, set_dir, 5)
    assert row["error"] == "context overflow"
    assert row["app"] is False and row["topic"] is False and row["passed"] is False
    assert bench.mark(row) == "ERROR"


def test_evaluate_missing_image(tmp_path):
    row = bench.evaluate(FakeBackend({}), bench.Case("gone.png", topic=["x"]), tmp_path, 5)
    assert row["error"] == "image missing from set"


def test_app_is_optional(tmp_path):
    set_dir = _set(tmp_path, "a.png")
    case = bench.Case("a.png", topic=["quality"])
    row = bench.evaluate(FakeBackend({"a.png": "quality vs speed chart"}), case, set_dir, 5)
    assert row["app"] is None and row["passed"] is True


def test_unlabeled_cases_are_unscored(tmp_path):
    set_dir = _set(tmp_path, "a.png")
    row = bench.evaluate(FakeBackend({"a.png": "whatever"}), bench.Case("a.png"), set_dir, 5)
    assert row["passed"] is None
    assert bench.mark(row) == "-"


def test_summarize():
    rows = [
        {"passed": True, "app": True, "topic": True, "well_formed": True, "error": None, "secs": 1.0, "reviewed": True},
        {"passed": False, "app": False, "topic": True, "well_formed": False, "error": None, "secs": 3.0, "reviewed": False},
        {"passed": None, "app": None, "topic": None, "well_formed": True, "error": None, "secs": 2.0, "reviewed": False},
        {"passed": False, "app": False, "topic": False, "well_formed": False, "error": "boom", "secs": 5.0, "reviewed": True},
    ]
    s = bench.summarize(rows)
    assert s["pass"] == "1/3"
    assert s["app"] == "1/3"
    assert s["topic"] == "2/3"
    assert s["well_formed"] == "2/4"
    assert s["errors"] == 1
    assert s["unreviewed"] == 1  # the unscored draft does not count
    assert s["median"] == 2.5
    assert s["total"] == 11.0


def test_draft_case_from_a_renamed_file():
    case = bench.draft_case("2026-09-16-08-26-datalk-query-results-table-2.png")
    assert case.app == ["datalk"]
    assert case.topic == ["query", "results", "table"]
    assert not case.reviewed


def test_draft_case_keeps_a_meaningful_trailing_number():
    assert bench.draft_case("2026-09-13-23-14-github-issue-1000.png").topic == ["issue", "1000"]


def test_draft_case_from_a_raw_screenshot():
    case = bench.draft_case("Screenshot 2026-09-18 at 7.26.13 AM.png")
    assert not case.scored
    assert "unlabeled" in case.note


def test_cases_round_trip(tmp_path):
    path = tmp_path / bench.CASES_FILE
    cases = [
        bench.Case("Screenshot 2026-09-18 at 7.26.13 AM.png", note="unlabeled"),
        bench.Case("b.png", app=["github"], topic=["pull request", "diff"], reviewed=True),
    ]
    bench.append_cases(path, cases[:1])
    bench.append_cases(path, cases[1:])
    text = path.read_text()
    assert text.startswith("# Answer key")
    assert text.count("# Answer key") == 1
    assert bench.load_cases(tmp_path) == cases


def test_spread():
    items = list(range(10))
    assert bench.spread(items, 20) == items
    assert bench.spread(items, 4) == [0, 3, 6, 9]
    assert bench.spread(items, 1) == [9]


def test_harvest_samples_and_is_idempotent(tmp_path, monkeypatch):
    shots = tmp_path / "shots"
    shots.mkdir()
    for minute in range(10):
        (shots / f"2026-09-1{minute}-10-00-github-issue-{minute}0.png").write_bytes(b"png")
    (shots / "notes.txt").write_text("not a screenshot")
    monkeypatch.setattr(bench, "shrink", lambda src, dst, px: dst.write_bytes(src.read_bytes()))
    config = tmp_path / "missing.toml"
    out = tmp_path / "set"

    assert bench.main(["--config", str(config), "harvest", "--from", str(shots), "-n", "4", "--out", str(out)]) == 0
    cases = bench.load_cases(out)
    assert [c.image for c in cases] == sorted(p.name for p in out.glob("*.png"))
    assert len(cases) == 4
    assert all(c.app == ["github"] for c in cases)

    # A second harvest adds only what is new and keeps hand edits.
    (out / bench.CASES_FILE).write_text((out / bench.CASES_FILE).read_text().replace("reviewed = false", "reviewed = true", 1))
    bench.main(["--config", str(config), "harvest", "--from", str(shots), "-n", "10", "--out", str(out)])
    cases = bench.load_cases(out)
    assert len(cases) == 10
    assert cases[0].reviewed and not cases[-1].reviewed


def test_schema_backend_joins_the_fields(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"subject": "issue 1000", "app": "github"}', "")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    variant = bench.VARIANTS["fm-schema-ocr"]
    backend = variant.backend(Config(max_image_px=0), tmp_path)
    image = tmp_path / "a.png"
    image.write_bytes(b"png")

    assert backend.describe(image) == "github issue 1000"
    argv = seen[-1]
    assert argv[argv.index("--schema") + 1] == str(tmp_path / "schema.json")
    assert {"--greedy", "--tool", "ocr"} <= set(argv)
    assert json.loads((tmp_path / "schema.json").read_text())["required"] == ["app", "subject"]


def test_schema_backend_rejects_free_text(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backends.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "github issue", ""),
    )
    backend = bench.VARIANTS["fm-schema"].backend(Config(max_image_px=0), tmp_path)
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    with pytest.raises(BackendError, match="ignored --schema"):
        backend.describe(image)


def test_plain_fm_variant_keeps_config_extra_args(tmp_path):
    cfg = Config()
    cfg.fm.extra_args = ["--greedy"]
    assert bench.VARIANTS["fm"].backend(cfg, tmp_path).fm.extra_args == ["--greedy"]
    assert bench.VARIANTS["fm-ocr"].backend(cfg, tmp_path).fm.extra_args == ["--greedy", "--tool", "ocr"]
    assert cfg.fm.extra_args == ["--greedy"]  # variants must not mutate the config


def test_run_and_compare_end_to_end(tmp_path, monkeypatch, capsys):
    set_dir = tmp_path / "set"
    set_dir.mkdir()
    _set(set_dir, "a.png", "b.png")
    bench.append_cases(
        set_dir / bench.CASES_FILE,
        [
            bench.Case("a.png", app=["github"], topic=["issue"], reviewed=True),
            bench.Case("b.png", app=["slack"], topic=["channel"], reviewed=True),
        ],
    )
    fake = FakeBackend({"a.png": "github issue 1000", "b.png": "discord server"})
    monkeypatch.setattr(bench.Variant, "backend", lambda self, cfg, workdir: fake)
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    config = str(tmp_path / "missing.toml")

    assert bench.main(["--config", config, "run", str(set_dir), "-b", "fm", "--out", str(out_a)]) == 0
    report = json.loads(out_a.read_text())
    assert [r["passed"] for r in report["results"]] == [True, False]
    assert "finished" in report
    printed = capsys.readouterr().out
    assert "1/2" in printed and "FAIL app+topic" in printed

    fake.answers["b.png"] = "slack channel messages"
    bench.main(["--config", config, "run", str(set_dir), "-b", "fm", "--out", str(out_b)])
    capsys.readouterr()
    assert bench.main(["compare", str(out_a), str(out_b)]) == 0
    printed = capsys.readouterr().out
    assert "1/2" in printed and "2/2" in printed
    assert "discord-server" in printed and "slack-channel-messages" in printed


def test_run_skips_an_unreachable_backend(tmp_path, monkeypatch, capsys):
    set_dir = _set(tmp_path, "a.png")
    bench.append_cases(set_dir / bench.CASES_FILE, [bench.Case("a.png", topic=["x"])])

    class Down(FakeBackend):
        def check(self):
            raise BackendError("cannot reach http://localhost:1234/v1")

    monkeypatch.setattr(bench.Variant, "backend", lambda self, cfg, workdir: Down({}))
    out = tmp_path / "r.json"
    bench.main(["--config", str(tmp_path / "missing.toml"), "run", str(set_dir), "-b", "openai", "--out", str(out)])
    report = json.loads(out.read_text())
    assert "cannot reach" in report["variants"]["openai"]["skipped"]
    assert report["results"] == []
    assert "skipped" in capsys.readouterr().out


def test_run_finds_a_set_by_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_dir = tmp_path / bench.DEFAULT_SETS_DIR / "studio"
    set_dir.mkdir(parents=True)
    (set_dir / bench.CASES_FILE).write_text("")
    assert bench._resolve_set(Path("studio")) == bench.DEFAULT_SETS_DIR / "studio"
    with pytest.raises(SystemExit, match="harvest"):
        bench._resolve_set(Path("laptop"))


def test_rescore_applies_a_corrected_key_without_rerunning(tmp_path, monkeypatch, capsys):
    set_dir = tmp_path / "set"
    set_dir.mkdir()
    _set(set_dir, "a.png")
    cases_path = set_dir / bench.CASES_FILE
    bench.append_cases(cases_path, [bench.Case("a.png", app=["datalk"], topic=["query"])])
    monkeypatch.setattr(
        bench.Variant, "backend", lambda self, cfg, workdir: FakeBackend({"a.png": "datatalk query results"})
    )
    results = set_dir / bench.RESULTS_DIR / "studio-1.json"
    bench.main(["--config", str(tmp_path / "missing.toml"), "run", str(set_dir), "-b", "fm", "--out", str(results)])
    assert json.loads(results.read_text())["results"][0]["passed"] is False

    # The drafted answer copied a misspelling from the filename; fix and review it.
    cases_path.write_text(cases_path.read_text().replace('"datalk"', '"datatalk"').replace("= false", "= true"))
    assert bench.main(["rescore", str(results)]) == 0
    row = json.loads(results.read_text())["results"][0]
    assert row["passed"] is True and row["reviewed"] is True
    assert "1 verdicts changed" in capsys.readouterr().out


@pytest.mark.parametrize("out", ['"github issue"', '["github", "issue"]', "42"])
def test_schema_backend_rejects_json_that_is_not_an_object(monkeypatch, tmp_path, out):
    monkeypatch.setattr(
        backends.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, "")
    )
    backend = bench.VARIANTS["fm-schema"].backend(Config(max_image_px=0), tmp_path)
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    with pytest.raises(BackendError, match="not an object"):
        backend.describe(image)


def test_p90_stays_within_observed_times():
    row = {"passed": None, "app": None, "topic": None, "well_formed": True, "error": None, "reviewed": True}
    s = bench.summarize([{**row, "secs": 1.0}, {**row, "secs": 3.0}])
    assert 1.0 <= s["p90"] <= 3.0


def test_run_records_the_fm_flags_each_variant_used(tmp_path, monkeypatch):
    set_dir = _set(tmp_path, "a.png")
    bench.append_cases(set_dir / bench.CASES_FILE, [bench.Case("a.png", topic=["x"])])
    monkeypatch.setattr(bench.Variant, "backend", lambda self, cfg, workdir: FakeBackend({"a.png": "x y"}))
    config = tmp_path / "config.toml"
    config.write_text('[backend.fm]\nextra_args = ["--greedy"]\n')
    out = tmp_path / "r.json"
    bench.main(["--config", str(config), "run", str(set_dir), "-b", "fm", "-b", "fm-ocr", "-b", "openai", "--out", str(out)])
    report = json.loads(out.read_text())
    assert report["config"]["fm_extra_args"] == ["--greedy"]
    assert report["variants"]["fm"]["fm_args"] == ["--greedy"]
    assert report["variants"]["fm-ocr"]["fm_args"] == ["--greedy", "--tool", "ocr"]
    assert "fm_args" not in report["variants"]["openai"]
