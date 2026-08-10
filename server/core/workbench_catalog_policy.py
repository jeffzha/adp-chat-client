import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from vendor.interface import BaseVendor


class WorkbenchCatalogPolicyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedCatalogAction:
    action: str
    catalog_kind: str
    cache_scope: tuple[str, ...]
    page_size: int | None = None
    detail_id: str | None = None
    sensitive_values: tuple[str, ...] = field(default=(), repr=False)


class WorkbenchCatalogPolicy:
    """Strict read-only projection for the Tencent ADP 2026-05-20 catalog."""

    ACTION_CAPABILITIES = {
        "DescribeModelList": "catalog_models",
        "DescribeSkillSummaryList": "catalog_skills",
        "DescribeSkillDetail": "catalog_skills",
        "DescribePluginSummaryList": "catalog_plugins",
        "DescribePlugin": "catalog_plugins",
    }
    _SUMMARY_ACTIONS = frozenset(
        {
            "DescribeModelList",
            "DescribeSkillSummaryList",
            "DescribePluginSummaryList",
        }
    )
    _DETAIL_ACTIONS = frozenset({"DescribeSkillDetail", "DescribePlugin"})
    _CATALOG_KINDS = {
        "DescribeModelList": "model",
        "DescribeSkillSummaryList": "skill",
        "DescribeSkillDetail": "skill",
        "DescribePluginSummaryList": "plugin",
        "DescribePlugin": "plugin",
    }
    _PAGE_SIZE_MAX = 50
    _PAGE_NUMBER_MAX = 100
    _FILTER_COUNT_MAX = 5
    _FILTER_VALUE_COUNT_MAX = 8
    _CACHE_TTL_SECONDS = 120
    _CACHE_SCOPE_MAX = 256
    _CACHE_IDS_PER_SCOPE_MAX = 500
    _catalog_ids: OrderedDict[
        tuple[str, ...], tuple[float, OrderedDict[str, None]]
    ] = OrderedDict()
    _clock = time.monotonic
    _URL_PATTERN = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
    _FILTER_VALUE_PATTERN = re.compile(r"^[\w .:/@+-]{1,64}$", re.UNICODE)
    _BLOCKED_RESPONSE_KEYS = frozenset(
        {
            "accesskey",
            "accesstoken",
            "appkey",
            "appid",
            "authconfig",
            "authentication",
            "authorization",
            "auth",
            "credential",
            "credentials",
            "externalmcpserverurl",
            "headerparameterlist",
            "header",
            "headers",
            "mcpserverurl",
            "oauth",
            "oauthconfig",
            "password",
            "passwordsalt",
            "pluginheader",
            "pluginquery",
            "queryparameterlist",
            "queryparameters",
            "refreshtoken",
            "secretid",
            "secretinfo",
            "secretkey",
            "securityreporturl",
            "skillmarkdownurl",
            "skillurl",
            "spaceid",
        }
    )

    @classmethod
    def prepare(
        cls,
        *,
        action: str,
        payload: dict[str, Any],
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        vendor_app: BaseVendor,
    ) -> tuple[dict[str, Any], PreparedCatalogAction]:
        capability = cls.ACTION_CAPABILITIES.get(action)
        if capability is None:
            raise WorkbenchCatalogPolicyError("catalog Action is not available")
        try:
            WorkbenchPolicy.require_capability(app_context, capability)
        except WorkbenchPolicyError as error:
            raise WorkbenchCatalogPolicyError(str(error), error.status_code) from error

        cls._validate_trusted_context(identity, app_context, vendor_app)
        space_id = cls._require_string(
            app_context.space_id,
            "trusted SpaceId",
            max_length=128,
            status_code=503,
        )
        catalog_kind = cls._CATALOG_KINDS[action]
        cache_scope = (
            str(account_id),
            str(identity.binding_id),
            str(identity.canonical_subject),
            str(identity.customer_id),
            app_context.application_id,
            app_context.app_profile_id,
            str(app_context.config_version),
            str(app_context.auth_epoch),
            space_id,
            catalog_kind,
        )
        sensitive_values = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in (
                        app_context.app_id,
                        app_context.app_key,
                        app_context.secret_id,
                        app_context.secret_key,
                    )
                    if value and len(str(value).strip()) >= 4
                },
                key=len,
                reverse=True,
            )
        )

        if action == "DescribeModelList":
            cls._reject_unknown_fields(
                payload,
                {"Query", "PageNumber", "PageSize", "FilterList"},
                action,
            )
            page_number, page_size = cls._pagination(payload)
            upstream = {
                "SpaceId": space_id,
                "ModelScene": 18,
                "PageNumber": page_number,
                "PageSize": page_size,
            }
            cls._copy_query(payload, upstream)
            cls._copy_filters(
                payload,
                upstream,
                {"DeveloperName", "ProviderName", "ProviderType"},
            )
            return upstream, PreparedCatalogAction(
                action,
                catalog_kind,
                cache_scope,
                page_size=page_size,
                sensitive_values=sensitive_values,
            )

        if action == "DescribeSkillSummaryList":
            cls._reject_unknown_fields(
                payload,
                {"Query", "PageNumber", "PageSize", "FilterList"},
                action,
            )
            page_number, page_size = cls._pagination(payload)
            upstream = {
                "SpaceId": space_id,
                "PageNumber": page_number,
                "PageSize": page_size,
            }
            cls._copy_query(payload, upstream)
            cls._copy_filters(
                payload,
                upstream,
                {"ProviderType", "CategoryKey", "AnalysisStatus", "RiskLevel"},
            )
            return upstream, PreparedCatalogAction(
                action,
                catalog_kind,
                cache_scope,
                page_size=page_size,
                sensitive_values=sensitive_values,
            )

        if action == "DescribePluginSummaryList":
            cls._reject_unknown_fields(
                payload,
                {"Query", "PageNumber", "PageSize", "FilterList", "SortType"},
                action,
            )
            page_number, page_size = cls._pagination(payload)
            upstream = {
                "SpaceId": space_id,
                "Module": 3,
                "PageNumber": page_number,
                "PageSize": page_size,
            }
            cls._copy_query(payload, upstream)
            cls._copy_filters(
                payload,
                upstream,
                {"PluginKind", "CategoryKey", "PluginSource", "PluginClass", "BillingType"},
            )
            if "SortType" in payload:
                upstream["SortType"] = cls._bounded_int(
                    payload["SortType"], "SortType", 0, 2
                )
            return upstream, PreparedCatalogAction(
                action,
                catalog_kind,
                cache_scope,
                page_size=page_size,
                sensitive_values=sensitive_values,
            )

        if action == "DescribeSkillDetail":
            cls._reject_unknown_fields(payload, {"SkillId"}, action)
            detail_id = cls._require_string(
                payload.get("SkillId"), "SkillId", max_length=128
            )
            cls._require_cached_id(cache_scope, detail_id)
            return {
                "SkillId": detail_id,
                "SpaceId": space_id,
                "VersionFilterList": [
                    {"Name": "Perspective", "ValueList": ["USER"]}
                ],
            }, PreparedCatalogAction(
                action,
                catalog_kind,
                cache_scope,
                detail_id=detail_id,
                sensitive_values=sensitive_values,
            )

        cls._reject_unknown_fields(payload, {"PluginId"}, action)
        detail_id = cls._require_string(
            payload.get("PluginId"), "PluginId", max_length=128
        )
        cls._require_cached_id(cache_scope, detail_id)
        return {
            "PluginId": detail_id,
            "SpaceId": space_id,
            "Module": 3,
        }, PreparedCatalogAction(
            action,
            catalog_kind,
            cache_scope,
            detail_id=detail_id,
            sensitive_values=sensitive_values,
        )

    @classmethod
    def project_response(
        cls,
        prepared: PreparedCatalogAction,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        if prepared.action in cls._SUMMARY_ACTIONS:
            list_key = {
                "DescribeModelList": "ModelList",
                "DescribeSkillSummaryList": "SkillSummaryList",
                "DescribePluginSummaryList": "PluginList",
            }[prepared.action]
            items = response.get(list_key)
            if not isinstance(items, list):
                raise WorkbenchCatalogPolicyError(
                    "provider returned an invalid catalog list", 502
                )
            if prepared.page_size is None or len(items) > prepared.page_size:
                raise WorkbenchCatalogPolicyError(
                    "provider exceeded the catalog page boundary", 502
                )
            identifiers = [
                cls._catalog_item_id(prepared.action, item) for item in items
            ]
            if len(identifiers) != len(set(identifiers)):
                raise WorkbenchCatalogPolicyError(
                    "provider returned duplicate catalog identifiers", 502
                )
            total_count = response.get("TotalCount")
            if (
                isinstance(total_count, bool)
                or not isinstance(total_count, int)
                or total_count < len(items)
                or total_count > 10_000_000
            ):
                raise WorkbenchCatalogPolicyError(
                    "provider returned an invalid catalog count", 502
                )
            if prepared.catalog_kind in {"skill", "plugin"}:
                cls._remember_ids(prepared.cache_scope, identifiers)

        if prepared.action in cls._DETAIL_ACTIONS:
            if prepared.action == "DescribeSkillDetail":
                detail = response.get("SkillDetail")
                summary = detail.get("SkillSummary") if isinstance(detail, dict) else None
                identifier = summary.get("SkillId") if isinstance(summary, dict) else None
            else:
                detail = response.get("Plugin")
                identifier = detail.get("PluginId") if isinstance(detail, dict) else None
            provider_id = cls._require_string(
                identifier,
                "provider catalog identifier",
                max_length=128,
                status_code=502,
            )
            if provider_id != prepared.detail_id:
                raise WorkbenchCatalogPolicyError(
                    "provider returned a catalog item outside the trusted context", 502
                )

        return cls._sanitize(response, prepared.sensitive_values)

    @classmethod
    def revoke_application(cls, application_id: str) -> None:
        for scope in list(cls._catalog_ids):
            if len(scope) > 4 and scope[4] == application_id:
                cls._catalog_ids.pop(scope, None)

    @classmethod
    def _remember_ids(cls, scope: tuple[str, ...], identifiers: list[str]) -> None:
        now = cls._clock()
        cls._purge_expired(now)
        existing = cls._catalog_ids.pop(scope, None)
        values = existing[1] if existing is not None else OrderedDict()
        for identifier in identifiers:
            values.pop(identifier, None)
            values[identifier] = None
        while len(values) > cls._CACHE_IDS_PER_SCOPE_MAX:
            values.popitem(last=False)
        cls._catalog_ids[scope] = (now + cls._CACHE_TTL_SECONDS, values)
        while len(cls._catalog_ids) > cls._CACHE_SCOPE_MAX:
            cls._catalog_ids.popitem(last=False)

    @classmethod
    def _require_cached_id(cls, scope: tuple[str, ...], identifier: str) -> None:
        now = cls._clock()
        cls._purge_expired(now)
        cached = cls._catalog_ids.get(scope)
        if cached is None or identifier not in cached[1]:
            raise WorkbenchCatalogPolicyError(
                "catalog detail requires a recent trusted summary result", 404
            )
        cls._catalog_ids.move_to_end(scope)

    @classmethod
    def _purge_expired(cls, now: float) -> None:
        for scope, (expires_at, _) in list(cls._catalog_ids.items()):
            if expires_at <= now:
                cls._catalog_ids.pop(scope, None)

    @classmethod
    def _validate_trusted_context(
        cls,
        identity: WorkbenchIdentityContext,
        context: WorkbenchAppContext,
        vendor_app: BaseVendor,
    ) -> None:
        expected = {
            "Vendor": context.vendor,
            "ServiceVendor": context.service_vendor,
            "AppId": context.app_id,
            "AppKey": context.app_key,
            "SpaceId": context.space_id,
            "SecretId": context.secret_id,
            "SecretKey": context.secret_key,
        }
        if (
            context.vendor != "Tencent"
            or context.service_vendor not in {"ChinaTencentCloud", "ChinaTencentADP"}
            or identity.application_id != context.application_id
            or identity.app_profile_id != context.app_profile_id
            or identity.auth_epoch != context.auth_epoch
            or identity.config_version != context.config_version
            or getattr(vendor_app, "application_id", None) != context.application_id
            or any(vendor_app.config.get(key) != value for key, value in expected.items())
        ):
            raise WorkbenchCatalogPolicyError(
                "trusted workbench catalog context is invalid", 503
            )

    @classmethod
    def _catalog_item_id(cls, action: str, item: Any) -> str:
        if not isinstance(item, dict):
            raise WorkbenchCatalogPolicyError(
                "provider returned an invalid catalog item", 502
            )
        if action == "DescribeModelList":
            basic = item.get("ModelBasic")
            identifier = basic.get("ModelId") if isinstance(basic, dict) else None
        elif action == "DescribeSkillSummaryList":
            identifier = item.get("SkillId")
        else:
            identifier = item.get("PluginId")
        return cls._require_string(
            identifier,
            "provider catalog identifier",
            max_length=128,
            status_code=502,
        )

    @classmethod
    def _pagination(cls, payload: dict[str, Any]) -> tuple[int, int]:
        return (
            cls._bounded_int(payload.get("PageNumber", 0), "PageNumber", 0, cls._PAGE_NUMBER_MAX),
            cls._bounded_int(payload.get("PageSize", 20), "PageSize", 1, cls._PAGE_SIZE_MAX),
        )

    @classmethod
    def _copy_query(cls, source: dict[str, Any], target: dict[str, Any]) -> None:
        if "Query" in source:
            target["Query"] = cls._require_string(
                source["Query"], "Query", max_length=128, allow_empty=True
            )

    @classmethod
    def _copy_filters(
        cls,
        source: dict[str, Any],
        target: dict[str, Any],
        allowed_names: set[str],
    ) -> None:
        if "FilterList" not in source:
            return
        filters = source["FilterList"]
        if not isinstance(filters, list) or len(filters) > cls._FILTER_COUNT_MAX:
            raise WorkbenchCatalogPolicyError("FilterList is invalid", 400)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in filters:
            if not isinstance(item, dict):
                raise WorkbenchCatalogPolicyError("FilterList item is invalid", 400)
            cls._reject_unknown_fields(item, {"Name", "ValueList"}, "FilterList item")
            name = cls._require_string(item.get("Name"), "Filter Name", max_length=64)
            values = item.get("ValueList")
            if name not in allowed_names or name in seen:
                raise WorkbenchCatalogPolicyError("Filter Name is not allowed", 400)
            if (
                not isinstance(values, list)
                or not values
                or len(values) > cls._FILTER_VALUE_COUNT_MAX
            ):
                raise WorkbenchCatalogPolicyError("Filter ValueList is invalid", 400)
            normalized_values: list[str] = []
            for value in values:
                normalized_value = cls._require_string(
                    value, "Filter value", max_length=64
                )
                if not cls._FILTER_VALUE_PATTERN.fullmatch(normalized_value):
                    raise WorkbenchCatalogPolicyError("Filter value is invalid", 400)
                normalized_values.append(normalized_value)
            seen.add(name)
            normalized.append({"Name": name, "ValueList": normalized_values})
        target["FilterList"] = normalized

    @classmethod
    def _sanitize(cls, value: Any, sensitive_values: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).replace("_", "").lower()
                if (
                    normalized_key in cls._BLOCKED_RESPONSE_KEYS
                    or "secret" in normalized_key
                    or "credential" in normalized_key
                    or "authconfig" in normalized_key
                    or "oauth" in normalized_key
                ):
                    continue
                projected[key] = cls._sanitize(item, sensitive_values)
            return projected
        if isinstance(value, list):
            return [cls._sanitize(item, sensitive_values) for item in value]
        if not isinstance(value, str):
            return value
        sanitized = value
        for secret in sensitive_values:
            sanitized = sanitized.replace(secret, "[redacted]")
        return cls._URL_PATTERN.sub(cls._sanitize_url, sanitized)

    @staticmethod
    def _sanitize_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        try:
            parsed = urlsplit(raw_url)
            if not parsed.hostname:
                return "[redacted-url]"
            netloc = parsed.hostname
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
        except (ValueError, UnicodeError):
            return "[redacted-url]"

    @staticmethod
    def _reject_unknown_fields(
        value: dict[str, Any], allowed: set[str], field_name: str
    ) -> None:
        unknown = sorted(set(value).difference(allowed))
        if unknown:
            raise WorkbenchCatalogPolicyError(
                f"{field_name} contains unsupported fields: {', '.join(unknown)}",
                400,
            )

    @staticmethod
    def _require_string(
        value: Any,
        field_name: str,
        *,
        max_length: int,
        allow_empty: bool = False,
        status_code: int = 400,
    ) -> str:
        if not isinstance(value, str):
            raise WorkbenchCatalogPolicyError(
                f"{field_name} must be a string", status_code
            )
        normalized = value.strip()
        if (not normalized and not allow_empty) or len(normalized) > max_length:
            raise WorkbenchCatalogPolicyError(f"{field_name} is invalid", status_code)
        if any(ord(character) < 32 for character in normalized):
            raise WorkbenchCatalogPolicyError(
                f"{field_name} contains control characters", status_code
            )
        return normalized

    @staticmethod
    def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkbenchCatalogPolicyError(
                f"{field_name} must be an integer", 400
            )
        if value < minimum or value > maximum:
            raise WorkbenchCatalogPolicyError(
                f"{field_name} must be between {minimum} and {maximum}", 400
            )
        return value
