from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


class ProviderIntegrationContractError(RuntimeError):
    """The provider response cannot be used for a bounded Agent mutation."""


@dataclass(frozen=True)
class ProviderToolDefinition:
    plugin_id: str
    tool_id: str
    name: str
    description: str
    access_mode: int
    modify_config: dict[str, Any]


@dataclass(frozen=True)
class ProviderPluginDefinition:
    plugin_id: str
    name: str
    description: str
    plugin_class: int
    auth_type: int
    oauth_consent: int | None
    status: int
    modify_config: dict[str, Any]
    tools: tuple[ProviderToolDefinition, ...]

    def tool(self, tool_id: str) -> ProviderToolDefinition | None:
        return next((item for item in self.tools if item.tool_id == tool_id), None)


@dataclass(frozen=True)
class ProviderAgentIntegrationState:
    agent_id: str
    plugin_configs: dict[str, dict[str, Any]]
    plugin_auth_status: dict[str, int]
    plugin_status: dict[str, int]
    plugin_class: dict[str, int]
    tool_configs: dict[tuple[str, str], dict[str, Any]]
    tool_access_mode: dict[tuple[str, str], int]
    tool_status: dict[tuple[str, str], int]

    @property
    def active_tool_keys(self) -> set[tuple[str, str]]:
        return {
            key
            for key, config in self.tool_configs.items()
            if config.get("IsDisabled") is False
        }


_PLUGIN_CONFIG_FIELDS = (
    "PluginId",
    "HeaderParameterList",
    "QueryParameterList",
    "EnableCamRoleAuth",
    "AuthType",
    "OAuthConsent",
)
_TOOL_CONFIG_FIELDS = (
    "PluginId",
    "ToolId",
    "Description",
    "InputList",
    "OutputList",
    "HeaderParameterList",
    "QueryParameterList",
    "ToolSource",
    "IsDisabled",
)


def parse_plugin_detail(response: Any, expected_plugin_id: str) -> ProviderPluginDefinition:
    plugin = response.get("Plugin") if isinstance(response, dict) else None
    if not isinstance(plugin, dict) or plugin.get("PluginId") != expected_plugin_id:
        raise ProviderIntegrationContractError("provider Plugin detail identity is invalid")
    profile = plugin.get("Profile")
    config = plugin.get("Config")
    tools = plugin.get("ToolList")
    if not isinstance(profile, dict) or not isinstance(config, dict) or not isinstance(tools, list):
        raise ProviderIntegrationContractError("provider Plugin detail is incomplete")
    plugin_class = _bounded_int(profile.get("PluginClass"), "PluginClass", 0, 1)
    status = _bounded_int(plugin.get("Status"), "Plugin Status", 1, 2)
    name = _string(profile.get("Name"), "Plugin Name", 128)
    description = _optional_string(profile.get("Description"), 2048)

    plugin_kind_configs = [
        value
        for key in ("ApiPluginConfig", "MCPPluginConfig", "AppPluginConfig")
        if isinstance((value := config.get(key)), dict)
    ]
    if len(plugin_kind_configs) != 1:
        raise ProviderIntegrationContractError("provider Plugin configuration is incomplete")
    auth = plugin_kind_configs[0].get("AuthConfig")
    if auth is None:
        auth_type = 0
        oauth_consent = None
    elif isinstance(auth, dict):
        auth_type = _bounded_int(auth.get("AuthType"), "AuthType", 0, 3)
        oauth = auth.get("OAuthConfig")
        consent_value = auth.get("OAuthConsent")
        if consent_value is None and isinstance(oauth, dict):
            consent_value = oauth.get("OAuthConsent")
        oauth_consent = (
            _bounded_int(consent_value, "OAuthConsent", 0, 1)
            if consent_value is not None
            else None
        )
    else:
        raise ProviderIntegrationContractError("provider Plugin authorization is incomplete")
    if auth_type == 3 and oauth_consent is None:
        raise ProviderIntegrationContractError("provider OAuth consent mode is incomplete")

    plugin_modify_config: dict[str, Any] = {
        "PluginId": expected_plugin_id,
        "HeaderParameterList": [],
        "QueryParameterList": [],
        "EnableCamRoleAuth": auth_type == 2,
        "AuthType": auth_type,
    }
    if auth_type == 3:
        plugin_modify_config["OAuthConsent"] = oauth_consent

    parsed_tools: list[ProviderToolDefinition] = []
    seen: set[str] = set()
    for raw_tool in tools:
        tool = _parse_tool(raw_tool, expected_plugin_id)
        if tool.tool_id in seen:
            raise ProviderIntegrationContractError("provider Plugin contains duplicate tools")
        seen.add(tool.tool_id)
        parsed_tools.append(tool)
    return ProviderPluginDefinition(
        plugin_id=expected_plugin_id,
        name=name,
        description=description,
        plugin_class=plugin_class,
        auth_type=auth_type,
        oauth_consent=oauth_consent,
        status=status,
        modify_config=plugin_modify_config,
        tools=tuple(parsed_tools),
    )


