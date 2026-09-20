import os

from faultline_telemetry.dotenv import load_repo_dotenv


def test_loads_env_from_repo_root_found_via_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".env").write_text('# comment\nFOO="bar baz"\nEMPTY=\n\n')
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("EMPTY", raising=False)

    found = load_repo_dotenv(nested)

    assert found == repo / ".env"
    assert os.environ["FOO"] == "bar baz"
    assert os.environ["EMPTY"] == ""


def test_never_overrides_existing_env_vars(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".env").write_text("FOO=from-file\n")
    monkeypatch.setenv("FOO", "from-env")

    load_repo_dotenv(repo)

    assert os.environ["FOO"] == "from-env"


def test_returns_none_without_repo_or_env(tmp_path):
    assert load_repo_dotenv(tmp_path) is None
