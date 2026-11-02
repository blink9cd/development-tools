# atomic_write

One atomic file replacement, for every tool that writes a file something else may
read. A library, not a CLI: there is nothing to run.

## What it is for

A file another process reads while its writer runs (a state file a later run
loads, a document a person or a crawler opens, a configuration a service reads at
start) is exposed half-written by a truncate-and-refill, which surfaces as an
intermittent failure that never reproduces on its own.

Writing beside the target and renaming closes that window: the old file stays
whole until one indivisible step swaps in the new one.

## Who calls it

In this repository, `git_workspace_backup` writes all of its state files and each
snapshot's documents through it, and the property list of the agent it installs.

## Why it exists rather than being re-typed

The recipe was once written out by hand in seven tools, and the copies had drifted
apart on every axis that matters:

| Axis                      | What the copies did                                         |
| ------------------------- | ----------------------------------------------------------- |
| `fsync` before the rename | two did, five did not                                       |
| the published mode        | three set one, four renamed `mkstemp`'s 0600 into place     |
| cleanup on failure        | five removed the staging file, one did not remove it at all |
| line endings              | some pinned LF, some took the host's                        |

None of those differences was a decision. Each was whatever the tool in front
of the author happened to do that day, and the one with no cleanup stranded a
`.tmp` beside committed data whenever a replace failed.

Every caller shares `STAGING_SUFFIX`. What identifies a leftover is the target's
own name, which sits in the middle of the staging name, so it still says what it
was going to _be_, just not who was writing it. A per-caller suffix would put back
into this module exactly the per-tool variation it exists to remove, and the
cleanup here is wide enough (`BaseException`, so an interrupt too) that a leftover
needs a `SIGKILL` rather than a Ctrl-C.

A tool that has to recognize a staging file derives the pattern from
`staging_name` rather than spelling it again. Passing a glob wildcard for each
part, `staging_name("*", "*")`, gives a pattern that matches every staging file,
and `staging_name(target, "*")` gives one that matches a single target's.
The pattern is published for exactly those callers: a tool that keeps staging
files out of a snapshot or a sweep derives its ignore line from here, so renaming
the staging file here moves every such pattern with it instead of leaving one
that silently stops matching.

## Using it

Put its folder on `sys.path` from one fixed place derived from your own file, as
`git_workspace_backup/src/backup_fs.py` does, appending it so it can shadow neither
your own modules nor the standard library (`stdio_utf8/stdio_utf8.md`, "How this
project's tools import it"). Then call `replace_text` or `replace_bytes`. Both take
the whole new contents; neither appends.

The `mode` keyword defaults to `SERVED_MODE`, which is what a generated file
wants. Pass a narrower one for a file that is not served, or `PRESERVE` to keep
whatever the target already carries.

`PRESERVE` is the policy for a file whose mode is not this tool's to decide: a
rewrite keeps the bits it found, and a file that does not exist yet gets what a
plain write would have given it, the creation mode filtered by the caller's
umask. That is what a plain `Path.write_text` does by construction, so it is
what a writer replacing a plain write needs. Forcing such a file onto a flat mode
would strip an execute bit from a file that had one and ignore the umask on a
file that had none.

## A last check before the rename

Some writers must ask, as late as possible, whether the replace should still
happen: a writer that re-reads the target and compares its digest with the one
taken when it read it refuses a concurrent writer's edit rather than overwriting
it. Such a writer takes the `before_rename` keyword.

The check is handed the staging file once it holds its final bytes, carries its
final mode and has been fsynced, and it runs immediately before the rename. That
placement is the point. Anything still left to do after the check widens the
window it cannot cover, so the only step after it is the rename itself. A check
run earlier would leave the write, the mode and the fsync inside that window.

It answers with a boolean. False vetoes the replace: the staging file is
removed, the target is left as it was, and the call returns False. An exception
the check raises does the same and then propagates, so a caller that wants to
report _why_ it refused can raise its own error. Without a check, or when the
check allows it, the call returns True.

This narrows the window rather than closing it. The check and the rename are two
steps, and a writer that lands between them is still lost. Closing it needs a
lock.

## What it guarantees, and why each part is not optional

- **Staged in the target's own directory.** `os.replace` is atomic only within
  one filesystem; across a mount boundary it quietly becomes a copy, which is
  the very window this closes.
- **`fsync` before the rename.** The rename's atomicity says nothing about
  durability: a crash afterward could otherwise publish an empty file under
  the target's name.
- **The mode set on the descriptor.** `mkstemp` creates at 0600 and the rename
  carries that through, so without it a published file is readable by the
  account that wrote it alone.
  Setting it on the descriptor rather than afterward leaves no window in which
  the finished bytes sit on disk under the wrong mode. Windows has no POSIX mode
  bits, so none is set there; `os.fchmod` does not exist on it below Python
  3.13, and this project's floor is 3.12.
- **Any failure removes the staging file and re-raises**, including a
  `KeyboardInterrupt`: an operator who interrupts a build is exactly the person
  who would otherwise find the leftovers.

## A note for whoever tests a mode

A test that asserts the published mode must fail when the `fchmod` is deleted,
and whether it does depends on how the staging file was created. This module
stages through a path-based `os.open`, because `PRESERVE` needs a new file
to land where a plain write would have put it, and `mkstemp` takes no mode. A
path-based open starts from 0666 and lets the umask reduce it, which under the
usual 0022 lands on 0644: the right answer for the wrong reason.

So the strict umask in `test_atomic_write.py` is load-bearing rather than
belt-and-braces: measured, the mutant with the `fchmod` deleted passes at 0022
and fails only at 0077. Do not relax it, and do not assert a published mode
from a scratch directory that has not set one.

One property here is deliberately not covered, because no filesystem test
can see it: the mode is set on the descriptor _before_ the bytes are written,
so no window exists in which finished content sits under the wrong mode. Moving
the `fchmod` after the write leaves the same end state and survives the suite.
It is held by the comment at the call, not by an assertion.

## Not in scope

A writer that stages through a temporary path for a reason other than atomicity
is deliberately not a caller:

- one that stages its output and then decides, after validating it, whether to
  install it at all, which is a stage-then-validate-then-install flow, not
  "write this content";
- one that stages a file to verify something about it, such as that it carries
  no identity metadata, before moving it, where the staging exists for the
  verification, not for the atomicity;
- one that writes every file of a multi-file change to a staging file first,
  proves the set, and only then replaces the targets in a crash-safe order. That
  is a multi-file stage-then-verify-then-install, not one file's replace.
