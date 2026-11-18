"""The tool-agnostic sync engine.

One :class:`LinkRunner` per link executes the algorithm from the plan:

1. Record a run row; load ``since`` = last successful run's start (minus an
   overlap margin; hash comparison makes re-processing harmless).
2. Fetch changed items from BOTH sides.
3. Classify each pair: create / update / conflict / no-op — using the hashes
   stored in ``id_map`` as the source of truth (never cross-tool timestamps).
4. Apply the configured conflict strategy; execute writes through adapters.
5. Update ``id_map`` hashes, record stats, emit a Markdown report.

Direction handling: conflicts are *detected* regardless of direction (so
out-of-process edits surface in the report), but writes only happen along
directions the link permits.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from websync.adapters.base import Adapter, SyncError
from websync.config import LinkConfig, ProgramConfig
from websync.mapping import (
    AmbiguousReverseType,
    Mapper,
    MappingError,
    UnmappedValue,
)
from websync.models import RunStats, Side, SyncItem
from websync.reporting import render_report
from websync.state import Pair, StateError, StateStore, field_hash

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
OVERLAP_MARGIN = timedelta(minutes=5)

_S2T = {"bidirectional", "source_to_target"}
_T2S = {"bidirectional", "target_to_source"}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


class LinkRunner:
    def __init__(
        self,
        link: LinkConfig,
        source: Adapter,
        target: Adapter,
        state: StateStore,
        run_id: int,
        config_path: str = "",
        dry_run: bool = False,
    ) -> None:
        self.link = link
        self.source = source
        self.target = target
        self.state = state
        self.run_id = run_id
        self.config_path = config_path
        self.dry_run = dry_run
        self.mapper = Mapper.for_link(link)
        self.stats = RunStats()
        self.events: list[dict] = []

    # ------------------------------------------------------------------ run

    def run(self) -> RunStats:
        since = self.state.last_successful_started(self.link.name)
        whence = (since - OVERLAP_MARGIN) if since else EPOCH

        src_items = self.source.fetch_changed(whence)
        tgt_items = self.target.fetch_changed(whence)
        tgt_by_id = {i.native_id: i for i in tgt_items}
        processed_target_ids: set[str] = set()

        for item in src_items:
            self._safe(self._handle_source_item, item, tgt_by_id, processed_target_ids)

        for item in tgt_items:
            if item.native_id in processed_target_ids:
                continue
            self._safe(self._handle_target_item, item)

        return self.stats

    def _safe(self, handler, *args) -> None:
        try:
            handler(*args)
        except (UnmappedValue, AmbiguousReverseType) as exc:
            item = args[0]
            self._conflict(native_id=item.native_id, reason=type(exc).__name__, detail=str(exc))
        except SyncError as exc:
            self.stats.errors += 1
            self._event("error", native_id=args[0].native_id, detail=str(exc))
            log.warning("adapter error on %s: %s", self.link.name, exc)
        except MappingError as exc:
            self.stats.skipped += 1
            self._event("skip", native_id=args[0].native_id, detail=str(exc))

    # ------------------------------------------------------- source side

    def _handle_source_item(self, item: SyncItem, tgt_by_id, processed: set) -> None:
        if not self.mapper.has_source_type(item.type):
            self.stats.skipped += 1
            self._event("skip", native_id=item.native_id, detail=f"type {item.type!r} not mapped")
            return
        norm = self.mapper.to_normalized(item, Side.SOURCE)
        src_hash = field_hash(norm)
        pair = self.state.get_pair(self.link.name, Side.SOURCE.value, item.native_id)

        if pair is None:
            self._create_counterpart(item, norm, src_hash, from_side=Side.SOURCE)
            return

        tgt_item = tgt_by_id.get(pair.target_id)
        if tgt_item is not None:
            processed.add(pair.target_id)
            tgt_norm = self.mapper.to_normalized(tgt_item, Side.TARGET)
            tgt_hash = field_hash(tgt_norm)
            tgt_changed = tgt_hash != pair.target_hash
        else:
            tgt_norm = tgt_hash = None
            tgt_changed = False
        src_changed = src_hash != pair.source_hash

        if tgt_changed and src_changed:
            self._resolve_conflict(pair, item, norm, src_hash, tgt_item, tgt_norm, tgt_hash)
            return
        if tgt_changed:
            # Target edited (possibly out-of-process); push back if direction allows.
            self._push(tgt_norm, tgt_hash, pair, Side.SOURCE)
            return
        if not src_changed:
            self.stats.skipped += 1  # overlap re-fetch, nothing to do
            return
        self._push(norm, src_hash, pair, Side.TARGET)

    # ------------------------------------------------------- target side

    def _handle_target_item(self, item: SyncItem) -> None:
        pair = self.state.get_pair(self.link.name, Side.TARGET.value, item.native_id)
        if pair is None:
            self._create_counterpart(item, None, None, from_side=Side.TARGET)
            return
        tgt_norm = self.mapper.to_normalized(item, Side.TARGET)
        tgt_hash = field_hash(tgt_norm)
        if tgt_hash == pair.target_hash:
            self.stats.skipped += 1
            return
        # Source side was not in this run's changed set -> safe to push back.
        self._push(from_norm=tgt_norm, from_hash=tgt_hash, pair=pair, to_side=Side.SOURCE)

    # ------------------------------------------------------- actions

    def _create_counterpart(
        self,
        item: SyncItem,
        norm: dict | None,
        norm_hash: str | None,
        from_side: Side,
    ) -> None:
        if not self.link.create_missing:
            self.stats.skipped += 1
            self._event("skip", native_id=item.native_id, detail="create_missing=false")
            return
        if from_side == Side.SOURCE:
            if self.link.direction not in _S2T:
                self.stats.skipped += 1
                return
            new_type = self.mapper.target_type_for(item.type)
            fields = self.mapper.from_normalized(norm, Side.TARGET)
            to_adapter, to_side = self.target, Side.TARGET
        else:
            if self.link.direction not in _T2S:
                self.stats.skipped += 1
                return
            new_type = self.mapper.source_type_for(item.type)
            norm = self.mapper.to_normalized(item, Side.TARGET)
            norm_hash = field_hash(norm)
            fields = self.mapper.from_normalized(norm, Side.SOURCE)
            to_adapter, to_side = self.source, Side.SOURCE

        if self.dry_run:
            self._event(
                "create",
                side=from_side.value,
                native_id=item.native_id,
                detail=f"would create {to_side.value} item of type {new_type}",
            )
            self.stats.created += 1
            return
        new_id = to_adapter.create(SyncItem(native_id="", type=new_type, fields=fields))
        if from_side == Side.SOURCE:
            self.state.put_pair(
                self.link.name, _pair_of(item.native_id, new_id, norm_hash, norm_hash)
            )
        else:
            self.state.put_pair(
                self.link.name, _pair_of(new_id, item.native_id, norm_hash, norm_hash)
            )
        self.stats.created += 1
        self._event(
            "create",
            side=from_side.value,
            native_id=item.native_id,
            counterpart=new_id,
            detail=f"created {to_side.value} item",
        )

    def _push(self, from_norm: dict, from_hash: str, pair, to_side: Side) -> None:
        """Write one side's normalized fields to the counterpart side."""
        if to_side == Side.TARGET:
            if self.link.direction not in _S2T:
                self.stats.skipped += 1
                return
            fields = self.mapper.from_normalized(from_norm, Side.TARGET)
            to_adapter, to_id = self.target, pair.target_id
        else:
            if self.link.direction not in _T2S:
                self.stats.skipped += 1
                return
            fields = self.mapper.from_normalized(from_norm, Side.SOURCE)
            to_adapter, to_id = self.source, pair.source_id

        if self.dry_run:
            self._event("update", side=to_side.value, native_id=to_id, detail="would update")
            self.stats.updated += 1
            return
        to_adapter.update(to_id, fields)
        if to_side == Side.TARGET:
            self.state.put_pair(
                self.link.name, _pair_of(pair.source_id, pair.target_id, from_hash, from_hash)
            )
        else:
            self.state.put_pair(
                self.link.name, _pair_of(pair.source_id, pair.target_id, from_hash, from_hash)
            )
        self.stats.updated += 1
        self._event("update", side=to_side.value, native_id=to_id)

    def _resolve_conflict(
        self, pair, src_item, src_norm, src_hash, tgt_item, tgt_norm, tgt_hash
    ) -> None:
        strategy = self.link.conflict_strategy
        if strategy == "skip_and_report":
            self._conflict(
                native_id=src_item.native_id,
                reason="changed_on_both_sides",
                detail=f"strategy=skip_and_report; counterpart={pair.target_id}",
                counterpart=tgt_item.native_id,
            )
            return

        if strategy == "newest_wins":
            src_ts, tgt_ts = _aware(src_item.changed_at), _aware(tgt_item.changed_at)
            if src_ts is None or tgt_ts is None:
                self._conflict(
                    native_id=src_item.native_id,
                    reason="changed_on_both_sides",
                    detail="strategy=newest_wins but a changed_at timestamp is unknown",
                    counterpart=tgt_item.native_id,
                )
                return
            strategy = "source_wins" if src_ts >= tgt_ts else "target_wins"

        if strategy == "source_wins":
            self._push(src_norm, src_hash, pair, Side.TARGET)
        elif strategy == "target_wins":
            self._push(tgt_norm, tgt_hash, pair, Side.SOURCE)
        else:  # pragma: no cover - validated at config load
            raise ValueError(f"unknown conflict strategy {strategy!r}")

    # ------------------------------------------------------- reporting

    def _conflict(
        self, native_id: str, reason: str, detail: str, counterpart: str | None = None
    ) -> None:
        self.stats.conflicts += 1
        self.events.append(
            {
                "action": "conflict",
                "native_id": native_id,
                "reason": reason,
                "detail": detail,
                "counterpart": counterpart,
            }
        )
        # Conflicts are run-scoped; recorded even in dry runs for the report.
        self.state.record_conflict(
            self.run_id,
            self.link.name,
            native_id,
            {
                "reason": reason,
                "detail": detail,
                "counterpart": counterpart,
                "strategy": self.link.conflict_strategy,
            },
        )

    def _event(self, action: str, **kw) -> None:
        self.events.append({"action": action, **kw})


