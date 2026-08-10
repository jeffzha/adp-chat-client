import asyncio

from sanic import json
from sanic.exceptions import SanicException
from sanic.views import stream
from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from router import login_required
from config import tagentic_config
from core.workbench_file_ownership import WorkbenchFileOwnership, WorkbenchFileOwnershipError
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from core.workbench_runtime import WorkbenchRuntimeError, WorkbenchRuntimeGuard
from core.workbench_secure_file import (
    WorkbenchSecureFileError,
    WorkbenchSecureFilePipeline,
)
from vendor.interface import FileSizeLimitExceeded
from app_factory import TAgenticApp
app: TAgenticApp = TAgenticApp.get_app()


class FileUploadApi(HTTPMethodView):
    @stream
    @login_required
    async def post(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=True, location="args")
        parser.add_argument("Type", type=str, default='image/jpeg', location="args")
        parser.add_argument("Name", type=str, default="file", location="args")
        parser.add_argument("Mode", type=str, default='standard', location="args")
        args = parser.parse_args(request)
        application_id = args['ApplicationId']
        if tagentic_config.WORKBENCH_MODE:
            unknown = sorted(
                set(request.args.keys()).difference({"ApplicationId", "Type", "Name", "Mode"})
            )
            if unknown:
                raise SanicException(
                    f"unsupported workbench upload fields: {', '.join(unknown)}",
                    status_code=400,
                )
            if args['Mode'] not in {None, "claw", "standard"}:
                raise SanicException("unsupported upload mode", status_code=400)
            try:
                limits = WorkbenchPolicy.validate_file_write(
                    request.ctx.workbench_context,
                    request.ctx.workbench_app_context,
                )
                lease = await WorkbenchRuntimeGuard.acquire(
                    account_id=request.ctx.account_id,
                    identity=request.ctx.workbench_context,
                    app_context=request.ctx.workbench_app_context,
                    operation="file_upload",
                )
                pipeline = WorkbenchSecureFilePipeline()
                try:
                    effective_max_file_bytes = min(
                        limits["max_file_bytes"],
                        tagentic_config.WORKBENCH_FILE_ABSOLUTE_MAX_BYTES,
                        tagentic_config.WORKBENCH_FILE_SCANNER_MAX_BYTES,
                        (
                            tagentic_config.WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES
                            // tagentic_config.WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS
                        ),
                    )
                    stored = await pipeline.store_request(
                        request,
                        file_name=args['Name'],
                        declared_type=args['Type'],
                        max_file_bytes=effective_max_file_bytes,
                        customer_id=request.ctx.workbench_context.customer_id,
                        binding_id=request.ctx.workbench_context.binding_id,
                    )
                    result = stored.ownership_result()
                finally:
                    await WorkbenchRuntimeGuard.release(lease)
                if not isinstance(result, dict):
                    raise WorkbenchFileOwnershipError(
                        "provider returned an invalid upload result",
                        502,
                    )
                try:
                    file_id = await WorkbenchFileOwnership.bind_upload(
                        request.ctx.db,
                        account_id=request.ctx.account_id,
                        identity=request.ctx.workbench_context,
                        app_context=request.ctx.workbench_app_context,
                        result=result,
                        file_name=stored.file_name,
                        file_type=stored.mime_type,
                    )
                except BaseException:
                    cleanup_task = asyncio.create_task(pipeline.discard(stored))
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        try:
                            await cleanup_task
                        except WorkbenchSecureFileError:
                            pass
                    except WorkbenchSecureFileError:
                        pass
                    raise
                # Provider upload responses contain signed URLs and COS locators.
                # The browser gets only the opaque ownership handle plus inert
                # display metadata; the encrypted locator remains server-side.
                result = WorkbenchFileOwnership.public_upload_result(
                    file_id=file_id,
                    file_name=stored.file_name,
                    file_type=stored.mime_type,
                    file_size=result["Size"],
                )
            except FileSizeLimitExceeded as error:
                raise SanicException(str(error), status_code=413) from error
            except (
                WorkbenchFileOwnershipError,
                WorkbenchPolicyError,
                WorkbenchRuntimeError,
                WorkbenchSecureFileError,
            ) as error:
                raise SanicException(str(error), status_code=error.status_code) from error
        else:
            vendor_app = app.get_vendor_app(application_id)
            result = await vendor_app.upload(
                request.ctx.db,
                request,
                request.ctx.account_id,
                args['Type'],
                mode=args['Mode']
            )
        # 兼容返回字典或字符串两种格式
        if isinstance(result, dict):
            return json(result)
        return json({"Url": result})


app.add_route(FileUploadApi.as_view(), "/file/upload")
