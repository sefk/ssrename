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
  can take `--image`. It needs no server and answers in about 2s, but names
  screenshots far less accurately than a mid-size local vision model: see
  [Benchmarking backends][bench]. On macOS 26 the `fm` binary
  does not exist and the on-device Foundation Model has no image input.

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
bin/deploy
```

`bin/deploy` does the whole install, and is also how you upgrade after a
`git pull`; it is safe to re-run. It:

1. installs the tool with `uv tool install --reinstall .`,
2. writes the default config if there is none (`ssrename init`),
3. points macOS screenshots at `watch_dir` (`ssrename set-screenshot-dir`),
4. if `[sync] peer` is set, sets up SSH to the peer (see [Syncing two
   Macs][sync]),
5. (re)starts the LaunchAgent (`ssrename install`) and waits for its first
   sync,
6. runs `ssrename doctor`.

`bin/deploy --remove` stops and removes the LaunchAgent. The steps also work
one at a time, as the `ssrename` commands named above.

Check it works on a few files before letting the agent loose:

```sh
ssrename --dry-run backfill --limit 3
```

### Where macOS saves screenshots

**The preference key depends on the macOS version.** macOS 27 split the single
`location` key into per-capture-type keys, matching the `target-screenshot` /
`target-screenrecording` pair it already had. Write both, so the setting works
either way:

```sh
defaults write com.apple.screencapture location-screenshot ~/Pictures/Screenshots  # macOS 27+
defaults write com.apple.screencapture location ~/Pictures/Screenshots             # older
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

### Keeping the backend up across reboots

With the `openai` backend, ssrename is only as available as the server it points
at, and a local server does not necessarily come back after a restart. LM Studio
is the case worth calling out: `lms daemon up` starts the background service but
does **not** open the HTTP server, so port 1234 stays closed and every screenshot
fails with `Connection refused`. Both are needed at login:

```sh
lms daemon up && lms server start
```

`lms server start` reuses whatever port it was last started on.

ssrename handles the outage rather than fighting it: when the backend can't be
reached it logs one error, pauses, and retries with a backoff that doubles from
15s up to 10 minutes. Queued screenshots are kept, not dropped — when the server
returns it logs `backend reachable again, resuming` and works through the
backlog. So a dead backend costs you one line in the log and nothing else, and
you can leave it down as long as you like.

### Full Disk Access (only if you move the watch directory)

The default `~/Pictures/Screenshots` needs no permissions at all, and that is the
whole reason for the default. Skip this section unless you point `watch_dir` at
`~/Desktop`, `~/Documents`, or `~/Downloads`.

Those are gated by TCC, and a LaunchAgent cannot show a permission prompt — so it
fails silently. Symptom: nothing is ever renamed and
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
| `general.watch_dir` | `~/Pictures/Screenshots` | directory watched |
| `general.debounce_seconds` | `8.0` | delay after a file appears |
| `general.max_words` | `5` | words kept from the description |
| `general.max_image_px` | `1600` | longest edge sent to the model (`sips`) |
| `backend.kind` | `openai` | `openai` or `fm` |
| `backend.openai.base_url` | `http://localhost:1234/v1` | LM Studio's default port |
| `backend.openai.model` | `qwen/qwen3.6-27b` | must accept images |
| `backend.fm.model` | `device` | `device` or `pcc` |
| `backend.fm.extra_args` | `[]` | extra `fm respond` flags, e.g. `["--greedy"]` |
| `sync.peer` | `""` (off) | the other Mac's Tailscale name; see [Syncing two Macs][sync] |
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

## Syncing two Macs

ssrename can keep the screenshot folders on two Macs identical: a screenshot
taken on the laptop shows up on the desktop, already renamed, and the other way
round. It goes straight from one machine to the other over SSH, using
[Tailscale][tailscale] to find the peer, with no cloud service in between.

```toml
[sync]
peer = "sef-m3"     # on the desktop; "studio" on the laptop
```

Then run `bin/deploy` on both Macs. Each side needs to be able to SSH to the
other without a password, and `bin/deploy` sets that up:

1. **Remote Login** has to be on (System Settings → General → Sharing → Remote
   Login). `bin/deploy` warns if it is off; it can't switch it on for you.
2. **A key just for sync.** `bin/deploy` creates `~/.ssh/ssrename_ed25519`
   with no passphrase, and sync uses that key and nothing else: no ssh-agent.
   A LaunchAgent cannot type a passphrase or approve a Touch ID prompt, and an
   agent that wants one — 1Password's, for instance — stalls every connection
   until it gives up. Your own SSH setup is left as it is.
3. **The peer accepts it.** If the peer turns the key down, `bin/deploy` runs
   `ssh-copy-id`, which logs in the usual way. This is the one step that may
   ask you for a password or a Touch ID.
4. **The agent can use it.** `bin/deploy` waits for the LaunchAgent's first
   sync and shows the result, so a key that works from your terminal but not
   from the agent is caught right away.

After that, the LaunchAgent syncs whenever the folder changes and every
`interval_seconds` (default 5 minutes). `ssrename sync` runs one sync in the
foreground and lists what it moved; `ssrename doctor` checks the connection.

How it behaves:

- **Only renamed images are synced** (`png`, `jpg`, `jpeg`, `heic`; recordings
  are not). A raw `Screenshot ...` file stays on the machine that took it until
  that machine has renamed it, so the model never names the same image twice.
- **Nothing is ever overwritten.** If both machines already have a file with the
  same name, each keeps its own.
- **Deletes are synced.** Deleting a screenshot on one Mac moves the other
  Mac's copy to its Trash the next time that Mac syncs. Renaming a file by hand
  works the same way: the old name is deleted on the peer and the new name is
  copied over. If you restore a file from the Trash, the restored copy is kept
  and synced again.
