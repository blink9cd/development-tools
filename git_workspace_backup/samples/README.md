# git_workspace_backup hand-verification sample

Two inputs, and the one transform they can show without touching this machine. A real snapshot
needs git, the configured checkout, the home directory's Claude files and, for an iCloud
destination, iCloud Drive itself; `git_workspace_backup.md` describes that path, and nothing
here runs it.

| File | What it is |
| --- | --- |
| `expected-manifest.json` | The `MANIFEST.json` a run would write for a checkout configured with `sample-config.toml`, on a machine whose identity is the obvious fixture label `fixture-mac`, with obviously fake hashes, sizes and object ids. It is byte for byte what the tool's own writer emits, so its key names, order and layout are a real manifest's: compare those, never the hashes. |
| `sample-config.toml` | A minimal `[tool.workspace_backup]` table: one Claude-home file whitelisted, no workspace extras and no home files, the memory pre-step off, two example re-arm commands and an example setup document. |

## Render the RESTORE.md this snapshot would carry

Run from the `git_workspace_backup` folder with Python 3.12 or later. It reads
the sample manifest with the tool's own parser, takes the re-arm commands and the setup
document from `sample-config.toml`, and prints the restore document to stdout. It runs no
command, and reads and writes nothing else.

```bash
python3.12 - <<'EOF'
import git_workspace_backup  # its bootstrap puts src/ on sys.path; it builds no host
import tomllib
from pathlib import Path
from backup_manifest import parse_manifest
from backup_rearm import rearm_for
from backup_restore_doc import render_restore_doc

config = tomllib.loads(Path("samples/sample-config.toml").read_text(encoding="utf-8"))
table = config["tool"]["workspace_backup"]
rearm = rearm_for(table["rearm_commands"], Path.cwd(), setup_doc=table["setup_doc"])
manifest = parse_manifest(Path("samples/expected-manifest.json").read_bytes())
print("not a manifest this tool reads" if manifest is None else render_restore_doc(manifest, rearm=rearm))
EOF
```

What to look for:

- The opening line, naming the machine `fixture-mac` beside `Created`, then `complete: yes`,
  from the manifest's `machine` and `complete`.
- One `sh` block for each bundle, then one for the files.
- Every `sh` block first stops unless `RESTORE_ROOT` and `SNAPSHOT` are absolute, and the first
  block also unless `RESTORE_ROOT` is an existing, readable, empty directory.
- The `workspace.bundle` block checks out without naming a commit, then stops unless `HEAD` is
  the recorded head.
- The `workspace.bundle` block fetches the worktree head `example-worktree` into
  `refs/backup/worktrees/<name>/HEAD`, the ref verification proved.
- The `workspace.bundle` block then stores both stash entries back with `git stash store`, the
  older first, and stops unless `git stash list` holds the recorded commits in that order.
- A `Setting up a second machine` note after the files block, naming the one Claude file the
  sample's archive holds, `settings.json` under `claude/`, as one that can carry the machine's
  own facts;
  saying any memory corpus or project folder is that machine's own too, since memory holds
  machine facts; and that a second machine takes its own machine identity, kept in
  `~/.workspace_backup/machine`.
- The re-arm step last: a `text` fence that changes into the restored workspace, then lists the
  two `rearm_commands` from `sample-config.toml`, in order, after prose telling you to rebuild
  the virtualenvs and prerequisites as the sample's `setup_doc` describes, named in backticks.

`tests/test_samples.py` fails when the sample manifest stops being what the writer emits, when
the rendered document loses what this list names, or when `sample-config.toml` stops passing
validation, naming exactly the manifest's archive members, or listing the re-arm commands the
document shows.
