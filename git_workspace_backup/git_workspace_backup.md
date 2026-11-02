# `git_workspace_backup`: a daily, verified iCloud Drive backup of the workspace

A remote holds only what was pushed to it, so every branch that was not, every stash, the
gitignored-but-irreplaceable files and the auto-memory corpus still live on one disk. Once a day this tool copies the irreplaceable set into a destination folder, normally in
iCloud Drive: the git history as bundles, and the gitignored-but-irreplaceable files as a
whitelisted zip. Each snapshot is verified by restoring it before it is promoted, pruned only
through a ledger the tool keeps, and judged by `--mode status`, which also asks iCloud whether
the snapshot has actually left the machine.

A handful of large files per snapshot is the shape iCloud syncs best, which is why the history
travels as bundles. Three routes were rejected:

- **restic.** Deduplication and retention are built in, but its repository password is a
  second secret that must itself be backed up off the machine, or the backup is inert, and a
  restore needs restic installed. On the workspace it was built for, the deduplication saving
  was estimated from measured sizes at about 500 MB, which does not buy that failure mode.
- **An rsync mirror including `.git`.** Thousands of churning loose objects and conflicted
  copies, and hardlinked snapshots (`--link-dest`) save nothing: iCloud keeps no hardlinks, so
  each linked file uploads and counts in full.
- **A bare git remote inside iCloud Drive.** The same small-file problem, and it covers none of
  the gitignored or Claude-home files.

It needs macOS, Python 3.12 or later, and git 2.36 or later on `PATH`. It drives `/bin/launchctl`
for the agent and `/usr/bin/osascript` for notifications and upload state, each by that absolute
path: the agent's `PATH` puts git's directory first, which a Homebrew install makes writable by
its user. A restore also needs `unzip`.

## What a snapshot holds

