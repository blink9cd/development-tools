"""The kernel32 layouts and signatures ``bounded_run``'s Windows job containment marshals.

Private to ``bounded_run``: it sits in that tool's own ``src/`` folder, so nothing outside the
tool folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

Why these sit apart
-------------------
They keep ``bounded_run.py`` to a reviewable length. These are transcriptions of the Win32
headers (structure layouts and the function table) with no behavior of their own; the job API that
calls through them, and the containment that drives it, stay in ``bounded_run.py`` beside the
spawn they are part of. ``bounded_run`` re-exports each name, so its tests read them where they
always have.

Entry point assumptions
-----------------------
Imported by ``bounded_run`` alone. Standard library only, and importable on every host: nothing
here loads a library.
"""

from __future__ import annotations

import ctypes


class _ThreadEntry32(ctypes.Structure):
    """``THREADENTRY32`` (tlhelp32.h). Fixed-width fields: c_ulong is 8 bytes on 64-bit POSIX."""

    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    )


class _JobBasicLimits(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION`` (winnt.h); SIZE_T and ULONG_PTR are pointer-sized."""

    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _JobExtendedLimits(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` (winnt.h), the form BREAKAWAY_OK is set in."""

    _fields_ = (
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),  # IO_COUNTERS: six ULONGLONGs, reserved here
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


# Win32 type names, so the table below reads like the headers it transcribes.
_HANDLE, _DWORD, _BOOL = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
_LIMITS_PTR = ctypes.POINTER(_JobExtendedLimits)
_ENTRY_PTR = ctypes.POINTER(_ThreadEntry32)

#: Every kernel32 function the job API calls, as (name, restype, argtypes). One table serves both
#: the Windows binding and the tests' marshaled fakes, so the two cannot drift apart. The restype
#: is not decoration: ctypes defaults to a C int, which truncates a HANDLE on 64-bit Windows.
_KERNEL32_SIGNATURES = (
    ("CreateJobObjectW", _HANDLE, (ctypes.c_void_p, ctypes.c_wchar_p)),
    ("SetInformationJobObject", _BOOL, (_HANDLE, ctypes.c_int, _LIMITS_PTR, _DWORD)),
    ("OpenProcess", _HANDLE, (_DWORD, _BOOL, _DWORD)),
    ("AssignProcessToJobObject", _BOOL, (_HANDLE, _HANDLE)),
    ("CreateToolhelp32Snapshot", _HANDLE, (_DWORD, _DWORD)),
    ("Thread32First", _BOOL, (_HANDLE, _ENTRY_PTR)),
    ("Thread32Next", _BOOL, (_HANDLE, _ENTRY_PTR)),
    ("OpenThread", _HANDLE, (_DWORD, _BOOL, _DWORD)),
    ("ResumeThread", _DWORD, (_HANDLE,)),
    ("TerminateJobObject", _BOOL, (_HANDLE, ctypes.c_uint32)),
    ("CloseHandle", _BOOL, (_HANDLE,)),
)
