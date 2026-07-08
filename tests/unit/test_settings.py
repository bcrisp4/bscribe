"""Tests for bscribe.settings."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from bscribe.settings import Settings


class TestDefaults:
    """Defaults match the design doc."""

    def test_worker_count_defaults_to_four(self) -> None:
        assert Settings().worker_count == 4

    def test_job_timeout_defaults_to_ten_minutes(self) -> None:
        assert Settings().job_timeout_seconds == 600

    def test_worker_max_tasks_defaults_to_one_hundred(self) -> None:
        assert Settings().worker_max_tasks == 100

    def test_max_upload_defaults_to_fifty_megabytes(self) -> None:
        assert Settings().max_upload_bytes == 50 * 1024 * 1024

    def test_scratch_dir_defaults_under_system_tempdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shared conftest points BSCRIBE_SCRATCH_DIR at a temp dir (the
        # lifespan sweep wipes the scratch dir); clear it to see the default.
        monkeypatch.delenv("BSCRIBE_SCRATCH_DIR")
        assert Settings().scratch_dir == Path(tempfile.gettempdir()) / "bscribe"

    def test_db_path_defaults_to_absolute_user_data_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shared conftest points BSCRIBE_DB_PATH at a temp file (the
        # store is built eagerly by create_app); clear it to see the default.
        monkeypatch.delenv("BSCRIBE_DB_PATH")
        default = Settings().db_path
        assert default.is_absolute()
        assert default == Path.home() / ".local" / "share" / "bscribe" / "bscribe.db"

    def test_result_ttl_defaults_to_seven_days(self) -> None:
        assert Settings().result_ttl_seconds == 7 * 24 * 3600

    def test_purge_interval_defaults_to_one_hour(self) -> None:
        assert Settings().purge_interval_seconds == 3600

    def test_log_level_defaults_to_info(self) -> None:
        assert Settings().log_level == "INFO"


class TestEnvOverrides:
    """Every field is overridable via a BSCRIBE_-prefixed env var."""

    def test_int_field_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_WORKER_COUNT", "2")
        assert Settings().worker_count == 2

    def test_path_field_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_SCRATCH_DIR", "/data/scratch")
        assert Settings().scratch_dir == Path("/data/scratch")

    def test_db_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_DB_PATH", "/data/bscribe.db")
        assert Settings().db_path == Path("/data/bscribe.db")

    def test_log_level_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_LOG_LEVEL", "DEBUG")
        assert Settings().log_level == "DEBUG"

    def test_purge_interval_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_PURGE_INTERVAL_SECONDS", "60")
        assert Settings().purge_interval_seconds == 60


class TestValidation:
    """Invalid env values are rejected loudly at startup, not at use."""

    def test_zero_worker_count_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_WORKER_COUNT", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_zero_worker_max_tasks_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_WORKER_MAX_TASKS", "0")
        assert Settings().worker_max_tasks == 0

    def test_negative_worker_max_tasks_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_WORKER_MAX_TASKS", "-1")
        with pytest.raises(ValidationError):
            Settings()

    def test_non_numeric_timeout_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_JOB_TIMEOUT_SECONDS", "ten minutes")
        with pytest.raises(ValidationError):
            Settings()

    def test_bogus_log_level_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSCRIBE_LOG_LEVEL", "LOUD")
        with pytest.raises(ValidationError):
            Settings()

    def test_negative_upload_limit_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_MAX_UPLOAD_BYTES", "-1")
        with pytest.raises(ValidationError):
            Settings()

    def test_zero_purge_interval_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_PURGE_INTERVAL_SECONDS", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_negative_purge_interval_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSCRIBE_PURGE_INTERVAL_SECONDS", "-1")
        with pytest.raises(ValidationError):
            Settings()


def test_settings_are_frozen() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.worker_count = 8
