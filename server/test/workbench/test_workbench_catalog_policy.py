from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.workbench_action_policy import (
    WorkbenchActionPolicy,
    WorkbenchActionPolicyError,
)
from core.workbench_app_resolver import WorkbenchAppResolver
from core.workbench_catalog_policy import WorkbenchCatalogPolicy
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from util.tca import load_action_version_config


class CatalogVendor:
    application_id = "customer-app-7"
    config = {
        "Vendor": "Tencent",
        "ServiceVendor": "ChinaTencentCloud",
        "AppId": "provider-app-7",
        "AppKey": "provider-app-key-secret",
        "SpaceId": "trusted-space-7",
        "SecretId": "provider-secret-id",
        "SecretKey": "provider-secret-key",
    }

    @staticmethod
    def get_vendor():
        return "Tencent"


@pytest.fixture(autouse=True)
def clear_catalog_cache(monkeypatch):
    WorkbenchCatalogPolicy._catalog_ids.clear()
    monkeypatch.setattr(WorkbenchCatalogPolicy, "_clock", lambda: 1000.0)


@pytest.fixture
def identity():
    return WorkbenchIdentityContext(
        binding_id="binding-7",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="profile-7",
        access_mode="active",
        config_version=4,
    )


@pytest.fixture
def app_context():
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="profile-7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-app-key-secret",
        space_id="trusted-space-7",
        template_agent_id="template-7",
        secret_id="provider-secret-id",
        secret_key="provider-secret-key",
        capabilities=("catalog_models", "catalog_skills", "catalog_plugins"),
        limits=None,
    )


@pytest.fixture
def vendor():
    return CatalogVendor()


def request_body(payload=None):
    return {"ApplicationId": "customer-app-7", "Payload": payload or {}}


async def prepare(action, payload, identity, app_context, vendor, account_id="account-9"):
    return await WorkbenchActionPolicy.prepare(
        None,
        action=action,
        request_body=request_body(payload),
        account_id=account_id,
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )


@pytest.mark.asyncio
async def test_model_catalog_forces_claw_scene_space_and_bounded_discovery(
    identity, app_context, vendor
):
    prepared = await prepare(
        "DescribeModelList",
        {
            "Query": "hunyuan",
            "PageNumber": 2,
            "PageSize": 50,
            "FilterList": [
                {"Name": "ProviderType", "ValueList": ["Self"]}
            ],
        },
        identity,
        app_context,
        vendor,
    )

    assert prepared.payload == {
        "SpaceId": "trusted-space-7",
        "ModelScene": 18,
        "PageNumber": 2,
        "PageSize": 50,
        "Query": "hunyuan",
        "FilterList": [
            {"Name": "ProviderType", "ValueList": ["Self"]}
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("SpaceId", "attacker-space"),
        ("AppId", "attacker-app"),
        ("SecretId", "attacker-secret"),
        ("AuthConfig", {"Token": "attacker"}),
        ("OAuth", {"AccessToken": "attacker"}),
        ("ModelScene", 0),
    ],
)
async def test_browser_cannot_inject_catalog_scope_or_credentials(
    identity, app_context, vendor, field, value
):
    with pytest.raises(WorkbenchActionPolicyError, match="unsupported fields") as exc_info:
        await prepare(
            "DescribeModelList",
            {field: value},
            identity,
            app_context,
            vendor,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_unknown_capability_does_not_open_catalog(identity, app_context, vendor):
    unknown_context = replace(app_context, capabilities=("catalog_everything",))

    with pytest.raises(WorkbenchActionPolicyError, match="catalog_models"):
        await prepare(
            "DescribeModelList", {}, identity, unknown_context, vendor
        )
    with pytest.raises(WorkbenchPolicyError, match="catalog_models"):
        WorkbenchPolicy.validate_action(
            "DescribeModelList", identity, unknown_context
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"PageNumber": 101},
        {"PageSize": 51},
        {"PageSize": True},
        {
            "FilterList": [
                {"Name": "ModelId", "ValueList": ["arbitrary-model"]}
            ]
        },
        {
            "FilterList": [
                {"Name": "ProviderType", "ValueList": ["x"] * 9}
            ]
        },
    ],
)
async def test_catalog_pagination_and_filters_have_hard_boundaries(
    identity, app_context, vendor, payload
):
    with pytest.raises(WorkbenchActionPolicyError) as exc_info:
        await prepare(
            "DescribeModelList", payload, identity, app_context, vendor
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_name",
    ["SkillIdList", "SkillStatus", "ShareStatus", "Perspective", "Creator"],
)
async def test_skill_filters_that_enable_id_or_editor_enumeration_are_closed(
    identity, app_context, vendor, filter_name
):
    with pytest.raises(WorkbenchActionPolicyError, match="Filter Name is not allowed"):
        await prepare(
            "DescribeSkillSummaryList",
            {"FilterList": [{"Name": filter_name, "ValueList": ["value"]}]},
            identity,
            app_context,
            vendor,
        )


