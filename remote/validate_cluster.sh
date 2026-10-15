#!/usr/bin/env bash
# Validates what configure_cluster set up. Runs on the head. Starts a
# short-lived TCP listener on every node, then lets every node check every node
# (check_peers.sh): /etc/hosts resolution, node-to-node SSH, and TCP through
# the firewall. Read-only: changes no rules.
set -euo pipefail

HOSTS_FILE="$1"

# Any port other than 22: the firewall trusts the peers by source address, so
# getting through on an arbitrary high port shows that all ports between the
# peers are open, not just SSH.
PORT="${VALIDATE_PORT:-47913}"
LISTEN_SECONDS=300
PID_FILE="/tmp/validate_tcp_listener.pid"

REMOTE_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH_OPTS=(-n -o BatchMode=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

IPS=()
while read -r ip _; do
    IPS+=("$ip")
done < "$HOSTS_FILE"

stop_listeners() {
    for ip in "${IPS[@]}"; do
        ssh "${SSH_OPTS[@]}" "$ip" \
            "kill \$(cat $PID_FILE) 2>/dev/null; rm -f $PID_FILE" || true
    done
}
trap stop_listeners EXIT

echo "Starting TCP listeners on port $PORT"
for ip in "${IPS[@]}"; do
    ssh "${SSH_OPTS[@]}" "$ip" "
        set -euo pipefail
        command -v python3 >/dev/null || { echo \"python3 missing on \$(hostname)\" >&2; exit 1; }
        nohup python3 $REMOTE_DIR/tcp_listener.py $PORT $LISTEN_SECONDS \
            >/dev/null 2>&1 </dev/null &
        echo \$! > $PID_FILE
        for _ in \$(seq 1 50); do
            if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && kill -0 \$(cat $PID_FILE) 2>/dev/null; then
                echo \"  listening on \$(hostname):$PORT\"
                exit 0
            fi
            sleep 0.2
        done
        echo \"Listener did not start on \$(hostname):$PORT (port in use?)\" >&2
        exit 1
    "
done

echo
echo "Checking every node pair (resolution, ssh, tcp/$PORT)"
failed=0
for ip in "${IPS[@]}"; do
    ssh "${SSH_OPTS[@]}" "$ip" "bash $REMOTE_DIR/check_peers.sh $HOSTS_FILE $PORT" || failed=1
done

echo
if [ "$failed" -ne 0 ]; then
    echo "Cluster validation FAILED"
    exit 1
fi
echo "Cluster validation completed"
