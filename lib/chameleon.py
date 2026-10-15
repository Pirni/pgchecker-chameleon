import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from chi import context, hardware, lease, server

from lib import images

# chameleon.image_id may be a UUID or the name of a community image.
images.install()


@dataclass
class Node:
    name: str
    id: str
    private_ip: str | None = None
    floating_ip: str | None = None
    is_head: bool = False
    reservation_id: str | None = None
    # Physical machine (e.g. P3-CPU-015) the server runs on, None if unknown.
    machine: str | None = None

    @property
    def label(self) -> str:
        """Server name plus the physical machine, for log output."""
        return f"{self.name} ({self.machine or 'machine unknown'})"


@dataclass
class Cluster:
    reservation_id: str
    node_reservation_ids: list[str]
    nodes: list[Node]

    @property
    def head(self) -> Node:
        for node in self.nodes:
            if node.is_head:
                return node

        raise RuntimeError("Cluster has no head node")

    @property
    def workers(self) -> list[Node]:
        return [node for node in self.nodes if not node.is_head]

def assign_floating_ip_to_head(
    nodes: list[Node],
    floating_ip: str | None,
) -> None:
    head = nodes[0]

    chameleon_server = server.get_server(head.name)

    existing_floating_ip = extract_ip(chameleon_server, ip_type="floating")
    if existing_floating_ip:
        head.floating_ip = existing_floating_ip
        print(f"Head already has Floating IP: {head.floating_ip}")
        return

    if floating_ip is None:
        raise RuntimeError("No Floating IP was provided for the head node")

    chameleon_server.associate_floating_ip(floating_ip)

    if hasattr(chameleon_server, "refresh"):
        chameleon_server.refresh()

    head.floating_ip = extract_ip(chameleon_server, ip_type="floating") or floating_ip

    print(f"Head Floating IP: {head.floating_ip}")

def provision_cluster(
    config: dict[str, Any],
    selected_nodes: list[Any] | None = None,
    lease_name: str | None = None,
) -> Cluster:
    configure_chameleon_site(config)

    if selected_nodes is None:
        selected_nodes = select_nodes(config)

    node_reservation_ids, floating_ips = get_or_create_reservation_resources(
        config=config,
        selected_nodes=selected_nodes,
        lease_name=lease_name,
    )

    machines = reservation_machines(lease_name or config["reservation"]["name"])

    # launch_nodes rolls back its own servers if a launch fails.
    nodes = launch_nodes(
        config=config,
        node_reservation_ids=node_reservation_ids,
        machines=machines,
    )

    # If a later step fails, tear down the servers we just launched.
    try:
        assign_floating_ip_to_head(
            nodes=nodes,
            floating_ip=floating_ips[0] if floating_ips else None,
        )
    except Exception:
        print("Floating IP assignment failed -- rolling back launched servers.")
        _rollback_servers([node.name for node in nodes])
        raise

    return Cluster(
        reservation_id=node_reservation_ids[0],
        node_reservation_ids=node_reservation_ids,
        nodes=nodes,
    )


def configure_chameleon_site(config: dict[str, Any]) -> None:
    site = config["chameleon"]["site"]

    if hasattr(context, "version"):
        context.version = "1.0"

    if hasattr(context, "use_site"):
        context.use_site(site)
    else:
        context.choose_site(default=site)


def node_has_mellanox(node: Any) -> bool:
    adapters = get_node_field(node, "network_adapters") or []

    for adapter in adapters:
        vendor = str(adapter.get("vendor", "")).lower()
        model = str(adapter.get("model", "")).lower()
        driver = str(adapter.get("driver", "")).lower()

        if (
            "mellanox" in vendor
            or "connectx" in model
            or "mlx5" in driver
        ):
            return True

    return False


def node_has_at_most_ram_gib(node: Any, max_ram_gib: int) -> bool:
    ram_gib = node_ram_gib(node)

    if ram_gib is None:
        return False

    return ram_gib <= max_ram_gib


