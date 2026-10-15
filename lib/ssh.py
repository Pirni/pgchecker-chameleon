import subprocess
import time
from pathlib import Path

from chi import server

from lib.paths import runtime_file

DEFAULT_PRIVATE_KEY_PATH = runtime_file("ssh_key")
DEFAULT_KEY_TYPE = "ed25519"

def ensure_chameleon_keypair(
    keypair_name: str,
    private_key_path: str | Path = DEFAULT_PRIVATE_KEY_PATH,
    key_type: str = DEFAULT_KEY_TYPE,
) -> Path:
    private_key_path = Path(private_key_path).expanduser()
    public_key_path = Path(f"{private_key_path}.pub")

    private_key_path.parent.mkdir(parents=True, exist_ok=True)

    if not private_key_path.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                key_type,
                "-f",
                str(private_key_path),
                "-N",
                "",
            ],
            check=True,
        )

    private_key_path.chmod(0o600)

    if not public_key_path.exists():
        raise FileNotFoundError(f"Missing public key file: {public_key_path}")

    public_key = public_key_path.read_text(encoding="utf-8").strip()

    # Registers the public key with Chameleon/Nova under keypair_name.
    # If the keypair already exists and has the same fingerprint, python-chi keeps it.
    # If it differs, python-chi recreates it.
    registered_keypair = server.update_keypair(
        key_name=keypair_name,
        public_key=public_key,
    )

    print("Private key:", private_key_path)
    print("Chameleon keypair:", registered_keypair.name)

    return private_key_path


def build_ssh_command(
    key_path: str | Path | None = None,
    proxy_jump: str | None = None,
) -> list[str]:
    cmd = ["ssh"]

    if key_path is not None:
        cmd += ["-i", str(key_path)]

    cmd += [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
    ]

    if proxy_jump is not None:
        proxy_cmd = "ssh"

        if key_path is not None:
            proxy_cmd += f" -i {key_path}"

        proxy_cmd += (
            " -o StrictHostKeyChecking=no"
            " -o UserKnownHostsFile=/dev/null"
            " -o ConnectTimeout=10"
            f" -W %h:%p {proxy_jump}"
        )

        cmd += ["-o", f"ProxyCommand={proxy_cmd}"]

    return cmd


def run_ssh(
    host: str,
    command: str,
    key_path: str | Path | None = None,
    user: str = "cc",
    proxy_jump: str | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    cmd = build_ssh_command(
        key_path=key_path,
        proxy_jump=proxy_jump,
    )
    cmd += [f"{user}@{host}", command]

    print("+", " ".join(cmd))

    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def wait_for_ssh(
    host: str,
    key_path: str | Path | None = None,
    user: str = "cc",
    proxy_jump: str | None = None,
    max_attempts: int = 30,
    interval_seconds: int = 15,
) -> None:
    last_result = None

    for attempt in range(1, max_attempts + 1):
        print(f"SSH attempt {attempt}/{max_attempts}: {user}@{host}")

        result = run_ssh(
            host=host,
            command="echo SSH_READY",
            key_path=key_path,
            user=user,
            proxy_jump=proxy_jump,
            check=False,
            capture_output=True,
        )

        last_result = result

        if result.returncode == 0:
            print(f"SSH ready: {host}")
            return

        if "Permission denied (publickey)" in result.stderr:
            raise PermissionError(
                f"SSH key rejected for {user}@{host}.\n"
                "Check chameleon.keypair_name and the matching local private key.\n"
                f"STDERR:\n{result.stderr}"
            )

        if attempt < max_attempts:
            time.sleep(interval_seconds)

    raise TimeoutError(
        f"SSH not ready after {max_attempts} attempts: {host}\n"
        f"Last STDOUT:\n{last_result.stdout if last_result else ''}\n"
        f"Last STDERR:\n{last_result.stderr if last_result else ''}"
    )