# launchd_agent.py

Install, uninstall and read a user LaunchAgent, each step confirmed by launchd's own answer
rather than by a launchctl exit code. An import-only library: no CLI and no output.

## Why it exists

Every job that runs under launchd repeats one sequence: write the plist, boot out any loaded
copy, wait until launchd says it is gone, bootstrap the new one, and wait until launchd says it
is loaded. This module is that sequence written once, so the tools that install an agent cannot
drift apart on how. In this repository the workspace backup installs, uninstalls and reads its
daily agent through it (`git_workspace_backup/src/backup_launchd.py`).

It decides nothing about what an agent runs. The caller hands it a label, the plist's path and
the plist's bytes, and builds those bytes however it likes; `build_keepalive_plist` is there
for the common always-running case.

## Public surface

| Name | What it is |
| --- | --- |
| `LAUNCHCTL` | `/bin/launchctl`, by absolute path so a minimal PATH cannot shadow it |
| `PRINT_ABSENT_RC` | 113, `launchctl print`'s exit for a service its domain does not hold |
| `DEFAULT_DEADLINE_S`, `POLL_S` | how long one wait may take, and the pause between its reads |
| `LAUNCHCTL_TIMEOUT_S` | the bound on any one launchctl call |
| `AgentState` | `LOADED`, `ABSENT` or `UNKNOWN` |
| `LaunchdError` | launchd did not settle in the wanted state, or answered unknown |
| `Launchctl` | the seam: `run(argv, timeout)`, the `uid`, `clock` and `sleep` |
| `service_target(uid, label)` | `gui/<uid>/<label>`; `ValueError` for a label `build_keepalive_plist` would refuse |
| `agent_state(ctl, label)` | one `launchctl print`, read as an `AgentState` |
| `ServicePrint` | one `launchctl print`: its state, its stdout when loaded, and why when unknown |
| `print_service(ctl, label)` | one `launchctl print`, kept whole |
| `wait_for(ctl, label, wanted, deadline_s)` | poll until launchd reports `wanted` |
| `install_agent(ctl, label=, plist_path=, plist=)` | write, replace and confirm loaded |
| `uninstall_agent(ctl, label=, plist_path=)` | unload, confirm absent, then remove the plist |
| `stop_agent(ctl, label=)` | unload and confirm absent, keeping the plist |
| `kickstart_argv(uid, label)` | the command that starts the agent now; never run here |
| `build_keepalive_plist(...)` | the plist of an agent launchd keeps running, under a restart policy |
| `KEEP_ALIVE_ALWAYS` · `RESTART_AFTER_FAILURE` | the two restart policies: after every exit, or only after a failed one |
| `real_launchctl()` | the real host seam: `run_bounded`, `os.getuid()` and real time |

## The sequence, and why each wait exists

`launchctl print` has three answers, not two. Exit 0 is loaded, 113 is absent, and anything
else (a timeout and a launchctl that cannot run included) is unknown. An unknown answer ends a
wait at once with `LaunchdError` naming the exit code and stderr's first line. It is never
taken for either settled answer, and polling it would only hide that exit code behind the
deadline. A wait that reaches its deadline says which state it still read:
`<label>: still loaded after 30 s`.

`install_agent`:

1. Writes the plist atomically with mode 0644 (`atomic_write.replace_bytes`), creating its
   folder if absent. It is written before anything is unloaded, so no failure leaves the old
   agent gone with no plist to retry from.
2. Runs `bootout` on the target, ignoring its exit: a first install has nothing to boot out.
3. Waits for absent. `bootout` is not instantaneous, and a bootstrap of the same label while
   the old instance is still tearing down fails or races it.
4. Runs `bootstrap gui/<uid> <plist_path>`, ignoring its exit too: one racing the old
   instance's teardown can exit non-zero (an I/O error) while launchd goes on to load the
   agent.
5. Waits for loaded. This read-back is the confirmation; the exit codes above are not.
   When the agent never loads and bootstrap had exited non-zero, the error adds
   `launchctl bootstrap failed (exit N): <first line of stderr, else stdout>`, so a
   malformed plist or a bad program path says why rather than only `still absent`.

`uninstall_agent` runs `bootout`, waits for absent, and only then removes the plist. While
the agent is still loaded, or its state unknown, the plist is what a retry needs, so a failed
wait keeps it. An absent plist is not an error.

`stop_agent` is `uninstall_agent` without the last step: it runs `bootout` and waits for
absent, and leaves the plist alone. It is for a caller that replaces what the agent runs and
then loads it again with `install_agent`: if anything fails in between, the plist is still
there, so the agent still reads as installed rather than as never installed.

A timeout of `bootout` or `bootstrap` is ignored for the same reason as their exits: the wait
after each reads the real state.