def select_nodes(
    config: dict[str, Any],
    predicate: Callable[[Any], bool] | None = None,
    sort_key: Callable[[Any], Any] | None = None,
) -> list[Any]:
    """Select node_count available nodes of the configured node_type.

    Default (no arguments): the **first** available nodes, without filter and
    without sorting. Custom selection logic comes from the caller:

      - predicate: Callable[[node], bool]  -> filter (matching nodes only)
      - sort_key:  Callable[[node], Any]   -> order before truncating

    Building blocks: `node_has_mellanox`, `node_has_at_most_ram_gib`,
    `get_node_name`. Example (pgchecker rules):

        select_nodes(
            config,
            predicate=lambda n: node_has_mellanox(n)
                                and node_has_at_most_ram_gib(n, 256),
            sort_key=lambda n: str(get_node_name(n) or ""),
        )
    """
    configure_chameleon_site(config)

    reservation_cfg = config["reservation"]
    requested_count = reservation_cfg["node_count"]

    available_nodes = hardware.get_nodes(
        node_type=reservation_cfg["node_type"],
        filter_reserved=True,
    )

    if predicate is not None:
        available_nodes = [node for node in available_nodes if predicate(node)]

    if sort_key is not None:
        available_nodes = sorted(available_nodes, key=sort_key)

    selected_nodes = available_nodes[:requested_count]

    if len(selected_nodes) < requested_count:
        raise RuntimeError(
            f"Requested {requested_count} nodes, "
            f"but only found {len(selected_nodes)} matching available nodes."
        )

    return selected_nodes


def select_preferred_nodes(
    available_nodes: list[Any],
    preferred_node_names: list[str],
) -> list[Any]:
    nodes_by_name = {
        get_node_name(node): node
        for node in available_nodes
        if get_node_name(node) is not None
    }

    missing_nodes = [
        name
        for name in preferred_node_names
        if name not in nodes_by_name
    ]

    if missing_nodes:
        raise RuntimeError(
            "Previously selected nodes are no longer available: "
            + ", ".join(missing_nodes)
        )

    return [nodes_by_name[name] for name in preferred_node_names]


def selected_nodes_to_dict(nodes: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": get_node_name(node),
            "id": get_node_field(node, "id", "uuid"),
            "node_type": get_node_field(node, "node_type", "resource_class"),
            "raw": str(node),
        }
        for node in nodes
    ]


