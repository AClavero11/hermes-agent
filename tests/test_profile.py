"""Tests for hermes_cli.profile — profile CRUD and env var wiring."""

import pytest

from hermes_cli.profile import (
    create_profile,
    delete_profile,
    get_profile_memory_dir,
    get_profile_soul_path,
    list_profiles,
    profile_exists,
)


@pytest.fixture
def tmp_profiles(tmp_path, monkeypatch):
    """Redirect profiles to a clean temp directory."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setattr("hermes_cli.profile.get_profiles_dir", lambda: profiles_dir)
    monkeypatch.setattr("hermes_cli.profile.get_hermes_home", lambda: hermes_home)
    return profiles_dir


class TestProfileCRUD:
    def test_create_profile(self, tmp_profiles):
        path = create_profile("research")
        assert path.exists()
        assert (path / "SOUL.md").exists()
        assert (path / "memories").is_dir()

    def test_create_profile_starter_soul(self, tmp_profiles):
        create_profile("ops")
        soul = (tmp_profiles / "ops" / "SOUL.md").read_text()
        assert "ops agent" in soul

    def test_create_duplicate_raises(self, tmp_profiles):
        create_profile("research")
        with pytest.raises(ValueError, match="already exists"):
            create_profile("research")

    def test_invalid_name_raises(self, tmp_profiles):
        with pytest.raises(ValueError, match="Invalid profile name"):
            create_profile("bad name!")

    def test_list_profiles(self, tmp_profiles):
        assert list_profiles() == []
        create_profile("alpha")
        create_profile("beta")
        assert list_profiles() == ["alpha", "beta"]

    def test_profile_exists(self, tmp_profiles):
        assert not profile_exists("research")
        create_profile("research")
        assert profile_exists("research")

    def test_delete_profile(self, tmp_profiles):
        create_profile("temp")
        assert profile_exists("temp")
        assert delete_profile("temp")
        assert not profile_exists("temp")

    def test_delete_nonexistent(self, tmp_profiles):
        assert not delete_profile("ghost")

    def test_clone_with_soul(self, tmp_profiles):
        # Create a fake global SOUL.md in hermes_home
        hermes_home = tmp_profiles.parent / "hermes_home"
        global_soul = hermes_home / "SOUL.md"
        global_soul.write_text("I am the global soul.", encoding="utf-8")

        path = create_profile("cloned", clone=True)
        cloned_soul = (path / "SOUL.md").read_text()
        assert cloned_soul == "I am the global soul."

    def test_clone_with_memories(self, tmp_profiles):
        # Create fake global memories in hermes_home
        hermes_home = tmp_profiles.parent / "hermes_home"
        global_mem = hermes_home / "memories"
        global_mem.mkdir(exist_ok=True)
        (global_mem / "MEMORY.md").write_text("fact1\n\xa7\nfact2", encoding="utf-8")

        path = create_profile("cloned", clone=True)
        cloned_mem = path / "memories" / "MEMORY.md"
        assert cloned_mem.exists()
        assert "fact1" in cloned_mem.read_text()

    def test_get_profile_soul_path(self, tmp_profiles):
        create_profile("research")
        soul = get_profile_soul_path("research")
        assert soul is not None
        assert soul.name == "SOUL.md"

    def test_get_profile_memory_dir(self, tmp_profiles):
        create_profile("research")
        mem_dir = get_profile_memory_dir("research")
        assert mem_dir.is_dir()


class TestEnvVarWiring:
    def test_memory_dir_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_MEMORY_DIR", str(tmp_path / "custom-mem"))
        # Re-import to pick up the env var
        import importlib
        import tools.memory_tool as mt
        importlib.reload(mt)
        assert str(mt.MEMORY_DIR) == str(tmp_path / "custom-mem")
        # Restore
        monkeypatch.delenv("HERMES_MEMORY_DIR")
        importlib.reload(mt)

    def test_soul_path_override(self, tmp_path, monkeypatch):
        soul_file = tmp_path / "custom-soul.md"
        soul_file.write_text("Custom soul content.", encoding="utf-8")
        monkeypatch.setenv("HERMES_SOUL_PATH", str(soul_file))

        from agent.prompt_builder import build_context_files_prompt
        result = build_context_files_prompt(cwd=str(tmp_path))
        assert "Custom soul content" in result
