import fcntl
import os
import secrets
from pathlib import Path
from typing import Callable


ADJECTIVES = (
    "amber",
    "brisk",
    "calm",
    "coral",
    "green",
    "lucky",
    "mellow",
    "silver",
    "solar",
    "swift",
    "violet",
    "warm",
)

ANIMALS = (
    "badger",
    "finch",
    "fox",
    "koala",
    "lynx",
    "otter",
    "panda",
    "robin",
    "seal",
    "tiger",
    "whale",
    "wolf",
)


def generate_device_name() -> str:
    return f"{secrets.choice(ADJECTIVES)}-{secrets.choice(ANIMALS)}"


def load_or_create_device_name(
    path: str,
    generator: Callable[[], str] = generate_device_name,
) -> str:
    """Return a stable generated name, creating it on the first app start."""
    identity_path = Path(path).expanduser()
    generated = generator().strip()
    if not generated:
        raise ValueError("generated device name is empty")

    try:
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        with identity_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = handle.read().strip()
            if existing:
                return existing
            handle.seek(0)
            handle.truncate()
            handle.write(generated + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(identity_path, 0o600)
    except OSError:
        # A read-only home must not prevent Talk from starting. The fallback is
        # stable for this process, though persistence requires a writable path.
        return generated
    return generated
