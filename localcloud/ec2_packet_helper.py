"""Firewall namespace holder and bounded TCP relay for opt-in Docker EC2.

Run only in a dedicated, owned container network namespace. This module never
joins or configures the host network namespace.
"""
import base64
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

READY = Path('/tmp/localcloud-packet-ready')


def install(rules, ipv6=None):
    subprocess.run(['iptables-restore', '--wait', '5'], input=rules.encode(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    if ipv6 is not None:
        subprocess.run(['ip6tables-restore', '--wait', '5'], input=ipv6.encode(), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)


def lockdown():
    return '*filter\n:INPUT DROP [0:0]\n:FORWARD DROP [0:0]\n:OUTPUT DROP [0:0]\n-A INPUT -i lo -j ACCEPT\n-A OUTPUT -o lo -j ACCEPT\nCOMMIT\n'


def forward(port):
    """Relay stdin/stdout to guest loopback; no remote destination is accepted."""
    if not 1 <= port <= 65535:
        raise ValueError('Invalid TCP relay port')
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.setblocking(False)
        inputs = [sys.stdin.buffer, connection]
        while inputs:
            ready, _, _ = select.select(inputs, [], [], 60)
            if not ready:
                continue
            for source in ready:
                if source is connection:
                    data = connection.recv(65536)
                    if not data:
                        return
                    sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
                else:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        inputs.remove(sys.stdin.buffer)
                        connection.shutdown(socket.SHUT_WR)
                    else:
                        connection.setblocking(True)
                        connection.sendall(data)
                        connection.setblocking(False)


def main():
    if sys.argv[1:2] == ['--forward'] and len(sys.argv) == 3:
        forward(int(sys.argv[2])); return
    if sys.argv[1:] == ['--deny']:
        install(lockdown(), lockdown()); return
    if sys.argv[1:2] == ['--apply'] and len(sys.argv) == 3:
        install(base64.b64decode(sys.argv[2]).decode()); return
    if sys.argv[1:] != ['--hold']:
        raise ValueError('Expected --hold, --apply BASE64, --deny or --forward PORT')
    install(os.environ['LOCALCLOUD_PACKET_RULES'], lockdown())
    READY.write_text('ready')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
