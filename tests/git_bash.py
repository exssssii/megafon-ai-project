"""The bash that runs the hooks of Claude Code: on Windows that is Git Bash, not the WSL launcher."""
from pathlib import Path
import shutil


def git_bash() -> str | None:
    found = shutil.which("bash")
    # System32\bash.exe and the WindowsApps alias start WSL, which sees neither the Windows paths nor Python.
    if found and not any(part in found.lower() for part in ("\\system32\\", "\\windowsapps\\")):
        return found
    git = shutil.which("git")
    if git:
        for folder in Path(git).resolve().parents[:2]:
            candidate = folder / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return None
