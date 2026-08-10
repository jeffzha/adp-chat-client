import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import tagentic_config
from vendor.interface import FileSizeLimitExceeded
from vendor.tcadp.tcadp import ProviderFileStream, TCADP


class _Chunks:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


@pytest.fixture(autouse=True)
def _workspace_host_allowlist(monkeypatch):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_HOST_SUFFIXES",
        "example.com",
    )


@pytest.mark.parametrize(
    "domain",
    (
        "http://workspace.example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://169.254.169.254/latest/meta-data",
        "https://user:password@workspace.example.com",
        "https://workspace.example.com:8443",
        "https://workspace.example.com?redirect=https://attacker.example",
        "https://workspace.example.com/attacker-controlled-path",
    ),
)
def test_workspace_domain_rejects_non_tls_private_and_ambiguous_endpoints(domain):
    with pytest.raises(ValueError, match="workspace domain"):
        TCADP._workspace_domain(domain)


def test_workspace_credential_accepts_only_a_bounded_safe_header():
    assert TCADP._workspace_credential(
        {
            "Credential": {"AccessToken": "short-lived-token"},
            "SandboxStorage": {
                "Domain": "https://workspace.example.com",
                "TokenTag": "X-File-Ticket",
            },
        }
    ) == (
        "https://workspace.example.com",
        "X-File-Ticket",
        "short-lived-token",
    )

    with pytest.raises(ValueError, match="credential response"):
        TCADP._workspace_credential(
            {
                "Credential": {"AccessToken": "token"},
                "SandboxStorage": {
                    "Domain": "https://workspace.example.com",
                    "TokenTag": "X-File-Ticket\r\nX-Injected",
                },
            }
        )

    with pytest.raises(ValueError, match="credential response"):
        TCADP._workspace_credential(
            {
                "Credential": {"AccessToken": "token"},
                "SandboxStorage": {
                    "Domain": "https://workspace.example.com",
                    "TokenTag": "Authorization",
                },
            }
        )

    with pytest.raises(ValueError, match="credential response"):
        TCADP._workspace_credential(
            {
                "Credential": {"AccessToken": "token\r\nX-Injected: yes"},
                "SandboxStorage": {
                    "Domain": "https://workspace.example.com",
                    "TokenTag": "X-File-Ticket",
                },
            }
        )


def test_workbench_workspace_credential_uses_documented_api_conversation_context(
    monkeypatch,
):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    vendor = TCADP(
        {
            "AppKey": "provider-app-key",
            "ServiceVendor": "ChinaTencentADP",
        },
        "local-app",
    )

    assert vendor._workspace_credential_payload(
        app_id="provider-app",
        workspace_id="workspace-opaque",
        user_id="napi:prod:customer:7:user:9",
    ) == {
        "AppId": "provider-app",
        "AppKey": "provider-app-key",
        "Type": 5,
        "UserId": "napi:prod:customer:7:user:9",
        "WorkspaceId": "workspace-opaque",
    }

    with pytest.raises(ValueError, match="context is incomplete"):
        vendor._workspace_credential_payload(
            app_id="provider-app",
            workspace_id="workspace-opaque",
        )


def test_legacy_workspace_credential_keeps_evaluation_contract(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", False)
    vendor = TCADP({"AppKey": "legacy-key"}, "legacy-app")

    assert vendor._workspace_credential_payload(
        app_id="provider-app",
        workspace_id="workspace-opaque",
    ) == {
        "AppId": "provider-app",
        "Type": 2,
        "WorkspaceId": "workspace-opaque",
    }


def test_workspace_domain_requires_an_explicit_verified_suffix(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_WORKSPACE_HOST_SUFFIXES", "")

    with pytest.raises(ValueError, match="configured allowlist"):
        TCADP._workspace_domain("https://workspace.example.com")


@pytest.mark.parametrize("suffix", ("com", "example..com", "-example.com"))
def test_workspace_domain_rejects_broad_or_malformed_suffixes(monkeypatch, suffix):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_WORKSPACE_HOST_SUFFIXES", suffix)

    with pytest.raises(ValueError, match="configured allowlist"):
        TCADP._workspace_domain("https://workspace.example.com")


@pytest.mark.asyncio
async def test_workspace_response_reader_enforces_the_limit_across_chunks():
    assert await TCADP._read_bounded(
        _Chunks((b"abc", b"def")),
        6,
        "too large",
    ) == b"abcdef"

    with pytest.raises(FileSizeLimitExceeded, match="too large"):
        await TCADP._read_bounded(
            _Chunks((b"abc", b"def", b"g")),
            6,
            "too large",
        )


@pytest.mark.asyncio
async def test_provider_file_stream_is_bounded_and_closes_upstream_once():
    response = SimpleNamespace(
        content=_Chunks((b"abc", b"def", b"g")),
        release=Mock(),
    )
    session = SimpleNamespace(close=AsyncMock())
    stream = ProviderFileStream(
        session=session,
        response=response,
        max_bytes=6,
        content_type="text/plain",
        file_name="report.txt",
    )

    with pytest.raises(FileSizeLimitExceeded, match="download limit"):
        _ = [chunk async for chunk in stream.iter_chunks()]
    await stream.close()
    await stream.close()

    response.release.assert_called_once_with()
    session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_workbench_mode_rejects_legacy_buffered_file_paths(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    vendor = TCADP({"AppKey": "provider-app-key"}, "local-app")

    with pytest.raises(ValueError, match="signed-URL file fetch is disabled"):
        await vendor.fetch_file("provider-app", "workspace", "/workdir/report.txt")
    with pytest.raises(ValueError, match="buffered file download is disabled"):
        await vendor.download_file_content(
            "provider-app",
            "workspace",
            "/workdir/report.txt",
            user_id="napi:prod:customer:7:user:9",
        )


@pytest.mark.asyncio
async def test_workspace_dns_rejects_any_private_result_and_pins_public_results(
    monkeypatch,
):
    getaddrinfo = AsyncMock(
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]
    )
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(getaddrinfo=getaddrinfo),
    )

    with pytest.raises(ValueError, match="outside the public Internet"):
        await TCADP._resolve_workspace_addresses("workspace.example.com")

    getaddrinfo.return_value = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]
    assert await TCADP._resolve_workspace_addresses("workspace.example.com") == (
        (socket.AF_INET, "93.184.216.34"),
    )
    getaddrinfo.assert_awaited_with(
        "workspace.example.com",
        443,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
