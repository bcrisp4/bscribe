"""The ``bscribe`` command-line interface.

One entrypoint for both the server (``serve`` — the container CMD) and local
administration. Token management is deliberately CLI-only, never HTTP
(docs/design.md — Non-goals): the commands talk straight to SQLite, so
tampering with tokens requires host access, not just network access.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import typer
import uvicorn

from bscribe.adapters.sqlite import SqliteTokenStore
from bscribe.domain.tokens import mint_token
from bscribe.settings import Settings

app = typer.Typer(no_args_is_help=True, help="bscribe document conversion service.")
token_app = typer.Typer(no_args_is_help=True, help="Manage bearer tokens.")
app.add_typer(token_app, name="token")


def _token_store() -> SqliteTokenStore:
    """Open the same database the server uses (BSCRIBE_DB_PATH)."""
    return SqliteTokenStore(Settings().db_path)


@app.command()
def serve(
    host: str = "0.0.0.0",  # noqa: S104 - container default; loopback-only makes no sense behind podman
    port: int = 8000,
) -> None:
    """Run the bscribe server (the container CMD)."""
    uvicorn.run("bscribe.app:create_app", factory=True, host=host, port=port)


@app.command()
def healthcheck(
    url: str = "http://127.0.0.1:8000/healthz",
    timeout_seconds: float = 3.0,
) -> None:
    """Probe the local liveness endpoint; exit 0 if healthy, 1 otherwise.

    Used by the container HEALTHCHECK — avoids shipping curl in the image.
    """
    if not url.startswith(("http://", "https://")):
        typer.echo(f"unsupported URL scheme: {url}", err=True)
        raise typer.Exit(1)
    try:
        # S310 audits dynamic urlopen schemes; guarded to http(s) above.
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            healthy = response.status == 200
    except urllib.error.URLError, TimeoutError, OSError:
        healthy = False
    if not healthy:
        typer.echo("unhealthy", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


@token_app.command("add")
def token_add(label: str) -> None:
    """Create a token; prints the secret ONCE. It cannot be recovered."""
    token, secret = mint_token(label)
    _token_store().add(token)
    typer.echo(f"id:     {token.id}")
    typer.echo(f"label:  {token.label}")
    typer.echo(f"secret: {secret}")
    typer.echo("Store the secret now — it is shown only once.")


@token_app.command("list")
def token_list() -> None:
    """List tokens (ids, labels, creation times — never secrets)."""
    for token in _token_store().list_all():
        typer.echo(f"{token.id}  {token.created_at.isoformat()}  {token.label}")


@token_app.command("delete")
def token_delete(token_id: str) -> None:
    """Revoke a token by id, effective immediately — no server restart."""
    if not _token_store().delete(token_id):
        typer.echo(f"no such token: {token_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"deleted {token_id}")
