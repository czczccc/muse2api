"""抓取 muse.ai 网页在「发一条消息」时的网络请求，用来研究能否改用内部接口替代 DOM 操作。

用法（在已部署 muse2api 的环境里，已导入至少一个有效账号）：
    docker compose exec muse2api python tools/capture_network.py "你好" --wait 25
    docker compose exec muse2api python tools/capture_network.py "生成一张图片，随机" --wait 120
输出：data/capture-<时间>.jsonl（完整记录）和屏幕上的请求汇总。
Cookie / Authorization / hatch_* 等会话凭证已自动打码，但请求体里可能有你的对话内容，分享前请自己看一眼。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CFG  # noqa: E402
from engine import MuseEngine  # noqa: E402
from store import Store  # noqa: E402

SECRET_HDR = re.compile(r"cookie|authorization|token|auth|session|csrf", re.I)
SECRET_VAL = re.compile(r"(hatch_[a-z_]+=)[^;\s\"']+|(Bearer\s+)[A-Za-z0-9._\-]+", re.I)
SKIP_URL = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|css|map)(\?|$)|/_next/static/|google|doubleclick|facebook", re.I)


def scrub(text, limit=3000):
    if text is None:
        return None
    text = SECRET_VAL.sub(lambda m: (m.group(1) or m.group(2)) + "<redacted>", str(text))
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} 字符)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", help="发送给 muse 的消息")
    ap.add_argument("--wait", type=int, default=30, help="发送后继续记录多少秒")
    ap.add_argument("--account", help="账号 id，默认第一个可用账号")
    a = ap.parse_args()

    store = Store(CFG)
    accs = [x for x in store.accounts if x.get("enabled", True) and x.get("cookies")]
    if a.account:
        accs = [x for x in accs if x["id"] == a.account]
    if not accs:
        sys.exit("没有可用账号，请先导入 cookie")
    acc = accs[0]

    engine = MuseEngine(CFG)
    engine.start()
    page = engine.ensure_page(acc["cookies"], acc.get("cookies_exp"), account_id=acc["id"])

    out_path = os.path.join(CFG.data_dir, time.strftime("capture-%Y%m%d-%H%M%S.jsonl"))
    out = open(out_path, "w", encoding="utf-8")
    reqs: dict[str, dict] = {}
    summary: list[tuple] = []

    def write(rec):
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    def on_event(ev):
        m, p = ev.get("method"), ev.get("params", {})
        t = round(time.time() - t0, 2)
        if m == "Network.requestWillBeSent":
            r = p.get("request", {})
            url = r.get("url", "")
            if SKIP_URL.search(url):
                return
            hdr = {k: ("<redacted>" if SECRET_HDR.search(k) else scrub(v, 300))
                   for k, v in (r.get("headers") or {}).items()}
            reqs[p.get("requestId")] = {"url": url, "method": r.get("method")}
            print(f"  [{t:5.1f}s] {r.get('method'):6} {scrub(url.split('?')[0], 150)}", flush=True)
            write({"t": t, "type": "request", "id": p.get("requestId"), "method": r.get("method"),
                   "url": scrub(url, 500), "headers": hdr, "body": scrub(r.get("postData")),
                   "resource": p.get("type")})
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in reqs:
                r = p.get("response", {})
                reqs[rid]["status"] = r.get("status")
                write({"t": t, "type": "response", "id": rid, "status": r.get("status"),
                       "mime": r.get("mimeType"), "url": scrub(r.get("url"), 500)})
        elif m == "Network.webSocketCreated":
            write({"t": t, "type": "ws_open", "id": p.get("requestId"), "url": scrub(p.get("url"), 500)})
            print(f"  [{t:5.1f}s] WS     {scrub((p.get('url') or '').split('?')[0], 150)}", flush=True)
            summary.append(("WS", p.get("url", ""), "open"))
        elif m in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
            d = (p.get("response") or {})
            write({"t": t, "type": "ws_send" if m.endswith("Sent") else "ws_recv", "id": p.get("requestId"),
                   "opcode": d.get("opcode"), "data": scrub(d.get("payloadData"))})
        elif m == "Network.eventSourceMessageReceived":
            write({"t": t, "type": "sse", "id": p.get("requestId"), "event": p.get("eventName"),
                   "data": scrub(p.get("data"))})

    t0 = time.time()
    page.on_event = on_event
    page.send("Network.enable", {"maxPostDataSize": 20000})
    time.sleep(0.5)
    print(f"账号 {acc.get('label') or acc['id']}，发送：{a.prompt}", flush=True)
    engine._send(a.prompt)
    print(f"已发送，继续记录 {a.wait} 秒（请求会实时打印在下面，结束后再出汇总）…", flush=True)
    end = time.time() + a.wait
    while time.time() < end:
        page.pump(min(5.0, end - time.time()), on_event=on_event, sock_timeout=1.0)
        print(f"  …还剩 {max(0, int(end - time.time()))} 秒，已记录 {len(reqs)} 个请求", flush=True)
    out.close()
    engine.close_current_page()

    print(f"\n记录文件：{out_path}\n请求汇总（方法 状态 地址）：")
    seen = set()
    for r in reqs.values():
        key = (r["method"], r["url"].split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['method']:6} {r.get('status', '-')!s:4} {scrub(r['url'].split('?')[0], 140)}")
    for w in summary:
        print("  ", *w)


if __name__ == "__main__":
    main()