**A launchctl that cannot be run at all** is a `LaunchdError` too. The `print` read-back turns
it into an unknown answer, and `bootout` and `bootstrap` raise `LaunchdError` naming the label
and the verb (`<label>: could not run launchctl <verb>: <cause>`, so a caller installing
several agents can tell which failed), chained (`from`) to the `OSError`, since no read-back
can follow them. So `install_agent` raises it from its `bootout`, after the new plist is
written, and `uninstall_agent` and `stop_agent` raise it before anything is removed. Either
way, re-running recovers once launchctl runs.

Two failures escape as `OSError` rather than `LaunchdError`, and both concern the plist file,
not launchd:

- **A plist that cannot be written.** `install_agent` writes it before unloading anything, so
  the old agent is left as it was.
- **A plist that cannot be removed.** `uninstall_agent` unlinks the plist only after launchd
  confirmed the agent absent, so an unlink failure surfaces as `OSError` with the agent
  already unloaded; only the stale plist remains.

`build_keepalive_plist` sets `RunAtLoad` and `KeepAlive`, a `ThrottleInterval` of 10 seconds
so a crashing agent is not restarted in a tight loop, `ProcessType` `Background`, the two log
paths, and the agent's whole `PATH`, since launchd's own is minimal. launchd opens the log
paths at spawn and does not create their folder, so the caller creates it. It refuses a label
that `os_schedule_gen.launchd_plist_path` would refuse, by calling it, so a label cannot name
a file outside `~/Library/LaunchAgents`. `service_target` applies the same rule, and every
verb builds its target through it, so a label with a `/`, which would name another service
target, or a leading `-`, which `launchctl` would read as an option, is refused with
`ValueError` before `launchctl` runs, and before `install_agent` writes the plist.

Its `keep_alive` argument is the restart policy, `KEEP_ALIVE_ALWAYS` (`KeepAlive` `true`) by
default: launchd restarts the agent after every exit, a clean one included, which suits a
server that should never stop. `RESTART_AFTER_FAILURE` writes the dictionary
`{SuccessfulExit: false}`: launchd restarts the agent only after a nonzero exit or a crash,
and leaves it loaded but stopped after exit 0, until its next load. Under plain `true`, an
agent that cannot recover from a failure (a folder it may not use, say) is restarted every
throttle interval forever, each start appending to its error log, which launchd never
rotates; under `RESTART_AFTER_FAILURE` it ends that loop by exiting 0.

## Using it

```python
from pathlib import Path

import launchd_agent

ctl = launchd_agent.real_launchctl()
plist = launchd_agent.build_keepalive_plist(
    label="local.example.serve",
    program_arguments=["<python>", "<script>", "--mode", "serve"],
    working_directory="<working-folder>",
    log_paths=("<logs>/out.log", "<logs>/err.log"),
    path_value="/usr/bin:/bin",
)
plist_path = Path.home() / "Library" / "LaunchAgents" / "local.example.serve.plist"
launchd_agent.install_agent(
    ctl, label="local.example.serve", plist_path=plist_path, plist=plist
)
```

A caller puts this folder on `sys.path` from one fixed place derived from its own file, as
`git_workspace_backup/src/backup_launchd.py` does, appending it so it can shadow neither the
caller's own modules nor the standard library (`stdio_utf8/stdio_utf8.md`, "How this
project's tools import it").

## How it finds its own libraries

It imports the libraries `_LIBRARIES` names, and finds each by a bounded upward search from
its own file (`_nearest_library`): it looks for each library in its own folder and its
parent, trying each as `<ancestor>/<name>`, and appends the nearest folder holding
`<name>.py` to `sys.path`.

- **The parent holds every library.** Here each library is a folder directly under the
  repository root, beside this module's own folder, so the parent is where each one is found.
- **Two ancestors, and no more** (`_ANCESTORS_SEARCHED`): the module's own folder and its
  parent. Searching every ancestor would also reach the folders above the repository's
  root, where a same-named folder beside a clone would be imported in place of the real
  library.
- **Appended, not prepended,** so the standard library and the caller's own modules stay
  ahead of anything the search adds.

## Tests

The suites in `tests/` (`test_launchd_reading.py`, `test_launchd_lifecycle.py`, run through the
`test_launchd_agent.py` aggregator) script launchd with a fake whose `run` answers each expected
argv from a queue and fails the test on any other, so each test pins the order of the calls
and that nothing else ran. A fake clock advances only when the library sleeps. Temporary
folders stand in for `~/Library/LaunchAgents`, so the suite never touches the real launchd.

One live test installs `local.launchd-agent.selftest` running `/usr/bin/true` from a temporary
folder, reads it loaded, uninstalls it and reads it absent. It mutates the real launchd, so it
is skipped unless the owner opts in with `LAUNCHD_AGENT_LIVE=1`, on macOS:

```bash
cd tests && LAUNCHD_AGENT_LIVE=1 python -m unittest test_launchd_lifecycle.LiveLaunchdTest -v
```
