"""Threaded WSGI server used by both services.

``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)``, a reverse
DNS lookup that can stall for tens of seconds on managed / VPN laptops (measured
~35 s here). A typist-machine sidecar cannot afford that at boot, so we skip it.
For the cloud deployment the central app would sit behind gunicorn instead.
"""

from __future__ import annotations

import socketserver

from werkzeug.serving import ThreadedWSGIServer


class FastBindWSGIServer(ThreadedWSGIServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def make_server(host: str, port: int, app) -> FastBindWSGIServer:
    return FastBindWSGIServer(host, port, app)


def serve(app, host: str, port: int) -> None:
    server = make_server(host, port, app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
