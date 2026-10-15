#!/usr/bin/env bash
# Runs pgchecker (Spack-installed, OpenMPI) across the whole cluster for the
# input file given via INPUT_FILE. Called by the experiment runner as:
#   cd <working_dir>
#   NODE_COUNT=.. PROCESSES_PER_NODE=.. bash run.sh
# Results (pgchecker .dat files + pgmpi_info.csv) go to ./data, which must
# match results.remote_dir in the config.
set -euo pipefail

: "${NODE_COUNT:?NODE_COUNT is required}"
: "${PROCESSES_PER_NODE:?PROCESSES_PER_NODE is required}"

# Which pgchecker input to run. Override per invocation with
# `INPUT_FILE=input_large.txt bash run.sh`.
INPUT_FILE="${INPUT_FILE:-input_all.txt}"

TOTAL_PROCESSES=$((NODE_COUNT * PROCESSES_PER_NODE))
CLUSTER_HOSTS="$HOME/cluster-runtime/cluster_hosts"
HOSTFILE="$HOME/hosts.mpi.run"
DATA_DIR="$PWD/data"
PRIVATE_SUBNET="10.140.0.0/16"

if [ ! -f "$INPUT_FILE" ]; then
    echo "Missing input file: $INPUT_FILE (cwd: $PWD)"
    exit 1
fi
if [ ! -f "$CLUSTER_HOSTS" ]; then
    echo "Missing cluster hosts file: $CLUSTER_HOSTS"
    exit 1
fi

# OpenMPI hostfile format is "<host> slots=<procs>". Node names resolve to the
# private 10.140.x IPs via /etc/hosts.
awk -v s="$PROCESSES_PER_NODE" '{print $2 " slots=" s}' "$CLUSTER_HOSTS" > "$HOSTFILE"
echo "MPI hostfile:"
cat "$HOSTFILE"

# Activate Spack and load the pgchecker packages (built against OpenMPI). This
# puts the matching OpenMPI mpirun on PATH ahead of any system mpirun.
. "$HOME/spack/share/spack/setup-env.sh"
spack load pgchecker.reprompi

REPROMPI_PREFIX="$(spack location -i pgchecker.reprompi)"
PGTUNE_PREFIX="$(spack location -i pgchecker.pgmpitunelib)"

mkdir -p "$DATA_DIR"

# pgchecker needs the MPI collectives info produced by pgmpi_info.
"$PGTUNE_PREFIX/bin/pgmpi_info" > "$DATA_DIR/pgmpi_info.csv"

# `-n` and the fd-3 loops below are required: a plain ssh reads stdin and would
# swallow the rest of CLUSTER_HOSTS, so the loop would only run for the head.
SSH_OPTS="-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

# Non-shared filesystem, and pgchecker opens the CSV on every rank.
while read -r ip name <&3; do
    [ -n "${ip:-}" ] || continue
    echo "Distributing pgmpi_info.csv to $name ($ip)"
    ssh $SSH_OPTS "$ip" "mkdir -p '$DATA_DIR'"
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$DATA_DIR/pgmpi_info.csv" "$ip:$DATA_DIR/pgmpi_info.csv"
done 3< "$CLUSTER_HOSTS"

# Pre-flight: what every rank needs. pgchecker's Logger only prints on rank 0, so
# a worker missing one of these would just exit 1 without a message.
PGCHECKER_BIN="$REPROMPI_PREFIX/bin/pgchecker"
echo "Pre-flight (input, pgmpi CSV, binary, libmpi per node):"
while read -r ip name <&3; do
    [ -n "${ip:-}" ] || continue
    echo "  $name ($ip):"
    ssh $SSH_OPTS "$ip" "
        for f in '$PWD/$INPUT_FILE' '$DATA_DIR/pgmpi_info.csv' '$PGCHECKER_BIN'; do
            if [ ! -e \"\$f\" ]; then echo \"    MISSING: \$f\"; exit 1; fi
        done
        echo \"    files ok\"
        echo \"    libmpi: \$(ldd '$PGCHECKER_BIN' | awk '/libmpi\.so/ { print \$3; exit }')\"
    "
done 3< "$CLUSTER_HOSTS"

echo "Running pgchecker: input=$INPUT_FILE processes=$TOTAL_PROCESSES across $NODE_COUNT nodes"

# Per-rank output tagging: renamed in OpenMPI 5, so pick by major version.
MPI_MAJOR="$(mpirun --version 2>/dev/null | head -n 1 | grep -oE '[0-9]+' | head -n 1)"
if [ "${MPI_MAJOR:-4}" -ge 5 ]; then
    OUTPUT_OPTS="--output tag"
else
    OUTPUT_OPTS="--tag-output"
fi
echo "MPI major version: ${MPI_MAJOR:-unknown} -> output option: $OUTPUT_OPTS"

# OpenMPI over the private network (names -> 10.140.x via /etc/hosts):
# - if_include pins the TCP BTL to the private interface; the ConnectX is
#   Ethernet-only, so there is no verbs/IB to probe
# - TCP ports stay dynamic (configure_firewall.sh trusts the peers by source)
# - -x PATH/LD_LIBRARY_PATH so the remote ranks resolve the same Spack OpenMPI
#   as the head, not the system 4.1.6 that `validate` installs
export PGCHECKER_CSV="$DATA_DIR/pgmpi_info.csv"
mpirun \
    --hostfile "$HOSTFILE" \
    -np "$TOTAL_PROCESSES" \
    $OUTPUT_OPTS \
    --mca oob_tcp_if_include "$PRIVATE_SUBNET" \
    --mca btl_tcp_if_include "$PRIVATE_SUBNET" \
    -x PGCHECKER_CSV \
    -x PATH \
    -x LD_LIBRARY_PATH \
    "$REPROMPI_PREFIX/bin/pgchecker" \
    -f "$INPUT_FILE" \
    -o "$DATA_DIR"

find "$DATA_DIR" -type f -ls
