# Declarative Odysseus skills

A skill is untrusted, bounded prompt context—not code or a plugin. Each immediate
child directory contains exactly these two regular files:

- `skill.json`: exact JSON manifest fields: `name`, `description`, `version`,
  `languages`, `categories`, and `tags`.
- `SKILL.md`: UTF-8 declarative guidance, bounded by the configuration limits.

No scripts, hooks, commands, symlinks, nested directories, or extra files are
accepted. A skill is only loaded when its name is explicitly listed in
`skills.enabled`; `auto_select` can only narrow that enabled set using structured
benchmark language/category/tags. Candidate runs with enabled auto-selection must
pass trusted `--benchmark-language` and `--benchmark-category`; repeat
`--benchmark-tag` only when an enabled skill declares tags. With no enabled skills,
auto-selection requires no metadata. It never uses note text and never searches
home directories or global Codex skills.
