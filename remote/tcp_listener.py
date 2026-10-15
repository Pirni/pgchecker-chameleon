"""Short-lived TCP listener for validate_cluster.sh.

Usage: python3 tcp_listener.py PORT SECONDS

Accepts connections on PORT (all interfaces) for at most SECONDS, then exits.
Every connection gets this node's hostname back, so the peer sees which machine
it actually reached.
"""

import socket
import sys
import time

port = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
reply = socket.gethostname().encode() + b"\n"

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", port))
    server.listen(128)

    while (remaining := deadline - time.monotonic()) > 0:
        server.settimeout(remaining)
        try:
            conn, _ = server.accept()
        except socket.timeout:
            break
        with conn:
            conn.sendall(reply)