def get_or_create_reservation_resources(
    config: dict[str, Any],
    selected_nodes: list[Any],
    lease_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """`lease_name` only affects a self-created lease; a brought-along one is
    always looked up under reservation.name."""
    reservation_cfg = config["reservation"]

    reservation_ids = reservation_cfg.get("reservation_ids")
    if reservation_ids:
        print("Using existing node reservations:", reservation_ids)
        return reservation_ids, _reserved_floating_ips(reservation_cfg["name"])

    reservation_id = reservation_cfg.get("reservation_id")
    if reservation_id:
        if reservation_cfg["node_count"] != 1:
            raise ValueError(
                "reservation.reservation_id can only be used with node_count: 1. "
                "For multiple existing node reservations, use reservation.reservation_ids."
            )

        print(f"Using existing node reservation: {reservation_id}")
        return [reservation_id], _reserved_floating_ips(reservation_cfg["name"])

    return create_reservation(
        config=config,
        selected_nodes=selected_nodes,
        lease_name=lease_name,
    )


def _reserved_floating_ips(lease_name: str) -> list[str]:
    """Reserved Floating IPs of an existing lease (looked up by name).

    Used when reusing a lease so the head node still gets a Floating IP. Returns
    [] if the lease cannot be loaded or reserved none -- then the head must
    already have one associated (handled by assign_floating_ip_to_head).
    """
    try:
        chameleon_lease = lease.get_lease(lease_name)
    except Exception as error:
        print(f"Could not load existing lease {lease_name}: {error}")
        return []

    get_fips = getattr(chameleon_lease, "get_reserved_floating_ips", None)
    if get_fips is None:
        return []

    try:
        floating_ips = get_fips() or []
    except Exception as error:
        print(f"Could not read reserved Floating IPs of {lease_name}: {error}")
        return []

    floating_ips = [str(fip) for fip in floating_ips]
    if floating_ips:
        print("Reused reserved Floating IPs:", floating_ips)
    return floating_ips


def reservation_machines(lease_name: str) -> dict[str, str]:
    """Map node reservation id -> physical machine name, read back from Blazar.

    `add_node_reservation(nodes=...)` pins every reservation to one machine via
    `["==", "$node_name", <name>]`, so a server launched on that reservation can
    only land on that machine. Reservations without such a pin (e.g. a
    brought-along count-based lease) are left out. Never raises -- this is for
    logging and bookkeeping only.
    """
    try:
        chameleon_lease = lease.get_lease(lease_name)
    except Exception as error:
        print(f"Could not load lease {lease_name} to read its machines: {error}")
        return {}

    machines = {}
    for reservation in getattr(chameleon_lease, "node_reservations", None) or []:
        try:
            reservation_id = extract_reservation_id(reservation)
        except RuntimeError:
            continue

        properties = get_node_field(reservation, "resource_properties")
        if isinstance(properties, str):
            try:
                properties = json.loads(properties) if properties else None
            except json.JSONDecodeError:
                properties = None

        machine = _pinned_node_name(properties)
        if machine:
            machines[reservation_id] = machine

    return machines


def _pinned_node_name(properties: Any) -> str | None:
    """Find `["==", "$node_name", <name>]` in a (possibly nested) constraint."""
    if not isinstance(properties, list):
        return None

    if (
        len(properties) == 3
        and properties[0] == "=="
        and properties[1] == "$node_name"
    ):
        return str(properties[2])

    for item in properties:
        name = _pinned_node_name(item)
        if name:
            return name

    return None


def create_reservation(
    config: dict[str, Any],
    selected_nodes: list[Any],
    lease_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """Create a lease. `lease_name` overrides reservation.name.

    run_matrix passes a per-allocation name: reusing the name of a just-deleted
    lease makes Blazar answer `lease.get()` with HTTP 500.
    """
    reservation_cfg = config["reservation"]

    chameleon_lease = lease.Lease(
        lease_name or reservation_cfg["name"],
        duration=timedelta(hours=reservation_cfg["lease_hours"]),
    )

    chameleon_lease.add_node_reservation(nodes=selected_nodes)

    # Only reserve one Floating IP. It will be attached to the head node.
    chameleon_lease.add_fip_reservation(1)

    chameleon_lease.submit(idempotent=True)

    if hasattr(chameleon_lease, "refresh"):
        chameleon_lease.refresh()

    node_reservation_ids = extract_node_reservation_ids(chameleon_lease)

    if len(node_reservation_ids) != len(selected_nodes):
        raise RuntimeError(
            f"Expected {len(selected_nodes)} node reservations, "
            f"but got {len(node_reservation_ids)}."
        )

    floating_ips = chameleon_lease.get_reserved_floating_ips()

    if not floating_ips:
        raise RuntimeError("Lease has no reserved Floating IPs")

    floating_ips = [str(fip) for fip in floating_ips]

    print("Created node reservations:", node_reservation_ids)
    print("Reserved Floating IPs:", floating_ips)

    return node_reservation_ids, floating_ips


def launch_nodes(
    config: dict[str, Any],
    node_reservation_ids: list[str],
    machines: dict[str, str] | None = None,
) -> list[Node]:
    """Launch all servers in parallel.

    Phase 1 fires every launch without waiting, so all servers boot at the same
    time. Phase 2 does a blocking submit per server, which waits for ACTIVE and
    reliably populates the addresses (the proven mechanism); because all were
    fired in phase 1, these waits overlap, so total wall-clock is ~one launch
    (~10 min) instead of N x 10 min.

    Wrapped so that if any launch fails, servers already started are torn down.
    `machines` (reservation id -> physical machine) is only used for the output
    and `Node.machine`.
    """
    cluster_cfg = config["cluster"]
    machines = machines or {}

    # Names of servers already launched, for rollback if any launch fails.
    launched_names: list[str] = []

    try:
        # Phase 1: fire all launches without blocking (all boot concurrently).
        launched = []  # (index, name, reservation_id, server)
        for index, node_reservation_id in enumerate(node_reservation_ids):
            name = f"{cluster_cfg['name_prefix']}-{index}"

            chameleon_server = create_server_object(
                config=config,
                name=name,
                reservation_id=node_reservation_id,
            )

            machine = machines.get(node_reservation_id, "machine unknown")
            print(f"Launching server: {name} on {machine}")
            launched_names.append(name)
            _submit_no_wait(chameleon_server)
            launched.append((index, name, node_reservation_id, chameleon_server))

        # Phase 2: block until each is ACTIVE (waits overlap) and read addresses.
        # The blocking, idempotent submit attaches to the already-started
        # instance, waits for it, and populates the server's addresses.
        nodes = []
        for index, name, node_reservation_id, chameleon_server in launched:
            machine = machines.get(node_reservation_id, "machine unknown")
            print(f"Waiting for server to become active: {name} on {machine}")
            chameleon_server.submit(idempotent=True)

            if hasattr(chameleon_server, "refresh"):
                chameleon_server.refresh()

            private_ip = extract_ip(chameleon_server, ip_type="fixed")

            if not private_ip:
                raise RuntimeError(
                    f"Server has no private IP after provisioning: {name}"
                )

            nodes.append(
                Node(
                    name=name,
                    id=str(get_node_field(chameleon_server, "id") or name),
                    private_ip=private_ip,
                    floating_ip=extract_ip(chameleon_server, ip_type="floating"),
                    is_head=index == 0,
                    reservation_id=node_reservation_id,
                    machine=machines.get(node_reservation_id),
                )
            )

        return nodes
    except Exception:
        # A server failed to launch: tear down what we already started.
        print("Launch failed -- rolling back started servers.")
        _rollback_servers(launched_names)
        raise


def _submit_no_wait(chameleon_server: Any) -> None:
    """Fire a server launch without blocking on it becoming active.

    On older python-chi without the wait_for_active kwarg this does nothing and
    the phase-2 blocking submit creates + waits instead (serial fallback).
    """
    try:
        chameleon_server.submit(idempotent=True, wait_for_active=False)
    except TypeError:
        pass


def delete_server(name: str) -> None:
    """Delete a server by name; missing servers are ignored."""
    try:
        chameleon_server = server.get_server(name)
    except Exception as error:
        print(f"Server not found or cannot be loaded: {name}")
        print(error)
        return

    print(f"Deleting server: {name}")

    try:
        chameleon_server.delete()
        print(f"Deleted server: {name}")
        return
    except AttributeError:
        pass

    try:
        server.delete_server(name)
        print(f"Deleted server: {name}")
    except Exception as error:
        print(f"Failed to delete server: {name}")
        raise error


def delete_lease(name: str) -> None:
    """Delete a lease by name; a lease that no longer exists counts as deleted.

    Only "not found" means gone: any other lookup error (e.g. Blazar's
    `504 Gateway Time-out`) is raised, so a flaky API never hides a lease that
    is still running.
    """
    print(f"Deleting lease: {name}")

    try:
        chameleon_lease = lease.get_lease(name)
    except Exception as error:
        if _is_not_found(error):
            print(f"Lease is gone: {name}")
            return
        raise RuntimeError(
            f"Could not load lease {name} to delete it -- it may still be "
            "running. Check the Chameleon dashboard (Reservations > Leases)."
        ) from error

    try:
        if hasattr(chameleon_lease, "delete"):
            chameleon_lease.delete()
        else:
            lease.delete_lease(name)
    except Exception as error:
        raise RuntimeError(
            f"Deleting lease {name} failed -- it may still be running. Check "
            "the Chameleon dashboard (Reservations > Leases)."
        ) from error

    print(f"Deleted lease: {name}")


def _is_not_found(error: Exception) -> bool:
    if getattr(error, "code", None) == 404:
        return True
    text = str(error).lower()
    return "not found" in text or "could not find" in text


def _rollback_servers(names: list[str]) -> None:
    """Best-effort delete of already-launched servers during a failed launch.

    Must not raise, so it never masks the original exception.
    """
    for name in names:
        try:
            delete_server(name)
        except Exception as error:
            print(f"Rollback: could not delete server {name}: {error}")


def create_server_object(
    config: dict[str, Any],
    name: str,
    reservation_id: str,
) -> Any:
    chameleon_cfg = config["chameleon"]
    image_ref = chameleon_cfg["image_id"]

    constructor_attempts = [
        {
            "reservation_id": reservation_id,
            "image_name": image_ref,
            "flavor_name": "baremetal",
            "key_name": chameleon_cfg["keypair_name"],
            "network_name": chameleon_cfg["network"],
        },
        {
            "reservation_id": reservation_id,
            "image_name": image_ref,
            "key_name": chameleon_cfg["keypair_name"],
            "network_name": chameleon_cfg["network"],
        },
        {
            "reservation_id": reservation_id,
            "image_name": image_ref,
        },
    ]

    last_error = None

    for kwargs in constructor_attempts:
        try:
            return server.Server(name, **kwargs)
        except Exception as error:
            last_error = error

    raise RuntimeError(
        f"Could not create chi.server.Server object for {name}"
    ) from last_error

    
def extract_node_reservation_ids(chameleon_lease: Any) -> list[str]:
    reservations = getattr(chameleon_lease, "node_reservations", None)

    if not reservations:
        raise RuntimeError("Lease has no node reservations")

    return [extract_reservation_id(reservation) for reservation in reservations]


def extract_reservation_id(reservation: Any) -> str:
    if isinstance(reservation, dict):
        return str(reservation["id"])

    reservation_id = getattr(reservation, "id", None)

    if reservation_id is None:
        raise RuntimeError(f"Could not extract reservation id from: {reservation}")

    return str(reservation_id)


def extract_ip(chameleon_server: Any, ip_type: str) -> str | None:
    addresses = getattr(chameleon_server, "addresses", None)

    if not addresses:
        instance = getattr(chameleon_server, "instance", None)
        addresses = getattr(instance, "addresses", None)

    if not addresses:
        return None

    for network_addresses in addresses.values():
        for address in network_addresses:
            if not isinstance(address, dict):
                continue

            if address.get("OS-EXT-IPS:type") == ip_type:
                return address.get("addr")

    return None


def extract_floating_ip(floating_ip: Any) -> str:
    if isinstance(floating_ip, str):
        return floating_ip

    if isinstance(floating_ip, dict):
        for key in ("floating_ip_address", "ip", "addr"):
            if key in floating_ip:
                return str(floating_ip[key])

    for attr in ("floating_ip_address", "ip", "addr"):
        value = getattr(floating_ip, attr, None)
        if value is not None:
            return str(value)

    return str(floating_ip)


def get_node_name(node: Any) -> str | None:
    value = get_node_field(
        node,
        "name",
        "hostname",
        "node_name",
    )

    if value is None:
        return None

    return str(value)


def get_node_field(node: Any, *names: str) -> Any:
    for name in names:
        if isinstance(node, dict) and name in node:
            return node[name]

        if hasattr(node, name):
            return getattr(node, name)

    return None


def cluster_to_dict(cluster: Cluster) -> dict[str, Any]:
    return {
        "reservation_id": cluster.reservation_id,
        "node_reservation_ids": cluster.node_reservation_ids,
        "nodes": [
            {
                "name": node.name,
                "id": node.id,
                "private_ip": node.private_ip,
                "floating_ip": node.floating_ip,
                "is_head": node.is_head,
                "reservation_id": node.reservation_id,
                "machine": node.machine,
            }
            for node in cluster.nodes
        ],
    }


def cluster_from_dict(data: dict[str, Any]) -> Cluster:
    return Cluster(
        reservation_id=data["reservation_id"],
        node_reservation_ids=data.get(
            "node_reservation_ids",
            [data["reservation_id"]],
        ),
        nodes=[
            Node(
                name=node["name"],
                id=node["id"],
                private_ip=node.get("private_ip"),
                floating_ip=node.get("floating_ip"),
                is_head=node.get("is_head", False),
                reservation_id=node.get("reservation_id"),
                machine=node.get("machine"),
            )
            for node in data["nodes"]
        ],
    )

def node_ram_gib(node: Any) -> float | None:
    memory = get_node_field(node, "main_memory") or {}
    ram_size = memory.get("ram_size")

    if ram_size is None:
        return None

    return ram_size / (1024 ** 3)
