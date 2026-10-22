# development-tools

Small, local tools for working with a git workspace. Each tool lives in its own folder with
its own guide; the folders beside it that hold no tool are the shared libraries the tools and
their tests use, shipped with their own tests.

| Tool | What it does |
| --- | --- |
| [`git_workspace_backup`](git_workspace_backup/git_workspace_backup.md) | A daily, verified backup of a git checkout's un-pushed work, normally to iCloud Drive |

## git_workspace_backup

A remote holds only what was pushed to it. Every other branch, every stash entry and the
gitignored files you could not rebuild live on one disk. Once a day this tool copies that set
into a destination folder, normally in iCloud Drive:

- every ref in the checkout (branches, tags, the stash), each linked worktree's `HEAD` and
  every stash entry's commit, as a git bundle;
- the gitignored files you list, and the files you list under your home folder and under
  `~/.claude`, as one zip;
- a `MANIFEST.json` saying what the snapshot holds, and a `RESTORE.md` with the steps that
  restore it.

Each snapshot is verified by restoring it into a scratch folder before it is promoted. Old
snapshots are pruned only through a ledger the tool keeps, so it prunes only snapshots it
recorded. `--mode status` judges the destination, and asks iCloud whether the newest snapshot
has actually been uploaded. Uncommitted changes are listed in the manifest, never captured.

### Requirements

- macOS 11 (Big Sur) or newer: launchd schedules the daily run, and `osascript` posts the
  failure notification and reads iCloud's upload state. The floor is the newer of the two
  system features the tool depends on, taken from Apple's release history rather than
  measured on an old release: install loads and removes the agent with `launchctl bootstrap`
  and `bootout`, which arrived in OS X 10.10, and the archive's reads refuse a symlink
  anywhere in a path with `O_NOFOLLOW_ANY`, which arrived in macOS 11.
- Python 3.12 or newer. The stock `python3` on macOS is too old; install 3.12 or newer, for
  example from python.org or Homebrew. The standard library is enough to run the tool; the
  tests need Hypothesis.
- git 2.36 or newer on `PATH`. A restore also needs `unzip`.
- For the upload check, a destination inside iCloud Drive. A destination elsewhere still
  works, and `status` notes that the check does not apply.

### Install

Clone this repository anywhere outside the one you want backed up:

```bash
git clone <repository-url> development-tools
```

Then put the `[tool.workspace_backup]` table below in the root `pyproject.toml` of the
repository you want backed up, and point the tool at that repository with `--repo`. Below,
`~/your-repo` stands for its path, which must be absolute or start with `~/`; a linked
worktree's path means its primary checkout:

```bash
python3.12 development-tools/git_workspace_backup/git_workspace_backup.py --mode run --dry-run --repo ~/your-repo
python3.12 development-tools/git_workspace_backup/git_workspace_backup.py --mode install --repo ~/your-repo
python3.12 development-tools/git_workspace_backup/git_workspace_backup.py --mode status --repo ~/your-repo
```

The dry run prints the plan and writes nothing. Install writes a per-user launchd agent and
loads it, recording the absolute paths of the interpreter, the script, the repository and the
destination; if any of them moves, or you change `destination`, run install again. The agent
runs daily at the install's `--time` and again at login, and loading it starts a run unless
today already has a complete snapshot. Run status once that run has finished: install prints
the status command to use, carrying both of the agent's pins, `--repo` and `--destination`.
Status judges the folder its own options or the configuration name, and flags it when that
folder is not the destination the installed agent has pinned.

There is one agent per account: its launchd label is fixed, so installing for a second
repository replaces the agent the first install loaded, and backs up only the second. Install
ends by naming the repository the agent now backs up. Every option, the exit codes and what
status judges are in
[`git_workspace_backup/git_workspace_backup.md`](git_workspace_backup/git_workspace_backup.md).

### If a run fails with "Operation not permitted"

