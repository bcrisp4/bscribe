"""Tests for the /v1/jobs endpoints."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.domain.errors import DocumentUnparseableError
from bscribe.domain.jobs import create_job
from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    Token,
)
from bscribe.domain.tokens import mint_token
from bscribe.errors import JOB_FAILED_NO_RESULT_DETAIL, UNPARSEABLE_DETAIL
from bscribe.settings import Settings
from tests.unit.fakes import FakeJobStore, GatedPool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

    from bscribe.domain.ports import JobStorePort


PDF_BYTES = b"%PDF-1.4 fake body"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    structlog.reset_defaults()


def make_app(
    tmp_path: Path, *, pool: GatedPool | None = None, **settings_overrides: object
) -> tuple[FastAPI, GatedPool]:
    settings = Settings(
        db_path=tmp_path / "bscribe.db",
        scratch_dir=tmp_path / "scratch",
        **settings_overrides,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    pool = pool or GatedPool()
    app.state.worker_pool = pool
    return app, pool


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def issue_token(app: FastAPI, label: str = "bsearch") -> tuple[Token, str]:
    token, secret = mint_token(label)
    app.state.token_store.add(token)
    return token, secret


def scratch_files(tmp_path: Path) -> list[Path]:
    scratch = tmp_path / "scratch"
    return list(scratch.iterdir()) if scratch.exists() else []


def seed_job(
    app: FastAPI,
    token_id: str,
    *,
    status: JobStatus = JobStatus.QUEUED,
    result: ParsedDocument | None = None,
    failure_detail: str = "timeout",
    created_at: datetime | None = None,
) -> Job:
    """Persist a job directly in the store, walked to the wanted status.

    Endpoint reads are tested against seeded store state; the runner's own
    lifecycle timing is covered in tests/unit/test_runner.py.
    """
    # app.state is untyped (Any); pin the port so mypy checks the calls.
    store: JobStorePort = app.state.job_store
    job = create_job(token_id=token_id, output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)
    if created_at is not None:
        # replace() re-runs __post_init__, so validation still applies.
        job = replace(job, created_at=created_at)
    store.add(job)
    if status is not JobStatus.QUEUED:
        store.mark_running(job.id)
    if status is JobStatus.DONE:
        store.mark_done(
            job.id,
            result or ParsedDocument(content="# Heading", pages=3, duration_ms=41.7),
        )
    elif status is JobStatus.FAILED:
        store.mark_failed(job.id, failure_detail)
    refreshed = store.get(job.id, token_id)
    assert refreshed is not None
    return refreshed


async def submit(client: httpx.AsyncClient, secret: str, **form: str) -> httpx.Response:
    return await client.post(
        "/v1/jobs",
        files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
        data=form,
        headers={"Authorization": f"Bearer {secret}"},
    )


class TestSubmitJob:
    async def test_returns_201_with_queued_job(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await submit(client, secret, output="text", ocr="off")
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "queued"
        assert body["output"] == "text"
        assert body["ocr"] == "off"
        assert len(body["id"]) == 16
        assert body["started_at"] is None
        assert body["finished_at"] is None
        assert body["failure_detail"] is None

    async def test_persists_job_owned_by_calling_token(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        async with make_client(app) as client:
            response = await submit(client, secret)
        job_id = response.json()["id"]
        assert app.state.job_store.get(job_id, token.id) is not None

    async def test_job_runs_to_done_and_cleans_scratch(self, tmp_path: Path) -> None:
        app, pool = make_app(tmp_path)
        pool.release.set()
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await submit(client, secret)
            await app.state.job_runner.drain()
            status = await client.get(
                f"/v1/jobs/{response.json()['id']}",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert status.json()["status"] == "done"
        assert scratch_files(tmp_path) == []

    async def test_parse_failure_becomes_failed_job_not_http_error(
        self, tmp_path: Path
    ) -> None:
        pool = GatedPool(exc=DocumentUnparseableError("quotes document internals"))
        pool.release.set()
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await submit(client, secret)
            assert response.status_code == 201
            await app.state.job_runner.drain()
            status = await client.get(
                f"/v1/jobs/{response.json()['id']}",
                headers={"Authorization": f"Bearer {secret}"},
            )
        body = status.json()
        assert body["status"] == "failed"
        assert body["failure_detail"] == UNPARSEABLE_DETAIL
        assert scratch_files(tmp_path) == []

    async def test_missing_token_is_401_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        app, pool = make_app(tmp_path)
        token = issue_token(app)[0]
        async with make_client(app) as client:
            response = await client.post(
                "/v1/jobs",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
            )
        assert response.status_code == 401
        assert pool.calls == []
        assert app.state.job_store.list_for_token(token.id) == []
        assert scratch_files(tmp_path) == []

    async def test_bogus_output_value_is_400(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await submit(client, secret, output="xml")
        assert response.status_code == 400

    async def test_unsupported_format_is_415_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/jobs",
                files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 415
        assert app.state.job_store.list_for_token(token.id) == []
        assert scratch_files(tmp_path) == []

    async def test_swapped_job_store_is_used_by_runner_too(
        self, tmp_path: Path
    ) -> None:
        """The factory comment invites swapping app.state.job_store before
        serving; the runner must honor the swap (no frozen store snapshot),
        or submitted jobs sit queued in one store while parsed in another."""
        app, pool = make_app(tmp_path)
        pool.release.set()
        fake_store = FakeJobStore()
        app.state.job_store = fake_store
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await submit(client, secret)
            await app.state.job_runner.drain()
        job_id = response.json()["id"]
        assert fake_store.jobs[job_id].status is JobStatus.DONE

    async def test_store_add_failure_leaks_no_scratch_file_or_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handoff to the runner is unconditional, so even a failed add
        leaves no scratch file behind (the task finds no row and cleans up).
        (Starlette's catch-all handler produces the 500 in production but
        re-raises under ASGITransport, hence pytest.raises here.)"""
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)

        def broken_add(job: Job) -> None:
            del job
            raise RuntimeError("database is locked")

        monkeypatch.setattr(app.state.job_store, "add", broken_add)
        async with make_client(app) as client:
            with pytest.raises(RuntimeError, match="database is locked"):
                await submit(client, secret)
            await app.state.job_runner.drain()
        assert scratch_files(tmp_path) == []
        assert app.state.job_store.list_for_token(token.id) == []

    async def test_oversized_upload_is_413_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        app, _ = make_app(tmp_path, max_upload_bytes=64)
        token, secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/jobs",
                files={"file": ("sample.pdf", b"x" * 200, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 413
        assert app.state.job_store.list_for_token(token.id) == []
        assert scratch_files(tmp_path) == []


class TestGetJob:
    async def test_returns_queued_job(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id)
        async with make_client(app) as client:
            response = await client.get(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job.id
        assert body["status"] == "queued"

    async def test_failed_job_exposes_fixed_failure_detail(
        self, tmp_path: Path
    ) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id, status=JobStatus.FAILED)
        async with make_client(app) as client:
            response = await client.get(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
        body = response.json()
        assert body["status"] == "failed"
        assert body["failure_detail"] == "timeout"
        assert body["finished_at"] is not None

    async def test_unknown_id_is_404(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs/deadbeefdeadbeef",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 404

    async def test_cross_token_404_is_indistinguishable_from_unknown(
        self, tmp_path: Path
    ) -> None:
        """Same status and byte-identical body for both cases — job
        existence never leaks across tokens."""
        app = make_app(tmp_path)[0]
        owner = issue_token(app, "owner")[0]
        other_secret = issue_token(app, "other")[1]
        job = seed_job(app, owner.id)
        async with make_client(app) as client:
            cross = await client.get(
                f"/v1/jobs/{job.id}",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
            unknown = await client.get(
                "/v1/jobs/deadbeefdeadbeef",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
        assert cross.status_code == 404
        assert cross.content == unknown.content


class TestGetJobResult:
    @pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
    async def test_pending_job_is_202_with_current_status(
        self, tmp_path: Path, status: JobStatus
    ) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id, status=status)
        async with make_client(app) as client:
            response = await client.get(
                f"/v1/jobs/{job.id}/result",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["id"] == job.id
        assert body["status"] == status.value

    async def test_done_job_returns_convert_response(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(
            app,
            token.id,
            status=JobStatus.DONE,
            result=ParsedDocument(content="# Heading", pages=3, duration_ms=41.7),
        )
        async with make_client(app) as client:
            response = await client.get(
                f"/v1/jobs/{job.id}/result",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 200
        # Same document shape the sync endpoint returns inline.
        assert response.json() == {
            "output": "markdown",
            "content": "# Heading",
            "metadata": {"pages": 3, "duration_ms": 42},
        }

    async def test_failed_job_is_409_problem(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id, status=JobStatus.FAILED)
        async with make_client(app) as client:
            response = await client.get(
                f"/v1/jobs/{job.id}/result",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 409
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["detail"] == JOB_FAILED_NO_RESULT_DETAIL

    async def test_cross_token_404_is_indistinguishable_from_unknown(
        self, tmp_path: Path
    ) -> None:
        app = make_app(tmp_path)[0]
        owner = issue_token(app, "owner")[0]
        other_secret = issue_token(app, "other")[1]
        job = seed_job(app, owner.id, status=JobStatus.DONE)
        async with make_client(app) as client:
            cross = await client.get(
                f"/v1/jobs/{job.id}/result",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
            unknown = await client.get(
                "/v1/jobs/deadbeefdeadbeef/result",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
        assert cross.status_code == 404
        assert cross.content == unknown.content


class TestListJobs:
    async def test_no_jobs_is_empty_wrapper(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 200
        assert response.json() == {"jobs": []}

    async def test_lists_newest_first(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        older = seed_job(app, token.id, created_at=datetime(2026, 7, 1, tzinfo=UTC))
        newer = seed_job(app, token.id, created_at=datetime(2026, 7, 5, tzinfo=UTC))
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs", headers={"Authorization": f"Bearer {secret}"}
            )
        assert [job["id"] for job in response.json()["jobs"]] == [newer.id, older.id]

    async def test_status_filter(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        seed_job(app, token.id, status=JobStatus.QUEUED)
        failed = seed_job(app, token.id, status=JobStatus.FAILED)
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs?status=failed",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert [job["id"] for job in response.json()["jobs"]] == [failed.id]

    async def test_invalid_status_filter_is_400(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs?status=exploded",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 400

    async def test_lists_only_calling_tokens_jobs(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        mine_token, mine_secret = issue_token(app, "mine")
        other_token = issue_token(app, "other")[0]
        mine = seed_job(app, mine_token.id)
        seed_job(app, other_token.id)
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs", headers={"Authorization": f"Bearer {mine_secret}"}
            )
        assert [job["id"] for job in response.json()["jobs"]] == [mine.id]

    async def test_list_never_carries_result_content(self, tmp_path: Path) -> None:
        """Listings are metadata-only: stored text must not appear."""
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        seed_job(
            app,
            token.id,
            status=JobStatus.DONE,
            result=ParsedDocument(content="SENSITIVE-TEXT", pages=1, duration_ms=1.0),
        )
        async with make_client(app) as client:
            response = await client.get(
                "/v1/jobs", headers={"Authorization": f"Bearer {secret}"}
            )
        assert "SENSITIVE-TEXT" not in response.text


class TestDeleteJob:
    @pytest.mark.parametrize(
        "status",
        [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE, JobStatus.FAILED],
    )
    async def test_purges_job_in_any_state(
        self, tmp_path: Path, status: JobStatus
    ) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id, status=status)
        async with make_client(app) as client:
            response = await client.delete(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
            followup = await client.get(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
            listed = await client.get(
                "/v1/jobs", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 204
        assert response.content == b""
        assert followup.status_code == 404
        assert listed.json() == {"jobs": []}
        # The DONE case seeds a stored result; delete purges it with the row.
        store: JobStorePort = app.state.job_store
        assert store.get_result(job.id, token.id) is None

    async def test_running_job_is_cancelled_and_scratch_cleaned(
        self, tmp_path: Path
    ) -> None:
        """DELETE on an in-flight job cancels its runner task (which kills
        the worker in production — WorkerPool.parse) and the task's cleanup
        deletes the scratch upload; the pool gate is never released, so the
        job was cancelled, not completed."""
        app, pool = make_app(tmp_path)
        token, secret = issue_token(app)
        async with make_client(app) as client:
            submitted = await submit(client, secret)
            job_id = submitted.json()["id"]
            await pool.started.wait()
            task = app.state.job_runner.task_for(job_id)
            assert task is not None
            response = await client.delete(
                f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {secret}"}
            )
            await app.state.job_runner.drain()
        assert response.status_code == 204
        assert task.cancelled()
        assert not pool.release.is_set()
        assert app.state.job_store.get(job_id, token.id) is None
        assert scratch_files(tmp_path) == []

    async def test_second_delete_is_404(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        token, secret = issue_token(app)
        job = seed_job(app, token.id)
        async with make_client(app) as client:
            first = await client.delete(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
            second = await client.delete(
                f"/v1/jobs/{job.id}", headers={"Authorization": f"Bearer {secret}"}
            )
        assert first.status_code == 204
        assert second.status_code == 404

    async def test_unknown_id_is_404(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            response = await client.delete(
                "/v1/jobs/deadbeefdeadbeef",
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 404

    async def test_cross_token_404_is_indistinguishable_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        """Same status and byte-identical body as an unknown id, and the
        owner's job survives — deletion never leaks or acts across tokens."""
        app = make_app(tmp_path)[0]
        owner = issue_token(app, "owner")[0]
        other_secret = issue_token(app, "other")[1]
        job = seed_job(app, owner.id)
        async with make_client(app) as client:
            cross = await client.delete(
                f"/v1/jobs/{job.id}",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
            unknown = await client.delete(
                "/v1/jobs/deadbeefdeadbeef",
                headers={"Authorization": f"Bearer {other_secret}"},
            )
        assert cross.status_code == 404
        assert cross.content == unknown.content
        assert app.state.job_store.get(job.id, owner.id) is not None

    async def test_missing_token_is_401_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        app = make_app(tmp_path)[0]
        token = issue_token(app)[0]
        job = seed_job(app, token.id)
        async with make_client(app) as client:
            response = await client.delete(f"/v1/jobs/{job.id}")
        assert response.status_code == 401
        assert app.state.job_store.get(job.id, token.id) is not None


class TestJobsPrivacy:
    async def test_filename_and_content_absent_from_info_logs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pool = GatedPool(
            result=ParsedDocument(content="SENSITIVE-TEXT", pages=1, duration_ms=1.0)
        )
        pool.release.set()
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)[1]
        async with make_client(app) as client:
            await client.post(
                "/v1/jobs",
                files={
                    "file": ("bank-statement-SECRET.pdf", PDF_BYTES, "application/pdf")
                },
                headers={"Authorization": f"Bearer {secret}"},
            )
            await app.state.job_runner.drain()
        captured = capsys.readouterr()
        # Filename is DEBUG-only; extracted text is never logged at any level.
        assert "bank-statement-SECRET" not in captured.out
        assert "SENSITIVE-TEXT" not in captured.out
