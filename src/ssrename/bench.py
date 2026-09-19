"""Benchmark screenshot-naming backends against screenshots with known answers.

    ssrename-bench harvest        sample this machine's screenshots into a set
    ssrename-bench run SET        run backend variants over a set and score them
    ssrename-bench compare A B    put two result files side by side
    ssrename-bench rescore FILE   re-judge saved results after fixing cases.toml
    ssrename-bench variants       list the backend variants

A set is a directory of shrunk screenshots plus a cases.toml answer key. Sets
live under bench/sets/, which git ignores: screenshots are private. Copy a set
between machines with rsync to compare them on identical images.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .backends import Backend, BackendError, FmBackend, OpenAIBackend
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .naming import _STOPWORDS, datetime_for, slugify

DEFAULT_SETS_DIR = Path("bench/sets")
CASES_FILE = "cases.toml"
RESULTS_DIR = "results"
RESULTS_VERSION = 1

#: What the prompt asks for: 2-5 lowercase words, no punctuation.
_WELL_FORMED = re.compile(r"[a-z0-9]+(?: [a-z0-9]+){1,4}")

#: ssrename's own output, yyyy-mm-dd-HH-mm-description, maybe with a -N
#: collision suffix. Only one digit, so "github-issue-1000" keeps its number.
_RENAMED = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-(?P<desc>.+?)(?:-[2-9])?$")

CASES_HEADER = """\
# Answer key for this benchmark set. Each [[case]] is one image in this directory.
#
#   app       phrases naming the app or site; the generated name must contain
#             one. Leave empty when no app is identifiable.
#   topic     phrases for what is on screen; the name must contain at least one.
#   reviewed  false for drafts guessed from the filename. Look at the image, fix
#             app and topic, then set it to true. Scores on drafts are provisional.
#
# Phrases match whole words of the final filename, ignoring case, punctuation,
# and the stopwords ssrename drops (a, the, of, on, ...).
"""


# --- answer key -------------------------------------------------------------


@dataclass
class Case:
    image: str
    app: list[str] = field(default_factory=list)
    topic: list[str] = field(default_factory=list)
    reviewed: bool = False
    note: str = ""

    @property
    def scored(self) -> bool:
        return bool(self.app or self.topic)


def load_cases(set_dir: Path) -> list[Case]:
    path = set_dir / CASES_FILE
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return [
        Case(
            image=c["image"],
            app=[str(p) for p in c.get("app", [])],
            topic=[str(p) for p in c.get("topic", [])],
            reviewed=bool(c.get("reviewed", False)),
            note=str(c.get("note", "")),
        )
        for c in data.get("case", [])
    ]


def format_case(case: Case) -> str:
    # JSON strings and string arrays are valid TOML, escapes included.
    lines = [
        "[[case]]",
        f"image = {json.dumps(case.image)}",
        f"app = {json.dumps(case.app)}",
        f"topic = {json.dumps(case.topic)}",
        f"reviewed = {'true' if case.reviewed else 'false'}",
    ]
    if case.note:
        lines.append(f"note = {json.dumps(case.note)}")
    return "\n".join(lines) + "\n"


def append_cases(path: Path, cases: list[Case]) -> None:
    """Add cases to the end of the file, leaving hand edits above them alone."""
    text = "" if path.exists() else CASES_HEADER
    for case in cases:
        text += "\n" + format_case(case)
    with path.open("a") as fh:
        fh.write(text)


def draft_case(image: str) -> Case:
    """A starting point for the answer key, guessed from the filename.

    Only files ssrename already renamed carry a description, and that came from
    whichever model named them, misspellings included, so it is a draft.
    """
    m = _RENAMED.match(Path(image).stem)
    if not m:
        return Case(image, note="unlabeled: fill in app and topic")
    words = m["desc"].split("-")
    return Case(image, app=words[:1], topic=words[1:], note="draft from filename")


# --- scoring ----------------------------------------------------------------


def _padded(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return " " + " ".join(w for w in words if w not in _STOPWORDS) + " "


def mentions(name: str, phrases: list[str]) -> bool:
    """Whether any phrase appears in `name` as whole words."""
    haystack = _padded(name)
    return any(_padded(p) in haystack for p in phrases if p.strip())


def evaluate(backend: Backend, case: Case, set_dir: Path, max_words: int) -> dict:
    """Describe one image and score the filename ssrename would give it."""
    image = set_dir / case.image
    description, error = "", None
    start = time.monotonic()
    if not image.exists():
        error = "image missing from set"
    else:
        try:
            description = backend.describe(image)
        except BackendError as e:
            error = str(e)
    secs = time.monotonic() - start

    name = slugify(description, max_words) if description else ""
    return {
        "image": case.image,
        "secs": round(secs, 2),
        "description": description,
        "name": name,
        "error": error,
        "well_formed": bool(_WELL_FORMED.fullmatch(description.strip())),
        **judge(case, name, error),
    }


def judge(case: Case, name: str, error: str | None) -> dict:
    """The answer-key checks for one generated filename. An error misses them all."""
    if error:
        app = False if case.app else None
        topic = False if case.topic else None
    else:
        app = mentions(name, case.app) if case.app else None
        topic = mentions(name, case.topic) if case.topic else None
    checks = [c for c in (app, topic) if c is not None]
    return {
        "app": app,
        "topic": topic,
        "passed": all(checks) if checks else None,
        "reviewed": case.reviewed,
    }


def rescore(report: dict, cases: list[Case]) -> int:
    """Re-judge saved results against a corrected answer key; returns rows changed."""
    by_image = {c.image: c for c in cases}
    changed = 0
    for row in report["results"]:
        case = by_image.get(row["image"])
        if case is None:
            continue
        verdict = judge(case, row["name"], row["error"])
        changed += any(row.get(k) != v for k, v in verdict.items())
        row.update(verdict)
    return changed


def mark(row: dict) -> str:
    if row["error"]:
        return "ERROR"
    if row["passed"] is None:
        return "-"
    if row["passed"]:
        return "pass"
    missed = [k for k in ("app", "topic") if row[k] is False]
    return "FAIL " + "+".join(missed)


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["passed"] is not None]
    secs = sorted(r["secs"] for r in rows)

    def frac(key: str) -> str:
        judged = [r for r in rows if r[key] is not None]
        return f"{sum(1 for r in judged if r[key])}/{len(judged)}"

    return {
        "images": len(rows),
        "pass": frac("passed"),
        "app": frac("app"),
        "topic": frac("topic"),
        "well_formed": f"{sum(1 for r in rows if r['well_formed'])}/{len(rows)}",
        "errors": sum(1 for r in rows if r["error"]),
        "unreviewed": sum(1 for r in scored if not r["reviewed"]),
        "median": statistics.median(secs) if secs else 0.0,
        # Inclusive, so a small set's p90 stays within the times actually seen.
        "p90": (
            statistics.quantiles(secs, n=10, method="inclusive")[-1]
            if len(secs) > 1
            else (secs[0] if secs else 0.0)
        ),
        "total": sum(secs),
    }


# --- variants ---------------------------------------------------------------

_SCHEMA = {
    "title": "ScreenshotName",
    "type": "object",
    "additionalProperties": False,
    "required": ["app", "subject"],
    "x-order": ["app", "subject"],
    "properties": {
        "app": {
            "type": "string",
            "description": "Name of the app, website, or product in the screenshot, "
            "read from its logo, title bar, or tab; lowercase",
        },
        "subject": {
            "type": "string",
            "description": "What specifically is on screen, 2-4 lowercase words, no punctuation",
        },
    },
}


class SchemaFmBackend(FmBackend):
    """fm with guided generation: it must fill in {app, subject}, which get joined."""

    def __init__(self, cfg: Config, workdir: Path):
        schema = workdir / "schema.json"
        schema.write_text(json.dumps(_SCHEMA))
        fm = dataclasses.replace(
            cfg.fm, extra_args=[*cfg.fm.extra_args, "--schema", str(schema)]
        )
        super().__init__(dataclasses.replace(cfg, fm=fm))

    def _run(self, args: list[str], timeout: float) -> str:
        out = super()._run(args, timeout)
        if not args or args[0] != "respond":
            return out
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise BackendError(f"fm ignored --schema and returned: {out[:200]!r}") from e
        if not isinstance(data, dict):
            raise BackendError(f"fm returned JSON that is not an object: {out[:200]!r}")
        return f"{data.get('app', '')} {data.get('subject', '')}"


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str  # "openai" or "fm"
    about: str
    #: Replaces [backend.fm] extra_args; None keeps the config's.
    fm_args: tuple[str, ...] | None = None
    schema: bool = False

    def backend(self, cfg: Config, workdir: Path) -> Backend:
        if self.kind == "openai":
            return OpenAIBackend(cfg)
        if self.fm_args is not None:
            cfg = dataclasses.replace(
                cfg, fm=dataclasses.replace(cfg.fm, extra_args=list(self.fm_args))
            )
        return SchemaFmBackend(cfg, workdir) if self.schema else FmBackend(cfg)


_OCR = ("--greedy", "--tool", "ocr")

VARIANTS = {
    v.name: v
    for v in (
        Variant("openai", "openai", "the [backend.openai] server in your config (LM Studio, ...)"),
        Variant("fm", "fm", "fm respond with your config's [backend.fm] settings"),
        Variant("fm-greedy", "fm", "fm with --greedy", ("--greedy",)),
        Variant("fm-ocr", "fm", "fm with --greedy --tool ocr", _OCR),
        Variant("fm-schema", "fm", "fm with --greedy and an {app, subject} schema", ("--greedy",), True),
        Variant("fm-schema-ocr", "fm", "fm-schema plus --tool ocr", _OCR, True),
    )
}


# --- machine ----------------------------------------------------------------


def _sh(*argv: str) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def machine_name() -> str:
    return _sh("scutil", "--get", "LocalHostName") or socket.gethostname().split(".")[0]


def machine_info() -> dict:
    mem = _sh("sysctl", "-n", "hw.memsize")
    repo = Path(__file__).resolve().parents[2]
    commit = _sh("git", "-C", str(repo), "describe", "--always", "--dirty") if (repo / ".git").exists() else ""
    return {
        "name": machine_name(),
        "chip": _sh("sysctl", "-n", "machdep.cpu.brand_string") or platform.machine(),
        "memory_gb": round(int(mem) / 2**30) if mem.isdigit() else None,
        "macos": f"{_sh('sw_vers', '-productVersion')} ({_sh('sw_vers', '-buildVersion')})",
        "ssrename": commit or __version__,
    }


def describe_machine(info: dict) -> str:
    mem = f", {info['memory_gb']} GB" if info.get("memory_gb") else ""
    return f"{info['name']}: {info['chip']}{mem}, macOS {info['macos']}, ssrename {info['ssrename']}"


# --- commands ---------------------------------------------------------------


def spread(items: list, count: int) -> list:
    """`count` items evenly spaced across the list, first and last included."""
    if count <= 0 or len(items) <= count:
        return list(items)
    if count == 1:
        return [items[-1]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def shrink(src: Path, dst: Path, max_px: int) -> None:
    """Copy `src` to `dst` at the size the backends would send it."""
    if max_px > 0 and shutil.which("sips"):
        result = subprocess.run(
            ["sips", "-Z", str(max_px), str(src), "--out", str(dst)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and dst.exists():
            return
    shutil.copy2(src, dst)


def cmd_harvest(args) -> int:
    cfg = load_config(args.config)
    source = (args.source or cfg.watch_dir).expanduser()
    set_dir = args.out or DEFAULT_SETS_DIR / machine_name()
    if not source.is_dir():
        print(f"no such directory: {source}", file=sys.stderr)
        return 1
    files = sorted(
        (
            p
            for p in source.iterdir()
            if p.is_file() and p.suffix.lower().lstrip(".") in cfg.extensions
        ),
        key=datetime_for,
    )
    if not files:
        print(f"no screenshots in {source}", file=sys.stderr)
        return 1

    set_dir.mkdir(parents=True, exist_ok=True)
    cases_path = set_dir / CASES_FILE
    known = {c.image for c in load_cases(set_dir)} if cases_path.exists() else set()
    new = []
    for src in spread(files, args.count):
        if src.name in known:
            continue
        shrink(src, set_dir / src.name, cfg.max_image_px)
        new.append(draft_case(src.name))
    append_cases(cases_path, new)

    unlabeled = sum(1 for c in new if not c.scored)
    print(f"added {len(new)} of {len(files)} screenshots in {source} to {set_dir}")
    if new:
        print(f"{len(new) - unlabeled} answers drafted from filenames, {unlabeled} unlabeled.")
        print(f"Review them in {cases_path} (open the images alongside) and set")
        print("reviewed = true; until then, scores are provisional.")
    return 0


def _resolve_set(arg: Path) -> Path:
    for candidate in (arg, DEFAULT_SETS_DIR / arg):
        if (candidate / CASES_FILE).exists():
            return candidate
    raise SystemExit(
        f"no {CASES_FILE} in {arg} or {DEFAULT_SETS_DIR / arg}; "
        "run `ssrename-bench harvest` or copy a set from another machine"
    )


def _save(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(path)


def _row_line(row: dict) -> str:
    what = row["name"] if not row["error"] else row["error"].splitlines()[0][:70]
    return f"  {mark(row):<17} {row['secs']:6.1f}s  {what:<44}  {row['image']}"


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    set_dir = _resolve_set(args.set)
    cases = load_cases(set_dir)
    names = args.variant or list(VARIANTS)
    stamp = datetime.now()
    out = args.out or set_dir / RESULTS_DIR / f"{machine_name()}-{stamp:%Y%m%d-%H%M%S}.json"
    report = {
        "version": RESULTS_VERSION,
        "set": set_dir.name,
        "started": stamp.isoformat(timespec="seconds"),
        "machine": machine_info(),
        "config": {
            "source": str(cfg.source),
            "max_words": cfg.max_words,
            "max_image_px": cfg.max_image_px,
            "openai_model": cfg.openai.model,
            "openai_base_url": cfg.openai.base_url,
            "fm_model": cfg.fm.model,
            "fm_extra_args": list(cfg.fm.extra_args),
            "instructions": cfg.instructions,
            "prompt": cfg.prompt,
        },
        "variants": {},
        "results": [],
    }
    print(describe_machine(report["machine"]))
    print(f"{len(cases)} images in {set_dir}, variants: {', '.join(names)}")

    try:
        with tempfile.TemporaryDirectory(prefix="ssrename-bench-") as tmp:
            for name in names:
                variant = VARIANTS[name]
                backend = variant.backend(cfg, Path(tmp))
                print()
                try:
                    status = backend.check()
                except BackendError as e:
                    print(f"{name}: skipped - {e}")
                    report["variants"][name] = {"about": variant.about, "skipped": str(e)}
                    continue
                print(f"{name}: {status}")
                report["variants"][name] = {"about": variant.about, "status": status}
                if variant.kind == "fm":
                    # The plain fm variant inherits the config's flags, so the
                    # name alone does not say what ran.
                    args_used = cfg.fm.extra_args if variant.fm_args is None else variant.fm_args
                    report["variants"][name].update(fm_args=list(args_used), schema=variant.schema)
                for case in cases:
                    row = {"variant": name, **evaluate(backend, case, set_dir, cfg.max_words)}
                    report["results"].append(row)
                    print(_row_line(row), flush=True)
                    _save(out, report)
    except KeyboardInterrupt:
        report["interrupted"] = True
        print("\ninterrupted; keeping what finished")
    report["finished"] = datetime.now().isoformat(timespec="seconds")
    _save(out, report)

    print()
    print_summary([report])
    print(f"\nresults: {out}")
    return 0


_SUMMARY_COLUMNS = (
    ("pass", "pass", 7),
    ("app", "app", 7),
    ("topic", "topic", 7),
    ("well_formed", "2-5 words", 9),
    ("errors", "errors", 6),
)


def print_summary(reports: list[dict], only: list[str] | None = None) -> None:
    labels = "AB" if len(reports) > 1 else " "
    head = f"{'variant':<15}" + (" " if len(reports) > 1 else "")
    head += "".join(f"{title:>{w + 1}}" for _, title, w in _SUMMARY_COLUMNS)
    head += f"{'median':>8}{'p90':>8}{'total':>8}"
    print(head)
    order = []
    for report in reports:
        order += [v for v in report["variants"] if v not in order]
    notes = []
    for name in order:
        if only and name not in only:
            continue
        for label, report in zip(labels, reports):
            prefix = f"{name:<15}" + (label if len(reports) > 1 else "")
            info = report["variants"].get(name)
            if info is None:
                print(f"{prefix}  (not run)")
                continue
            if info.get("skipped"):
                print(f"{prefix}  skipped: {info['skipped'][:60]}")
                continue
            rows = [r for r in report["results"] if r["variant"] == name]
            s = summarize(rows)
            line = prefix + "".join(f"{s[key]!s:>{w + 1}}" for key, _, w in _SUMMARY_COLUMNS)
            line += f"{s['median']:7.1f}s{s['p90']:7.1f}s{s['total']:7.0f}s"
            print(line)
            if s["unreviewed"]:
                notes.append(f"{name} ({label.strip() or 'this run'}): {s['unreviewed']} answers are unreviewed drafts")
    if len(reports) > 1:
        for label, report in zip(labels, reports):
            print(f"  {label} = {describe_machine(report['machine'])}, {report['started']}")
    for note in dict.fromkeys(notes):
        print(f"  note: {note}")


def cmd_compare(args) -> int:
    reports = [json.loads(Path(p).read_text()) for p in (args.a, args.b)]
    if reports[0]["set"] != reports[1]["set"]:
        print(f"warning: different sets ({reports[0]['set']} vs {reports[1]['set']})")
    print_summary(reports, args.variant)
    for name in reports[0]["variants"]:
        if args.variant and name not in args.variant:
            continue
        a_rows = {r["image"]: r for r in reports[0]["results"] if r["variant"] == name}
        b_rows = {r["image"]: r for r in reports[1]["results"] if r["variant"] == name}
        images = [i for i in a_rows if i in b_rows]
        if not images:
            continue
        print(f"\n{name}")
        for image in images:
            a, b = a_rows[image], b_rows[image]
            print(f"  {image}")
            for label, row in (("A", a), ("B", b)):
                what = row["name"] if not row["error"] else row["error"].splitlines()[0][:70]
                print(f"    {label} {mark(row):<17} {row['secs']:6.1f}s  {what}")
    return 0


def cmd_rescore(args) -> int:
    for path in args.results:
        report = json.loads(path.read_text())
        # Results live in SET/results/, next to the answer key they were scored on.
        set_dir = args.set or path.resolve().parent.parent
        changed = rescore(report, load_cases(set_dir))
        _save(path, report)
        print(f"{path}: {changed} verdicts changed")
        print_summary([report])
    return 0


def cmd_variants(args) -> int:
    for v in VARIANTS.values():
        print(f"{v.name:<15} {v.about}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssrename-bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", type=Path, help=f"ssrename config (default {DEFAULT_CONFIG_PATH})")
    sub = p.add_subparsers(dest="command", required=True)

    harvest = sub.add_parser("harvest", help="sample this machine's screenshots into a set")
    harvest.add_argument(
        "--from", dest="source", type=Path, help="screenshot directory (default: watch_dir)"
    )
    harvest.add_argument("-n", "--count", type=int, default=20, help="images to take (default 20)")
    harvest.add_argument(
        "--out", type=Path, help=f"set directory (default {DEFAULT_SETS_DIR}/<this machine>)"
    )
    harvest.set_defaults(func=cmd_harvest)

    run = sub.add_parser("run", help="run backend variants over a set and score them")
    run.add_argument("set", type=Path, help=f"set directory, or a set name under {DEFAULT_SETS_DIR}")
    run.add_argument(
        "-b",
        "--variant",
        action="append",
        choices=list(VARIANTS),
        help="variant to run, repeatable (default: all; see `variants`)",
    )
    run.add_argument("--out", type=Path, help="results file (default SET/results/<machine>-<time>.json)")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="put two result files side by side")
    compare.add_argument("a", type=Path)
    compare.add_argument("b", type=Path)
    compare.add_argument("-b", "--variant", action="append", help="limit to these variants")
    compare.set_defaults(func=cmd_compare)

    rescore = sub.add_parser(
        "rescore", help="re-judge saved results after correcting cases.toml, without re-running"
    )
    rescore.add_argument("results", nargs="+", type=Path)
    rescore.add_argument("--set", type=Path, help="set directory (default: the results file's set)")
    rescore.set_defaults(func=cmd_rescore)

    sub.add_parser("variants", help="list the backend variants").set_defaults(func=cmd_variants)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
