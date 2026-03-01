"""Tests for node.register_system_skills() — one-method registration for system skills."""
import pytest
from unittest.mock import AsyncMock, patch, call
from knarr.dht.node import DHTNode


@pytest.mark.asyncio
async def test_register_system_skills_registers_mail():
    """register_system_skills() with default mail config registers knarr-mail."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock(return_value="knarr-mail")
    config = {"mail": {}}
    await node.register_system_skills(config)

    assert "knarr-mail" in node._handlers
    # Find the knarr-mail call (knarr-static is also registered)
    mail_calls = [c for c in node.announce.call_args_list
                  if c[0][0]["name"] == "knarr-mail"]
    assert len(mail_calls) == 1
    sheet = mail_calls[0][0][0]
    assert sheet["version"] == "1.0.0"
    assert node._skill_visibility.get("knarr-mail") == "public"


@pytest.mark.asyncio
async def test_register_system_skills_registers_static():
    """register_system_skills() registers knarr-static by default."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock(return_value="knarr-static")
    config = {}
    await node.register_system_skills(config)

    assert "knarr-static" in node._handlers
    static_calls = [c for c in node.announce.call_args_list
                    if c[0][0]["name"] == "knarr-static"]
    assert len(static_calls) == 1
    sheet = static_calls[0][0][0]
    assert sheet["price"] == 0.0
    assert "system" in sheet["tags"]


@pytest.mark.asyncio
async def test_register_system_skills_static_disabled():
    """register_system_skills() skips knarr-static when disabled."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock(return_value="knarr-mail")
    config = {"static": {"enabled": False}}
    await node.register_system_skills(config)

    assert "knarr-static" not in node._handlers


@pytest.mark.asyncio
async def test_register_system_skills_respects_mail_disabled():
    """register_system_skills() skips mail when accept_from=none."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock()
    config = {"mail": {"accept_from": "none"}, "static": {"enabled": False}}
    await node.register_system_skills(config)

    assert "knarr-mail" not in node._handlers
    assert "knarr-static" not in node._handlers
    node.announce.assert_not_called()


@pytest.mark.asyncio
async def test_register_system_skills_custom_price():
    """register_system_skills() uses mail price from config."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock(return_value="knarr-mail")
    config = {"mail": {"price": 2.5}}
    await node.register_system_skills(config)

    mail_calls = [c for c in node.announce.call_args_list
                  if c[0][0]["name"] == "knarr-mail"]
    sheet = mail_calls[0][0][0]
    assert sheet["price"] == 2.5


@pytest.mark.asyncio
async def test_register_system_skills_empty_config():
    """register_system_skills() with empty config still registers mail and static."""
    node = DHTNode("127.0.0.1", 0, storage_path=":memory:")
    node.announce = AsyncMock(return_value="ok")
    await node.register_system_skills({})

    assert "knarr-mail" in node._handlers
    assert "knarr-static" in node._handlers
    assert node.announce.call_count == 2
