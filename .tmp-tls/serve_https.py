"""临时 HTTPS 静态服务 + /api 反向代理（一次性的调试工具）。

为什么需要它：
    VAPTCHA 强制页面走 HTTPS，http://127.0.0.1 会返回「当前网站未授权」。
    但页面一旦是 https，再 fetch http://127.0.0.1:8000 就变成混合内容，
    浏览器直接拦掉。所以这里把 /api/ 反代到本地后端 —— 页面同源调接口，
    既不撞混合内容，也不用改后端 CORS 白名单。

用法：python .tmp-tls/serve_https.py
访问：https://127.0.0.1:5443/vaptcha-demo.html （自签证书，浏览器要点「继续前往」）
"""

import functools
import http.client
import http.server
import ssl
from pathlib import Path

PORT = 5443
BACKEND = ("127.0.0.1", 8000)
ROOT = Path(__file__).resolve().parent.parent / "web"

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        conn = http.client.HTTPConnection(*BACKEND, timeout=30)
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }
        try:
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as exc:  # 后端没起时给个能看懂的 502，而不是空响应
            msg = f'{{"code":-1,"message":"反代失败：{exc}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        finally:
            conn.close()

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy("POST")
        self.send_error(405)

    def do_PUT(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy("PUT")
        self.send_error(405)

    def do_DELETE(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy("DELETE")
        self.send_error(405)

    def do_OPTIONS(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy("OPTIONS")
        self.send_error(405)

    def log_message(self, fmt, *args):
        print(f"[https] {self.command} {self.path} -> {fmt % args}", flush=True)


handler = functools.partial(Handler, directory=str(ROOT))
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(
    certfile=str(Path(__file__).parent / "cert.pem"),
    keyfile=str(Path(__file__).parent / "key.pem"),
)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

print(
    f"serving {ROOT} at https://127.0.0.1:{PORT}  (proxy /api -> {BACKEND[0]}:{BACKEND[1]})",
    flush=True,
)
httpd.serve_forever()