An `EPERM` on the destination means macOS's privacy controls refused the process the agent
runs. Grant it access under Privacy & Security in System Settings (macOS 13 and later), or
under Security & Privacy, on its Privacy tab, in System Preferences (macOS 11 and 12): Full
Disk Access covers every protected folder, and a Files and Folders entry for the destination
is enough where one is offered.

Which process that is depends on how your Python was built. A virtual environment's
`python3.12` is only a link, and on a framework build, such as Homebrew's or python.org's,
the file it resolves to is itself only a launcher stub: the process macOS judges is the
`Python.app` inside the framework, at `<sys.base_prefix>/Resources/Python.app`. On any other
build it is the resolved interpreter. Run this with the interpreter you installed with, to
print the one to grant:

```bash
python3.12 -c 'import os, sys; app = os.path.join(sys.base_prefix, "Resources", "Python.app"); print(os.path.realpath(app if os.path.isdir(app) else sys.executable))'
```

A Homebrew upgrade of Python changes that path, so grant the new one again after each
upgrade. Before granting anything, confirm which client macOS actually denied, in its privacy
log, from shortly after a failed run:

```bash
log show --last 1h --predicate 'subsystem == "com.apple.TCC"'
```

Then wait for the next run, or install again to start one, and run status.

### Uninstalling

```bash
python3.12 development-tools/git_workspace_backup/git_workspace_backup.py --mode uninstall --repo ~/your-repo
rm -r ~/.workspace_backup
```

Uninstall stops and removes the agent, prints which repository it was backing up, and keeps
every snapshot and all recorded state. The second command finishes the job by hand:
`~/.workspace_backup` holds the run records, the ledger that pruning trusts, this machine's
identity file, the staging folder runs build in and the agent's logs, never a finished
snapshot. The tool keeps that folder readable by your account alone, and a run refuses one
that another account owns or can write. Pruning happens in runs, and only through the ledger,
so once it is gone no run prunes the snapshots already in the destination: they are yours to
keep or delete. If you install again later, the new ledger starts empty, and status flags each
old snapshot folder named for this machine's label as a problem, one its ledger does not
track: check each, and keep or delete it by hand.

### Configuration

The tool reads a `[tool.workspace_backup]` table from the nearest `pyproject.toml` at or above
the root of the repository `--repo` names, and refuses an unknown key. A repository that is not a Python project
needs a `pyproject.toml` at its root just to hold the table. The sample,
[`git_workspace_backup/samples/sample-config.toml`](git_workspace_backup/samples/sample-config.toml):

```toml
[tool.workspace_backup]
destination = "~/backup-sample"
keep = 3
extras = []
regenerable = ["**/__pycache__"]
claude_files = ["settings.json"]
home_files = []
memory_snapshot = false
rearm_commands = [
  "python tools/example_hooks.py --install",
  "python <path to git_workspace_backup.py> --mode install --repo <path to your repo>",
]
setup_doc = "docs/EXAMPLE_SETUP.md"
```

| Key | Meaning |
| --- | --- |
| `destination` | Required. The folder snapshots go into: absolute, or starting with `~/`. Its parent must exist. |
| `keep` | Complete snapshots to retain, at least 1. |
| `extras` | Gitignored files in the checkout that cannot be rebuilt, archived under `workspace/`. |
| `regenerable` | Gitignored paths deliberately not backed up. `status` reports any gitignored path in neither list, so each one is a decision. |
| `claude_files` | Files under `~/.claude` to archive, under `claude/`. |
| `home_files` | Files under your home folder to archive, under `home/`. |
| `memory_snapshot` | Leave it `false`. On, each run first runs a `memory_snapshot` tool this repository does not ship. |
| `rearm_commands` | The commands `RESTORE.md` lists for setting the restored checkout up again. Absent, it lists only this tool's install line, naming this clone's script and, with `--repo`, your repository, both where they were when the snapshot was taken. A listed set replaces that line, so include it, with `--repo`. |
| `setup_doc` | Optional: a setup document in the checkout, as a relative path, that `RESTORE.md` points the restorer to. |