- **An offline peer is normal.** A sleeping laptop gets one `WARNING` line in
  the log, and the sync catches up when it wakes. Deletes made in the meantime
  are remembered and applied then.
- **Mass disappearance is refused.** If more than half of the synced files
  vanish at once (at least 10), ssrename assumes the folder is unmounted or
  `watch_dir` is wrong rather than that you deleted them all, logs an error,
  and does not delete anything on the peer. If you really did delete them, run
  `ssrename sync --accept-deletions`.

The first sync merges both folders' full histories. ssrename remembers what it
has synced, and the names you have deleted, in
`~/.local/state/ssrename/sync.json`; the peer reads that file over SSH.

`remote_dir` defaults to the same place as `watch_dir`, relative to the home
directory. Set it if the peer keeps its screenshots somewhere else.

## Commands

| Command | Does |
| --- | --- |
| `ssrename watch` | run the watcher in the foreground |
| `ssrename once FILE...` | rename specific files now (`--force` ignores the name filter) |
| `ssrename backfill [--limit N]` | rename existing screenshots in the watch dir |
| `ssrename sync` | sync with the `[sync]` peer once, now |
| `ssrename doctor` | check config, backend reachability, macOS settings, agent, sync peer |
| `ssrename init [--force]` | write the default config |
| `ssrename set-screenshot-dir [PATH]` | point macOS at a screenshot folder (see above) |
| `ssrename install` / `uninstall` | manage the LaunchAgent |

`--dry-run` works with everything; logs go to `~/Library/Logs/ssrename.log`.

## Benchmarking backends

`ssrename-bench` scores how well a backend names screenshots, so choosing a
backend — or comparing the same one on two machines — rests on numbers rather
than impressions. Run it from the repo checkout:

```sh
uv run ssrename-bench variants              # what can be measured
uv run ssrename-bench harvest               # sample 20 of this machine's screenshots
uv run ssrename-bench run <set>             # run every variant, score, save results
uv run ssrename-bench compare A.json B.json # two runs side by side
```

**Sets.** A set is a directory under `bench/sets/` holding shrunk screenshots
and a `cases.toml` answer key. Git ignores `bench/sets/` because screenshots
are private — they show chats, names, and whatever else was on screen. To
compare two machines on identical images, copy the set across:

```sh
rsync -a studio.local:src/ssrename/bench/sets/ bench/sets/
uv run ssrename-bench run studio
ls bench/sets/studio/results/            # one JSON file per run, <machine>-<time>
uv run ssrename-bench compare bench/sets/studio/results/studio-<time>.json \
                              bench/sets/studio/results/<laptop>-<time>.json
```

`harvest` samples evenly across the history of `watch_dir` (or `--from DIR`),
shrinks each image to `max_image_px`, and drafts an answer from the filename.
Those drafts inherit whatever the naming model got wrong, so open each image,
correct its `app` and `topic`, and set `reviewed = true`; scores on unreviewed
drafts are flagged as provisional. Raw `Screenshot ...` files get no draft.

**Scoring.** Each case lists `app` phrases (the app or site, say `datatalk`)
and `topic` phrases (what is on screen). A name passes when it contains one of
each as whole words of the *final filename* — after ssrename's cleanup and
five-word cut — so a misspelled app name is a miss. The summary also counts
answers that weren't 2-5 plain lowercase words, errors, and latency.

| Variant | Runs |
| --- | --- |
| `openai` | `[backend.openai]` from your config; skipped if unreachable |
| `fm` | `fm respond` with your `[backend.fm]` settings |
| `fm-greedy` | `fm` with `--greedy` |
| `fm-ocr` | `fm` with `--greedy --tool ocr` |
| `fm-schema` | `fm` with `--greedy` and a two-field `{app, subject}` output schema |
| `fm-schema-ocr` | both of the above |

Results land in `<set>/results/<machine>-<time>.json`, rewritten after every
image so an interrupted run keeps what finished. Each records the chip, macOS
build, and ssrename commit. Correcting `cases.toml` later doesn't need a re-run:
`ssrename-bench rescore FILE...` re-judges saved names against the current key.

`fm-greedy` is deterministic — repeat runs give identical names — which makes
it a fingerprint for the on-device model. Run it on two machines over the same
set: identical names mean the same model, so any quality gap lies elsewhere;
different names mean a different model or macOS build. Plain `fm` samples, so
its score moves by a point or two from run to run. `fm` also fails now and then
with `LanguageModelError error -1`; it passes on retry, and counts as an error.

On a Mac Studio (M1 Max, macOS 27.0 26A428), over 21 screenshots of browser
apps, terminals, Slack, and Zoom:

| Variant | Pass | Median time |
| --- | --- | --- |
| `openai` (qwen3.6-35b-a3b 4-bit, LM Studio) | 15/21 | 5.5s |
| `fm` | 4/21 | 2.2s |
| `fm-schema` | 5/21 | 2.6s |
| `fm-ocr` | 10/21 | 7.1s |
| `fm-schema-ocr` | 4/21 (13 errors) | 28.4s |

`fm` mostly misses by naming the wrong app — Jira for GitHub, Discord for
Slack, Reddit for a DataTalk page. OCR fixes much of that, but text-heavy
screens overflow the model's 4,096-token context and fail outright.

## Development

```sh
uv sync
uv run pytest
```

[lmstudio]: https://lmstudio.ai
[ollama]: https://ollama.com
[fm]: https://developer.apple.com/videos/play/wwdc2026/334/
[bench]: #benchmarking-backends
[sync]: #syncing-two-macs
[tailscale]: https://tailscale.com
