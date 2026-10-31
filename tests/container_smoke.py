"""在已启动的隔离容器内，通过真实 HTTP 接口验证管理面板。"""

import http.cookiejar
import json
import urllib.error
import urllib.request

base = "http://127.0.0.1:8080"
client = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
)
assert client.open(base + "/healthz", timeout=10).read() == b"ok\n"
assert b'<div id="root">' in client.open(base + "/", timeout=10).read()
try:
    client.open(base + "/api/models", timeout=10)
    raise AssertionError("missing authentication")
except urllib.error.HTTPError as error:
    assert error.code == 401
print("HEALTH_STATIC_401_OK")


def post(path, body):
    """发送真实管理请求，检查 HTTP 状态并返回 JSON。"""
    result = client.open(urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    ), timeout=10)
    assert result.status == 200
    return json.loads(result.read())


try:
    post("/api/login", {"password": "wrong-password"})
    raise AssertionError("wrong password accepted")
except urllib.error.HTTPError as error:
    assert error.code == 401
post("/api/login", {"password": "dummy-admin-password"})
assert client.open(base + "/api/models", timeout=10).status == 200
print("LOGIN_OK")
post("/api/models/providers", {
    "provider": "smoke", "api": "openai-completions",
    "base_url": "http://napcat:8765/v1", "api_key": "dummy-model-key",
})
post("/api/models/items", {
    "provider": "smoke", "model_id": "fake", "context_window": "64000",
    "max_output_tokens": "256",
})
post("/api/models/active", {"provider": "smoke", "model_id": "fake"})
assert json.loads(client.open(base + "/api/models", timeout=10).read())["active"] == "smoke/fake"
print("MODEL_SETUP_OK")