@pytest.mark.asyncio
async def test_plugin_sort_type_follows_the_official_zero_to_two_range(
    identity, app_context, vendor
):
    prepared = await prepare(
        "DescribePluginSummaryList",
        {"SortType": 2},
        identity,
        app_context,
        vendor,
    )
    assert prepared.payload["SortType"] == 2

    with pytest.raises(WorkbenchActionPolicyError, match="between 0 and 2"):
        await prepare(
            "DescribePluginSummaryList",
            {"SortType": 3},
            identity,
            app_context,
            vendor,
        )


@pytest.mark.asyncio
async def test_skill_detail_requires_same_identity_summary_scope(
    identity, app_context, vendor
):
    skill_id = "5dd39985-a496-4fef-b753-a9bdca1b8433"
    summary = await prepare(
        "DescribeSkillSummaryList", {}, identity, app_context, vendor
    )
    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=summary,
        account_id="account-9",
        response={
            "SkillSummaryList": [{"SkillId": skill_id, "Profile": {"Name": "safe"}}],
            "TotalCount": 1,
            "RequestId": "request-summary",
        },
    )
    assert projected["SkillSummaryList"][0]["SkillId"] == skill_id

    detail = await prepare(
        "DescribeSkillDetail",
        {"SkillId": skill_id},
        identity,
        app_context,
        vendor,
    )
    assert detail.payload == {
        "SkillId": skill_id,
        "SpaceId": "trusted-space-7",
        "VersionFilterList": [
            {"Name": "Perspective", "ValueList": ["USER"]}
        ],
    }

    other_identity = replace(
        identity,
        binding_id="binding-8",
        canonical_subject="napi:prod:customer:8:user:9",
        customer_id=8,
    )
    with pytest.raises(WorkbenchActionPolicyError, match="recent trusted summary") as exc_info:
        await prepare(
            "DescribeSkillDetail",
            {"SkillId": skill_id},
            other_identity,
            app_context,
            vendor,
            account_id="account-other",
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_catalog_cache_expires_and_is_revoked_with_application(
    monkeypatch, identity, app_context, vendor
):
    plugin_id = "c813b306-e49a-46ac-9bb9-c6897df4de4d"
    summary = await prepare(
        "DescribePluginSummaryList", {}, identity, app_context, vendor
    )
    await WorkbenchActionPolicy.project_response(
        None,
        prepared=summary,
        account_id="account-9",
        response={"PluginList": [{"PluginId": plugin_id}], "TotalCount": 1},
    )
    WorkbenchAppResolver._remove(
        SimpleNamespace(apps={"customer-app-7": vendor}),
        "customer-app-7",
    )
    with pytest.raises(WorkbenchActionPolicyError, match="recent trusted summary"):
        await prepare(
            "DescribePlugin", {"PluginId": plugin_id}, identity, app_context, vendor
        )

    await WorkbenchActionPolicy.project_response(
        None,
        prepared=summary,
        account_id="account-9",
        response={"PluginList": [{"PluginId": plugin_id}], "TotalCount": 1},
    )
    monkeypatch.setattr(WorkbenchCatalogPolicy, "_clock", lambda: 1121.0)
    with pytest.raises(WorkbenchActionPolicyError, match="recent trusted summary"):
        await prepare(
            "DescribePlugin", {"PluginId": plugin_id}, identity, app_context, vendor
        )


@pytest.mark.asyncio
async def test_catalog_response_redacts_secrets_auth_and_url_credentials(
    identity, app_context, vendor
):
    skill_id = "5dd39985-a496-4fef-b753-a9bdca1b8433"
    prepared = await prepare(
        "DescribeSkillSummaryList", {}, identity, app_context, vendor
    )
    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "SkillSummaryList": [
                {
                    "SkillId": skill_id,
                    "Profile": {
                        "Description": (
                            "echo provider-app-key-secret and "
                            "https://user:password@example.com/icon.png?token=leak#fragment"
                        ),
                        "IconUrl": "https://example.com/icon.png?signature=secret",
                    },
                    "NestedSecretMaterial": {"Value": "must-not-leak"},
                    "AuthConfig": {"Authorization": "Bearer must-not-leak"},
                    "CurrentVersionInfo": {
                        "SkillUrl": "https://example.com/package.zip?signature=secret"
                    },
                }
            ],
            "TotalCount": 1,
        },
    )

    rendered = str(projected)
    assert "provider-app-key-secret" not in rendered
    assert "password" not in rendered
    assert "token=" not in rendered
    assert "signature=" not in rendered
    assert "NestedSecretMaterial" not in projected["SkillSummaryList"][0]
    assert "AuthConfig" not in projected["SkillSummaryList"][0]
    assert "SkillUrl" not in projected["SkillSummaryList"][0]["CurrentVersionInfo"]
    assert projected["SkillSummaryList"][0]["Profile"]["IconUrl"] == (
        "https://example.com/icon.png"
    )


