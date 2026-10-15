import subprocess
from pathlib import Path

from lib.ssh import build_ssh_command


def collect_results_from_head(
    head_ip: str,
    remote_dir: str,
    local_dir: str | Path,
    key_path: str | Path | None = None,
    user: str = "cc",
) -> None:
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    ssh_command = " ".join(build_ssh_command(key_path))

    cmd = [
        "rsync",
        "-avz",
        "-e",
        ssh_command,
        f"{user}@{head_ip}:{remote_dir}/",
        str(local_dir),
    ]

    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, text=True)