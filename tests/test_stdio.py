import asyncio
from types import SimpleNamespace

import pytest


def test_main_uses_explicit_stdio_transport(monkeypatch, tmp_path):
    from telegram_dot import server

    calls = []

    class FakeConnector:
        def __init__(self, root, transport):
            calls.append(("connector", root, transport))

        def run_stdio(self):
            calls.append(("stdio",))

    monkeypatch.setattr(server, "Connector", FakeConnector)
    monkeypatch.setenv("DOT_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: calls.append(("http",))))

    server.main(["--transport", "stdio"])

    assert calls == [("connector", tmp_path, "stdio"), ("stdio",)]


def test_http_remains_default_and_explicit(monkeypatch, tmp_path):
    from telegram_dot import server

    calls = []

    class FakeConnector:
        def __init__(self, root, transport):
            calls.append(("connector", root, transport))

        def app(self):
            calls.append(("app",))
            return "app"

    monkeypatch.setattr(server, "Connector", FakeConnector)
    monkeypatch.setenv("DOT_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "uvicorn", SimpleNamespace(run=lambda app, **kwargs: calls.append(("http", app))))

    server.main([])

    assert calls == [("connector", tmp_path, "http"), ("app",), ("http", "app")]


@pytest.mark.asyncio
async def test_stdio_registers_all_tools_without_http_base_url(tmp_path, monkeypatch):
    from telegram_dot.server import Connector

    monkeypatch.delenv("DOT_BASE_URL", raising=False)
    monkeypatch.setenv("DOT_OWNER_ID", "123")
    monkeypatch.setenv("DOT_SEND_ENABLED", "false")
    monkeypatch.setenv("DOT_BACKGROUND_MONITORING_ENABLED", "false")

    connector = Connector(tmp_path, client=object(), transport="stdio")
    names = {tool.name for tool in await connector.mcp.list_tools()}

    assert {"save_reply_draft", "list_reply_drafts", "get_reply_draft",
            "preview_saved_messages", "forward_to_saved_messages"} <= names


def test_http_still_requires_base_url(tmp_path, monkeypatch):
    from telegram_dot.server import Connector

    monkeypatch.delenv("DOT_BASE_URL", raising=False)
    monkeypatch.setenv("DOT_OWNER_ID", "123")
    with pytest.raises(KeyError, match="DOT_BASE_URL"):
        Connector(tmp_path, client=object(), transport="http")
