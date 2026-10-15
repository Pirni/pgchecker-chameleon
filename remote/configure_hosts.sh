#!/usr/bin/env bash
set -euo pipefail

HOSTS_FILE="$1"

sudo cp /etc/hosts "/etc/hosts.bak.$(date +%s)"

sudo sed -i '/# BEGIN EXPERIMENT CLUSTER/,/# END EXPERIMENT CLUSTER/d' /etc/hosts

{
    echo "# BEGIN EXPERIMENT CLUSTER"
    cat "$HOSTS_FILE"
    echo "# END EXPERIMENT CLUSTER"
} | sudo tee -a /etc/hosts > /dev/null

echo "Updated /etc/hosts"
cat "$HOSTS_FILE"