@pytest.mark.asyncio
async def test_catalog_provider_error_cannot_echo_a_secret_as_its_code(
    identity, app_context, vendor
):
    prepared = await prepare(
        "DescribeModelList", {}, identity, app_context, vendor
    )
    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "Error": {
                "Code": "provider-app-key-secret",
                "Message": "provider-secret-key",
            },
            "RequestId": "request-error",
        },
    )

    assert projected == {
        "Error": {
            "Code": "ProviderError",
            "Message": "provider request failed",
        },
        "RequestId": "request-error",
    }


@pytest.mark.asyncio
async def test_provider_detail_must_echo_the_exact_cached_identifier(
    identity, app_context, vendor
):
    plugin_id = "c813b306-e49a-46ac-9bb9-c6897df4de4d"
    summary = await prepare(
        "DescribePluginSummaryList", {}, identity, app_context, vendor
    )
    await WorkbenchActionPolicy.project_response(
        None,
        prepared=summary,
        account_id="account-9",
        response={"PluginList": [{"PluginId": plugin_id}], "TotalCount": 1},
    )
    detail = await prepare(
        "DescribePlugin", {"PluginId": plugin_id}, identity, app_context, vendor
    )

    with pytest.raises(WorkbenchActionPolicyError, match="outside the trusted context") as exc_info:
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=detail,
            account_id="account-9",
            response={"Plugin": {"PluginId": "another-plugin"}},
        )
    assert exc_info.value.status_code == 502


def test_catalog_actions_have_fixed_adp_service_version_and_region():
    actions = {
        "DescribeModelList",
        "DescribeSkillSummaryList",
        "DescribeSkillDetail",
        "DescribePluginSummaryList",
        "DescribePlugin",
    }
    for service_vendor in ("ChinaTencentCloud", "ChinaTencentADP"):
        config = load_action_version_config(service_vendor)
        for action in actions:
            assert config[action]["service"] == "adp"
            assert config[action]["headers"] == {
                "X-TC-Version": "2026-05-20",
                "X-TC-Region": "ap-guangzhou",
            }