def parse_agent_state(response: Any, expected_agent_id: str) -> ProviderAgentIntegrationState:
    agent = response.get("Agent") if isinstance(response, dict) else None
    if not isinstance(agent, dict) or agent.get("AgentId") != expected_agent_id:
        raise ProviderIntegrationContractError("provider Agent identity is invalid")
    raw_plugins = agent.get("PluginList")
    raw_tools = agent.get("ToolList")
    if not isinstance(raw_plugins, list) or not isinstance(raw_tools, list):
        raise ProviderIntegrationContractError("provider Agent integration lists are incomplete")

    plugin_configs: dict[str, dict[str, Any]] = {}
    plugin_auth_status: dict[str, int] = {}
    plugin_status: dict[str, int] = {}
    plugin_class: dict[str, int] = {}
    for raw_plugin in raw_plugins:
        if not isinstance(raw_plugin, dict) or not isinstance(raw_plugin.get("Config"), dict):
            raise ProviderIntegrationContractError("provider Agent PluginList is invalid")
        config = normalize_plugin_config(raw_plugin["Config"])
        plugin_id = config["PluginId"]
        if plugin_id in plugin_configs:
            raise ProviderIntegrationContractError("provider Agent PluginList has duplicates")
        plugin_configs[plugin_id] = config
        plugin_auth_status[plugin_id] = _bounded_int(
            raw_plugin.get("AuthConfigStatus"), "AuthConfigStatus", 0, 2
        )
        plugin_status[plugin_id] = _bounded_int(raw_plugin.get("Status"), "Plugin Status", 0, 2)
        plugin_class[plugin_id] = _bounded_int(
            raw_plugin.get("PluginClass", 0), "PluginClass", 0, 1
        )

    tool_configs: dict[tuple[str, str], dict[str, Any]] = {}
    tool_access_mode: dict[tuple[str, str], int] = {}
    tool_status: dict[tuple[str, str], int] = {}
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or not isinstance(raw_tool.get("Config"), dict):
            raise ProviderIntegrationContractError("provider Agent ToolList is invalid")
        config = normalize_tool_config(raw_tool["Config"])
        key = (config["PluginId"], config["ToolId"])
        if key in tool_configs or key[0] not in plugin_configs:
            raise ProviderIntegrationContractError("provider Agent ToolList is inconsistent")
        tool_configs[key] = config
        tool_access_mode[key] = _bounded_int(
            raw_tool.get("ToolAccessMode"), "ToolAccessMode", 0, 2
        )
        tool_status[key] = _bounded_int(raw_tool.get("Status"), "Tool Status", 1, 3)
    return ProviderAgentIntegrationState(
        expected_agent_id,
        plugin_configs,
        plugin_auth_status,
        plugin_status,
        plugin_class,
        tool_configs,
        tool_access_mode,
        tool_status,
    )


def normalize_plugin_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ProviderIntegrationContractError("provider Agent Plugin config is invalid")
    plugin_id = _string(config.get("PluginId"), "PluginId", 128)
    result: dict[str, Any] = {
        "PluginId": plugin_id,
        "HeaderParameterList": _plugin_parameters(config.get("HeaderParameterList", [])),
        "QueryParameterList": _plugin_parameters(config.get("QueryParameterList", [])),
        "EnableCamRoleAuth": _boolean(config.get("EnableCamRoleAuth", False), "EnableCamRoleAuth"),
        "AuthType": _bounded_int(config.get("AuthType", 0), "AuthType", 0, 3),
    }
    if result["AuthType"] == 3:
        result["OAuthConsent"] = _bounded_int(
            config.get("OAuthConsent"), "OAuthConsent", 0, 1
        )
    return result


