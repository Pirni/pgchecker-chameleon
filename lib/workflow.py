"""Single source of truth for the experiment workflow.

The logic of notebooks 01-07 lives here as callable functions. Both
`main.ipynb` (interactive, "Launch on Chameleon") and `run.py` (headless) are
thin callers of these functions -- no duplicated logic.

Layout:
  Phase 0 (once): prepare_site_and_keypair  (interactive)
  Phase 1 (matrix): run_matrix
      per k (one allocation each):
          select nodes (fresh per allocation; never the previous k's machines)
          provision -> configure_cluster -> validate
          per i: run_experiment -> collect
          finally: delete_servers (always)
                   delete_lease (only if this run created the lease itself)

k is the number of allocations: the machines are reserved k times, and every
allocation runs the i run-commands once. A brought-along lease (reservation_id/
reservation_ids) is reused for all k allocations and never deleted -- only its
servers are torn down and freshly launched each time. A self-created lease is
created and deleted per k allocation.

The selection rules stay with the caller (predicate/sort_key/blacklist) but are
handed to `run_matrix`, which selects per allocation. Two consecutive allocations
never get the same machines: re-deploying hardware released seconds ago fails
with `status ERROR` while Ironic is still cleaning it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from lib import experiment as exp
from lib.chameleon import (
    Cluster,
    cluster_from_dict,
    cluster_to_dict,
    configure_chameleon_site,
    delete_lease as chameleon_delete_lease,
    delete_server as chameleon_delete_server,
    get_node_name,
    provision_cluster,
    select_nodes,
    selected_nodes_to_dict,
)
from lib.config import experiment_plan
from lib.paths import repo_root, runtime_file
from lib.results import collect_results_from_head
from lib.ssh import (
    DEFAULT_PRIVATE_KEY_PATH,
    build_ssh_command,
    ensure_chameleon_keypair,
    run_ssh,
    wait_for_ssh,
)

REMOTE_RUNTIME_DIR = "/home/cc/cluster-runtime"

# Stops the periodic apt jobs for the lifetime of the allocation. A fresh node
# starts unattended-upgrades shortly after boot, which
#   - can restart services such as firewalld mid-experiment; a firewalld
#     restart after configure_firewall.sh drops the peer rules added directly
#     to iptables ("No route to host" between the nodes)
#   - is background load (downloads, package installs) that distorts the
#     measurements.
QUIESCE_APT_COMMAND = r"""
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer || true
"""

OnStep = Callable[[str], None]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _head_ip(cluster: Cluster) -> str:
    ip = cluster.head.floating_ip
    if not ip:
        raise RuntimeError("Head node has no Floating IP")
    return ip


def _node_target(node: Any, head_ip: str) -> tuple[str | None, str | None]:
    """(host, proxy_jump) for a node. Head directly, workers via the head."""
    if node.is_head:
        return node.floating_ip, None
    return node.private_ip, f"cc@{head_ip}"


def _rsync_to_node(
    source: Path,
    destination: str,
    host: str,
    key_path: str | Path,
    proxy_jump: str | None = None,
) -> None:
    ssh_command = " ".join(
        shlex.quote(part)
        for part in build_ssh_command(proxy_jump=proxy_jump, key_path=key_path)
    )

    cmd = [
        "rsync",
        "-avz",
        "-e",
        ssh_command,
        str(source),
        f"cc@{host}:{destination}",
    ]

    print("+", " ".join(shlex.quote(part) for part in cmd))
    subprocess.run(cmd, check=True, text=True)


def _write_cluster_files(cluster: Cluster) -> None:
    """Write runtime/cluster.json and runtime/cluster_hosts.

    Kept for the thin step notebooks and for debugging.
    """
    with runtime_file("cluster.json").open("w", encoding="utf-8") as file:
        json.dump(cluster_to_dict(cluster), file, indent=2)

    with runtime_file("cluster_hosts").open("w", encoding="utf-8") as file:
        for node in cluster.nodes:
            if not node.private_ip:
                raise RuntimeError(f"Node has no private IP: {node.name}")
            file.write(f"{node.private_ip} {node.name}\n")


def load_cluster() -> Cluster:
    """Load the most recently provisioned cluster from runtime/cluster.json."""
    with runtime_file("cluster.json").open("r", encoding="utf-8") as file:
        return cluster_from_dict(json.load(file))


# --------------------------------------------------------------------------- #
# Phase 0
# --------------------------------------------------------------------------- #

def prepare_site_and_keypair(
    config: dict[str, Any],
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
) -> None:
    """Choose site/project and register the SSH keypair with Chameleon (01)."""
    configure_chameleon_site(config)
    ensure_chameleon_keypair(
        keypair_name=config["chameleon"]["keypair_name"],
        private_key_path=key_path,
    )


def _blacklist_names(blacklist: Iterable[str] | None) -> frozenset[str]:
    if not blacklist:
        return frozenset()
    return frozenset(str(name) for name in blacklist)


def _with_exclusions(
    predicate: Callable[[Any], bool] | None,
    excluded: frozenset[str] | set[str],
) -> Callable[[Any], bool]:
    """Caller predicate plus: excluded machine names never match."""

    def allowed(node: Any) -> bool:
        if str(get_node_name(node) or "") in excluded:
            return False
        return predicate(node) if predicate is not None else True

    return allowed


def preview_node_selection(
    config: dict[str, Any],
    predicate: Callable[[Any], bool] | None = None,
    sort_key: Callable[[Any], Any] | None = None,
    blacklist: Iterable[str] | None = None,
    on_step: OnStep = print,
) -> list[dict[str, Any]]:
    """Show what the selection rules would pick right now (never raises).

    Phase-0 checkpoint only -- `run_matrix` selects again per allocation. Nothing
    is reserved yet, so problems are reported instead of raised. Pass the same
    `blacklist` as to `run_matrix` so both agree.
    """
    if not _lease_was_created(config):
        on_step(
            f"Lease '{config['reservation']['name']}' already has assigned "
            "machines (brought-along lease) -- nothing to select here, and "
            "run_matrix will not select either."
        )
        return []

    blocked = _blacklist_names(blacklist)
    if blocked:
        on_step("Blacklisted machines (never selected): " + ", ".join(sorted(blocked)))

    try:
        return selected_nodes_to_dict(
            select_nodes(
                config,
                predicate=_with_exclusions(predicate, blocked),
                sort_key=sort_key,
            )
        )
    except Exception as error:
        on_step(f"Selection preview unavailable: {error}")
        return []


# --------------------------------------------------------------------------- #
# Steps (formerly notebooks 02-07)
# --------------------------------------------------------------------------- #

def provision(
    config: dict[str, Any],
    selected_nodes: list[Any],
    lease_name: str | None = None,
) -> Cluster:
    """02: lease/reservation + servers, one Floating IP for the head.

    `lease_name` names a self-created lease; None uses reservation.name.
    """
    cluster = provision_cluster(
        config=config,
        selected_nodes=selected_nodes,
        lease_name=lease_name,
    )

    # If writing runtime files fails, tear down the servers we just created
    # (run_matrix never sees `cluster` when provision raises).
    try:
        _write_cluster_files(cluster)
    except Exception:
        delete_servers(cluster)
        raise

    return cluster


def configure_cluster(
    config: dict[str, Any],
    cluster: Cluster,
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
) -> None:
    """03: configure the private network (hosts, ssh, authorized_keys, firewall)."""
    head_ip = _head_ip(cluster)
    proxy_jump = f"cc@{head_ip}"
    nodes = cluster.nodes
    head = cluster.head

    # Wait for SSH reachability.
    wait_for_ssh(host=head_ip, key_path=key_path)
    for worker in cluster.workers:
        if not worker.private_ip:
            raise RuntimeError(f"Worker has no private IP: {worker.name}")
        wait_for_ssh(host=worker.private_ip, proxy_jump=proxy_jump, key_path=key_path)

    # Stop the periodic apt jobs before the firewall is configured.
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        print(f"Stopping periodic apt jobs on {node.label}")
        run_ssh(
            host=host,
            proxy_jump=node_proxy_jump,
            command=QUIESCE_APT_COMMAND,
            key_path=key_path,
        )

    # Create the remote runtime directory.
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        run_ssh(
            host=host,
            proxy_jump=node_proxy_jump,
            command=f"mkdir -p {REMOTE_RUNTIME_DIR}",
            key_path=key_path,
        )

    # Copy the hosts file and the remote/ scripts to all nodes.
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        _rsync_to_node(
            source=runtime_file("cluster_hosts"),
            destination=f"{REMOTE_RUNTIME_DIR}/cluster_hosts",
            host=host,
            key_path=key_path,
            proxy_jump=node_proxy_jump,
        )
        _rsync_to_node(
            source=repo_root() / "remote",
            destination=f"{REMOTE_RUNTIME_DIR}/",
            host=host,
            key_path=key_path,
            proxy_jump=node_proxy_jump,
        )

    # Configure /etc/hosts and the local SSH key on each node.
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        run_ssh(
            key_path=key_path,
            host=host,
            proxy_jump=node_proxy_jump,
            command=f"""