List entries are relative paths. The guide's *Configuration* section has the full rules for
each key.

The sample lists `settings.json` only to show the key's shape. Claude Code's `settings.json`
can hold API keys in its `env` map; list it only when yours does not.

**Who chooses what leaves the machine.** The table lives in the repository being backed up, so
whoever can change that file, a merged pull request included, chooses which files the next run
copies out. Only the destination is pinned: install bakes it into the agent, and a later edit to
`destination` takes effect only when you install again. Every other key is read afresh on each
run, so review a change to the table as you would a change to a script that runs daily with your
account's access.

### What a snapshot holds, and what never to list

When the home folder holds Claude Code's auto-memory repositories, under
`~/.claude/projects/<project>/memory`, every memory repository with commits is bundled beside
the checkout's, on every run: no key turns it off, and with none there, nothing is.

Never list a credential store, a `.env` file, a private key, or a `settings.json` whose `env`
map holds keys, under any of `extras`, `claude_files` or `home_files`. A gitignored secret
belongs in `regenerable`, which records the decision not to back it up. The bundles and the
archive are not encrypted: a snapshot is exactly as private as the destination folder, and
whatever protection that folder's storage gives it is the only protection it has.

### Restoring

Each snapshot carries its own `RESTORE.md`, rendered from its manifest: shell blocks that
check the restore folder first, restore each bundle, check the recorded `HEAD`, store the stash
entries back and unpack the archive. The guide's *Restoring a snapshot* section explains each
step. [`git_workspace_backup/samples/README.md`](git_workspace_backup/samples/README.md) renders
one from the sample manifest without touching your machine. On a second machine, its note names
the Claude files the archive holds, which describe the first machine's setup.

**Nothing authenticates `RESTORE.md`.** It is read back from the destination, and anyone who can
write there can rewrite it, together with the bundles beside it. Read every `sh` block before you
paste it into a shell.

### Known limits

- macOS only, and nothing runs while you are logged out: the agent is a per-user one.
- Snapshot names record local time, so across a daylight-saving change an older snapshot can
  outrank a newer one in retention. Every deletion is still proven first.
- Whether the failure notification is displayed depends on notification permissions the tool
  cannot read. `status` is the guarantee, not the notification.

The guide's *Known limits* section lists the rest.

### Use at your own risk

It is provided as-is, with no warranty. Check a restore by hand before you rely on it.

## Running the tests

The suites need Hypothesis. `pip install ".[test]"` does not work here, since the repository
declares no build system, so install it directly into a Python 3.12 virtual environment, at the
exact version the `test` list in `pyproject.toml` names, in place of `<version>` below. Run
every command in this section from the clone's root, after `cd development-tools`:

```bash
python3.12 -m venv .venv
export PATH="$PWD/.venv/bin:$PATH"
python3.12 -m pip install "hypothesis==<version>"
```

The second line puts the environment's `python3.12` first on `PATH` in that shell. Then each
suite runs from its own folder's test file, still from the clone's root:

```bash
python3.12 git_workspace_backup/test_git_workspace_backup.py
python3.12 atomic_write/test_atomic_write.py
python3.12 bounded_run/test_bounded_run.py
python3.12 help_formatter/test_help_formatter.py
python3.12 live_pid_guard/test_live_pid_guard.py
python3.12 pyproject_config/test_pyproject_config.py
python3.12 schedule_artifacts/test_schedule_artifacts.py
python3.12 stdio_utf8/test_stdio_utf8.py
```

Two tests run the real `osascript` on temporary files; `WORKSPACE_BACKUP_OFFLINE=1` skips them.

Not affiliated with or endorsed by Anthropic or Apple.

## License

AGPL-3.0-or-later.
