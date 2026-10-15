#!/usr/bin/env bash
set -euo pipefail

HOSTS_FILE="$1"

echo "Configuring firewall on $(hostname)"

# OpenMPI picks dynamic TCP ports, so the peers are trusted by source address --
# no port range to open, and none to run out of. Everything below is idempotent,
# so the script can be re-run on an already configured node.

# firewalld first. A --permanent change needs a --reload to take effect, and
# that reload flushes the iptables rules added further down -- so it has to
# happen before them. Runtime AND permanent: the runtime set covers the current
# session, the permanent one survives a reload or a firewalld restart. With only
# the runtime set (the previous behaviour) a reload silently dropped the peer
# exceptions while the base rule for port 22 survived, which surfaced as SSH
# working but MPI's dynamic ports failing with "No route to host".
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
    while read -r ip host; do
        sudo firewall-cmd --permanent --zone=trusted --add-source="$ip" || true
    done < "$HOSTS_FILE"

    sudo firewall-cmd --reload || true

    while read -r ip host; do
        sudo firewall-cmd --zone=trusted --add-source="$ip" || true
    done < "$HOSTS_FILE"
fi

# iptables: -C first, so repeated runs do not stack duplicate rules.
while read -r ip host; do
    sudo iptables -C INPUT -p tcp -s "$ip" -j ACCEPT 2>/dev/null \
        || sudo iptables -I INPUT -p tcp -s "$ip" -j ACCEPT || true
    sudo iptables -C OUTPUT -p tcp -d "$ip" -j ACCEPT 2>/dev/null \
        || sudo iptables -I OUTPUT -p tcp -d "$ip" -j ACCEPT || true
done < "$HOSTS_FILE"

# Persist them: on their own the rules live in the running kernel only, and
# anything that rebuilds the filter chains drops them (same symptom as above).
if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save || true
elif command -v iptables-save >/dev/null 2>&1; then
    sudo mkdir -p /etc/iptables
    sudo sh -c 'iptables-save > /etc/iptables/rules.v4' || true
fi

sudo iptables -S || true
