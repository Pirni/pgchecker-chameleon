from pathlib import Path


def repo_root() -> Path:
    # lib/paths.py -> repo root is the directory above lib/.
    # CWD-independent, so it works the same from notebooks/ and from the repo
    # root (run.py, main.ipynb).
    return Path(__file__).resolve().parent.parent


def runtime_dir() -> Path:
    path = repo_root() / "runtime"
    path.mkdir(exist_ok=True)
    return path


def runtime_file(name: str) -> Path:
    return runtime_dir() / name
