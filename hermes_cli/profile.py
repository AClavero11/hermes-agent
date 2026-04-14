"""
Profile management for Hermes Agent.

Profiles provide isolated agent personas with independent SOUL.md and memory.
All profiles share the same AGENTS.md (project-level coordination), config.yaml
(base settings), and .env (credentials).

Directory structure:
    ~/.hermes/profiles/{name}/
        SOUL.md          # Profile-specific persona
        memories/        # Isolated memory store (MEMORY.md, USER.md)

Usage:
    hermes profile list
    hermes profile create research --clone
    hermes profile show research
    hermes profile delete research
    hermes -p research              # Launch CLI with profile
    hermes chat -p research         # Same via subcommand
"""

import shutil
from pathlib import Path
from typing import Optional

from hermes_cli.config import get_hermes_home


def get_profiles_dir() -> Path:
    """Return the profiles root directory (~/.hermes/profiles/)."""
    return get_hermes_home() / "profiles"


def list_profiles() -> list[str]:
    """Return sorted list of profile names."""
    profiles_dir = get_profiles_dir()
    if not profiles_dir.exists():
        return []
    return sorted(
        d.name
        for d in profiles_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def profile_exists(name: str) -> bool:
    return (get_profiles_dir() / name).is_dir()


def get_profile_dir(name: str) -> Path:
    """Return the directory for a named profile. Does not check existence."""
    return get_profiles_dir() / name


def get_profile_soul_path(name: str) -> Optional[Path]:
    """Return the SOUL.md path for a profile, or None if it doesn't exist."""
    soul = get_profile_dir(name) / "SOUL.md"
    return soul if soul.exists() else None


def get_profile_memory_dir(name: str) -> Path:
    """Return the memories directory for a profile."""
    return get_profile_dir(name) / "memories"


def create_profile(name: str, clone: bool = False) -> Path:
    """Create a new profile directory with SOUL.md and memories/.

    Args:
        name: Profile name (alphanumeric + hyphens/underscores).
        clone: If True, copy the global SOUL.md as the starting point.

    Returns:
        Path to the created profile directory.

    Raises:
        ValueError: If name is invalid or profile already exists.
    """
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(
            f"Invalid profile name '{name}'. Use alphanumeric characters, hyphens, or underscores."
        )
    if profile_exists(name):
        raise ValueError(f"Profile '{name}' already exists.")

    profile_dir = get_profile_dir(name)
    memories_dir = profile_dir / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)

    hermes_home = get_hermes_home()

    # SOUL.md: clone from global or create a starter
    if clone:
        global_soul = hermes_home / "SOUL.md"
        if global_soul.exists():
            shutil.copy2(global_soul, profile_dir / "SOUL.md")
        else:
            _write_starter_soul(profile_dir / "SOUL.md", name)
    else:
        _write_starter_soul(profile_dir / "SOUL.md", name)

    # Clone memories if requested and they exist
    if clone:
        global_memories = hermes_home / "memories"
        if global_memories.exists():
            for mem_file in ("MEMORY.md", "USER.md"):
                src = global_memories / mem_file
                if src.exists():
                    shutil.copy2(src, memories_dir / mem_file)

    return profile_dir


def delete_profile(name: str) -> bool:
    """Delete a profile directory. Returns True if deleted, False if not found."""
    profile_dir = get_profile_dir(name)
    if not profile_dir.exists():
        return False
    shutil.rmtree(profile_dir)
    return True


def _write_starter_soul(path: Path, name: str):
    """Write a minimal starter SOUL.md for a new profile."""
    path.write_text(
        f"You are the {name} agent.\n\n"
        f"Define this profile's persona, expertise, and boundaries here.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI command handler
# ---------------------------------------------------------------------------

def profile_command(args):
    """Handle `hermes profile <action>` commands."""
    action = getattr(args, "profile_action", None)

    if action == "list":
        profiles = list_profiles()
        if not profiles:
            print("No profiles found. Create one with: hermes profile create <name>")
            return
        print(f"{'Name':<20} {'SOUL.md':>10} {'Memory':>10}")
        print("-" * 42)
        for name in profiles:
            has_soul = "yes" if get_profile_soul_path(name) else "no"
            mem_dir = get_profile_memory_dir(name)
            has_memory = "yes" if any(mem_dir.glob("*.md")) else "no"
            print(f"{name:<20} {has_soul:>10} {has_memory:>10}")

    elif action == "create":
        name = args.profile_name
        clone = getattr(args, "clone", False)
        try:
            path = create_profile(name, clone=clone)
            print(f"Created profile '{name}' at {path}")
            if clone:
                print("  Cloned SOUL.md and memories from global config.")
            else:
                print(f"  Edit {path / 'SOUL.md'} to define this agent's persona.")
        except ValueError as e:
            print(f"Error: {e}")

    elif action == "show":
        name = args.profile_name
        if not profile_exists(name):
            print(f"Profile '{name}' not found.")
            return
        profile_dir = get_profile_dir(name)
        soul_path = profile_dir / "SOUL.md"
        mem_dir = get_profile_memory_dir(name)
        print(f"Profile: {name}")
        print(f"  Path: {profile_dir}")
        print(f"  SOUL.md: {'exists' if soul_path.exists() else 'missing'}")
        if soul_path.exists():
            content = soul_path.read_text(encoding="utf-8").strip()
            # Show first 3 lines
            lines = content.split("\n")[:3]
            for line in lines:
                print(f"    {line}")
            if len(content.split("\n")) > 3:
                print("    ...")
        mem_files = list(mem_dir.glob("*.md"))
        print(f"  Memories: {len(mem_files)} file(s)")
        for mf in sorted(mem_files):
            size = mf.stat().st_size
            print(f"    {mf.name} ({size} bytes)")

    elif action == "delete":
        name = args.profile_name
        if not profile_exists(name):
            print(f"Profile '{name}' not found.")
            return
        confirm = getattr(args, "yes", False)
        if not confirm:
            try:
                reply = input(f"Delete profile '{name}' and all its data? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                reply = "n"
            if reply not in ("y", "yes"):
                print("Cancelled.")
                return
        if delete_profile(name):
            print(f"Deleted profile '{name}'.")
        else:
            print(f"Failed to delete profile '{name}'.")

    else:
        print("Usage: hermes profile <list|create|show|delete>")
        print("  hermes profile list                  List all profiles")
        print("  hermes profile create <name>         Create a new profile")
        print("  hermes profile create <name> --clone Clone from global config")
        print("  hermes profile show <name>           Show profile details")
        print("  hermes profile delete <name>         Delete a profile")
