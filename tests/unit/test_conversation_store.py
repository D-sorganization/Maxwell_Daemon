from pathlib import Path

from maxwell_daemon.backends.base import Message, MessageRole
from maxwell_daemon.conversation.store import JsonConversationStore, SqliteConversationStore


def test_json_conversation_store(tmp_path: Path):
    store = JsonConversationStore(tmp_path)

    # Test load non-existent
    assert store.load("test_1") == []

    # Test save and load
    msg = Message(role=MessageRole.USER, content="Hello", name=None, tool_call_id=None, metadata={})
    store.save("test_1", [msg])

    loaded = store.load("test_1")
    assert len(loaded) == 1
    assert loaded[0].role == MessageRole.USER
    assert loaded[0].content == "Hello"

    # Test append
    msg2 = Message(
        role=MessageRole.ASSISTANT, content="Hi", name=None, tool_call_id=None, metadata={}
    )
    store.append("test_1", msg2)
    loaded = store.load("test_1")
    assert len(loaded) == 2
    assert loaded[1].content == "Hi"

    # Test list
    assert store.list_ids() == ["test_1"]

    # Test delete
    store.delete("test_1")
    assert store.load("test_1") == []
    assert store.list_ids() == []


def test_sqlite_conversation_store(tmp_path: Path):
    store = SqliteConversationStore(tmp_path / "test.db")

    # Test load non-existent
    assert store.load("test_1") == []

    # Test save and load
    msg = Message(role=MessageRole.USER, content="Hello", name=None, tool_call_id=None, metadata={})
    store.save("test_1", [msg])

    loaded = store.load("test_1")
    assert len(loaded) == 1
    assert loaded[0].role == MessageRole.USER
    assert loaded[0].content == "Hello"

    # Test list
    assert store.list_ids() == ["test_1"]

    # Test append
    msg2 = Message(
        role=MessageRole.ASSISTANT, content="Hi", name=None, tool_call_id=None, metadata={}
    )
    store.append("test_1", msg2)
    loaded = store.load("test_1")
    assert len(loaded) == 2

    # Test delete
    store.delete("test_1")
    assert store.load("test_1") == []
    assert store.list_ids() == []