One folder per run, directly under the destination, named for the run's local start time with
the year first, then `-` and the label of the machine that took it, such as
`2026-10-22T060000-<machine>` (`snapshot_name` in `src/backup_promote.py`). The time comes first,
so names sort chronologically whatever the machine. A bare name, the time alone, is one taken
before machine identity: it still reads as a snapshot, naming no machine, and rolls off through
`keep` like any other. The label is this machine's identity, described under
[Several machines, one destination](#several-machines-one-destination).

| File | Holds |
| --- | --- |
| `workspace.bundle` | every ref under `refs/` in the primary checkout (branches, tags, the stash), each linked worktree's HEAD, and every stash entry's commit |
| `memory/<slug>.bundle` | one per auto-memory repository under the Claude home's `projects/<slug>/memory` that has refs; one with none is skipped |
| `extras.zip` | the files the `extras`, `claude_files` and `home_files` lists match, under the prefixes `workspace/`, `claude/` and `home/` |
| `RESTORE.md` | this snapshot's restore procedure, rendered from its manifest |
| `MANIFEST.json` | what the snapshot claims about itself: the machine that took it, each file's hash and size, each bundle's refs, HEAD, worktree heads and stash entries, each archive member's hash, failed items, dirty worktrees, skipped symlinks and unclassified ignored paths |

Uncommitted changes in any worktree are listed in the manifest, by worktree directory name and
changed-path count, and never captured.

A snapshot with failed items is still promoted, marked incomplete: it may be the only copy there
is. A bundle that fails, because a git query failed or the bundle lacks a ref its repository has,
is a failed item rather than a bundle. When its file was written anyway, the file is kept in the
snapshot and named in the failure reason, but it is not listed among the bundles, so neither
verification nor `RESTORE.md` uses it.

## How a run works

`--mode run` runs these steps in order:

1. **Lock.** The run lock is `run.lock` (`LOCK_NAME` in `src/backup_run.py`) under
   `$HOME/.workspace_backup` (`STATE_BASE_NAME` in `src/backup_model.py`). A run that finds
   another holding it prints a skip and exits 1, but records and posts nothing: a manual run
   racing the agent is not a fault, and the holder's record, perhaps a failure it is posting at
   that moment, must survive the skip.
2. **Validate.** macOS, git on `PATH`, and a destination whose parent directory exists. Promote
   creates the destination itself but never its parents: a missing parent means iCloud Drive's
   folder is absent, and creating it would put snapshots on local disk under a path that only
   looks synced. Then the destination's ledger is read; one this tool did not write, or one that
   cannot be read, stops the run. So does an identity file that holds no valid label: the first
   run derives this machine's identity and keeps it, and every later run reads it.
3. **If due.** With `--if-due`, a day whose ledger already records a complete snapshot is
   recorded as a skip, and the run exits 0.
4. **Staging.** Every stale staging folder is removed, and a fresh one is made under
   `$HOME/.workspace_backup/staging` (`STAGING_DIR` in `src/backup_run.py`, made fresh by
   `src/backup_staging.py`), outside iCloud, so iCloud never syncs a half-written snapshot.
5. **Memory.** When `memory_snapshot` is on in the configuration,
   the companion tool's `memory_snapshot --mode snapshot`, from that tool's own folder in the
   folder this tool's own folder sits in, runs first, so memory edits
   since the last session ended are committed before they are bundled. Off, the step never runs,
   and the memory corpora are still bundled as they stand.
6. **Build.** The workspace and memory bundles, the dirty-worktree listing, the drift check and
   `extras.zip`. An item that fails is recorded, and the rest proceed. The archive never holds
   the identity file, whatever entry reaches it.
7. **Verify.** Each bundle is restored into a scratch directory of its own in the system temp
   directory: a mirror clone, then `git fsck --full`. The restored refs must include every
   recorded ref, HEAD must resolve to the recorded head when one was recorded, and each worktree
   head is fetched into `refs/backup/worktrees/<name>/HEAD` and compared with its recorded
   commit. Each recorded stash entry must resolve to a commit the clone holds. `extras.zip` is CRC-checked, and each member is compared with its recorded hash.
   `git bundle verify` is not used: it checks only the header, and accepted a bundle whose pack
   data was corrupt.
8. **Seal.** Every staged file is hashed first, and one that cannot be hashed joins the failed
   items. Only then is completeness decided and `RESTORE.md` rendered, so its `complete` line
   always agrees with `MANIFEST.json`, which is written last.
9. **Promote.** One rename into the destination, or across volumes a copy that the ledger records
   before it starts. A folder is never replaced. The promoted `MANIFEST.json` is then read back
   within a bound, and the ledger records the snapshot as promoted only once that read-back
   hashes to the bytes written. Until then prune never touches the folder, and a run that cannot
   confirm its snapshot prunes nothing.
10. **Prune.** See [Pruning](#pruning).
11. **Record.** The outcome, whatever it is, is written as `last.json` (`LAST_RUN_NAME` in
    `src/backup_ledger.py`) in the destination's state directory, then printed, and every exit
    other than 0 posts a notification. A record that cannot be written is said in what is
    printed and posted, and the run exits at least 1, because `status` would go on reading an
    older record.

`$HOME/.workspace_backup` holds the run lock, `machine` (this machine's identity, one line;
`IDENTITY_NAME` in `src/backup_machine.py`), `staging`, `logs` and `state`. Staging holds each
build in progress, every backed-up file included, so the directory is this user's alone: run
and install create it, and every directory beneath it, `0700`, and make an existing one `0700`.
Run, install and status, dry runs included, refuse with exit 2 a `$HOME/.workspace_backup` that
is not a directory (a symlink in its place included), is not owned by the effective user, or
that its group or anyone else can write. Run and install check and change it through one
descriptor, opened without following a symlink, so a swap after the check cannot redirect the
change; one they cannot open (mode `000`, say) is refused rather than repaired. The run lock is opened, and probed, never through a
symlink; the identity file, the ledger and `last.json` are read only as regular files, never
through a symlink, and the ledger and `last.json` within `STATE_READ_LIMIT` in
`src/backup_ledger.py`. State lives under
`$HOME/.workspace_backup/state` (`STATE_DIR_NAME` in `src/backup_ledger.py`), in a directory named
for a short hash of the resolved destination path (`state_dir_for` in the same module), so a run
against a scratch destination can never overwrite the real destination's ledger or record.

The ledger, `last.json`, a snapshot's `RESTORE.md` and `MANIFEST.json`, and the agent's plist
are each written whole, never in place (`atomic_write_bytes` in `src/backup_fs.py`, through the
shared `../atomic_write` library): a staging file beside the target, opened exclusively
under an unpredictable dot-name, set to `0600` for the ledger, `last.json` and the identity file
and to `0644` for the rest, fsynced and then renamed over it. A fixed staging name such as `<name>.tmp` would be one
a symlink could be planted at in advance, and a plain open follows the link: the data would be
written into the link's target, and the link itself renamed into the target's place. A write
or rename that fails removes its temp file and leaves the previous content untouched.

`--dry-run` validates and prints the plan, naming the folder a run would write, this machine's
identity included. It takes no lock, and records, posts and writes nothing, the identity file
included: before the first run it shows the label that run would keep.

### Pruning

Prune keeps the newest `keep` complete snapshots and the newest `keep` incomplete ones, and
removes what an interrupted cross-volume copy left. It never deletes the snapshot its own run
has just promoted, which a clock set back far enough would otherwise name before `keep` newer
snapshots and so plan for deletion. It deletes only folders the destination's
ledger records, with the name shapes this tool writes, and a promoted folder only while its
`MANIFEST.json`, read within a bound and with no symlink beneath the destination, still hashes to
the ledger's value. A deletion it cannot prove, or a removal that fails, is deferred and named in
the run's summary line on every run until it succeeds. A destination that is not a readable
directory defers everything.

## Usage

From this folder, with `~/your-repo` standing for the repository to back up:

```bash
python3.12 git_workspace_backup.py --mode run --dry-run --repo ~/your-repo
python3.12 git_workspace_backup.py --mode install --repo ~/your-repo
python3.12 git_workspace_backup.py --mode status --repo ~/your-repo
```

The dry run prints the plan. Install loads the daily agent, and loading it starts a run unless
today already has a complete snapshot. Run status once that run has finished.

With `--repo`, the script can sit anywhere, and from another folder only its own path changes.
Without it, every mode backs up the checkout this script sits in, wherever it is run from;
`/path/to` stands for wherever the script is:

```bash
python3.12 /path/to/git_workspace_backup.py --mode install
```

Install bakes the interpreter that ran it, and this script's resolved path, into the agent, so
run install with the interpreter the agent should use, from a primary checkout unless `--repo`
names the repository. It refuses a script in a linked worktree, which a worktree cleanup can
later delete from under the agent, and names the script's folder in that refusal. Given
`--repo`, install also bakes `--repo` and the resolved primary checkout into every firing.
Either way it pins the destination, `--destination`'s or the configured one, resolved, as
`--destination <path>` in every firing, so a later edit to the configuration cannot move where
the agent writes. Without `--repo`, the agent runs the daily command with that pinned
destination and nothing else.

**One agent per account.** The agent's label is fixed, so installing for a second repository
replaces the agent the first install loaded; it does not add one. Install and its dry run end by
naming the repository the agent now backs up. The agent runs
`--mode run --if-due` daily at `--time` and again at load (`RunAtLoad`), because no calendar
firing survives a power-off, only a sleep; `--if-due` turns the extra firing into a no-op on a
day already covered. A sleeping Mac runs a missed firing on wake. Nothing runs while the user is
logged out, since the agent is a per-user LaunchAgent.

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--mode MODE` | yes | n/a | `run` takes one snapshot; `install` and `uninstall` add or remove the daily LaunchAgent; `status` judges the destination's health. |
| `--if-due` | no | off | `run` only, and not with `--dry-run`: only record a skip when the ledger already holds a complete snapshot taken today. What the agent runs. |
| `--time HH:MM` | no | `DEFAULT_TIME` in `src/backup_launchd.py`, which `--help` prints | `install` only: the daily run time, on a 24-hour clock. |
| `--keep N` | no | unset: the configured `keep` | `run` and `status`: complete snapshots to retain, in place of the configured `keep`; at least 1. |
| `--destination DIR` | no | unset: the configured `destination` | `run`, `status` and `install`: the destination directory, absolute or starting with `~/`, resolved as the configured one is; a relative path is refused. `install` pins the destination, this one or the configured one, into the agent. |
| `--dry-run` | no | off | `run` and `install` only: print the plan and write nothing. |
| `--repo PATH` | no | unset: the checkout this script sits in | every mode: the repository to back up, absolute or starting with `~/`, and an existing directory; a linked worktree means its primary checkout. A path in no git checkout is refused, and so is a submodule or a repository made with `--separate-git-dir`, whose work tree is not its git directory's parent. `install` bakes the resolved primary checkout into the agent. |

### Which modes take which options

An option the chosen mode does not use is refused with exit 2, never ignored. Otherwise
`uninstall --dry-run` would uninstall, and `install --keep` would install an agent that goes on
reading the configured `keep`. A value equal to the option's default counts as not
given. A dry run of `run` has its own row, because the plan reads no `--if-due`: on a day
already covered it would still say it would write a snapshot.

| Mode | `--if-due` | `--time` | `--keep` | `--destination` | `--dry-run` | `--repo` |
| --- | --- | --- | --- | --- | --- | --- |
| `run` | yes | refused | yes | yes | yes, and it selects the `run --dry-run` row | yes |
| `run --dry-run` | refused | refused | yes | yes | yes | yes |
| `install` | refused | yes | refused | yes, pinned into the agent | yes | yes, baked into the agent |
| `uninstall` | refused | refused | refused | refused | refused | yes, compared with the repository the removed agent was backing up; the one agent is removed either way |
| `status` | refused | refused | yes | yes | refused | yes |

This table restates `CONSUMED_OPTIONS` in `git_workspace_backup.py`, and
`tests/test_companion_doc.py` fails when the two disagree. Install takes no `--keep` because
the agent reads the configuration on every run; `--repo` only says where that configuration
is. It takes `--destination` because it pins the destination either way, so a later edit to
the configuration cannot move where the agent writes. Uninstall reads neither the checkout nor
the configuration, so it checks only that a `--repo` it is given is absolute.

### Install and uninstall

Before writing anything, `--mode install` refuses a `--time` that is not a 24-hour `HH:MM` time,
an unusable `--repo`, an unsafe `$HOME/.workspace_backup`, a start outside a git checkout, a
missing or invalid configuration, git missing from `PATH`, a script in a linked worktree, and a
backup run holding the run lock. Given
`--repo`, a script folder outside every checkout is not refused: the checkout is the one
`--repo` names. The bootout below would kill that run,
and its git children, which run in sessions of their own, could go on writing into staging after
the lock is freed. The lock is probed without waiting and without creating anything, so a dry run
refuses the same way and still writes nothing. It then:

1. creates the log directory and writes the agent's plist, atomically, under
   `$HOME/Library/LaunchAgents` (`launchd_plist_path` in
   `../os_schedule_gen/os_schedule_gen.py`), named for `LABEL` in
   `src/backup_launchd.py`;
2. boots out any loaded copy, and polls until launchd reports the agent absent;
3. bootstraps the plist, and polls until launchd reports the agent loaded, whatever bootstrap
   exited: one racing the old copy's teardown can fail while launchd goes on to load the agent.

`launchctl print` has three answers: exit 0 is loaded, `PRINT_ABSENT_RC` is absent, and anything
else is unknown, a timeout or a missing `launchctl` included. An unknown answer exits 2
immediately rather than being polled, and a wait that passes its deadline exits 2. Each such
failure says that the plist is written and that an agent loaded before the install may already be
unloaded, and that re-running install recovers. `--dry-run`
prints the plist and the commands. It writes nothing and runs no `launchctl` verb, though it still
asks git, read-only, for the checkout the agent would run from.

The agent's `PATH` is git's directory followed by the system directories, since launchd starts an
agent with a minimal environment rather than a login shell's. Its output and errors go to
`launchd.out.log` and `launchd.err.log` under `$HOME/.workspace_backup/logs`: the directory is
`logs_dir`, and the two names are the ones `build_launchd_plist` writes into the plist, both in
`../os_schedule_gen/os_schedule_gen.py`.

**What proves the install works.** The tests drive fakes of `launchctl` and `osascript`, so only
a run that launchd itself starts, and that takes a snapshot, proves launchd can run Python and
Python can write the destination. Install's load-time run is that run, unless today is already
covered; then the load-time run only records a skip, and the next scheduled firing is the proof,
with `--mode status` run after it. When the proving run has finished, `--mode status` judges what
it recorded. While status still exits
2 for a missing run record, that run has either not finished or failed before it could record
anything: `launchd.err.log` says which, and the last exit code `launchctl print` shows for the
agent says how its latest run ended. Install prints all three: the status command, the error
log's path and the `launchctl print` command. A kickstart of the agent proves nothing on a day
already covered: every firing runs `--if-due`, which then only records a skip.

`--mode uninstall` refuses while a backup run holds the run lock, for the same reason. A run lock
it cannot open at all (the tool's directory is a file, or the lock is unreadable, a FIFO or a
symlink loop) is one no run can hold either, since a run opens it read-write: uninstall prints
one warning naming the lock and goes ahead, so a broken state directory never makes the agent
impossible to remove. Install still refuses such a lock, because every run of the agent it would
install fails at it. Otherwise uninstall boots the agent out, polls until launchd reports it
absent, and only then removes the plist. While the agent is still loaded, or its state is unknown, the plist is kept
for a retry and the exit is 2. Every snapshot and all recorded state are kept. Uninstall reads
neither the checkout nor the configuration, so a broken configuration cannot make the agent
impossible to remove. What it does read is the removed agent's own plist, before anything else
runs, as a regular file within a bound and never through a symlink: once the agent is gone it
prints which repository that agent was backing up, its `--repo` or the checkout holding its
script. Given a `--repo` that names another repository, it says so, and the one agent is
removed all the same. A plist it cannot read that from is a warning, and removal goes ahead.

## Exit codes

| Code | `run` | `status` | `install` and `uninstall` |
| --- | --- | --- | --- |
| `0` | a complete, verified, promoted snapshot; an `--if-due` run on a day already covered; a printed plan | healthy, perhaps with notes | launchd confirmed the agent loaded, or absent; a printed install plan |
| `1` | an incomplete snapshot; a run that promoted nothing, or could not confirm or record what it did; another run holding the lock; a plan whose memory repositories cannot be listed | at least one problem, listed | never |
| `2` | could not run, below | could not judge, below | a refusal before anything was written, a plist that could not be written or removed, or a launchd step that failed, answered unknown or did not settle |

Every mode exits 2 for bad options, an option the mode does not use included, and off macOS.
Every mode but `uninstall` also exits 2 outside a git checkout and for a missing or invalid
configuration.

- `run`, `status` and `install`, dry runs included, also exit 2 for a
  `$HOME/.workspace_backup` that is not a directory, not the effective user's, or writable by
  its group or anyone else, and for a `--repo` that is a submodule or a repository made with
  `--separate-git-dir`.
- `run`, dry run included, also exits 2 for a `--keep` below 1, a relative `--destination`, git
  missing from `PATH`, and a destination parent that is missing or not a directory. A real run
  also exits 2 for a run lock that cannot be taken, and for a ledger this tool did not write or
  cannot read. Both, dry run included, exit 2 for an identity file that cannot be read or holds
  no valid label, and a real run for one it cannot keep.
- `status` also exits 2 for a `--keep` below 1 or a relative `--destination`; when the agent is
  not installed, meaning no plist or launchd reports it absent; when no run record for the
  destination can be read, because it never ran or its `last.json` is not this tool's; for a
  ledger or run record that is not this tool's or cannot be read; and for an identity file that
  cannot be read or holds no valid label.
- `install` also exits 2 for a relative `--destination`, a path the agent needs that is not
  absolute or holds a control character (Unicode category `Cc`: C0, DEL and C1), and a git directory whose name holds the `PATH`
  separator `:`.
- `uninstall` also exits 2 for a relative `--repo`.
- `install`, dry run included, and `uninstall` also exit 2 while a backup run holds the run lock.
  `install` also exits 2 when an existing run lock cannot be opened to check; `uninstall` warns
  and goes ahead.

### What `status` judges

`--mode status` reads the destination's ledger and last run record, the newest complete
snapshot's manifest, and iCloud's upload state for that snapshot's files. A state it cannot read,
or a value it does not recognize, is a problem, never health. Its thresholds are `STALE_AFTER`,
`UPLOAD_GRACE` and `MAX_SNAPSHOTS_PER_KEEP` in `src/backup_status.py`. It exits 1, listing every
problem and then any notes, when:

- the last run failed or was incomplete, or it was skipped while the newest snapshot the ledger
  holds is incomplete;
- launchd reports the agent's state as unknown;
- the installed agent's plist pins a `--destination` that is not the folder this status judges,
  once both are resolved and compared as APFS compares names: the line names both folders, and
  is added after the unjudged line too, since the agent writing elsewhere is the likeliest
  reason a destination has no run record. An agent installed before the pin names no
  destination and adds nothing, and a plist that cannot say is a warning, as uninstall treats
  it, never a refusal;
- the ledger holds more than `MAX_SNAPSHOTS_PER_KEEP` times `keep` snapshots, so pruning is not
  keeping up;
- no complete snapshot exists, or the newest one's name records no time;
- the newest complete snapshot is dated after now: the clock was set back, or the snapshot is
  dated ahead of it;
- it is at least `STALE_AFTER` old;
- its manifest cannot be read within a bound, does not hash to the ledger, or does not parse;
- its manifest lists unclassified ignored paths, each of which belongs in `extras` or
  `regenerable`;
- its upload state cannot be read: `osascript` failed or gave an unexpected answer, a snapshot
  partly in iCloud and partly not included;
- the destination is at or under iCloud Drive's folder, yet its files read as not in iCloud:
  check that iCloud Drive is signed in and turned on;
- it is still not uploaded `UPLOAD_GRACE` after it was taken;
- the destination holds a folder named for this machine that its ledger does not track. Most
  likely it is this machine's own snapshot that a run could not ledger: a promote that could
  not confirm, a ledger save that failed (either run is recorded as failed), or a restore of this
  machine that started with an empty ledger. Check it, and keep or delete it by hand; relabeling
  this machine would only turn such folders into another machine's, which no machine prunes.
  Only if a second machine uses the same label, edit the identity file on one of them.

Otherwise it exits 0, with notes: an upload still pending inside `UPLOAD_GRACE`; a destination
deliberately outside iCloud Drive, where the upload check does not apply; snapshot folders under
the destination that the ledger does not track, bare or named for this machine, the first
`UNTRACKED_NAMED` of them named; and one count of the folders named for other machines, which
each of those machines prunes for itself.
After a machine restore the ledger starts empty, since it lives under `$HOME/.workspace_backup`
rather than in the destination, so no old snapshot is pruned automatically. How status reports
them depends on their names. A bare name, from before machine identity, shows up in the
untracked note, as it always has. One named for the label the restored machine keeps (the same
host name, or the old label written into its identity file) is the own-label problem above, with
its keep-or-delete-by-hand fix. One named for a different label is counted as another machine's,
and this machine never prunes it. Both ages
run from the time the snapshot's name records, when its run started, so each threshold fires
earlier than one measured from promotion by the length of the build, never later.

Upload state is read per file from Foundation's `NSURLIsUbiquitousItemKey` and
`NSURLUbiquitousItemIsUploadedKey` through `osascript`: for `MANIFEST.json` and every file the
manifest lists, or for `MANIFEST.json` alone when the manifest is not trusted. A folder merely
existing proves nothing, because a signed-out account or a paused sync leaves it present and
writable. A skipped last run is not itself a problem: status judges the destination by what it
holds.

Every test of that query drives a fake `osascript` but two: `LiveFoundationTests` in
`tests/test_icloud.py` run the real one on files in a fresh temporary directory, the only proof
that the bridge answers on this macOS release. `WORKSPACE_BACKUP_OFFLINE=1` skips them for a run
that must not reach the real `osascript`, such as a proof that puts a stand-in first on `PATH`.

### Several machines, one destination

Each machine names its snapshots by its own identity, so two machines backing up into one synced
destination never write the same folder, even in the same second, and each keeps its own ledger,
`keep` count and `--if-due` decision under its own home. The identity is derived once, on the
first run, from the host name: the part before the first dot, lowercased, every run of
characters outside `a-z` and `0-9` made one hyphen, the ends trimmed, and capped at
`MAX_LABEL_LENGTH` in `src/backup_machine.py`; a host name that leaves nothing gives `machine`.
It is kept in `$HOME/.workspace_backup/machine` and read on every later run, so a host-name
change, such as macOS adding `-2` on a network name clash, does not change it. Edit the file to
choose another label: one line of lowercase letters and digits joined by single hyphens. A file
that holds anything else stops runs and status with exit 2 and is never rewritten. The file never
travels in a snapshot, whatever the configuration whitelists, so a machine set up from another's
snapshot derives its own; a replacement for the same machine writes the old label into the file
before its first run instead, so its old snapshots stay its own. Two machines with the same host
name derive the same label, which status reports as a problem: edit the file on one of them.

## Configuration

`[tool.workspace_backup]` in the nearest `pyproject.toml` at or above the primary checkout. That
nearest file must hold the table, and an unknown key is refused. `run`, `status` and `install`
load it; `uninstall` does not.

| Key | Meaning |
| --- | --- |
| `destination` | Required. The folder snapshots go into: absolute, or starting with `~/` and expanded against the home directory, then resolved through symlinks. Its parent must exist. |
| `keep` | Complete snapshots to retain, at least 1; when absent, `DEFAULT_KEEP` in `src/backup_config.py`. As many incomplete ones are kept besides. |
| `extras` | Gitignored but irreplaceable paths under the primary checkout, archived under `workspace/`. |
| `regenerable` | Gitignored paths deliberately not backed up: rebuildable, or out of scope by decision. |
| `claude_files` | Paths under the Claude home, archived under `claude/`. |
| `home_files` | Paths under the home directory, archived under `home/`. |
| `memory_snapshot` | `true` or `false`; when absent, `false`. On, each build first runs `memory_snapshot --mode snapshot` from the `memory_snapshot/` folder beside this tool's own, and a failure is a failed item. Turn it on only where that tool is installed there. |
| `rearm_commands` | The commands `RESTORE.md`'s re-arm step lists, in order, run from the restored workspace: each a non-empty string with no line break, other control character, Unicode format character (a bidi override, a zero-width character, a byte order mark) or Markdown fence marker, as it is written inside a fenced block. When absent, the step lists only this tool's install line, naming `git_workspace_backup.py` relative to the workspace when it lies inside it, and otherwise absolutely, with `--repo` naming the workspace, and a note that both paths are where they were when the snapshot was taken. A listed set replaces that line, so include it. An empty list is refused: omit the key for the default. |
| `setup_doc` | Optional. The workspace's setup document, as a path relative to the workspace root: the same rule as a re-arm command, plus no backtick, and refused when absolute or when it climbs with `..`. When set, the re-arm step tells the restorer to rebuild the virtualenvs and prerequisites as that document describes, naming it in backticks; when absent, the step's wording names no document. |

Every list entry is relative and has no empty, `.` or `..` component: an entry escaping its root
would archive files nobody listed.

**Whoever can edit this table chooses what is backed up.** It lives in the backed-up
repository's own `pyproject.toml`, so a change merged there can add any file under the
workspace, the Claude home or the home directory to the next snapshot, and every run reads the
lists afresh. Install pins only the destination, so such a change cannot move where the
installed agent writes; what goes there is still the table's. Review a change to it as you
would one to the agent itself.

**What always travels, and what never to list.** Every auto-memory repository under the Claude
home that has refs is bundled on every run, whatever the lists say: no key turns the memory
corpora off. The archive is a plain zip, not encrypted, and a snapshot is as private as the
destination folder. So never list a file holding a credential: Claude's `settings.json`, whose
`env` map often holds keys, a shell startup file exporting a token, a key or a `.env` file.

The ledger and state are keyed on the resolved destination path as spelled. On a
case-insensitive volume such as default APFS, two spellings that differ only in case name one
folder yet key two ledgers, so spell the destination one way.

### Patterns

All four lists follow one set of rules, the drift check's, so a path drift calls backed up is one
the archive holds. Matching is component by component and case-sensitive: `*` and `?` never
cross a slash, a `**` component matches any number of components, none included, and an entry
that matches a directory covers everything beneath it.

Every `extras`, `claude_files` and `home_files` entry must match at least one regular file, or
the run records it as a failed item: an entry that silently matches nothing is how a backup
quietly stops covering something. The identity file is never archived, so an entry that matches
nothing else is a failed item whose reason says it matched only the machine identity file, which
never travels. Nothing below a root is followed: a symlink a pattern reaches
is recorded and skipped, and special files such as FIFOs are never read. Each member read is
bounded, so a stalled iCloud-offloaded file costs one failed item rather than the run.

The drift check lists the primary checkout's gitignored paths with `git status --ignored=matching`.
A path neither `extras` nor `regenerable` covers is recorded in the manifest, and `status` fails
on it until one line in either list covers it.

## Restoring a snapshot

Each snapshot's own `RESTORE.md` is the procedure, rendered from its manifest; the tool's
round-trip test executes the blocks the renderer writes.

Nothing authenticates the copy you read. It is read back from the destination, and whatever
can write the bundles there can rewrite it too. So read every `sh` block before you paste or
run it, and run none you do not
understand. A mode that verifies a snapshot against what the tool recorded is planned, and
not yet built.

1. Set `SNAPSHOT` to the snapshot folder and `RESTORE_ROOT` to the directory to restore into,
   both as absolute paths; the document's first lines show how. Every `sh` block begins by
   refusing a relative value of either, which would name somewhere beneath whatever directory
   the shell is in, so a block pasted on its own cannot write there either. A path beginning
   with a dash is refused or read as an option.
2. Start from an empty `RESTORE_ROOT`. The first `sh` block refuses anything but an existing,
   readable, empty directory. It lists the directory through `command ls`, so a shell alias or
   function named `ls` cannot make a full directory look empty, or an empty one full.
3. Run the `sh` blocks in order: one for each bundle, then the files block, which unzips
   `extras.zip` beneath `RESTORE_ROOT`. Each block is a `( set -eu … )` subshell that stops at
   its first failure, a use of an unset or empty variable included, whether it runs under `sh -c`
   or is pasted into `sh`, `bash` or `zsh`; a shell it is pasted into stays open. If the first
   block fails, stop there, as the document's introduction also says: a paste that goes on runs
   every later block regardless. The files block extracts the whole `extras.zip`, so where its
   members land is the restorer's `unzip`'s decision, not this tool's. The Info-ZIP `unzip` macOS
   ships strips `../` and absolute member names and defers symlinks until the rest is
   extracted, measured on a hostile archive; another `unzip` is not covered.
4. Treat the `text` blocks as manual steps: placing the archive's `claude/` and `home/` prefixes
   into the Claude home and the home directory overwrites live files, and so does placing each
   restored memory corpus or archived project folder; re-arming the git hooks and reinstalling
   the agent depend on the restored machine. The re-arm step lists the configuration's
   `rearm_commands`, or by default only this tool's install line, and its prose names the
   configuration's `setup_doc` when it has one.

A snapshot restores the machine it was taken on, not a second one beside it. After its files
block, each `RESTORE.md` names the Claude files its archive holds under `claude/`, each top-level
name once and derived from the manifest's archive members, or says it holds none. Files like
these can carry that machine's own facts, such as its scheduled agents and local paths, so a
second machine that is not replacing it places only those it means to share, after reading
each. Any memory corpus or project folder the snapshot holds is that machine's own too, since
memory holds machine facts, so a second machine does not place it either. It also says a second machine
takes its own machine identity, so re-installing this backup's agent there, as the re-arm step
advises, adds a second machine to the destination rather than a second writer of the first
one's names. Each `RESTORE.md` names the machine that took the snapshot beside its `Created`
line; one taken before machine identity names none. A replacement for the same machine writes
that old label into `~/.workspace_backup/machine` before its first run, so its old snapshots
stay its own and are pruned as before; a genuinely new machine lets the first run derive its
own label. `RESTORE.md`'s second-machine note says both.

A restored checkout holds every ref and each worktree head at
`refs/backup/worktrees/<name>/HEAD`, and lists every stash entry the snapshot recorded.
`refs/stash` names only the newest entry: the older ones live in its reflog, which a bundle never
carries. So the build hands each entry's commit to `git bundle create` unnamed, which packs its
objects, and records the entries, oldest first, with their messages. The restore block deletes
the clone's reflog-less `refs/stash`, stores each entry back with `git stash store`, and fails
unless `git stash list` then holds the recorded commits in order. A manifest written before stash
entries were recorded still reads, as recording none. Its checkout names no commit, so it keeps the HEAD the mirror
clone chose: the branch whose tip is the recorded head, or that commit detached when no branch
tips it. The block then fails unless HEAD is the recorded head, the property verification proved
of the same clone. A bundle holds no `.git/config`, no hooks and no virtualenvs.

Memory bundles restore beneath `RESTORE_ROOT`, at `claude/projects/<slug>/memory`, never into the
Claude home, and any `claude_files` entry inside a project directory is archived as
`claude/projects/<slug>/…`, which the manual `claude/` copy places under that same slug. When a
snapshot holds either, its `RESTORE.md` has one `text` step listing each restored corpus and each
project directory the archive holds something inside, derived from the manifest's archive members
rather than the configured globs, and says all of it belongs under `~/.claude/projects/<slug>/`.
`<slug>` is Claude's encoding of the restored workspace's absolute path, so on a machine with a
different home path the directory it belongs in is named differently from the one the snapshot
recorded, and a restore that keeps the old name leaves it where Claude does not look.

`RESTORE.md` is refused, and recorded as a failed item, for a manifest it cannot render safely: a
value holding a fence marker, or a character a terminal shows differently from what runs, by the
re-arm commands' rule (a line break, an escape, a backspace, a bidi control; a tab passes in a
stash message alone); a bundle name the build does not write (`workspace.bundle` at most once,
and `memory/<slug>.bundle` for one safe path component, never twice); two write targets that
coincide or nest, where a target is a bundle's restore directory or the `extras` directory the
files block unzips into. Names and targets are compared as APFS compares them, ignoring case and
normalization form, since two spellings it reads as one folder restore into one. Also refused: a worktree name that is not one ref
component; a head that is not a full object id; a machine that is not a valid label; or a
re-arm command that could end or hide a line in its fence.

## Module map

The modules live in `src/`, private to this tool: nothing outside this folder imports them.

| Module | Responsibility |
| --- | --- |
| `src/backup_proc.py` | the one bounded subprocess seam, and every command's timeout |
| `src/backup_fs.py` | atomic writes, hashing, bounded reads, the run lock and its probe, the regular-file reader beneath a root |
| `src/backup_model.py` | the frozen records and their constants |
| `src/backup_machine.py` | this machine's identity: the label a host name gives, and the file that keeps it |
| `src/backup_json.py` | strict JSON readers, the record envelope, the safe-name rule |
| `src/backup_manifest.py` | the `MANIFEST.json` writer and total parser |
| `src/backup_ledger.py` | the ledger, the last-run record, and the per-destination state directory |
| `src/backup_config.py` | loading and validating the configuration |
| `src/backup_roots.py` | the primary checkout through git's shared admin dir, the Claude home, and the linked-worktree test |
| `src/backup_checkout.py` | the CLI's `--repo` and `--destination` resolved, the checkout and configuration they choose, and each refusal |
| `src/backup_drift.py` | the ignored-path listing, and the pattern rules every list follows |
| `src/backup_bundle.py` | one bundle with its refs, worktree heads and stash entries; dirty worktrees |
| `src/backup_zip.py` | the whitelisted `extras.zip` |
| `src/backup_build.py` | the memory step and build orchestration |
| `src/backup_verify.py` | verification by mirror clone and `fsck`; the archive check; file hashes |
| `src/backup_seal.py` | verification scratch, then `RESTORE.md` and `MANIFEST.json` |
| `src/backup_promote.py` | promote, the cross-volume fallback, ledger-authority prune, the staging sweep |
| `src/backup_restore_doc.py` | rendering `RESTORE.md`, and extracting its `sh` blocks |
| `src/backup_restore_notes.py` | `RESTORE.md`'s prose-only second-machine note |
| `src/backup_rearm.py` | the re-arm step: configured commands or the derived install line, and the rule each command meets |
| `src/backup_restore_stash.py` | the restore block's commands storing each stash entry back |
| `src/backup_notify.py` | the `osascript` notification |
| `src/backup_run.py` | the run pipeline |
| `src/backup_staging.py` | making the run's staging folder fresh |
| `src/backup_icloud.py` | per-file upload state through Foundation |
| `src/backup_status.py` | status facts and the verdict |
| `src/backup_launchd.py` | install, uninstall and the agent's state |
| `src/backup_preflight.py` | the CLI's refusals about the tool's own directory: a run in progress, and a directory that is not this user's alone, which run and install then make owner-only |
| `src/backup_restore_checks.py` | the refusals keeping `RESTORE.md`'s manifest values from changing what runs or where it writes |
| `src/backup_agent_args.py` | what an installed agent's plist says it runs, read back for uninstall's report |

`tests/test_companion_doc.py` fails when this map and `src/` disagree.

## Known limits

- Snapshot names record local wall-clock time, so across a daylight-saving fall-back an older
  snapshot can outrank a newer one in retention. Every deletion is still proven first.
- A leading `**` in `extras` walks the whole workspace tree, `.git`, dependency folders and
  worktrees included, and costs seconds on every run. Anchor `extras` entries instead.
- Destination spellings that differ only in case key separate ledgers on case-insensitive APFS.
- `git worktree list -z` needs git 2.36 or later. An older git fails the dirty-worktree listing,
  which makes every snapshot incomplete.
- `O_NOFOLLOW_ANY` is macOS's. An interpreter without it guards only a member's last path
  component, so a directory swapped for a symlink between the archive walk and the read is
  followed.
- A failed run followed on the same day by an `--if-due` skip, after an earlier complete snapshot
  that day, leaves `status` nothing to see: the skip replaces the record. That day is still
  covered, and the failure was notified.
- Whether a notification is displayed depends on per-app notification permission the tool cannot
  read. `status` is the guarantee, not the notification.