def normalize_tool_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ProviderIntegrationContractError("provider Agent Tool config is invalid")
    return {
        "PluginId": _string(config.get("PluginId"), "PluginId", 128),
        "ToolId": _string(config.get("ToolId"), "ToolId", 128),
        "Description": _optional_string(config.get("Description"), 2048),
        "InputList": _input_parameters(config.get("InputList", [])),
        "OutputList": _output_parameters(config.get("OutputList", [])),
        "HeaderParameterList": _plugin_parameters(config.get("HeaderParameterList", [])),
        "QueryParameterList": _plugin_parameters(config.get("QueryParameterList", [])),
        "ToolSource": _bounded_int(config.get("ToolSource", 0), "ToolSource", 0, 1),
        "IsDisabled": _boolean(config.get("IsDisabled"), "IsDisabled"),
    }


def exact_modify_lists(
    state: ProviderAgentIntegrationState,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [copy.deepcopy(state.plugin_configs[key]) for key in sorted(state.plugin_configs)],
        [
            {"Config": copy.deepcopy(state.tool_configs[key])}
            for key in sorted(state.tool_configs)
        ],
    )


def validate_executable_state(state: ProviderAgentIntegrationState) -> None:
    for plugin_id, config in state.plugin_configs.items():
        if config["AuthType"] not in {0, 1, 2}:
            raise ProviderIntegrationContractError("OAuth Plugin execution is disabled")
        if state.plugin_auth_status[plugin_id] != 2:
            raise ProviderIntegrationContractError("provider Plugin authorization is incomplete")
        if state.plugin_status[plugin_id] != 1:
            raise ProviderIntegrationContractError("provider Plugin is unavailable")
    for key, config in state.tool_configs.items():
        if state.tool_access_mode[key] != 1:
            raise ProviderIntegrationContractError("Tool access mode is not read-only")
        if state.tool_status[key] != 1:
            raise ProviderIntegrationContractError("provider Tool is unavailable")
        if config["IsDisabled"] is not True and config["IsDisabled"] is not False:
            raise ProviderIntegrationContractError("provider Tool disabled state is invalid")


def _parse_tool(raw_tool: Any, plugin_id: str) -> ProviderToolDefinition:
    if not isinstance(raw_tool, dict):
        raise ProviderIntegrationContractError("provider Tool detail is invalid")
    tool_id = _string(raw_tool.get("ToolId"), "ToolId", 128)
    if raw_tool.get("PluginId") not in {None, plugin_id}:
        raise ProviderIntegrationContractError("provider Tool parent is invalid")
    access_mode = _bounded_int(raw_tool.get("ToolAccessMode"), "ToolAccessMode", 0, 2)
    name = _string(raw_tool.get("Name"), "Tool Name", 128)
    description = _optional_string(raw_tool.get("Description"), 2048)
    tool_config = raw_tool.get("ToolConfig")
    if not isinstance(tool_config, dict):
        raise ProviderIntegrationContractError("provider Tool configuration is incomplete")
    variants = [
        value
        for key in ("ApiToolConfig", "MCPToolConfig", "AppToolConfig", "CodeToolConfig")
        if isinstance((value := tool_config.get(key)), dict)
    ]
    if len(variants) != 1:
        raise ProviderIntegrationContractError("provider Tool configuration is incomplete")
    variant = variants[0]
    if "ApiToolConfig" in tool_config:
        inputs = list(_list(variant.get("Query"))) + list(_list(variant.get("Body")))
    else:
        inputs = list(_list(variant.get("Inputs")))
    outputs = list(_list(variant.get("Outputs")))
    modify_config = {
        "PluginId": plugin_id,
        "ToolId": tool_id,
        "Description": description,
        "InputList": _catalog_input_parameters(inputs),
        "OutputList": _catalog_output_parameters(outputs),
        # Secrets, local OAuth tokens and browser-extension tokens are never
        # copied from Plugin detail into Agent configuration.
        "HeaderParameterList": [],
        "QueryParameterList": [],
        "ToolSource": 0,
        "IsDisabled": False,
    }
    return ProviderToolDefinition(
        plugin_id, tool_id, name, description, access_mode, modify_config
    )


