import os
from pathlib import Path

import pytest

from job_hunter import __main__ as cli
from job_hunter.bootstrap import (
    CONFIG_TEMPLATE,
    PROFILE_TEMPLATE,
    RESUME_TEMPLATE,
    TEMPLATE_MARKER,
    bootstrap,
    ensure_not_template,
)
from job_hunter.config import load_env_file

ROOT = Path(__file__).parent.parent


def test_templates_match_committed_examples() -> None:
    assert CONFIG_TEMPLATE == (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert PROFILE_TEMPLATE == (ROOT / "profile.example.md").read_text(encoding="utf-8")
    assert TEMPLATE_MARKER in PROFILE_TEMPLATE and TEMPLATE_MARKER in RESUME_TEMPLATE


def test_bootstrap_creates_everything_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the template's resume/profile paths are relative
    cfg = tmp_path / "data" / "config.yaml"
    created = bootstrap(cfg)
    assert cfg in created and len(created) == 3
    assert cfg.read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_bootstrap_only_fills_gaps_and_never_overwrites(tmp_path: Path) -> None:
    resume, profile = tmp_path / "me.md", tmp_path / "prof" / "p.md"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'home_location: "Austin, TX"\nresume_path: {resume}\nprofile_path: {profile}\n')
    resume.write_text("my real resume")
    created = bootstrap(cfg)
    assert created == [profile]  # config and resume untouched
    assert resume.read_text() == "my real resume"
    assert bootstrap(cfg) == []


def test_bootstrap_skips_missing_pdf_resume(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"resume_path: {tmp_path / 'cv.pdf'}\nprofile_path: {tmp_path / 'p.md'}\n")
    assert bootstrap(cfg) == [tmp_path / "p.md"]
    assert not (tmp_path / "cv.pdf").exists()


def test_template_detection() -> None:
    with pytest.raises(ValueError, match="unedited template"):
        ensure_not_template(Path("x.md"), RESUME_TEMPLATE)
    ensure_not_template(Path("x.md"), "real content")


def test_cli_reports_created_files_then_rejects_unedited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    assert cli.main(["--config", "data/config.yaml", "once", "--dry-run"]) == 2
    assert "Created starter files" in capsys.readouterr().err
    assert cli.main(["--config", "data/config.yaml", "once", "--dry-run"]) == 2
    assert "home_location" in capsys.readouterr().err


def test_env_file_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A_KEY", "B_KEY", "C_KEY", "D_KEY", "E_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("E_KEY", "from-real-env")
    env = tmp_path / ".env"
    env.write_bytes(  # CRLF line endings, as written by Windows editors
        b"A_KEY=plain\r\nB_KEY=spaced   # trailing comment\r\n"
        b'C_KEY="quoted # not a comment"\r\nD_KEY=\r\nE_KEY=from-file\r\n'
    )
    assert load_env_file(env) == 4
    assert os.environ["A_KEY"] == "plain"
    assert os.environ["B_KEY"] == "spaced"
    assert os.environ["C_KEY"] == "quoted # not a comment"
    assert os.environ["D_KEY"] == ""
    assert os.environ["E_KEY"] == "from-real-env"  # real environment wins
    assert load_env_file(tmp_path / "missing.env") == 0
