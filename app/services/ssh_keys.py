from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Tuple

from config import SSH_DIR, SSH_KEY
from i18n import t


DEFAULT_SSH_KEY_PATH = SSH_KEY or f"{SSH_DIR}/id_ed25519"


def _md(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
    )


def get_ssh_private_key_path() -> str:
    return DEFAULT_SSH_KEY_PATH


def get_ssh_public_key_path() -> str:
    return f"{get_ssh_private_key_path()}.pub"


def ensure_ssh_keypair(path: str | None = None) -> Tuple[bool, str]:
    private_path = pathlib.Path(path or get_ssh_private_key_path())
    public_path = pathlib.Path(f"{private_path}.pub")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(private_path.parent, 0o700)

    if private_path.exists() and public_path.exists():
        os.chmod(private_path, 0o600)
        os.chmod(public_path, 0o644)
        return True, ""

    if private_path.exists() and not public_path.exists():
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "ssh-keygen -y failed").strip()
        public_path.write_text((proc.stdout or "").strip() + "\n", encoding="utf-8")
        os.chmod(public_path, 0o644)
        return True, ""

    if private_path.exists():
        os.chmod(private_path, 0o600)
        return True, ""

    proc = subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "node-plane",
            "-f",
            str(private_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "ssh-keygen failed").strip()
    os.chmod(private_path, 0o600)
    if public_path.exists():
        os.chmod(public_path, 0o644)
    return True, ""


def get_public_key(path: str | None = None) -> Tuple[bool, str]:
    ok, err = ensure_ssh_keypair(path)
    if not ok:
        return False, err
    public_path = pathlib.Path(f"{path or get_ssh_private_key_path()}.pub")
    try:
        return True, public_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        return False, str(exc)


def render_public_key_guide(locale: str = "ru") -> Tuple[bool, str]:
    text = (
        f"{t(locale, 'ssh.guide_title')}\n\n"
        f"{t(locale, 'ssh.quick_steps')}\n\n"
        f"{t(locale, 'ssh.step1')}\n"
        f"{t(locale, 'ssh.step2')}\n"
        f"{t(locale, 'ssh.step3')}\n"
        f"{t(locale, 'ssh.step4')}\n"
    )
    return True, text


def render_public_key_summary(locale: str = "ru") -> Tuple[bool, str]:
    ok, payload = get_public_key()
    if not ok:
        return False, payload
    public_path = _md(get_ssh_public_key_path())
    public_key = _md(payload)
    text = (
        f"{t(locale, 'ssh.title')}\n\n"
        f"{t(locale, 'ssh.section_status')}\n"
        f"{t(locale, 'ssh.status_ready')}\n\n"
        f"{t(locale, 'ssh.section_path')}\n"
        f"{t(locale, 'ssh.public_path', path=public_path)}\n\n"
        f"{t(locale, 'ssh.public_key')}\n"
        f"`{public_key}`\n\n"
        f"{t(locale, 'ssh.section_next')}\n"
        f"{t(locale, 'ssh.summary_hint')}"
    )
    return True, text
