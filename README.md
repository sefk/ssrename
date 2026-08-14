# ssrename

Renames macOS screenshots from what's in them.

```
Screenshot 2026-07-31 at 6.59.43 AM.png  ->  2026-07-31-06-59-datatalk-campaign-finance-query.png
```

A background agent watches your screenshot folder, sends each new capture to a
vision model, and renames the file `yyyy-mm-dd-HH-mm-short-description.ext`, on
the 24-hour clock. No dock icon, no menu bar item — one config file and a
LaunchAgent.

Two backends:

- **`openai`** — any OpenAI-compatible server with a vision model. Verified
  against [LM Studio][lmstudio]; also works with [Ollama][ollama]'s `/v1`
  endpoint and vLLM. This is the one to use today.
- **`fm`** — Apple's [`fm` CLI][fm], which ships preinstalled with macOS 27 and
  can take `--image`. On macOS 26 the `fm` binary does not exist and the
  on-device Foundation Model has no image input, so use the `openai` backend
  until you upgrade.

## The thumbnail chip is not disturbed

macOS shows a new screenshot as a chip in the lower right for a few seconds, and
only writes the file to disk once that chip expires or is dismissed. ssrename
never touches a file before it exists, then waits `debounce_seconds` (default 8)
more, then waits for the file size to stop changing, and re-checks that the file
is still there immediately before renaming. Drag it out of the chip, mark it up,
or delete it — ssrename either never sees it or quietly skips it.

Only files still carrying macOS's own name (`Screenshot ...`, `Screen Shot ...`)
are considered, so renaming is idempotent and your own filenames are safe.

## Install

```sh
git clone <this repo> ~/src/ssrename
cd ~/src/ssrename
uv tool install .            # or: uv sync, then `uv run ssrename ...`

ssrename init                # writes ~/.config/ssrename/config.toml
ssrename set-screenshot-dir  # points macOS at config's watch_dir
ssrename doctor              # checks config, backend, macOS settings
ssrename install             # LaunchAgent, starts now and at login
```

Check it works before installing the agent:

```sh
ssrename --dry-run backfill --limit 3
```

### Where macOS saves screenshots

**The preference key depends on the macOS version.** macOS 27 split the single
`location` key into per-capture-type keys, matching the `target-screenshot` /
`target-screenrecording` pair it already had. Write both, so the setting works
either way:

```sh
defaults write com.apple.screencapture location-screenshot ~/Desktop/Screenshots  # macOS 27+
defaults write com.apple.screencapture location ~/Desktop/Screenshots             # older
```

Setting only the legacy `location` key on macOS 27 fails **silently** —
`/usr/sbin/screencapture` finds no `location-screenshot`, falls back to its
built-in `~/Desktop` default, and reports no error. `defaults read` still shows
the `location` you wrote, which makes it look like the setting took.

**No restart is needed.** macOS spawns a fresh `screencapture` for each
⇧⌘3/4/5 press and it reads the preference at launch, so the next screenshot
already goes to the new folder. (`killall SystemUIServer`, the advice you'll
find elsewhere, does nothing — that process has not been involved for years.)

`ssrename set-screenshot-dir` writes both keys and creates the directory for
you.

Capitalisation of the path doesn't matter here — ssrename resolves `watch_dir` to
the directory's real on-disk name — but the preference and `watch_dir` must point
at the same directory. `ssrename doctor` says so if they don't.

### Full Disk Access

If the watch directory is under `~/Desktop`, `~/Documents`, or `~/Downloads`,
macOS gates it behind TCC, and a LaunchAgent cannot show a permission prompt —
so it fails silently. Symptom: nothing is ever renamed and
`~/Library/Logs/ssrename.log` shows no activity.

`ssrename doctor` reports whether reading the directory actually works and, when
the directory is protected, prints the exact binary to add:

```
read access:       ok - listed 198 entries, read Screenshot 2026-03-23 at 8.04.42 AM.png
...
    /Users/you/.local/share/uv/python/cpython-3.14.4-macos-aarch64-none/bin/python3.14
```

That is the *interpreter*, not `~/.local/bin/ssrename`: a console script is a
text file with a shebang, and TCC grants apply to the binary that runs. In System
Settings → Privacy & Security → Full Disk Access, click **+**, then press
**⌘⇧G** in the picker and paste the path — the tool lives in a hidden directory,
so browsing to it won't work.

Two things to know about verifying it:

- `ssrename doctor` run from a terminal inherits *that terminal's* permissions,
  so it can pass while the LaunchAgent still fails. Install the agent, take a
  screenshot, and re-run `doctor` — it surfaces recent errors from the agent log.
- `uv tool install` can change the interpreter path when it upgrades Python, and
  the grant follows the old path. Re-run `doctor` if renaming stops.

Avoiding all of this is also legitimate: point `watch_dir` at somewhere
unprotected such as `~/Pictures/Screenshots` and no grant is needed.

## Configuration

`~/.config/ssrename/config.toml`, written with comments by `ssrename init`. The
things you're most likely to change:

| Key | Default | Meaning |
| --- | --- | --- |
| `general.watch_dir` | `~/Desktop/screenshots` | directory watched |
| `general.debounce_seconds` | `8.0` | delay after a file appears |
| `general.max_words` | `5` | words kept from the description |
| `general.max_image_px` | `1600` | longest edge sent to the model (`sips`) |
| `backend.kind` | `openai` | `openai` or `fm` |
| `backend.openai.base_url` | `http://localhost:1234/v1` | LM Studio's default port |
| `backend.openai.model` | `qwen/qwen3.6-27b` | must accept images |
| `backend.fm.model` | `device` | `device` or `pcc` |
| `prompt.instructions` | see file | system prompt shaping the names |

Reasoning models will spend their whole token budget thinking and return an
empty answer. The default `extra_body` turns that off; LM Studio honours
`reasoning_effort`, while some other servers want the template kwarg:

```toml
extra_body = { reasoning_effort = "none" }
# or
extra_body = { chat_template_kwargs = { enable_thinking = false } }
```

### Per-machine setup

The laptop and the desktop want different `[backend]` blocks and nothing else,
so keep one config per machine, or point the agent at an explicit file:

```sh
ssrename --config ~/.config/ssrename/studio.toml install
```

## Commands

| Command | Does |
| --- | --- |
| `ssrename watch` | run the watcher in the foreground |
| `ssrename once FILE...` | rename specific files now (`--force` ignores the name filter) |
| `ssrename backfill [--limit N]` | rename existing screenshots in the watch dir |
| `ssrename doctor` | check config, backend reachability, macOS settings, agent |
| `ssrename init [--force]` | write the default config |
| `ssrename set-screenshot-dir [PATH]` | point macOS at a screenshot folder (see above) |
| `ssrename install` / `uninstall` | manage the LaunchAgent |

`--dry-run` works with everything; logs go to `~/Library/Logs/ssrename.log`.

## Development

```sh
uv sync
uv run pytest
```

[lmstudio]: https://lmstudio.ai
[ollama]: https://ollama.com
[fm]: https://developer.apple.com/videos/play/wwdc2026/334/
