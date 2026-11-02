#!/usr/bin/env python3
"""Fixtures shared by more than one of os_schedule_gen's grouped suites.

Deliberately not named ``test_*``: it holds no test cases, and the aggregator's
``discover`` pattern must not try to load it as one. Anything used by a single suite
stays in that suite; only what genuinely crosses suite boundaries lives here.

Nothing here touches the filesystem: every subject in this library is a pure
function, so its fixtures are plain data.
"""

from __future__ import annotations

from datetime import date

# One row per weekday: the token each backend's schedule form must carry, plus a real
# date falling on that weekday. Authored independently of the production maps, and every
# row is cross-checked below against datetime's own reckoning (_DAYS[ref.weekday()] and
# ref.isoweekday()), an oracle neither this table nor the production code derives from.
# That is what makes this a real check rather than a restatement: an off-by-one in any
# single map is caught instead of being mirrored here.
SCHEDULE_FORMS: tuple[tuple[str, date, str, str, int], ...] = (
    # weekday, reference date, XML element, systemd OnCalendar day, launchd/cron dow
    ("Sunday", date(2026, 8, 16), "<Sunday />", "Sun", 0),
    ("Monday", date(2026, 8, 17), "<Monday />", "Mon", 1),
    ("Tuesday", date(2026, 8, 18), "<Tuesday />", "Tue", 2),
    ("Wednesday", date(2026, 8, 19), "<Wednesday />", "Wed", 3),
    ("Thursday", date(2026, 8, 20), "<Thursday />", "Thu", 4),
    ("Friday", date(2026, 8, 21), "<Friday />", "Fri", 5),
    ("Saturday", date(2026, 8, 22), "<Saturday />", "Sat", 6),
)

FAKE_PYTHON = "/usr/bin/python3"
FAKE_SCRIPT = "/opt/example/sweep/weekly_sweep.py"
# The register-time PATH the POSIX backends bake in. The third entry is the shape that
# makes the bake necessary at all: a user-local bin dir a scheduler would never inherit.
FAKE_PATH = "/usr/local/bin:/usr/bin:/opt/node/bin"