set -euo pipefail

chmod +x {REMOTE_RUNTIME_DIR}/remote/*.sh

bash {REMOTE_RUNTIME_DIR}/remote/configure_hosts.sh {REMOTE_RUNTIME_DIR}/cluster_hosts
bash {REMOTE_RUNTIME_DIR}/remote/configure_ssh.sh
""",
        )

    # Collect the public keys of all nodes.
    public_keys = []
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        result = run_ssh(
            key_path=key_path,
            host=host,
            proxy_jump=node_proxy_jump,
            command='cat "$HOME/.ssh/id_ed25519.pub"',
            capture_output=True,
        )
        public_keys.append(result.stdout.strip())

    authorized_keys = "\n".join(public_keys) + "\n"
    authorized_keys_path = runtime_file("authorized_keys")
    with authorized_keys_path.open("w", encoding="utf-8") as file:
        file.write(authorized_keys)

    # Distribute authorized_keys to all nodes (node-to-node SSH).
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        _rsync_to_node(
            source=authorized_keys_path,
            destination=f"{REMOTE_RUNTIME_DIR}/authorized_keys",
            host=host,
            key_path=key_path,
            proxy_jump=node_proxy_jump,
        )
        run_ssh(
            key_path=key_path,
            host=host,
            proxy_jump=node_proxy_jump,
            command=f"""
set -euo pipefail

cat {REMOTE_RUNTIME_DIR}/authorized_keys >> "$HOME/.ssh/authorized_keys"
sort -u "$HOME/.ssh/authorized_keys" -o "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
""",
        )

    # Firewall: trust the cluster peers.
    for node in nodes:
        host, node_proxy_jump = _node_target(node, head_ip)
        if not host:
            raise RuntimeError(f"Node has no reachable IP: {node.name}")
        print(f"Configuring firewall on {node.label}")
        run_ssh(
            host=host,
            key_path=key_path,
            proxy_jump=node_proxy_jump,
            command=f"""
set -euo pipefail

bash {REMOTE_RUNTIME_DIR}/remote/configure_firewall.sh {REMOTE_RUNTIME_DIR}/cluster_hosts
""",
        )

    # Optional extra files (copy_to).
    for item in config.get("copy_to", {}).get("files", []):
        local_path = repo_root() / item["local"]
        remote_path = item["remote"]
        target = item.get("target", "head")

        if not local_path.exists():
            raise FileNotFoundError(f"Missing local file: {local_path}")

        if target == "head":
            target_nodes = [head]
        elif target == "all":
            target_nodes = nodes
        else:
            raise ValueError(f"Invalid copy target: {target}")

        for node in target_nodes:
            host, node_proxy_jump = _node_target(node, head_ip)
            remote_parent = str(Path(remote_path).parent)
            run_ssh(
                key_path=key_path,
                host=host,
                proxy_jump=node_proxy_jump,
                command=f"mkdir -p {remote_parent}",
            )
            _rsync_to_node(
                source=local_path,
                destination=remote_path,
                host=host,
                key_path=key_path,
                proxy_jump=node_proxy_jump,
            )


def validate(
    config: dict[str, Any],
    cluster: Cluster,
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
) -> None:
    """04: validate what configure_cluster set up, between every pair of nodes:
    /etc/hosts resolution, node-to-node SSH and TCP through the firewall."""
    run_ssh(
        host=_head_ip(cluster),
        key_path=key_path,
        command=f"bash {REMOTE_RUNTIME_DIR}/remote/validate_cluster.sh {REMOTE_RUNTIME_DIR}/cluster_hosts",
    )


def run_experiment(
    config: dict[str, Any],
    cluster: Cluster,
    command: str,
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
    clean_remote: bool = True,
) -> dict[str, Any]:
    """05: run one experiment command on the head.

    Called once per inner repeat i with the configured experiment.command.
    Before the run the remote results directory is cleared so each i run can be
    collected in isolation.
    """
    head_ip = _head_ip(cluster)
    working_dir = config["experiment"]["working_dir"]
    remote_dir = config["results"]["remote_dir"]
    node_count = config["reservation"]["node_count"]
    processes_per_node = config["cluster"]["processes_per_node"]

    remote_log = f"{remote_dir}/run.log"

    if clean_remote:
        run_ssh(
            host=head_ip,
            key_path=key_path,
            command=f'rm -rf "{remote_dir}"/* 2>/dev/null || true; mkdir -p "{remote_dir}"',
        )

    started_at = _utcnow()

    # Show output live AND write it to remote_dir/run.log via tee; the log is
    # picked up by collect(). pipefail ensures the exit_code is the command's
    # (not tee's).
    result = run_ssh(
        host=head_ip,
        key_path=key_path,
        check=False,
        command=f"""
set -euo pipefail

cd {working_dir}

NODE_COUNT={node_count} \
PROCESSES_PER_NODE={processes_per_node} \
{command} 2>&1 | tee "{remote_log}"
""",
    )

    finished_at = _utcnow()

    if result.returncode != 0:
        print(f"WARNING: run command exited with code {result.returncode}")

    return {
        "command": command,
        "working_dir": working_dir,
        "node_count": node_count,
        "processes_per_node": processes_per_node,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "exit_code": result.returncode,
        "log_file": "run.log",
    }


def collect(
    config: dict[str, Any],
    cluster: Cluster,
    dest_dir: str | Path,
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
) -> None:
    """06: fetch results from the head into dest_dir."""
    head_ip = _head_ip(cluster)
    collect_results_from_head(
        head_ip=head_ip,
        remote_dir=config["results"]["remote_dir"],
        local_dir=dest_dir,
        key_path=key_path,
    )


def delete_servers(cluster: Cluster) -> None:
    """Tear down all servers of a cluster (delete primitive lives in chameleon).

    Keeps going when one delete fails, so a single API error does not leave
    the remaining servers running; raises afterwards if any failed.
    """
    failed = []
    for node in cluster.nodes:
        print(f"Tearing down {node.label}")
        try:
            chameleon_delete_server(node.name)
        except Exception as error:
            print(f"Could not delete server {node.label}: {error}")
            failed.append(node.label)

    if failed:
        raise RuntimeError("Servers could not be deleted: " + ", ".join(failed))


def delete_lease(config: dict[str, Any], lease_name: str | None = None) -> None:
    """Release the lease (by `lease_name`, else reservation.name)."""
    chameleon_delete_lease(lease_name or config["reservation"]["name"])


# --------------------------------------------------------------------------- #
# Matrix: k outer, i inner
# --------------------------------------------------------------------------- #

def _snapshot_config(config: dict[str, Any], path: Path, experiment_id: str) -> None:
    snapshot = dict(config)
    snapshot["experiment_id"] = experiment_id
    with Path(path).open("w", encoding="utf-8") as file:
        yaml.safe_dump(snapshot, file, sort_keys=False)


def _lease_was_created(config: dict[str, Any]) -> bool:
    """True if this run creates the lease itself.

    False if an existing lease is reused (reservation_id or reservation_ids
    set) -- in that case it is NOT deleted at the end.
    """
    reservation_cfg = config["reservation"]
    return not (
        reservation_cfg.get("reservation_id")
        or reservation_cfg.get("reservation_ids")
    )


def _select_for_allocation(
    config: dict[str, Any],
    k: int,
    predicate: Callable[[Any], bool] | None,
    sort_key: Callable[[Any], Any] | None,
    previous_names: set[str],
    blacklist: frozenset[str],
    lease_created: bool,
    on_step: OnStep,
) -> list[Any]:
    """Pick this allocation's machines, skipping the previous allocation's.

    Re-deploying a machine seconds after its release fails with `status ERROR`
    while Ironic is still cleaning it, and Blazar reports it as free anyway. Only
    the directly preceding allocation is skipped (k2 may reuse k0's machines);
    `blacklist` is permanent.

    A brought-along lease needs no selection -- its machines are reserved and
    therefore invisible to `select_nodes` (filter_reserved=True).
    """
    if not lease_created:
        on_step(f"[k{k}] existing lease -- no node selection needed.")
        return []

    excluded = set(blacklist) | previous_names

    if previous_names:
        on_step(
            f"[k{k}] excluding the previous allocation's machines: "
            + ", ".join(sorted(previous_names))
        )

    try:
        selected_nodes = select_nodes(
            config,
            predicate=_with_exclusions(predicate, excluded),
            sort_key=sort_key,
        )
    except RuntimeError as error:
        if not excluded:
            raise

        # No fallback to the excluded machines -- that is the point of excluding.
        details = []
        if previous_names:
            details.append(
                "the previous allocation used "
                + ", ".join(sorted(previous_names))
            )
        if blacklist:
            details.append("blacklisted: " + ", ".join(sorted(blacklist)))

        raise RuntimeError(
            f"{error} Excluded machines ({'; '.join(details)}) are not used as a "
            "fallback. Shrink the blacklist, wait for more nodes, lower "
            "reservation.node_count, or bring your own lease via "
            "reservation.reservation_ids (then nothing is released between "
            "allocations)."
        ) from error

    names = [str(get_node_name(node) or "?") for node in selected_nodes]
    on_step(f"[k{k}] selected machines: {', '.join(names)}")

    # Also when this allocation fails later: the machines were reserved anyway.
    previous_names.clear()
    previous_names.update(name for name in names if name != "?")

    return selected_nodes


def run_matrix(
    config: dict[str, Any],
    now: datetime,
    predicate: Callable[[Any], bool] | None = None,
    sort_key: Callable[[Any], Any] | None = None,
    blacklist: Iterable[str] | None = None,
    on_step: OnStep = print,
    key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
) -> str:
    """Run the k x i matrix as k separate allocations.

    The machines are reserved `k` (outer_repeats) times; each allocation is
    provisioned, configured and validated on its own, runs the single
    experiment.command `i` (inner_repeats) times, collects the results, and is
    then torn down again.

    Node selection happens per allocation (see `_select_for_allocation`):
    `predicate`/`sort_key` are the caller's rules -- same meaning as in
    `select_nodes`, omit both for the first available nodes -- and `blacklist`
    names machines to skip in every allocation. Raises rather than falling back to
    an excluded machine. The machines used land in each `allocation.json`.

    Optional `chameleon.skip_validate: true` skips the per-allocation cluster
    validation step; the experiment then runs on an unverified cluster.

    Lease lifecycle per allocation:
      - self-created lease (no reservation_id/reservation_ids): created and
        deleted for every k -- the servers come and go with the lease.
      - brought-along lease (reservation_id/reservation_ids set): reused for all
        k allocations and never deleted; only its servers are torn down and
        freshly launched each time.

    Returns the experiment_id.
    """
    name, outer_repeats, inner_repeats, command = experiment_plan(config)
    experiment_id = exp.new_experiment_id(name, now)
    total = outer_repeats * inner_repeats

    on_step(
        f"Experiment: {experiment_id}  "
        f"({outer_repeats} allocations x {inner_repeats} runs = {total} runs)"
    )

    experiment_root = exp.experiment_dir(experiment_id)
    _snapshot_config(config, experiment_root / "config_resolved.yaml", experiment_id)
    exp.write_json(
        experiment_root / "experiment.json",
        {
            "experiment_id": experiment_id,
            "name": name,
            "outer_repeats": outer_repeats,
            "inner_repeats": inner_repeats,
            "total_runs": total,
            "command": command,
            "created_from": str(now.isoformat()),
        },
    )

    manifest: dict[str, Any] = {
        "experiment_id": experiment_id,
        "name": name,
        "outer_repeats": outer_repeats,
        "inner_repeats": inner_repeats,
        "command": command,
        "runs": [],
    }

    # A self-created lease is (re)created and deleted for every allocation; a
    # brought-along lease is reused across all allocations and kept.
    lease_created = _lease_was_created(config)

    skip_validate = bool(config["chameleon"].get("skip_validate", False))

    # Machines of the directly preceding allocation, updated per k.
    previous_names: set[str] = set()

    blacklisted = _blacklist_names(blacklist)
    if blacklisted:
        on_step(
            "Blacklisted machines (never selected): "
            + ", ".join(sorted(blacklisted))
        )

    lease_base_name = config["reservation"]["name"]

    # One allocation per k. provision() is inside the try so that a failure
    # while launching servers still triggers cleanup of everything started for
    # this allocation. An exception propagates after this k's cleanup and stops
    # the remaining allocations (fail-fast, no leaked resources).
    for k in range(outer_repeats):
        cluster: Cluster | None = None

        # Outside the try: nothing is allocated yet, so a failing selection must
        # not trigger the cleanup path.
        selected_nodes = _select_for_allocation(
            config=config,
            k=k,
            predicate=predicate,
            sort_key=sort_key,
            previous_names=previous_names,
            blacklist=blacklisted,
            lease_created=lease_created,
            on_step=on_step,
        )

        # One lease name per allocation: reusing the name just deleted makes
        # Blazar answer lease.get() with HTTP 500.
        lease_name = f"{lease_base_name}-k{k}" if lease_created else None

        try:
            on_step(
                f"[k{k}] provisioning new lease '{lease_name}' + servers ..."
                if lease_created
                else f"[k{k}] reusing existing lease, launching servers ..."
            )
            cluster = provision(config, selected_nodes, lease_name=lease_name)
            on_step(
                f"[k{k}] servers on machines: "
                + ", ".join(node.label for node in cluster.nodes)
            )

            configure_cluster(config, cluster, key_path=key_path)
            if skip_validate:
                on_step(f"[k{k}] skipping cluster validation (skip_validate).")
            else:
                validate(config, cluster, key_path=key_path)

            exp.write_json(
                exp.allocation_dir(experiment_id, k) / "allocation.json",
                {
                    "k": k,
                    "reservation_id": cluster.reservation_id,
                    "node_reservation_ids": cluster.node_reservation_ids,
                    "lease_created": lease_created,
                    "lease_name": lease_name or config["reservation"]["name"],
                    "provisioned_at": _utcnow().isoformat(),
                    # Physical machines; empty for a brought-along lease.
                    "selected_nodes": selected_nodes_to_dict(selected_nodes),
                    "nodes": [
                        {
                            "name": node.name,
                            "id": node.id,
                            "private_ip": node.private_ip,
                            "floating_ip": node.floating_ip,
                            "is_head": node.is_head,
                            "machine": node.machine,
                        }
                        for node in cluster.nodes
                    ],
                },
            )

            for i in range(inner_repeats):
                rid = exp.run_id(experiment_id, k, i)
                dest = exp.run_dir(experiment_id, k, i)

                on_step(f"[{rid}] {command}")

                meta = run_experiment(config, cluster, command, key_path=key_path)
                collect(config, cluster, dest, key_path=key_path)

                meta.update(
                    run_id=rid,
                    k=k,
                    i=i,
                    reservation_id=cluster.reservation_id,
                    results_dir=str(dest.relative_to(repo_root())),
                )
                exp.write_json(dest / "run.json", meta)
                manifest["runs"].append(meta)

            # Persist the manifest after every allocation so partial progress
            # survives a later failure.
            exp.write_json(experiment_root / "manifest.json", manifest)
        except BaseException as error:
            failure = error
            raise
        else:
            failure = None
        finally:
            # provision() is atomic: on failure it already rolls back its own
            # servers, so `cluster` is set only when provisioning succeeded.
            # Tear this allocation's servers down (normal teardown or a
            # post-provision failure). The lease is deleted only if this run
            # created it -- a brought-along lease is kept for the next k.
            # Each step runs even if the previous one failed.
            cleanup_errors: list[Exception] = []

            if cluster is not None:
                on_step(f"[k{k}] cleanup: deleting servers ...")
                try:
                    delete_servers(cluster)
                except Exception as error:
                    cleanup_errors.append(error)

            if lease_created:
                on_step(
                    f"[k{k}] cleanup: deleting lease '{lease_name}' "
                    "(created by this run) ..."
                )
                try:
                    delete_lease(config, lease_name=lease_name)
                except Exception as error:
                    cleanup_errors.append(error)
            else:
                on_step(f"[k{k}] cleanup: keeping existing lease.")

            if cleanup_errors:
                on_step(
                    f"[k{k}] WARNING: cleanup incomplete -- check the "
                    "Chameleon dashboard:"
                )
                for error in cleanup_errors:
                    on_step(f"  - {error}")
                # Never hide the error that stopped the run; otherwise raise
                # the cleanup failure itself.
                if failure is None:
                    raise cleanup_errors[0]

    exp.write_json(experiment_root / "manifest.json", manifest)
    on_step(f"Done. Results in results/{experiment_id}")
    return experiment_id
