"""The kernel32 calls behind the job seam, driven through real ctypes marshaling on any host.

Each fake kernel32 function is a C function pointer built from bounded_run's own signature table,
so the adapter's arguments cross the same argtype conversion they cross on Windows. A wrong
argtype, a structure passed where a handle belongs, or a mis-sized structure fails here -- not
only on a Windows host these tests cannot assume.

The expected constants below are restated from the Windows documentation on purpose: they are
the independent reference the module's own constants are checked against.
"""

from __future__ import annotations

import ctypes
import sys
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounded_run

_JOB, _PROCESS, _SNAPSHOT, _THREAD = 0x100, 0x200, 0x300, 0x400
_CHILD_PID = 4242
_OTHER_PID = 999

#: What each call returns on success. Handles are arbitrary, BOOL calls return TRUE, and
#: ResumeThread returns the previous suspend count -- 1 for a thread created suspended.
_SUCCESS: dict[str, int] = {
    "CreateJobObjectW": _JOB,
    "SetInformationJobObject": 1,
    "OpenProcess": _PROCESS,
    "AssignProcessToJobObject": 1,
    "CreateToolhelp32Snapshot": _SNAPSHOT,
    "Thread32First": 1,
    "Thread32Next": 1,
    "OpenThread": _THREAD,
    "ResumeThread": 1,
    "TerminateJobObject": 1,
    "CloseHandle": 1,
}

#: THREADENTRY32 is 28 bytes on every Windows; the extended limits are 144 on x64, 112 on x86.
_THREAD_ENTRY_SIZE = 28
_EXTENDED_LIMITS_SIZE = {8: 144, 4: 112}[ctypes.sizeof(ctypes.c_void_p)]

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_PROCESS_SET_QUOTA_AND_TERMINATE = 0x0101
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_RESUME_THREAD_FAILED = 0xFFFFFFFF
_TASKKILL_FORCED_EXIT_CODE = 1

_FAKE_WINERROR = 5

#: What the last error holds after Thread32First/Next return FALSE at the real end of a walk.
_ERROR_NO_MORE_FILES = 18


