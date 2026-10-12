"""ffmpeg discovery.

The bug these lock down: an app launched from Finder inherits a minimal PATH and
never sources the user's shell profile, so ffmpeg installed via Homebrew or
pip --user is invisible. It worked in Terminal and failed on double-click.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text import audio  # noqa: E402


class TestFindExecutable:
    def test_finds_something_that_is_on_path(self):
        assert audio.find_executable("ls") is not None

    def test_returns_none_for_a_name_that_does_not_exist(self):
        assert audio.find_executable("definitely-not-a-real-binary-xyz") is None

    def test_falls_back_when_path_cannot_see_it(self, tmp_path, monkeypatch):
        """The Finder case: a valid binary that PATH simply does not mention."""
        binary = tmp_path / "fakeffmpeg"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(audio, "FALLBACK_BIN_DIRS", (str(tmp_path),))

        assert audio.find_executable("fakeffmpeg") == str(binary)

    def test_ignores_a_non_executable_file(self, tmp_path, monkeypatch):
        binary = tmp_path / "notexec"
        binary.write_text("x")
        binary.chmod(0o644)

        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(audio, "FALLBACK_BIN_DIRS", (str(tmp_path),))

        assert audio.find_executable("notexec") is None

    def test_ignores_a_directory_with_the_right_name(self, tmp_path, monkeypatch):
        (tmp_path / "adir").mkdir()
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(audio, "FALLBACK_BIN_DIRS", (str(tmp_path),))

        assert audio.find_executable("adir") is None

    def test_require_ffmpeg_names_the_places_it_looked(self, monkeypatch):
        """The error has to say where it looked, or the user cannot fix it."""
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(audio, "FALLBACK_BIN_DIRS", ("/no/such/dir",))

        with pytest.raises(audio.AudioError) as excinfo:
            audio.require_ffmpeg()
        assert "/no/such/dir" in str(excinfo.value)
        assert "brew install ffmpeg" in str(excinfo.value)

    def test_real_ffmpeg_is_found_even_with_a_minimal_path(self, monkeypatch):
        """Whatever machine this runs on, a discovered ffmpeg must stay discoverable."""
        if audio.find_executable("ffmpeg") is None:
            pytest.skip("ffmpeg is not installed on this machine")

        monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        assert audio.require_ffmpeg()