def _pair_of(source_id: str, target_id: str, source_hash: str, target_hash: str) -> Pair:
    return Pair(source_id, target_id, source_hash, target_hash)


class SyncEngine:
    """Runs every selected link of a program config and produces reports."""

    def __init__(
        self,
        config: ProgramConfig,
        adapters: dict[str, Adapter],
        state: StateStore,
        config_path: str = "",
        dry_run: bool = False,
        reports_dir: str = "reports",
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.state = state
        self.config_path = config_path
        self.dry_run = dry_run
        self.reports_dir = reports_dir

    def run(self, link_filter: str | None = None) -> list[dict]:
        results: list[dict] = []
        for link in self.config.links:
            if link_filter and link.name != link_filter:
                continue
            source = self.adapters[link.source]
            target = self.adapters[link.target]
            try:
                run_id = self.state.start_run(
                    self.config.program,
                    self.config_path,
                    link.name,
                    self.dry_run,
                )
            except StateError as exc:
                log.warning("link %s skipped: %s", link.name, exc)
                results.append(
                    {
                        "link": link.name,
                        "run_id": None,
                        "stats": {"errors": 1, "detail": str(exc)},
                        "report": None,
                    }
                )
                continue
            runner = LinkRunner(
                link,
                source,
                target,
                self.state,
                run_id,
                config_path=self.config_path,
                dry_run=self.dry_run,
            )
            try:
                stats = runner.run()
                self.state.finish_run(run_id, "success", stats.model_dump())
                markdown = render_report(
                    program=self.config.program,
                    link=link,
                    run_id=run_id,
                    dry_run=self.dry_run,
                    stats=stats,
                    events=runner.events,
                )
                self.state.save_report(run_id, markdown)
                path = self._write_report_file(link, run_id, markdown)
                results.append(
                    {
                        "link": link.name,
                        "run_id": run_id,
                        "stats": stats.model_dump(),
                        "report": str(path),
                    }
                )
                log.info("link %s: %s", link.name, stats.model_dump())
            except Exception as exc:
                self.state.finish_run(run_id, "failed", {"errors": 1, "detail": str(exc)})
                log.exception("link %s failed", link.name)
                results.append(
                    {
                        "link": link.name,
                        "run_id": run_id,
                        "stats": {"errors": 1, "detail": str(exc)},
                        "report": None,
                    }
                )
        return results

    def _write_report_file(self, link, run_id: int, markdown: str):
        out_dir = Path(self.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.config.program}-{link.name}-run{run_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path
