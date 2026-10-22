"""The deterministic Hypothesis profile every git_workspace_backup property suite loads.

Deliberately NOT named ``test_*``, so the aggregator's discover skips it. Importing it
registers and loads the profile, so a suite imports it before defining any ``@given`` test:
such a test snapshots its settings when it is decorated. One definition, so the property
suites cannot drift onto different settings.

Derandomized, so every run of the suite is reproducible. A profile rather than a decorator,
because a fuzzing campaign may stamp its own settings object on each property and
Hypothesis refuses a second one on a decorated function. No deadline and no too-slow health
check, so a verdict never depends on how fast the host happens to be.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

PROFILE_NAME = "fuzz_gate_deterministic"

settings.register_profile(
    PROFILE_NAME,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(PROFILE_NAME)
