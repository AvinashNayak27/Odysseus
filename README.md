# Odysseus

Odysseus is a local, review-first Python 3.12 foundation for a Yukon optimization
harness. It writes inspectable JSON/JSONL records and deliberately has **no**
command for submission, public-note publication, normal sync, reset, force, or
arbitrary shell execution.

## Install

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
odysseus --help
```

Copy `odysseus.example.json` to a private location, configure absolute workspace
paths outside every benchmark checkout, then use `--config /private/odysseus.json`
for every stage. The runtime root is never implicitly placed in a checkout or in
`.odysseus`.

`discover` performs only Yukon `version`, `benchmark list`, and `benchmark show`
reads, then stores a deterministic immutable shortlist lock. `prepare` first
prints a hash-bound plan. It only executes a clone when `--execute-clone` and the
exact printed approval hash are both supplied; offline clone-output verification
uses `--clone-report` and stops with `restart_required` when Yukon requests a
relaunch. `baseline`, `research`, `candidates`, `experiment import-samples`, and `report` take
explicit input artifacts and perform pure parsing/validation/rendering where
possible. Imported samples are labeled unverified and cannot be ranked, recommended,
or used by `report`; reports require a sealed, manifest-verified experiment artifact.

`report --input REVIEW.json --experiment-artifact PATH` does not trust claimed
effects embedded in review metadata. It loads the sealed experiment artifact from
the configured state root and refuses imported or unverified measurements.

`verify-run RUN_ID` compares every retained artifact against the run's recorded
manifest and returns a non-zero result if a file is missing, added, or changed.

## Current command surface

The stage-oriented command surface is intentionally limited to `doctor`,
`discover`, `prepare`, `inspect-frontier`, `baseline`, `research`, `candidates`,
`experiment import-samples`, `report`, `status`, `verify-run`, and `yukon sync-harness`. Every
stage has a concrete handler. Stages operating on benchmark-facing artifacts
require explicit verified paths and fail with a prerequisite error rather than
inferring commands or executing arbitrary input. Yukon installation is a human
prerequisite and is not a product command.

`doctor` reports whether `YUKON_API_TOKEN` is present, never its value. The
application does not accept tokens in CLI arguments or configuration. Yukon and
provider credentials are inherited by approved child processes only.

## Candidate providers

The `candidate_provider` configuration supports three retained adapter modes:

- `human-import` validates a response supplied with `candidates --response` and
  never executes a provider.
- `external-cli` invokes the configured fixed `argv` using the harness's bounded
  shell-free `CommandRunner`. It remains the interoperability escape hatch.
- `reference` invokes the bundled `odysseus-reference-provider` in a separate
  process through the same command policy. Configure its HTTPS `base_url`, exact
  public Yukon `public_model` label (for example, `GPT 5.6 Sol`), exact API
  `api_model` identifier, `effort`, and `secret_env` name. The public label and
  API identifier are intentionally separate.

The reference provider is a standard-library implementation of a documented
OpenAI-compatible `POST /v1/responses` contract. It sends a strict JSON Schema
for the CandidateResponse, accepts exactly one structured output text value, and
re-validates that JSON locally. The returned exact public model label and effort
must match configuration; public Yukon labels are not required to contain a slash.
The secret
value is read only by the isolated provider process, is never part of its JSON
stdin/stdout or configuration, and provider errors are scrubbed. It permits only
HTTPS, has bounded request/response payloads and timeout, and refuses redirects.

For a compatible service that differs from this conservative Responses contract,
use `external-cli` or `human-import`. No SDK is installed or required.

## Security model

- Commands use allowlisted argument arrays, `shell=False`, a canonical working
  directory, a minimal inherited environment, output limits, and process-group
  timeout cleanup.
- Runtime records are append-only JSONL. Artifacts use write/fsync/rename and
  content hashes under collision-safe immutable run directories.
- Paths are canonicalized and containment checks reject traversal, absolute
  patch paths, symlink escapes, case-fold ambiguity, `.git`, submodules, and
  Yukon local-link metadata.
- Approval hashes bind the exact rendered plan, benchmark identity, paths, and
  command specification. Re-render or path changes invalidate approval. Harness-only
  sync hashes locked editable paths before and after execution, quarantines changes,
  and marks setup stale.
- Public rendering must use aliases and stricter redaction; raw private paths,
  credentials, token-like text, email addresses, trace content, and hidden
  metadata are rejected or scrubbed.

See [SECURITY.md](SECURITY.md) for operating constraints. A human must review
all installation, clone/setup, experiment, and any possible later publication
outside this program.
