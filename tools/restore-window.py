"""Un-minimize and re-maximize the containerized Chromium window via CDP."""
import json
import urllib.request

import websocket

ws_url = json.load(urllib.request.urlopen("http://localhost:9222/json/version"))["webSocketDebuggerUrl"]
pages = [p for p in json.load(urllib.request.urlopen("http://localhost:9222/json/list")) if p["type"] == "page"]
target_id = pages[0]["id"]

ws = websocket.create_connection(ws_url, suppress_origin=True)

def rpc(method, params=None):
    rpc.i = getattr(rpc, "i", 0) + 1
    ws.send(json.dumps({"id": rpc.i, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == rpc.i:
            return msg

win = rpc("Browser.getWindowForTarget", {"targetId": target_id})
window_id = win["result"]["windowId"]
print("before:", win["result"]["bounds"])
print(rpc("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}}).get("result"))
print(rpc("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "maximized"}}).get("result"))
print("after:", rpc("Browser.getWindowForTarget", {"targetId": target_id})["result"]["bounds"])
ws.close()
