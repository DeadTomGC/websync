"""websync command-line interface (typer)."""

from __future__ import annotations

import importlib
from pathlib import Path

import typer

from websync import __version__
from websync.adapters import ADAPTER_REGISTRY
from websync.env import load_env

# Load .env from the CWD (or nearest parent) before any command runs, so
# adapter settings and envvar-backed options (e.g. WEBSYNC_DB_PATH) see it.
# Real env vars take precedence (override=False), so CI is unaffected.
load_env()

app = typer.Typer(
    name="websync",
    help="Synchronize items across internal engineering tools.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the websync version."""
    typer.echo(f"websync {__version__}")


@app.command(name="validate-config")
def validate_config(configs: list[Path]) -> None:
    """Validate one or more mapping YAML configs.

    Also builds every link's Mapper, so bad transforms/formats/value maps fail
    here instead of mid-sync. Exits non-zero on any failure.
    """
    from websync.config import load_config
    from websync.mapping import Mapper

    failed = False
    for path in configs:
        try:
            cfg = load_config(path)
            for link in cfg.links:
                Mapper.for_link(link)  # raises on unknown transforms/formats
            link_names = [link.name for link in cfg.links]
            typer.echo(f"OK  {path}  (program={cfg.program}, links={link_names})")
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            failed = True
            typer.echo(f"FAIL {path}: {exc}", err=True)
    raise typer.Exit(code=1 if failed else 0)


def _build_adapter_factory(fake: bool):
    """Build ``factory(tool, scope) -> Adapter``, memoized per (tool, project).

    Real adapters are constructed from environment settings + the link's
    scope for that side (see docs/tools/*.md); ``--fake`` swaps in-memory
    adapters so the pipeline can be exercised without network access.
    """
    from websync.adapters.fake import InMemoryAdapter

    if fake:
        return lambda tool, scope: InMemoryAdapter()

    def factory(tool: str, scope):
        if tool not in ADAPTER_REGISTRY:
            raise typer.BadParameter(f"unknown tool {tool!r} (known: {sorted(ADAPTER_REGISTRY)})")
        module_path, _, class_name = ADAPTER_REGISTRY[tool].rpartition(".")
        adapter_cls = getattr(importlib.import_module(module_path), class_name)
        return adapter_cls.from_env(scope)

    cache: dict[tuple[str, str], object] = {}

    def memoized(tool: str, scope):
        key = (tool, str(scope.project) if scope else "")
        if key not in cache:
            cache[key] = factory(tool, scope)
        return cache[key]

    return memoized


@app.command()
def run(
    config: Path = typer.Option(..., "--config", help="Path to a project YAML config."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print intended changes without writing anything."
    ),
    link: str | None = typer.Option(
        None, "--link", help="Run only the named link from the config."
    ),
    db: Path = typer.Option(
        Path("state.db"), "--db", envvar="WEBSYNC_DB_PATH", help="SQLite state DB path."
    ),
    reports_dir: Path = typer.Option(
        Path("reports"), "--reports-dir", help="Directory for Markdown reports."
    ),
    fake: bool = typer.Option(
        False, "--fake", help="Use in-memory fake adapters (demo/testing only)."
    ),
) -> None:
    """Run a synchronization pass for a project config."""
    from websync.adapters.base import SyncError
    from websync.config import load_config
    from websync.engine import SyncEngine
    from websync.state import StateError, StateStore

    try:
        cfg = load_config(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"invalid config {config}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    factory = _build_adapter_factory(fake)
    try:
        # Pre-build every link's adapters so missing env vars / unimplemented
        # tools fail fast with a clear message instead of mid-run.
        # (Careful: do not shadow the `--link` option here.)
        for lnk in cfg.links:
            factory(lnk.source, lnk.scope.get("source"))
            factory(lnk.target, lnk.scope.get("target"))
    except NotImplementedError as exc:
        typer.echo(f"adapter not implemented yet: {exc}", err=True)
        typer.echo("hint: pass --fake to exercise the pipeline with in-memory adapters.", err=True)
        raise typer.Exit(code=2) from exc
    except SyncError as exc:
        typer.echo(f"cannot construct adapters: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    state = StateStore(db)
    try:
        engine = SyncEngine(
            cfg,
            state=state,
            config_path=str(config),
            dry_run=dry_run,
            reports_dir=str(reports_dir),
            adapter_factory=factory,
        )
        results = engine.run(link_filter=link)
    except StateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        state.close()

    exit_code = 0
    typer.echo(f"program {cfg.program}: {len(results)} link run(s)")
    for result in results:
        typer.echo(f"  link {result['link']}: {result['stats']} report={result['report']}")
        if result["stats"].get("errors"):
            exit_code = 1
    raise typer.Exit(code=exit_code)


@app.command()
def status(
    db: Path = typer.Option(
        Path("state.db"), "--db", envvar="WEBSYNC_DB_PATH", help="SQLite state DB path."
    ),
    limit: int = typer.Option(20, "--limit", help="How many recent runs to show."),
) -> None:
    """Show recent sync runs and pending conflicts."""
    from websync.state import StateStore

    if not db.exists():
        typer.echo(f"no state database at {db}", err=True)
        raise typer.Exit(code=1)
    state = StateStore(db)
    try:
        runs = state.list_runs(limit)
        if not runs:
            typer.echo("no runs recorded yet.")
            return
        typer.echo(f"{'id':>4}  {'program':<12} {'link':<28} {'status':<8} dry  started")
        pending = 0
        for r in runs:
            typer.echo(
                f"{r['id']:>4}  {r['program']:<12} {r['link_name']:<28} "
                f"{r['status']:<8} {'yes' if r['dry_run'] else 'no':<3}  {r['started']}"
            )
            if r["status"] == "running":
                pending += 1
        if pending:
            typer.echo(f"note: {pending} run(s) still marked running", err=True)
    finally:
        state.close()


@app.command()
def report(
    db: Path = typer.Option(
        Path("state.db"), "--db", envvar="WEBSYNC_DB_PATH", help="SQLite state DB path."
    ),
    run_id: int | None = typer.Option(
        None, "--run", help="Report for a specific run (default: latest)."
    ),
) -> None:
    """Print the Markdown report of a run."""
    from websync.state import StateStore

    if not db.exists():
        typer.echo(f"no state database at {db}", err=True)
        raise typer.Exit(code=1)
    state = StateStore(db)
    try:
        if run_id is None:
            latest = state.latest_run()
            if latest is None:
                typer.echo("no runs recorded yet.")
                return
            run_id = int(latest["id"])
        markdown = state.get_report(run_id)
        if markdown is None:
            typer.echo(f"no report stored for run {run_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(markdown)
    finally:
        state.close()


if __name__ == "__main__":
    app()
