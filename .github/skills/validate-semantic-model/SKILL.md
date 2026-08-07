---
name: validate-semantic-model
description: Validate an Ossie semantic model or a converted vendor artifact. Use when asked to check that a model is correct, that a converter round trip is faithful, or that generated TMSL/BIM will actually load in Power BI. Covers core-spec validation, offline TOM structural validation, and live engine validation.
---

# Validating a semantic model

Validation is layered. Each layer catches a defect class the layer above cannot see, so
running only the cheapest one gives false confidence. Start at layer 1 and stop at the
last layer the change actually needs.

## Layer 1 — core spec (always)

Every Ossie YAML document must validate against the core spec. This is the only layer
that applies to all converters.

```bash
uv run --script validation/validate.py path/to/model.yaml
```

Run from the repository root. A converter change that alters emitted YAML is not
finished until this passes on the converter's own examples.

## Layer 2 — round trip (converters with an import and an export)

Import the vendor artifact, export it back, and compare. Divergence means the converter
loses information in one direction.

```bash
cd converters/<name>
uv run ossie-<name> import -q -i tests/fixtures/<fixture> -o rt.yaml
uv run ossie-<name> export -q -i rt.yaml -o rt.bim
uv run --script ../../validation/validate.py rt.yaml
```

A round trip that is faithful is not the same as a round trip that is correct: if the
input fixture is itself invalid, a faithful converter reproduces the defect exactly. When
input and output are rejected identically, suspect the fixture, not the converter.

## Layer 3 — offline TOM structural validation (Microsoft converter)

Deserializes TMSL through the real Analysis Services object model and checks structure
and cross-object references. Needs .NET 8 and a one-time assembly restore; no
credentials, no network at validation time.

```bash
cd converters/microsoft
uv run python scripts/restore_tom.py
uv run pytest tests/test_tom_integration.py -v
```

**Offline TOM does not parse DAX at all.** Unknown functions, unknown columns,
unbalanced parentheses and wrong arity all deserialize clean. Never describe a model as
"validated" on the strength of this layer alone.

## Layer 4 — live engine (Microsoft converter, manual only)

The only layer that compiles DAX and enforces the invariants the engine applies at load
time. It publishes the model to a real Fabric workspace, swaps every partition for a
small inline sample table so a refresh runs, then evaluates each measure.

```bash
cd converters/microsoft
export OSSIE_MICROSOFT_FABRIC_WORKSPACE=<workspace-guid>
uv run python scripts/validate_with_engine.py path/to/model.bim
```

Tokens come from `az login` unless `OSSIE_MICROSOFT_FABRIC_TOKEN` and
`OSSIE_MICROSOFT_POWERBI_TOKEN` are set; the two APIs use different token resources.

This layer needs a tenant, a capacity and credentials, so it **cannot run in CI** and is
deliberately not wired into any workflow. Run it locally before landing a converter
change that alters generated TMSL. Each run creates and deletes a semantic model, so
point it at a scratch workspace, never a shared one.

## Choosing a layer

| Change | Layers to run |
|---|---|
| Docs, tests, refactor with no output change | 1 |
| Emitted YAML changes | 1, 2 |
| Emitted TMSL structure changes | 1, 2, 3 |
| Emitted DAX or column data types change | 1, 2, 3, 4 |

## Traps

- A DAX failure from `executeQueries` arrives as **HTTP 200** with a per-query `error`
  object and an empty row set. Code that trusts the status code turns every broken
  measure into a silent null and makes tests pass vacuously.
- A column with no resolved data type is read by TMSL as `automatic`, which only
  calculated columns may carry. Offline TOM accepts it; the engine refuses the whole
  model with `changes will cause data deletion`, naming no table or column. Bisect by
  deploying one table at a time, then cumulative column prefixes.
- Engine error text wraps object names in `<oii>...</oii>` telemetry markers. Strip them
  before showing an error to a user.
- Passing tests prove the converter is self-consistent, not that the output loads. Only
  layer 4 proves the latter.

## Reporting

State which layers ran and which did not, and say plainly what the layers you skipped
would have covered. "Validated" without a layer is not a useful claim.
