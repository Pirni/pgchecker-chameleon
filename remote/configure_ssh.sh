#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
fi

cat > "$HOME/.ssh/config" <<'EOF'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
EOF

chmod 600 "$HOME/.ssh/config"

echo "SSH configured on $(hostname)"
echo "Public key:"
cat "$HOME/.ssh/id_ed25519.pub"