class _FakeKernel32:
    """kernel32 stand-in whose functions are C function pointers from ``_KERNEL32_SIGNATURES``.

    Records every call as ``(name, *args)``, plus each read of the last error, so tests assert
    the ORDER. Nothing is asserted inside a callback: ctypes swallows an exception raised there.
    """

    def __init__(
        self,
        results: Mapping[str, int] | None = None,
        threads: Sequence[tuple[int, int]] = (),
        walk_error: int = _ERROR_NO_MORE_FILES,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._results = {**_SUCCESS, **(results or {})}
        self._threads = list(threads)
        self._walk_error = walk_error
        self._cursor = 0
        impls: dict[str, Callable[..., int]] = {
            name: self._returning(name) for name in _SUCCESS
        }
        impls["SetInformationJobObject"] = self._set_information
        impls["Thread32First"] = self._thread_first
        impls["Thread32Next"] = self._thread_next
        self.dll = SimpleNamespace(
            **{
                name: ctypes.CFUNCTYPE(restype, *argtypes)(impls[name])
                for name, restype, argtypes in bounded_run._KERNEL32_SIGNATURES
            }
        )

    def _returning(self, name: str) -> Callable[..., int]:
        def call(*args: object) -> int:
            self.calls.append((name, *args))
            return self._results[name]

        return call

    def _set_information(
        self,
        job: int,
        info_class: int,
        limits: ctypes._Pointer[bounded_run._JobExtendedLimits],
        size: int,
    ) -> int:
        flags = limits.contents.BasicLimitInformation.LimitFlags
        self.calls.append(("SetInformationJobObject", job, info_class, flags, size))
        return self._results["SetInformationJobObject"]

    def _thread_first(
        self, snapshot: int, entry: ctypes._Pointer[bounded_run._ThreadEntry32]
    ) -> int:
        self.calls.append(("Thread32First", snapshot, entry.contents.dwSize))
        self._cursor = 0
        return self._emit_thread(entry)

    def _thread_next(
        self, snapshot: int, entry: ctypes._Pointer[bounded_run._ThreadEntry32]
    ) -> int:
        self.calls.append(("Thread32Next", snapshot))
        return self._emit_thread(entry)

    def _emit_thread(self, entry: ctypes._Pointer[bounded_run._ThreadEntry32]) -> int:
        if self._cursor == len(self._threads):
            return 0
        thread_id, owner = self._threads[self._cursor]
        entry.contents.th32ThreadID = thread_id
        entry.contents.th32OwnerProcessID = owner
        self._cursor += 1
        return 1

    def last_error(self) -> OSError:
        self.calls.append(("last_error",))
        error = OSError(_FAKE_WINERROR, "fake last error")
        # Set on every host: only a Windows OSError carries winerror of its own.
        setattr(error, "winerror", self._walk_error)
        return error

    def api(self) -> bounded_run._KernelJobApi:
        # A namespace of C function pointers stands in for the loaded CDLL.
        return bounded_run._KernelJobApi(self.dll, self.last_error)  # type: ignore[arg-type]

    def names(self) -> list[object]:
        return [call[0] for call in self.calls]


class LayoutTests(unittest.TestCase):
    def test_structures_have_the_documented_windows_sizes(self) -> None:
        self.assertEqual(ctypes.sizeof(bounded_run._ThreadEntry32), _THREAD_ENTRY_SIZE)
        self.assertEqual(
            ctypes.sizeof(bounded_run._JobExtendedLimits), _EXTENDED_LIMITS_SIZE
        )


class CreateJobTests(unittest.TestCase):
    def test_allows_explicit_breakaway_and_returns_the_job(self) -> None:
        kernel = _FakeKernel32()
        self.assertEqual(kernel.api().create_job(), _JOB)
        self.assertEqual(
            kernel.calls,
            [
                ("CreateJobObjectW", None, None),
                (
                    "SetInformationJobObject",
                    _JOB,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    _JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                    _EXTENDED_LIMITS_SIZE,
                ),
            ],
        )

    def test_a_null_job_raises_the_last_error(self) -> None:
        kernel = _FakeKernel32({"CreateJobObjectW": 0})
        with self.assertRaisesRegex(OSError, "fake last error"):
            kernel.api().create_job()
        self.assertEqual(kernel.names(), ["CreateJobObjectW", "last_error"])

    def test_a_refused_limit_reads_the_error_before_closing_the_job(self) -> None:
        # The close is a kernel32 call too: reading the error after it would report the close.
        kernel = _FakeKernel32({"SetInformationJobObject": 0})
        with self.assertRaisesRegex(OSError, "fake last error"):
            kernel.api().create_job()
        self.assertEqual(
            kernel.names(),
            [
                "CreateJobObjectW",
                "SetInformationJobObject",
                "last_error",
                "CloseHandle",
            ],
        )
        self.assertEqual(kernel.calls[-1], ("CloseHandle", _JOB))


class AssignTests(unittest.TestCase):
    def test_opens_the_child_with_the_rights_assignment_needs_and_closes_it(
        self,
    ) -> None:
        kernel = _FakeKernel32()
        kernel.api().assign(_JOB, _CHILD_PID)
        self.assertEqual(
            kernel.calls,
            [
                ("OpenProcess", _PROCESS_SET_QUOTA_AND_TERMINATE, 0, _CHILD_PID),
                ("AssignProcessToJobObject", _JOB, _PROCESS),
                ("CloseHandle", _PROCESS),
            ],
        )

    def test_a_failed_call_raises_and_closes_only_what_was_opened(self) -> None:
        expected = {
            "OpenProcess": ["OpenProcess", "last_error"],
            "AssignProcessToJobObject": [
                "OpenProcess",
                "AssignProcessToJobObject",
                "last_error",
                "CloseHandle",
            ],
        }
        for failing, names in expected.items():
            with self.subTest(failing=failing):
                kernel = _FakeKernel32({failing: 0})
                with self.assertRaisesRegex(OSError, "fake last error"):
                    kernel.api().assign(_JOB, _CHILD_PID)
                self.assertEqual(kernel.names(), names)


class ResumeTests(unittest.TestCase):
    def test_resumes_only_the_childs_threads_and_closes_every_handle(self) -> None:
        kernel = _FakeKernel32(threads=[(11, _OTHER_PID), (12, _CHILD_PID)])
        kernel.api().resume(_CHILD_PID)
        self.assertEqual(
            kernel.calls,
            [
                ("CreateToolhelp32Snapshot", _TH32CS_SNAPTHREAD, 0),
                ("Thread32First", _SNAPSHOT, _THREAD_ENTRY_SIZE),
                ("Thread32Next", _SNAPSHOT),
                ("Thread32Next", _SNAPSHOT),
                ("last_error",),
                ("CloseHandle", _SNAPSHOT),
                ("OpenThread", _THREAD_SUSPEND_RESUME, 0, 12),
                ("ResumeThread", _THREAD),
                ("CloseHandle", _THREAD),
            ],
        )

    def test_each_failure_raises_and_closes_only_what_was_opened(self) -> None:
        walk = [
            "CreateToolhelp32Snapshot",
            "Thread32First",
            "Thread32Next",
            "last_error",
            "CloseHandle",
        ]
        fake_error, no_thread = "fake last error", "no thread found"
        cases: dict[str, tuple[dict[str, int], int, list[str], str]] = {
            "no snapshot": (
                {"CreateToolhelp32Snapshot": bounded_run._INVALID_HANDLE_VALUE},
                _CHILD_PID,
                ["CreateToolhelp32Snapshot", "last_error"],
                fake_error,
            ),
            "no thread of the child": ({}, _OTHER_PID, walk, no_thread),
            "thread not opened": (
                {"OpenThread": 0},
                _CHILD_PID,
                [*walk, "OpenThread", "last_error"],
                fake_error,
            ),
            "thread not resumed": (
                {"ResumeThread": _RESUME_THREAD_FAILED},
                _CHILD_PID,
                [*walk, "OpenThread", "ResumeThread", "last_error", "CloseHandle"],
                fake_error,
            ),
        }
        for case, (results, owner, names, message) in cases.items():
            with self.subTest(case=case):
                kernel = _FakeKernel32(results, threads=[(12, owner)])
                with self.assertRaisesRegex(OSError, message):
                    kernel.api().resume(_CHILD_PID)
                self.assertEqual(kernel.names(), names)

    def test_a_walk_that_fails_midway_raises_its_own_error(self) -> None:
        # FALSE from Thread32Next ends a walk both at its end AND on a failure; only the last
        # error tells them apart. Read as the end, this failure would resume a partial list or
        # surface as "no thread found", hiding the real cause on a first Windows run.
        kernel = _FakeKernel32(threads=[(12, _CHILD_PID)], walk_error=_FAKE_WINERROR)
        with self.assertRaisesRegex(OSError, "fake last error"):
            kernel.api().resume(_CHILD_PID)
        self.assertEqual(
            kernel.names(),
            [
                "CreateToolhelp32Snapshot",
                "Thread32First",
                "Thread32Next",
                "last_error",
                "CloseHandle",
            ],
        )


class TerminateAndCloseTests(unittest.TestCase):
    def test_terminate_kills_the_job_with_taskkills_exit_code(self) -> None:
        kernel = _FakeKernel32()
        kernel.api().terminate(_JOB)
        self.assertEqual(
            kernel.calls, [("TerminateJobObject", _JOB, _TASKKILL_FORCED_EXIT_CODE)]
        )

    def test_a_refused_terminate_raises_the_last_error(self) -> None:
        kernel = _FakeKernel32({"TerminateJobObject": 0})
        with self.assertRaisesRegex(OSError, "fake last error"):
            kernel.api().terminate(_JOB)
        self.assertEqual(kernel.names(), ["TerminateJobObject", "last_error"])

    def test_close_closes_the_job_handle(self) -> None:
        kernel = _FakeKernel32()
        kernel.api().close(_JOB)
        self.assertEqual(kernel.calls, [("CloseHandle", _JOB)])


class WindowsBindingTests(unittest.TestCase):
    def test_the_last_error_reader_returns_an_oserror(self) -> None:
        # The reader runs only when a kernel32 call fails, which no live Windows run provokes on
        # demand, so without this its line is a Windows-only coverage miss.
        if sys.platform != "win32":
            # skipTest never returns, so mypy's POSIX passes stop here and the win32-only
            # attribute below is checked on the win32 pass alone.
            self.skipTest("kernel32's last error exists only on Windows")
        self.assertIsInstance(bounded_run._last_windows_error(), OSError)


if __name__ == "__main__":
    unittest.main()
