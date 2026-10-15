#!/usr/bin/env bash
# Checks, from this node, every cluster node (itself included):
#   - the name resolves to the expected private IP (/etc/hosts)
#   - node-to-node SSH by name works
#   - a TCP connection by name to the listener validate_cluster.sh started on
#     PORT gets through the firewall
# Prints one line per peer and exits 1 if any check failed.
set -uo pipefail

HOSTS_FILE="$1"
PORT="$2"

SSH_OPTS=(-n -o BatchMode=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

self="$(hostname)"
failed=0

fail() {
    echo "  FAIL $self -> $*"
    failed=1
}

# fd 3: ssh would otherwise read the rest of HOSTS_FILE from stdin.
while read -r ip name <&3; do
    resolved="$(getent hosts "$name" | awk '{ print $1; exit }')"
    if [ "$resolved" != "$ip" ]; then
        fail "$name: resolves to '${resolved:-nothing}', expected $ip"
        continue
    fi

    if ! out="$(ssh "${SSH_OPTS[@]}" "$name" true 2>&1)"; then
        fail "$name ($ip): ssh failed: $out"
        continue
    fi

    if ! reply="$(timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1" && read -r line <&3 && echo "$line"' \
            "$name" "$PORT" 2>&1)"; then
        fail "$name ($ip): no TCP connection on port $PORT ${reply}"
        continue
    fi

    echo "  ok   $self -> $name ($ip): resolve, ssh, tcp/$PORT (reached $reply)"
done 3< "$HOSTS_FILE"

exit "$failed"
