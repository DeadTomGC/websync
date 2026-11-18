# Mapping Configuration Specification

One YAML file per program/project in `configs/`. Each file describes one or
more **links** (a directed pair of tools + what syncs between them). This is
the primary customization surface for end users, so it must be declarative,
validatable, and reviewable in a merge request.

## Schema (validated by a pydantic model on load)

```yaml
# configs/programA.cameo-jama.yaml
program: programA
links:
  - name: cameo-jama-requirements      # unique; used as id_map key
    source: cameo_twc
    target: jama
    types:                              # type map: source type -> target type
      requirement: requirement
      interface: interfaceRequirement
    scope:                              # limit what we look at
      source:
        project: "Program A / SysModel"  # TWC project path/ID
      target:
        project: 123                      # Jama container/project ID
        item_type_filter: [requirement]  # optional
    fields:                             # field map: normalized name -> per-tool field
      title:   {source: name,        target: name}
      text:    {source: specification, target: description, format: html}
      status:  {source: status,      target: status,
                 value_map: {draft: "In Progress", approved: "Approved"}}
      priority: {source: criticality, target: priority,
                 value_map: {high: 3, medium: 2, low: 1}}
    direction: bidirectional            # bidirectional | source_to_target | target_to_source
    conflict_strategy: newest_wins      # newest_wins | source_wins | target_wins | skip_and_report
    create_missing: true               # create counterpart if no id_map entry
    delete_behavior: skip              # skip | close_counterpart (never hard-delete by default)

  - name: jama-jira-epics-stories
    source: jama
    target: jira
    types:
      epic: epic
      story: story
      feature: story                   # many-to-one type folding is allowed
    scope:
      source: {project: 123, item_type_filter: [epic, story, feature]}
      target: {project: PROGA, board: "Program A Board"}
    fields:
      title:    {source: name, target: summary}
      text:     {source: description, target: description, format: html}
      status:   {source: status, target: status,
                 value_map: {"In Progress": "In Progress", Done: Done, ...}}
      epic_link: {source: parent, target: epic_link, relation: parent_child}
    direction: bidirectional
    conflict_strategy: source_wins
    create_missing: true
```

## Rules the mapping layer must enforce

1. **Normalized names:** `fields` keys are canonical names; each side maps
   them to that tool's native field. Translation always goes
   `toolA native → normalized → toolB native`. Never direct A→B.
2. **value_map** converts enumerations both directions (reverse lookup). An
   unmapped value is a **conflict/skip-and-report**, never a silent default.
3. **Unknown fields:** extra fields present on one side but absent from the
   config are ignored (left untouched) — the config is a whitelist.
4. **Relations:** `relation:` entries (e.g., parent/child, epic/story link)
   are resolved through the `id_map` table; unmatched counterparts defer the
   link (retry next run) rather than failing the item.
5. **HTML/text conversion** (`format:`) — tools disagree on rich text;
   implement converters per target format (html ↔ markdown/plain) and test
   round-tripping early. Jama/Jira both use rich text; Cameo uses plain.
6. **Type folding** (many→one in `types:`) is one-directional; when syncing
   the folded direction back, the config must specify an explicit
   `reverse_type` or the field is skipped.
7. **CLI check:** `websync validate-config configs/*.yaml` must be run in CI
   on every merge request so broken configs can't merge.

## Config validation model (pydantic, `src/websync/config.py`)

- `ProgramConfig`, `LinkConfig`, `FieldMap`, `ValueMap`, `ScopeConfig`
- Validation errors must be human-readable (list file + YAML path + reason)
  so non-developer config authors can self-correct.
- Schema additions (new fields like future Teamcenter release settings) must
  be additive and defaulted so old configs keep validating.

## Customization beyond YAML

If a mapping genuinely cannot be expressed (complex computed fields), add a
named **transform hook** in `src/websync/transforms.py` — a small registry of
pure functions referenced from YAML by name:

```yaml
    fields:
      estimate: {source: effort, target: story_point_estimate,
                 transform: man_days_to_points}
```

Prefer YAML-only solutions; add hooks sparingly and document each in this
file when introduced.
