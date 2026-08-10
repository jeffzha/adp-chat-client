"""文件代理下载接口

通过后端代理从工作空间下载文件，避免前端直接访问 COS 产生跨域问题。
前端直接使用同域的 /file/download?... URL 即可下载或预览文件。
"""
import logging
from urllib.parse import quote

from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from sanic.response import ResponseStream, raw
from sanic.exceptions import SanicException

from router import login_required
from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from core.workbench_runtime import WorkbenchRuntimeError, WorkbenchRuntimeGuard
from core.workbench_stream import WorkbenchStreamGuard
from core.workbench_workspace import CoreWorkbenchWorkspace, WorkbenchWorkspaceError

app: TAgenticApp = TAgenticApp.get_app()


class FileDownloadApi(HTTPMethodView):
    """文件代理下载

    前端通过 GET /file/download?ApplicationId=xxx&AppId=xxx&WorkspaceId=xxx&Path=xxx
    获取文件内容。后端内部从工作空间沙箱获取文件，直接返回给前端。

    Query 参数:
        ApplicationId: 应用配置 ID（用于定位 vendor 实例）
        AppId:         当前本地应用配置 ID（仅兼容现有客户端字段）
        WorkspaceId:   本地不透明 ww_* 工作空间句柄
        Path:          文件路径，如 /workdir/main.py
    """

    @login_required
    async def get(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=True, location="args")
        parser.add_argument("AppId", type=str, required=True, location="args")
        parser.add_argument("WorkspaceId", type=str, required=True, location="args")
        parser.add_argument("Path", type=str, required=True, location="args")
        args = parser.parse_args(request)

        if tagentic_config.WORKBENCH_MODE:
            unknown = sorted(
                set(request.args.keys()).difference(
                    {"ApplicationId", "AppId", "WorkspaceId", "Path"}
                )
            )
            if unknown:
                raise SanicException(
                    "unsupported workbench download fields",
                    status_code=400,
                )
            try:
                limits = WorkbenchPolicy.validate_file_read(
                    request.ctx.workbench_app_context
                )
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            active_application_id = request.ctx.workbench_app_context.application_id
            if (
                args['ApplicationId'] != active_application_id
                or args['AppId'] != active_application_id
            ):
                raise SanicException(
                    "application is outside the active workbench context",
                    status_code=403,
                )
            try:
                file_path = CoreWorkbenchWorkspace.require_path(
                    args['Path'], "download Path"
                )
                _, provider_workspace_id = (
                    await CoreWorkbenchWorkspace.resolve_provider_locator(
                        request.ctx.db,
                        workspace_id=args['WorkspaceId'],
                        account_id=request.ctx.account_id,
                        identity=request.ctx.workbench_context,
                        app_context=request.ctx.workbench_app_context,
                    )
                )
            except WorkbenchWorkspaceError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            vendor_app = app.get_vendor_app(active_application_id)
            if not hasattr(vendor_app, 'open_file_stream'):
                raise SanicException(
                    'This vendor does not support file download',
                    status_code=501,
                )
            max_bytes = min(
                int(limits["max_file_bytes"]),
                int(tagentic_config.WORKBENCH_FILE_ABSOLUTE_MAX_BYTES),
            )
            if max_bytes <= 0:
                raise SanicException("file downloads are disabled", status_code=403)
            lease = None
            provider_stream = None
            try:
                lease = await WorkbenchRuntimeGuard.acquire(
                    account_id=request.ctx.account_id,
                    identity=request.ctx.workbench_context,
                    app_context=request.ctx.workbench_app_context,
                    operation="file_download",
                )
                provider_stream = await vendor_app.open_file_stream(
                    app_id=request.ctx.workbench_app_context.app_id,
                    workspace_id=provider_workspace_id,
                    path=file_path,
                    max_bytes=max_bytes,
                    user_id=request.ctx.workbench_context.canonical_subject,
                    timeout_seconds=max(1, lease.max_runtime_seconds - 1),
                )
            except WorkbenchRuntimeError as error:
                await WorkbenchRuntimeGuard.release(lease)
                raise SanicException(str(error), status_code=error.status_code) from error
            except Exception as error:
                if provider_stream is not None:
                    await provider_stream.close()
                await WorkbenchRuntimeGuard.release(lease)
                logging.error(
                    '[FileDownloadApi] workbench provider download failed: error_type=%s',
                    type(error).__name__,
                )
                raise SanicException(
                    'provider file download failed',
                    status_code=502,
                ) from error
            encoded_filename = quote(provider_stream.file_name, safe='')
            stream_claims = dict(request.ctx.session_claims)
            stream_app_context = request.ctx.workbench_app_context

            async def streaming_fn(response):
                try:
                    await WorkbenchStreamGuard.pump(
                        provider_stream.iter_chunks(),
                        response.write,
                        claims=stream_claims,
                        method="GET",
                        resource_path="/file/download",
                        application_id=stream_app_context.application_id,
                        app_profile_id=stream_app_context.app_profile_id,
                        config_version=stream_app_context.config_version,
                        lease=lease,
                        max_runtime_seconds=lease.max_runtime_seconds,
                    )
                except Exception as error:
                    logging.error(
                        '[FileDownloadApi] workbench provider stream failed: error_type=%s',
                        type(error).__name__,
                    )
                    raise
                finally:
                    await provider_stream.close()

            return ResponseStream(
                streaming_fn,
                content_type=provider_stream.content_type,
                headers={
                    'Content-Disposition': (
                        f"attachment; filename=\"{encoded_filename}\"; "
                        f"filename*=UTF-8''{encoded_filename}"
                    ),
                    'Cache-Control': 'private, no-store',
                    'Content-Security-Policy': "default-src 'none'; sandbox",
                    'X-Content-Type-Options': 'nosniff',
                },
            )

        application_id = args['ApplicationId']
        app_id = args['AppId']
        workspace_id = args['WorkspaceId']
        file_path = args['Path']

        logging.info("[FileDownloadApi] starting provider file download")

        vendor_app = app.get_vendor_app(application_id)

        if not hasattr(vendor_app, 'download_file_content'):
            raise SanicException(
                'This vendor does not support file download',
                status_code=501
            )

        try:
            content, content_type, file_name = await vendor_app.download_file_content(
                app_id=app_id,
                workspace_id=workspace_id,
                path=file_path,
            )
        except Exception as e:
            logging.error('[FileDownloadApi] provider download failed')
            raise SanicException(
                'provider file download failed',
                status_code=502
            ) from e

        # 使用 RFC 5987 编码文件名以支持中文等非 ASCII 字符
        encoded_filename = quote(file_name, safe='')

        return raw(
            body=content,
            content_type=content_type,
            headers={
                'Content-Disposition': (
                    f"attachment; filename=\"{encoded_filename}\"; "
                    f"filename*=UTF-8''{encoded_filename}"
                ),
                'Cache-Control': 'no-cache',
            },
        )


app.add_route(FileDownloadApi.as_view(), "/file/download")
