"""websync command-line interface (typer)."""

from __future__ import annotations

from pathlib import Path

import typer

from websync import __version__

app = typer.Typer(
    name="websync",
    help="Synchronize items across internal engineering tools.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the websync version."""
    typer.echo(f"websync {__version__}")


@app.command()
def validate_config(configs: list[Path]) -> None:
    """Validate one or more mapping YAML configs.

    Exits non-zero with a human-readable error list if any config is invalid.
    """
    from websync.config import load_config

    failed = False
    for path in configs:
        try:
            cfg = load_config(path)
            link_names = [link.name for link in cfg.links]
            typer.echo(f"OK  {path}  (program={cfg.program}, links={link_names})")
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            failed = True
            typer.echo(f"FAIL {path}: {exc}", err=True)
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def run(
    config: Path = typer.Option(..., "--config", help="Path to a project YAML config."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print intended changes without writing anything."
    ),
    link: str | None = typer.Option(
        None, "--link", help="Run only the named link from the config."
    ),
) -> None:
    """Run a synchronization pass for a project config (Phase 1: not yet wired)."""
    typer.echo("The sync engine arrives in Phase 1; nothing to run yet.")
    typer.echo(f"(config={config}, dry_run={dry_run}, link={link})")
    raise typer.Exit(code=0)


@app.command()
def status() -> None:
    """Show recent sync runs and pending conflicts (Phase 1: not yet wired)."""
    typer.echo("State reporting arrives in Phase 1.")
    raise typer.Exit(code=0)


@app.command()
def report() -> None:
    """Render the Markdown report for the last run (Phase 1: not yet wired)."""
    typer.echo("Conflict reporting arrives in Phase 1.")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
