# Security policy

## Credential handling

`YUKON_API_TOKEN` is presence-checked only. Odysseus never reads its value,
accepts it as a flag/configuration field, serializes it, or writes it to logs.
Credentials for Yukon or candidate providers are inherited only by a narrowly
configured child process. The reference candidate provider reads its named secret
only inside its isolated process; the harness passes only the environment-variable
name and never serializes, logs, or echoes its value. Logs, exception text, state,
and public output are redacted before persistence.

## Process handling

The command runner accepts an allowlisted executable and argument array. It
never invokes a shell, evaluates user text, or exposes a generic command
passthrough. It uses a fixed canonical cwd, a minimal environment, bounded
stdout/stderr capture, timeouts, and process-group termination.

## Filesystem handling

State roots must be absolute and outside benchmark checkouts. All runtime data
is private and immutable once written. Patch paths are rejected if absolute,
traversing, ambiguous under case normalization, symlink-escaping, in `.git`, in
a submodule, or Yukon local-link metadata. Do not put credentials in config,
fixtures, issue reports, or artifacts.

## Responsible disclosure

Report vulnerabilities privately to the repository maintainers. Do not include
live tokens, benchmark private data, personal paths, or unredacted trace output
in a report. No Yukon public operation is implemented by this project.
