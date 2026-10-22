import pytest

from daily_digest import cli
from daily_digest.store import Store


def _run(monkeypatch, *argv):
    if argv[0] == "draft":
        argv = (*argv, "--stub")  # these tests are about the database, not the model key
    monkeypatch.setattr("sys.argv", ["daily-digest", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


@pytest.mark.parametrize("command", ["draft", "extract"])
def test_read_commands_fail_on_missing_db_and_create_nothing(command, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "typo" / "digest.db"
    assert _run(monkeypatch, command, "--db", str(missing)) != 0
    assert f"database not found: {missing}; run 'daily-digest ingest' first" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["draft", "extract"])
def test_read_commands_fail_on_missing_default_db(command, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DIGEST_DATA_DIR", str(tmp_path / "data"))
    assert _run(monkeypatch, command) != 0
    expected = tmp_path / "data" / "digest.db"
    assert f"database not found: {expected}; run 'daily-digest ingest' first" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["draft", "extract"])
def test_read_commands_still_work_on_existing_db(command, tmp_path, monkeypatch):
    path = tmp_path / "digest.db"
    Store(path).close()
    assert _run(monkeypatch, command, "--db", str(path)) == 0


def test_ingest_creates_db_on_first_run(tmp_path, monkeypatch):
    # No feeds, so nothing touches the network; only the database creation is under test.
    monkeypatch.setattr(cli, "load_sources", lambda path, include_disabled=False: [])
    monkeypatch.setattr(cli, "ingest", lambda *args: [])
    path = tmp_path / "new" / "digest.db"
    _run(monkeypatch, "ingest", "--db", str(path))
    assert path.is_file()
