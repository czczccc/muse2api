"""Run: python tests/test_chat_binding.py ; no live account needed."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSE2API_HOME"] = tempfile.mkdtemp()
os.environ["MUSE2API_KEY"] = "k"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as engine_mod  # noqa: E402
import wx_bridge  # noqa: E402


def test_close_page_keeps_chat_binding():
    e = object.__new__(engine_mod.MuseEngine)
    e.page, e.current_context_id, e.current_acc_id, e.browser = None, None, "a1", None
    e._contexts_lock = __import__("threading").Lock()
    e._active_contexts = set()
    e._chat_urls = {"a1": "https://muse.ai/thread/abc"}
    e._close_page()
    assert e._chat_urls == {"a1": "https://muse.ai/thread/abc"}, "每次请求结束关页面不能丢掉会话绑定"
    e.forget_chat_binding()
    assert e._chat_urls == {}


def test_turn_media_is_forwarded():
    turn = [{"k": "t", "v": "随机到一张雪山温泉"},
            {"k": "m", "kind": "image", "src": "blob:x", "data": b"\x89PNG", "mime": "image/png"},
            {"k": "t", "v": "想要其他风格告诉我"},
            {"k": "m", "kind": "image", "src": "blob:y", "data": None}]
    r = wx_bridge.wx_turn_replies(turn, [])
    kinds = [x[0] for x in r]
    assert kinds == ["text", "media", "text", "text"], kinds
    assert r[1][2] == "image" and os.path.isfile(r[1][1])
    assert "没能取回" in r[3][1]


if __name__ == "__main__":
    test_close_page_keeps_chat_binding()
    test_turn_media_is_forwarded()
    print("PASS")