def _catalog_input_parameters(values: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ProviderIntegrationContractError("provider Tool input is invalid")
        nested = value.get("SubParams", value.get("SubParameterList", []))
        one_of = value.get("OneOf", value.get("OneOfList", []))
        any_of = value.get("AnyOf", value.get("AnyOfList", []))
        item = {
            "Name": _string(value.get("Name"), "Tool input Name", 128),
            "Description": _optional_string(value.get("Description"), 2048),
            "Type": _bounded_int(value.get("Type", 0), "Tool input Type", 0, 99),
            "IsRequired": _boolean(value.get("IsRequired", False), "IsRequired"),
            "IsHidden": _boolean(
                value.get("IsGlobalHidden", value.get("IsHidden", False)), "IsHidden"
            ),
            "SubParameterList": _catalog_input_parameters(list(_list(nested))),
            "OneOfList": _catalog_input_parameters(list(_list(one_of))),
            "AnyOfList": _catalog_input_parameters(list(_list(any_of))),
        }
        result.append(item)
    return result


def _catalog_output_parameters(values: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ProviderIntegrationContractError("provider Tool output is invalid")
        nested = value.get("SubParams", value.get("SubParameterList", []))
        result.append(
            {
                "Name": _string(value.get("Name"), "Tool output Name", 128),
                "Description": _optional_string(value.get("Description"), 2048),
                "Type": _bounded_int(value.get("Type", 0), "Tool output Type", 0, 99),
                "SubParameterList": _catalog_output_parameters(list(_list(nested))),
            }
        )
    return result


def _plugin_parameters(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in _list(values):
        if not isinstance(value, dict):
            raise ProviderIntegrationContractError("provider Plugin parameter is invalid")
        item: dict[str, Any] = {
            "Name": _string(
                value.get("Name", value.get("ParameterName")), "Plugin parameter Name", 128
            ),
            "IsRequired": _boolean(value.get("IsRequired", False), "IsRequired"),
        }
        if value.get("Input") is not None:
            if not isinstance(value["Input"], dict):
                raise ProviderIntegrationContractError("provider Plugin parameter Input is invalid")
            item["Input"] = copy.deepcopy(value["Input"])
        result.append(item)
    return result


def _input_parameters(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in _list(values):
        if not isinstance(value, dict):
            raise ProviderIntegrationContractError("provider Agent Tool input is invalid")
        item = {
            "Name": _string(value.get("Name"), "Tool input Name", 128),
            "Description": _optional_string(value.get("Description"), 2048),
            "Type": _bounded_int(value.get("Type", 0), "Tool input Type", 0, 99),
            "IsRequired": _boolean(value.get("IsRequired", False), "IsRequired"),
            "IsHidden": _boolean(
                value.get("IsHidden", value.get("IsAgentHidden", False)), "IsHidden"
            ),
            "SubParameterList": _input_parameters(value.get("SubParameterList", [])),
            "OneOfList": _input_parameters(value.get("OneOfList", [])),
            "AnyOfList": _input_parameters(value.get("AnyOfList", [])),
        }
        if value.get("Input") is not None:
            if not isinstance(value["Input"], dict):
                raise ProviderIntegrationContractError("provider Agent Tool Input is invalid")
            item["Input"] = copy.deepcopy(value["Input"])
        result.append(item)
    return result


def _output_parameters(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in _list(values):
        if not isinstance(value, dict):
            raise ProviderIntegrationContractError("provider Agent Tool output is invalid")
        result.append(
            {
                "Name": _string(value.get("Name"), "Tool output Name", 128),
                "Description": _optional_string(value.get("Description"), 2048),
                "Type": _bounded_int(value.get("Type", 0), "Tool output Type", 0, 99),
                "SubParameterList": _output_parameters(value.get("SubParameterList", [])),
            }
        )
    return result


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 500:
        raise ProviderIntegrationContractError("provider integration list is invalid")
    return value


def _string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProviderIntegrationContractError(f"provider {field_name} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ProviderIntegrationContractError(f"provider {field_name} is invalid")
    return normalized


def _optional_string(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ProviderIntegrationContractError("provider integration text is invalid")
    return value


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderIntegrationContractError(f"provider {field_name} is invalid")
    return value


def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProviderIntegrationContractError(f"provider {field_name} is invalid")
    return value
