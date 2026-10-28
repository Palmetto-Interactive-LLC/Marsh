#!/usr/bin/env python3
"""Container entrypoint shim: materialize config and key files from the environment.

The orchestrator reads its fleet profile and the GitHub App private key from
files (MARSH_RUNNER_CONFIG, GH_APP_KEY_PATH). Kubernetes mounts both. ECS on EC2
cannot mount a ConfigMap or a secret as a file, but it can inject values as
environment variables, so this shim writes them out before it execs the
orchestrator.

    MARSH_RUNNERS_TOML     fleet profile text (non-secret task-definition env)
    MARSH_GITHUB_APP_PEM   GitHub App private key (Secrets Manager -> secrets)

If neither is set the shim does nothing but exec, so an image that uses it
behaves exactly as before under Kubernetes.

Where files go: MARSH_RUNNER_CONFIG / GH_APP_KEY_PATH when the task definition
sets them, otherwise <MARSH_SHIM_DIR>/runners.toml and <MARSH_SHIM_DIR>/
github-app.pem (MARSH_SHIM_DIR defaults to /tmp/marsh-config). The target
directory must be writable and should be tmpfs (ECS linuxParameters.tmpfs) so
the key never reaches disk. Files are 0600, the directory 0700, and the child
gets MARSH_RUNNER_CONFIG / GH_APP_KEY_PATH pointing at them.

The two source variables are removed from the child's environment so the key
does not sit in /proc/<pid>/environ of the long-running orchestrator. Nothing
here prints a value; errors name the variable only.
"""
from __future__ import annotations

import os
import sys
import tomllib

TOML_ENV = "MARSH_RUNNERS_TOML"
PEM_ENV = "MARSH_GITHUB_APP_PEM"
CONFIG_PATH_ENV = "MARSH_RUNNER_CONFIG"
KEY_PATH_ENV = "GH_APP_KEY_PATH"
DIR_ENV = "MARSH_SHIM_DIR"
DEFAULT_DIR = "/tmp/marsh-config"
DEFAULT_COMMAND = ["python", "/app/orchestrator.py"]


class ShimError(RuntimeError):
    """Raised with a message that never contains a configuration or key value."""


def _normalize_pem(raw: str) -> str:
    """Return PEM text with real newlines and exactly one trailing newline.

    Some secret stores and shells hand a key over with literal backslash-n
    sequences; undo that only when the value has no real newline at all.
    """
    text = raw.strip()
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    if not text.startswith("-----BEGIN ") or "-----END " not in text:
        raise ShimError(f"{PEM_ENV} is not PEM text")
    return text + "\n"


def _validate_toml(text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # str(exc) carries position info only, but keep it generic anyway.
        raise ShimError(f"{TOML_ENV} is not valid TOML ({type(exc).__name__})") from None
    return text if text.endswith("\n") else text + "\n"


def _write_private(path: str, content: str) -> None:
    """Write ``content`` to ``path`` as 0600 via a temp file and rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def prepare(env: dict[str, str]) -> dict[str, str]:
    """Write requested files and return the environment for the orchestrator."""
    child = dict(env)
    toml_text = child.pop(TOML_ENV, "")
    pem_text = child.pop(PEM_ENV, "")
    base = child.get(DIR_ENV) or DEFAULT_DIR

    if toml_text.strip():
        path = child.get(CONFIG_PATH_ENV) or os.path.join(base, "runners.toml")
        _write_private(path, _validate_toml(toml_text))
        child[CONFIG_PATH_ENV] = path
    if pem_text.strip():
        path = child.get(KEY_PATH_ENV) or os.path.join(base, "github-app.pem")
        _write_private(path, _normalize_pem(pem_text))
        child[KEY_PATH_ENV] = path
    return child


def main(argv: list[str]) -> None:
    command = argv[1:] or DEFAULT_COMMAND
    try:
        child_env = prepare(dict(os.environ))
    except (ShimError, OSError) as exc:
        print(f"marsh entrypoint: {exc}", file=sys.stderr)
        sys.exit(78)
    os.execvpe(command[0], command, child_env)


if __name__ == "__main__":
    main(sys.argv)
