from dataclasses import dataclass


STANDARD_V2 = "standard_v2"
MULTI_AGENT_V2 = "multi_agent_v2"
WORKFLOW_V2 = "workflow_v2"
CLAW_STATIC_V2 = "claw_static_v2"
CLAW_DYNAMIC_V2 = "claw_dynamic_v2"

RUNTIME_PROFILES = frozenset(
    {
        STANDARD_V2,
        MULTI_AGENT_V2,
        WORKFLOW_V2,
        CLAW_STATIC_V2,
        CLAW_DYNAMIC_V2,
    }
)

# A profile is added here only after its real Tencent Application has passed the
# minimum Turn, history, error, throttling, and revocation acceptance suite.
# The existing production Claw Chat path is the sole accepted profile today.
LOCALLY_ACCEPTED_EXECUTION_PROFILES = frozenset({CLAW_DYNAMIC_V2})


@dataclass(frozen=True)
class WorkbenchRuntimeProfile:
    provider_app_mode: int
    name: str
    execution_enabled: bool

    @property
    def uses_provider_user_agent(self) -> bool:
        return self.name == CLAW_DYNAMIC_V2


def validate_runtime_profile(
    provider_app_mode: object,
    runtime_profile: object,
    execution_enabled: object,
) -> WorkbenchRuntimeProfile:
    if (
        isinstance(provider_app_mode, bool)
        or not isinstance(provider_app_mode, int)
        or provider_app_mode not in {1, 2, 3, 4}
    ):
        raise ValueError("provider AppMode is invalid")
    if not isinstance(runtime_profile, str) or runtime_profile not in RUNTIME_PROFILES:
        raise ValueError("runtime profile is invalid")
    if not isinstance(execution_enabled, bool):
        raise ValueError("runtime profile execution gate is invalid")

    allowed_profiles = {
        1: {STANDARD_V2},
        2: {MULTI_AGENT_V2},
        3: {WORKFLOW_V2},
        4: {CLAW_STATIC_V2, CLAW_DYNAMIC_V2},
    }
    if runtime_profile not in allowed_profiles[provider_app_mode]:
        raise ValueError("runtime profile does not match provider AppMode")
    if execution_enabled and runtime_profile not in LOCALLY_ACCEPTED_EXECUTION_PROFILES:
        raise ValueError("runtime profile has not passed local execution acceptance")
    return WorkbenchRuntimeProfile(
        provider_app_mode=provider_app_mode,
        name=runtime_profile,
        execution_enabled=execution_enabled,
    )
