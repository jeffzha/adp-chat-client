import asyncio
import logging

import sanic
from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from sanic.response import ResponseStream
from sanic.exceptions import SanicException

from router import authorize_workbench_request, login_required, check_login
from core.chat import CoreChat
from core.conversation import CoreConversation
from core.share import CoreShareConversation
from config import tagentic_config
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from core.workbench_file_ownership import (
    WorkbenchFileOwnership,
    WorkbenchFileOwnershipError,
)
from core.workbench_secure_file import WorkbenchSecureFileError
from core.workbench_turn import WorkbenchTurnError, WorkbenchTurnManager
from core.workbench_integrations import WorkbenchIntegrationError, WorkbenchIntegrations
from core.workbench_control import WorkbenchControlClient, WorkbenchControlError
from core.workbench_app_resolver import WorkbenchAppResolver
from app_factory import TAgenticApp
app: TAgenticApp = TAgenticApp.get_app()


class ChatMessageApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        if tagentic_config.WORKBENCH_MODE:
            body = request.json if isinstance(request.json, dict) else {}
            unknown = sorted(
                set(body).difference(
                    {
                        "Contents",
                        "ConversationId",
                        "ApplicationId",
                        "SearchNetwork",
                        "IsChannel",
                        "ClientRequestId",
                    }
                )
            )
            if unknown:
                raise SanicException(
                    f"unsupported workbench chat fields: {', '.join(unknown)}",
                    status_code=400,
                )
        parser = reqparse.RequestParser()
        parser.add_argument("Contents", type=list, required=True, location="json")
        parser.add_argument("ConversationId", type=str, location="json")
        parser.add_argument("ApplicationId", type=str, location="json")
        parser.add_argument(
            "SearchNetwork",
            type=bool,
            default=not tagentic_config.WORKBENCH_MODE,
            location="json",
        )
        parser.add_argument("CustomVariables", type=dict, default={}, location="json")
        # 渠道会话（企微 / 微信 Bot 等）：vendor 侧才是权威数据源，
        # 本地不落地 chat_conversation，避免污染 /chat/conversations 侧栏列表。
        parser.add_argument("IsChannel", type=bool, default=False, location="json")
        parser.add_argument("ClientRequestId", type=str, location="json")
        args = parser.parse_args(request)
        application_id = args['ApplicationId']
        request_digest = None
        if tagentic_config.WORKBENCH_MODE:
            if args['IsChannel']:
                raise SanicException(
                    "channel conversations are disabled in workbench mode",
                    status_code=403,
                )
            application_id = request.ctx.workbench_app_context.application_id
            args['CustomVariables'] = {}
            request_digest = WorkbenchTurnManager.request_digest(
                {
                    "Contents": body.get("Contents"),
                    "ConversationId": args["ConversationId"] or None,
                    "ApplicationId": application_id,
                    "SearchNetwork": bool(args["SearchNetwork"]),
                    "IsChannel": False,
                }
            )
            try:
                args['Contents'] = await WorkbenchFileOwnership.prepare_chat_contents(
                    request.ctx.db,
                    contents=args['Contents'],
                    account_id=request.ctx.account_id,
                    identity=request.ctx.workbench_context,
                    app_context=request.ctx.workbench_app_context,
                    conversation_id=args['ConversationId'],
                )
                if args['ConversationId']:
                    conversation = await CoreConversation.get_owned(
                        request.ctx.db,
                        request.ctx.account_id,
                        application_id,
                        args['ConversationId'],
                        workbench_identity=request.ctx.workbench_context,
                        workbench_app_context=request.ctx.workbench_app_context,
                    )
                    if conversation is None:
                        raise WorkbenchFileOwnershipError("conversation not found", 404)
                if any(content.get("Type") == "file" for content in args['Contents']):
                    WorkbenchPolicy.require_capability(
                        request.ctx.workbench_app_context,
                        "files",
                    )
            except WorkbenchFileOwnershipError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
        vendor_app = app.get_vendor_app(application_id)

        workbench_submission = None
        if tagentic_config.WORKBENCH_MODE:
            try:
                limits = WorkbenchPolicy.validate_new_turn(
                    request.ctx.workbench_context,
                    request.ctx.workbench_app_context,
                    search_network=args['SearchNetwork'],
                )
                if tagentic_config.WORKBENCH_INTEGRATIONS_ENABLED:
                    await WorkbenchIntegrations.revalidate_turn(
                        request.ctx.db,
                        account_id=request.ctx.account_id,
                        identity=request.ctx.workbench_context,
                        app_context=request.ctx.workbench_app_context,
                    )
                workbench_submission = await WorkbenchTurnManager.create_or_get(
                    request.ctx.db,
                    account_id=request.ctx.account_id,
                    identity=request.ctx.workbench_context,
                    app_context=request.ctx.workbench_app_context,
                    client_request_id=args["ClientRequestId"],
                    request_digest=request_digest,
                    conversation_id=args["ConversationId"],
                )
                if workbench_submission.created:
                    WorkbenchTurnManager.schedule(
                        turn_id=workbench_submission.turn_id,
                        vendor_app=vendor_app,
                        account_id=request.ctx.account_id,
                        identity=request.ctx.workbench_context,
                        app_context=request.ctx.workbench_app_context,
                        claims=dict(request.ctx.session_claims),
                        contents=args["Contents"],
                        conversation_id=args["ConversationId"],
                        search_network=args["SearchNetwork"],
                        custom_variables=args["CustomVariables"],
                        limits=limits,
                    )
            except (WorkbenchPolicyError, WorkbenchTurnError, WorkbenchIntegrationError) as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            except Exception as error:
                logging.error(
                    "[ChatMessageApi] durable Turn submission failed: error_type=%s",
                    type(error).__name__,
                )
                raise SanicException(
                    "workbench Turn submission failed closed",
                    status_code=503,
                ) from error

        logging.info(
            "[ChatMessageApi] ApplicationId=%s, ContentCount=%s, IsChannel=%s",
            application_id,
            len(args['Contents']),
            args['IsChannel'],
        )

        async def streaming_fn(response):
            if tagentic_config.WORKBENCH_MODE:
                try:
                    await WorkbenchTurnManager.replay(
                        response.write,
                        turn_id=workbench_submission.turn_id,
                        account_id=request.ctx.account_id,
                        identity=request.ctx.workbench_context,
                        app_context=request.ctx.workbench_app_context,
                        claims=dict(request.ctx.session_claims),
                        method="POST",
                        resource_path="/chat/message",
                    )
                except asyncio.CancelledError:
                    logging.info(
                        "[ChatMessageApi] subscriber disconnected from Turn %s",
                        workbench_submission.turn_id,
                    )
                    raise
                return
            chat_gen = CoreChat.message(
                vendor_app,
                request.ctx.account_id,
                args['Contents'],
                args['ConversationId'],
                args['SearchNetwork'],
                args['CustomVariables'],
                is_channel=args['IsChannel'],
            )
            try:
                async for data in chat_gen:
                    await response.write(data)
            except asyncio.CancelledError:
                logging.info("[ChatMessageApi] Client disconnected, closing upstream")
                raise
            finally:
                await chat_gen.aclose()
        headers = {"Cache-Control": "no-store"}
        if workbench_submission is not None:
            headers["X-Workbench-Turn-Id"] = workbench_submission.turn_id
            headers["X-Workbench-Client-Request-Id"] = (
                workbench_submission.client_request_id
            )
        return ResponseStream(
            streaming_fn,
            content_type='text/event-stream; charset=utf-8',
            headers=headers,
        )


class WorkbenchTurnEventsApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        if not tagentic_config.WORKBENCH_MODE:
            raise SanicException("workbench Turn not found", status_code=404)
        turn_id = str(request.args.get("TurnId") or "").strip()
        if not turn_id or len(turn_id) > 64:
            raise SanicException("TurnId is required", status_code=400)
        raw_last_event_id = (
            request.headers.get("Last-Event-ID")
            or request.args.get("LastEventId")
            or "0"
        )
        try:
            last_event_id = int(raw_last_event_id)
        except (TypeError, ValueError) as error:
            raise SanicException("Last-Event-ID is invalid", status_code=400) from error
        if last_event_id < 0:
            raise SanicException("Last-Event-ID is invalid", status_code=400)
        turn = await WorkbenchTurnManager.get_owned(
            request.ctx.db,
            turn_id=turn_id,
            account_id=request.ctx.account_id,
            identity=request.ctx.workbench_context,
            app_context=request.ctx.workbench_app_context,
        )
        if turn is None:
            raise SanicException("workbench Turn not found", status_code=404)

        async def streaming_fn(response):
            await WorkbenchTurnManager.replay(
                response.write,
                turn_id=turn_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                after_sequence=last_event_id,
                claims=dict(request.ctx.session_claims),
                method="GET",
                resource_path="/chat/turn/events",
            )

        return ResponseStream(
            streaming_fn,
            content_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Workbench-Turn-Id": turn_id,
            },
        )


class WorkbenchTurnCancelApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        if not tagentic_config.WORKBENCH_MODE:
            raise SanicException("workbench Turn not found", status_code=404)
        body = request.json if isinstance(request.json, dict) else {}
        unknown = sorted(set(body).difference({"TurnId", "ReasonCode"}))
        if unknown:
            raise SanicException(
                f"unsupported cancellation fields: {', '.join(unknown)}",
                status_code=400,
            )
        turn_id = str(body.get("TurnId") or "").strip()
        if not turn_id or len(turn_id) > 64:
            raise SanicException("TurnId is required", status_code=400)
        reason_code = str(body.get("ReasonCode") or "user_stop").strip()
        try:
            result = await WorkbenchTurnManager.request_cancel(
                request.ctx.db,
                turn_id=turn_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                reason_code=reason_code,
            )
        except WorkbenchTurnError as error:
            raise SanicException(str(error), status_code=error.status_code) from error
        return sanic.json(
            {
                "TurnId": result.turn_id,
                "Status": result.status,
                "ProviderCancelSupported": result.provider_cancel_supported,
                "BackgroundConsumptionContinues": result.status == "cancel_requested",
            },
            status=202 if result.status == "cancel_requested" else 200,
            headers={"Cache-Control": "no-store"},
        )


