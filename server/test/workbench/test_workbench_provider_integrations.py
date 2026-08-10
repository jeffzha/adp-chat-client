import copy

import pytest

from core.workbench_integration_policy import IntegrationResource
from core.workbench_integrations import WorkbenchIntegrations
from core.workbench_provider_integrations import (
    ProviderIntegrationContractError,
    parse_agent_state,
    parse_plugin_detail,
    validate_executable_state,
)


def _plugin_detail(*, auth_type=0, oauth_consent=None, access_mode=1):
    auth = {"AuthType": auth_type}
    if oauth_consent is not None:
        auth["OAuthConsent"] = oauth_consent
    return {
        "Plugin": {
            "PluginId": "plugin-a",
            "Status": 1,
            "Profile": {
                "Name": "Read plugin",
                "Description": "Read-only",
                "PluginClass": 0,
            },
            "Config": {"MCPPluginConfig": {"AuthConfig": auth}},
            "ToolList": [
                {
                    "PluginId": "plugin-a",
                    "ToolId": "tool-a",
                    "Name": "Read",
                    "Description": "Read data",
                    "ToolAccessMode": access_mode,
                    "ToolConfig": {"MCPToolConfig": {"Inputs": [], "Outputs": []}},
                }
            ],
        }
    }


def _resource(kind="plugin", resource_id="plugin-a", parent_id=""):
    return IntegrationResource(
        kind, resource_id, parent_id, "resource", "resource", "", False
    )


def test_catalog_blocks_all_oauth_and_non_read_only_tools():
    user_oauth = parse_plugin_detail(
        _plugin_detail(auth_type=3, oauth_consent=1), "plugin-a"
    )
    assert (
        WorkbenchIntegrations._definition_blocked_reason(
            _resource(), user_oauth
        )
        == "oauth_execution_unsupported"
    )

    developer_oauth = parse_plugin_detail(
        _plugin_detail(auth_type=3, oauth_consent=0), "plugin-a"
    )
    assert (
        WorkbenchIntegrations._definition_blocked_reason(
            _resource(), developer_oauth
        )
        == "oauth_execution_unsupported"
    )

    write_tool = parse_plugin_detail(
        _plugin_detail(access_mode=2), "plugin-a"
    )
    assert (
        WorkbenchIntegrations._definition_blocked_reason(
            _resource(), write_tool
        )
        == "tool_access_mode_unsupported"
    )

    unspecified_tool = parse_plugin_detail(
        _plugin_detail(access_mode=0), "plugin-a"
    )
    assert (
        WorkbenchIntegrations._definition_blocked_reason(
            _resource(), unspecified_tool
        )
        == "tool_access_mode_unsupported"
    )


def test_agent_execution_requires_provider_auth_status_two_and_never_normalizes_oauth():
    plugin = parse_plugin_detail(
        _plugin_detail(auth_type=1), "plugin-a"
    )
    response = {
        "Agent": {
            "AgentId": "agent-a",
            "PluginList": [
                {
                    "Config": copy.deepcopy(plugin.modify_config),
                    "AuthConfigStatus": 1,
                    "Status": 1,
                    "PluginClass": 0,
                }
            ],
            "ToolList": [
                {
                    "Config": copy.deepcopy(plugin.tools[0].modify_config),
                    "ToolAccessMode": 1,
                    "Status": 1,
                }
            ],
        }
    }
    state = parse_agent_state(response, "agent-a")
    with pytest.raises(ProviderIntegrationContractError, match="authorization is incomplete"):
        validate_executable_state(state)
    assert "OAuthConsent" not in state.plugin_configs["plugin-a"]
    assert state.plugin_configs["plugin-a"]["HeaderParameterList"] == []


@pytest.mark.parametrize(
    ("auth_type", "oauth_consent", "access_mode"),
    [(3, 0, 1), (3, 1, 1), (0, None, 0), (0, None, 2)],
)
def test_agent_execution_rejects_oauth_and_non_read_only_modes(
    auth_type, oauth_consent, access_mode
):
    plugin = parse_plugin_detail(
        _plugin_detail(
            auth_type=auth_type,
            oauth_consent=oauth_consent,
            access_mode=access_mode,
        ),
        "plugin-a",
    )
    response = {
        "Agent": {
            "AgentId": "agent-a",
            "PluginList": [
                {
                    "Config": copy.deepcopy(plugin.modify_config),
                    "AuthConfigStatus": 2,
                    "Status": 1,
                    "PluginClass": 0,
                }
            ],
            "ToolList": [
                {
                    "Config": copy.deepcopy(plugin.tools[0].modify_config),
                    "ToolAccessMode": access_mode,
                    "Status": 1,
                }
            ],
        }
    }
    with pytest.raises(ProviderIntegrationContractError):
        validate_executable_state(parse_agent_state(response, "agent-a"))


def test_catalog_rejects_missing_oauth_consent_and_incomplete_tool_config():
    with pytest.raises(ProviderIntegrationContractError, match="consent mode"):
        parse_plugin_detail(_plugin_detail(auth_type=3), "plugin-a")

    incomplete = _plugin_detail()
    incomplete["Plugin"]["ToolList"][0]["ToolConfig"] = {}
    with pytest.raises(ProviderIntegrationContractError, match="configuration is incomplete"):
        parse_plugin_detail(incomplete, "plugin-a")
