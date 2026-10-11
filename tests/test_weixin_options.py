"""Run: python tests/test_weixin_options.py ; no live account needed."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSE2API_HOME"] = tempfile.mkdtemp()
os.environ["MUSE2API_KEY"] = "k"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402

OPTS = ["全部删除（保留当前聊天）", "只删打招呼/测试的，保留作品记录", "先归档，手动再决定"]
TEXT = "现在左侧有 45 个旁聊。\n\n想怎么处理？\n\n" + " ".join(OPTS)


def test_format_and_resolve():
    out = app.wx_format_options(TEXT, OPTS)
    assert "1. 全部删除（保留当前聊天）" in out and "3. 先归档，手动再决定" in out
    assert out.count("先归档") == 1  # 拼成一行的原文被去掉了
    assert app.wx_resolve_option("3", OPTS) == OPTS[2]
    assert app.wx_resolve_option("9", OPTS) == "9"
    assert app.wx_resolve_option("全归档", OPTS) == "全归档"


def test_chat_uses_main_chat_and_options():
    seen = {}

    def fake_stream(cookies, prompt, exp, timeout, account_id=None, main_chat=False,
                    fallback_prompt=None, on_options=None, on_prompt=None):
        seen.update(prompt=prompt, main=main_chat, fb=fallback_prompt)
        yield TEXT
        on_options(OPTS)

    app.safe_chat_stream = fake_stream
    app.store.pick_account = lambda **k: {"id": "a", "cookies": {}, "cookies_exp": None}
    out = app._wx_chat("u", "整理旁聊")
    assert seen["prompt"] == "整理旁聊" and seen["main"] is True and "整理旁聊" in seen["fb"]
    assert "2. 只删打招呼" in out
    app._wx_chat("u", "2")
    assert seen["prompt"] == OPTS[1]


def test_permission_prompt_roundtrip():
    import threading
    import time
    pushed = []
    res = {}

    def ask():
        res["idx"] = app.wx_ask_permission("u", "允许 Muse 删除 45 个聊天？", ["允许", "拒绝"],
                                           push=lambda u, m: pushed.append(m), timeout=5)
    th = threading.Thread(target=ask)
    th.start()
    for _ in range(50):
        if pushed:
            break
        time.sleep(0.05)
    assert "1. 允许" in pushed[0] and "2. 拒绝" in pushed[0]
    assert "请回复" in app.wx_answer_prompt("u", "abc")      # 听不懂不会解除等待
    assert app.wx_answer_prompt("u", "1").startswith("已选择")
    th.join(3)
    assert res["idx"] == 0
    assert app.wx_answer_prompt("u", "1") is None            # 已结束，不再拦截普通消息
    # 超时返回 None
    assert app.wx_ask_permission("u", "x", ["允许", "拒绝"], push=lambda u, m: None, timeout=0.1) is None


def test_engine_dialog_handling():
    import engine as E
    eng = object.__new__(E.MuseEngine)
    clicked = []
    eng._poll_dialog = lambda: {"text": "允许吗", "buttons": ["允许", "拒绝"]}
    eng._click_dialog_button = lambda i: clicked.append(i) or True
    eng.handle_dialog(lambda t, b: 0)
    eng.handle_dialog(lambda t, b: None)          # 未答复 → 点拒绝
    eng.handle_dialog(lambda t, b: 9)             # 非法下标 → 点拒绝
    eng._poll_dialog = lambda: {"text": "?", "buttons": ["好的", "继续"]}
    eng.handle_dialog(lambda t, b: None)          # 没有拒绝类按钮 → 点最后一个
    assert clicked == [0, 1, 1, 1], clicked


if __name__ == "__main__":
    test_format_and_resolve()
    test_chat_uses_main_chat_and_options()
    test_permission_prompt_roundtrip()
    test_engine_dialog_handling()
    print("weixin options tests OK")
