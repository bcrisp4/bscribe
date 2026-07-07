"""The ``bscribe`` command-line interface.

One entrypoint for both the server (``serve`` — the container CMD) and local
administration. Token management is deliberately CLI-only, never HTTP
(docs/design.md — Non-goals): the commands talk straight to SQLite, so
tampering with tokens requires host access, not just network access.

Heavy imports (pydantic settings, the SQLite adapter) are deliberately
function-local: ``bscribe healthcheck`` runs every 30 seconds as the
container HEALTHCHECK and must not pay the server's import bill for one
``urlopen``.
"""

from __future__ import annotations

import http.client
import os
import urllib.request
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from bscribe.adapters.sqlite import SqliteTokenStore

app = typer.Typer(no_args_is_help=True, help="bscribe document conversion service.")
token_app = typer.Typer(no_args_is_help=True, help="Manage bearer tokens.")
app.add_typer(token_app, name="token")


def _token_store(*, must_exist: bool) -> SqliteTokenStore:
    """Open the same database the server uses (``BSCRIBE_DB_PATH``).

    Args:
        must_exist: When true, a missing database file is an error instead
            of being silently created — ``token list``/``delete`` against a
            wrong path must not fabricate an empty database and report
            "no tokens".
    """
    from bscribe.adapters.sqlite import SqliteTokenStore
    from bscribe.settings import Settings

    db_path = Settings().db_path
    if must_exist and not db_path.exists():
        typer.echo(
            f"no token database at {db_path} (is BSCRIBE_DB_PATH correct?)",
            err=True,
        )
        raise typer.Exit(1)
    return SqliteTokenStore(db_path)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(envvar="BSCRIBE_HOST")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="BSCRIBE_PORT")] = 8000,
) -> None:
    """Run the bscribe server (the container CMD).

    Defaults to loopback; the container image sets ``BSCRIBE_HOST=0.0.0.0``.
    Any extra arguments are passed to uvicorn verbatim, so the full uvicorn
    flag surface (``--proxy-headers``, ``--root-path``, ``--log-level``, …)
    stays available: ``bscribe serve --proxy-headers``.
    """
    argv = [
        "uvicorn",
        "--factory",
        "bscribe.app:create_app",
        "--host",
        host,
        "--port",
        str(port),
        *ctx.args,
    ]
    # exec, not subprocess: uvicorn replaces this process (PID 1 in the
    # container), keeping signal handling identical to running it directly.
    # S606/S607: fixed binary name resolved from PATH (the venv's uvicorn),
    # args come from the operator's own CLI invocation — no untrusted input.
    os.execvp("uvicorn", argv)  # noqa: S606, S607


@app.command()
def healthcheck(
    url: Annotated[str | None, typer.Option(envvar="BSCRIBE_HEALTHCHECK_URL")] = None,
    timeout_seconds: float = 3.0,
) -> None:
    """Probe the local liveness endpoint; exit 0 if healthy, 1 otherwise.

    Used by the container HEALTHCHECK — avoids shipping curl in the image.
    The default URL follows ``BSCRIBE_PORT``, so moving the server port via
    env keeps the container health probe pointed at the right place.
    """
    if url is None:
        url = f"http://127.0.0.1:{os.environ.get('BSCRIBE_PORT', '8000')}/healthz"
    if not url.startswith(("http://", "https://")):
        typer.echo(f"unsupported URL scheme: {url}", err=True)
        raise typer.Exit(1)
    try:
        # S310 audits dynamic urlopen schemes; guarded to http(s) above.
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            healthy = response.status == 200
    # OSError covers URLError/TimeoutError; ValueError covers
    # http.client.InvalidURL (malformed port); HTTPException covers
    # non-HTTP responders (BadStatusLine). Anything probe-shaped is
    # "unhealthy", never a traceback.
    except OSError, ValueError, http.client.HTTPException:
        healthy = False
    if not healthy:
        typer.echo("unhealthy", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


@token_app.command("add")
def token_add(label: str) -> None:
    """Create a token; prints the secret ONCE. It cannot be recovered."""
    from bscribe.domain.tokens import mint_token

    token, secret = mint_token(label)
    _token_store(must_exist=False).add(token)
    typer.echo(f"id:     {token.id}")
    typer.echo(f"label:  {token.label}")
    typer.echo(f"secret: {secret}")
    typer.echo("Store the secret now — it is shown only once.")


@token_app.command("list")
def token_list() -> None:
    """List tokens (ids, labels, creation times — never secrets)."""
    for token in _token_store(must_exist=True).list_all():
        typer.echo(f"{token.id}  {token.created_at.isoformat()}  {token.label}")


@token_app.command("delete")
def token_delete(token_id: str) -> None:
    """Revoke a token by id, effective immediately — no server restart."""
    if not _token_store(must_exist=True).delete(token_id):
        typer.echo(f"no such token: {token_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"deleted {token_id}")
