import asyncio
import time
from collections import OrderedDict

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchControlError


class WorkbenchAppResolver:
    _SUPPORTED_VENDOR = "Tencent"
    _SUPPORTED_SERVICE_VENDORS = frozenset(
        {
            "ChinaTencentCloud",
            "ChinaTencentADP",
        }
    )
    _lock = asyncio.Lock()
    _versions: dict[str, tuple[int, int]] = {}
    _last_used: OrderedDict[str, float] = OrderedDict()
    _clock = time.monotonic

    @classmethod
    def _remove(cls, app, application_id: str) -> None:
        from core.workbench_catalog_policy import WorkbenchCatalogPolicy

        cls._versions.pop(application_id, None)
        cls._last_used.pop(application_id, None)
        app.apps.pop(application_id, None)
        WorkbenchCatalogPolicy.revoke_application(application_id)

    @classmethod
    def _evict_expired_locked(cls, app, now: float) -> int:
        ttl = tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS
        expired = [
            application_id
            for application_id, last_used in cls._last_used.items()
            if now - last_used >= ttl
        ]
        for application_id in expired:
            cls._remove(app, application_id)
        return len(expired)

    @classmethod
    async def ensure_vendor(cls, context: WorkbenchAppContext) -> None:
        from app_factory import TAgenticApp

        if context.vendor != cls._SUPPORTED_VENDOR:
            raise WorkbenchControlError("configured workbench vendor is not allowed", 503)
        if context.service_vendor not in cls._SUPPORTED_SERVICE_VENDORS:
            raise WorkbenchControlError(
                "configured workbench service vendor is not allowed",
                503,
            )

        async with cls._lock:
            app = TAgenticApp.get_app()
            now = cls._clock()
            cls._evict_expired_locked(app, now)
            version_key = (context.config_version, context.auth_epoch)
            if (
                cls._versions.get(context.application_id) == version_key
                and context.application_id in app.apps
            ):
                cls._last_used[context.application_id] = now
                cls._last_used.move_to_end(context.application_id)
                return
            cls._remove(app, context.application_id)
            vendor_class = app.vendors.get(context.vendor)
            if vendor_class is None:
                raise WorkbenchControlError("configured workbench vendor is unavailable", 503)
            vendor_config = {
                "Vendor": context.vendor,
                "ServiceVendor": context.service_vendor,
                "ApplicationId": context.application_id,
                "AppId": context.app_id,
                "AppKey": context.app_key,
                "SpaceId": context.space_id,
                "TemplateAgentId": context.template_agent_id,
                "SecretId": context.secret_id,
                "SecretKey": context.secret_key,
            }
            app.apps[context.application_id] = vendor_class(vendor_config, context.application_id)
            cls._versions[context.application_id] = version_key
            cls._last_used[context.application_id] = now
            cls._last_used.move_to_end(context.application_id)
            while len(cls._last_used) > tagentic_config.WORKBENCH_VENDOR_CACHE_MAX_ENTRIES:
                oldest_application_id = next(iter(cls._last_used))
                cls._remove(app, oldest_application_id)

    @classmethod
    async def evict_expired(cls) -> int:
        from app_factory import TAgenticApp

        async with cls._lock:
            return cls._evict_expired_locked(TAgenticApp.get_app(), cls._clock())

    @classmethod
    def revoke(cls, application_id: str) -> None:
        from app_factory import TAgenticApp

        app = TAgenticApp.get_app()
        cls._remove(app, application_id)