class ChatMessageListApi(HTTPMethodView):
    async def get(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ConversationId", type=str, required=False, location="args")
        parser.add_argument("LastRecordId", type=str, required=False, location="args")
        parser.add_argument("ShareId", type=str, required=False, location="args")
        args = parser.parse_args(request)

        if args["ConversationId"] is not None:
            claims = check_login(request)
            if tagentic_config.WORKBENCH_MODE:
                await authorize_workbench_request(request, claims)
                try:
                    WorkbenchPolicy.require_capability(
                        request.ctx.workbench_app_context,
                        "chat",
                    )
                except WorkbenchPolicyError as error:
                    raise SanicException(str(error), status_code=error.status_code) from error
            if tagentic_config.WORKBENCH_MODE:
                read_context = await CoreConversation.get_read_context(
                    request.ctx.db,
                    request.ctx.account_id,
                    args['ConversationId'],
                    request.ctx.workbench_context,
                    request.ctx.workbench_app_context,
                )
                application_id = read_context.application_id
                if application_id != request.ctx.workbench_app_context.application_id:
                    try:
                        history_context = await WorkbenchControlClient.get_app_context(
                            binding_id=request.ctx.workbench_context.binding_id,
                            canonical_subject=request.ctx.workbench_context.canonical_subject,
                            auth_epoch=request.ctx.workbench_context.auth_epoch,
                            requested_app_profile_id=int(read_context.app_profile_id),
                            requested_config_version=read_context.config_version,
                            current_app_profile_id=int(request.ctx.workbench_app_context.app_profile_id),
                            current_config_version=request.ctx.workbench_app_context.config_version,
                            purpose="history_read",
                        )
                        if (
                            history_context.application_id != application_id
                            or history_context.app_id != read_context.provider_app_id
                        ):
                            raise WorkbenchControlError("historical App tuple mismatched", 404)
                        await WorkbenchAppResolver.ensure_vendor(history_context)
                    except WorkbenchControlError as error:
                        status = 404 if error.status_code in {403, 404, 409, 410} else error.status_code
                        raise SanicException("conversation not found", status_code=status) from error
            else:
                application_id = await CoreConversation.get_application_id(
                    request.ctx.db,
                    request.ctx.account_id,
                    args['ConversationId'],
                )
            vendor_app = app.get_vendor_app(application_id)

            if tagentic_config.WORKBENCH_MODE:
                if not hasattr(vendor_app, 'get_messages_v2'):
                    raise SanicException(
                        "workbench history provider contract is unavailable",
                        status_code=503,
                    )
                try:
                    result = await vendor_app.get_messages_v2(
                        request.ctx.db,
                        request.ctx.workbench_context.canonical_subject,
                        args['ConversationId'],
                        app.config.CHAT_MESSAGE_PAGE_SIZE,
                        args['LastRecordId'],
                    )
                except WorkbenchSecureFileError as error:
                    raise SanicException(
                        str(error),
                        status_code=error.status_code,
                    ) from error
                return sanic.json(
                    {
                        'Response': {
                            'ApplicationId': application_id,
                            'Records': result['Records'],
                            'HasMoreBefore': result['HasMoreBefore'],
                            'LastRecordId': result['LastRecordId'],
                        }
                    }
                )

            # 判断是否为 claw 模式：优先从 apps_info 缓存查找，找不到时动态获取
            is_claw = False
            for info in getattr(request.ctx, 'apps_info', []):
                if hasattr(info, 'ApplicationId'):
                    app_id = info.ApplicationId
                else:
                    app_id = info.get('ApplicationId', '')
                pattern = info.Pattern if hasattr(info, 'Pattern') else info.get('Pattern', '')
                if app_id == application_id and pattern == 'ClawAgent':
                    is_claw = True
                    break

            # 缓存中未找到该应用信息时，通过 vendor_app 动态获取 Pattern
            if not is_claw:
                def _get_app_id(info):
                    if hasattr(info, 'ApplicationId'):
                        return info.ApplicationId
                    return info.get('ApplicationId', '')

                found_in_cache = any(
                    _get_app_id(info) == application_id
                    for info in getattr(request.ctx, 'apps_info', [])
                )
                if not found_in_cache and hasattr(vendor_app, 'get_info'):
                    try:
                        app_info = await vendor_app.get_info()
                        if getattr(app_info, 'Pattern', None) == 'ClawAgent':
                            is_claw = True
                    except (OSError, ValueError, KeyError, AttributeError) as e:
                        if tagentic_config.WORKBENCH_MODE:
                            logging.warning(
                                "[ChatApi] workbench get_info failed: error_type=%s",
                                type(e).__name__,
                            )
                        else:
                            logging.warning(
                                '[ChatApi] get_info failed for %s: %s',
                                application_id,
                                e,
                            )

            if is_claw and hasattr(vendor_app, 'get_messages_v2'):
                # claw 模式：通过 DescribeConversationMessageList 获取完整 V2 数据
                try:
                    result = await vendor_app.get_messages_v2(
                        request.ctx.db,
                        request.ctx.account_id,
                        args['ConversationId'],
                        app.config.CHAT_MESSAGE_PAGE_SIZE,
                        args['LastRecordId']
                    )
                except WorkbenchSecureFileError as error:
                    raise SanicException(
                        str(error),
                        status_code=error.status_code,
                    ) from error
                resp = {
                    'Response': {
                        'ApplicationId': application_id,
                        'Records': result['Records'],
                        'HasMoreBefore': result['HasMoreBefore'],
                        'LastRecordId': result['LastRecordId'],
                    }
                }
            else:
                # standard 模式：通过 GetMsgRecord 获取历史消息
                try:
                    records = await vendor_app.get_messages(
                        request.ctx.db,
                        request.ctx.account_id,
                        args['ConversationId'],
                        app.config.CHAT_MESSAGE_PAGE_SIZE,
                        args['LastRecordId']
                    )
                except WorkbenchSecureFileError as error:
                    raise SanicException(
                        str(error),
                        status_code=error.status_code,
                    ) from error
                resp = {
                    'Response': {
                        'ApplicationId': application_id,
                        'Records': records,
                    }
                }
            return sanic.json(resp)

        if args["ShareId"] is not None:
            if tagentic_config.WORKBENCH_MODE:
                raise SanicException("shared conversations are disabled in workbench mode", status_code=403)
            if args['LastRecordId'] is not None:
                # temporarily disable pagination loading for the share API
                result = []
            else:
                conversation = await CoreShareConversation.list(request.ctx.db, args["ShareId"])
                result = conversation.to_dict()
            return sanic.json({"Response": result})

        raise SanicException('ConversationId or ShareId is required')


class ChatConversationListApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=False, location="args")
        args = parser.parse_args(request)

        application_id = args["ApplicationId"]
        if tagentic_config.WORKBENCH_MODE:
            try:
                WorkbenchPolicy.require_capability(
                    request.ctx.workbench_app_context,
                    "chat",
                )
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            if application_id and application_id != request.ctx.workbench_app_context.application_id:
                raise SanicException("ApplicationId is outside the active workbench context", status_code=403)
            application_id = request.ctx.workbench_app_context.application_id

        conversations = await CoreConversation.list(
            request.ctx.db,
            request.ctx.account_id,
            application_id=application_id,
            workbench_identity=(
                request.ctx.workbench_context
                if tagentic_config.WORKBENCH_MODE
                else None
            ),
            workbench_app_context=(
                request.ctx.workbench_app_context
                if tagentic_config.WORKBENCH_MODE
                else None
            ),
        )
        return sanic.json([conversation.to_dict() for conversation in conversations])


class ChatConversationDeleteApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ConversationId", type=str, required=True, location="json")
        args = parser.parse_args(request)

        if tagentic_config.WORKBENCH_MODE:
            try:
                WorkbenchPolicy.require_capability(
                    request.ctx.workbench_app_context,
                    "chat",
                )
                WorkbenchPolicy.require_active(request.ctx.workbench_context)
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            conversation_application_id = await CoreConversation.get_application_id(
                request.ctx.db,
                request.ctx.account_id,
                args["ConversationId"],
                workbench_identity=request.ctx.workbench_context,
                workbench_app_context=request.ctx.workbench_app_context,
            )
            if conversation_application_id != request.ctx.workbench_app_context.application_id:
                raise SanicException("conversation not found", status_code=404)

        await CoreConversation.delete(
            request.ctx.db,
            request.ctx.account_id,
            args["ConversationId"],
            workbench_identity=(
                request.ctx.workbench_context
                if tagentic_config.WORKBENCH_MODE
                else None
            ),
            workbench_app_context=(
                request.ctx.workbench_app_context
                if tagentic_config.WORKBENCH_MODE
                else None
            ),
        )
        return sanic.json({"Success": 1})


app.add_route(ChatMessageApi.as_view(), "/chat/message")
app.add_route(WorkbenchTurnEventsApi.as_view(), "/chat/turn/events")
app.add_route(WorkbenchTurnCancelApi.as_view(), "/chat/turn/cancel")
app.add_route(ChatMessageListApi.as_view(), "/chat/messages")
app.add_route(ChatConversationListApi.as_view(), "/chat/conversations")
app.add_route(ChatConversationDeleteApi.as_view(), "/chat/conversation/delete")
