# Vendored agent skills

These evaluation skills are copied from a third-party repository so Claude Code can
use them in this project. The files are unmodified copies of the upstream folders.

- Source: https://github.com/ai-evals-course/evals-skills
- Pinned commit: `80d5f7b0127c7572ed9e9339937adbfd7240ffeb` (committed 2026-09-24)
- Retrieved: 2026-11-11
- License: Apache License 2.0. The upstream `LICENSE` is copied unmodified to
  [`LICENSE`](LICENSE) in this folder.

## Vendored skills

- `eval-audit`
- `write-judge-prompt`
- `validate-evaluator`
- `write-code-eval`
- `error-discovery`
- `generate-synthetic-data`

Each folder is a byte-for-byte copy of the upstream folder at the pinned commit,
including `agents/openai.yaml` and `error-discovery/review-loop.md`. Don't edit these
files in place. Changes come from an upstream update.

## Updating

1. Download the upstream archive at a newer commit, and record that commit hash.
2. Diff the new folders against these, for example
   `diff -r <new>/skills/eval-audit .claude/skills/eval-audit`.
3. Review every change like third-party code: the instructions, any scripts or
   prompts, any new dependencies, and anything that sends data outside this machine.
4. Replace the folders and `LICENSE`, then update the commit and retrieval date above.

## Notes for this repo

- For trace review, use `scio blind` and `scio annotate` rather than building a new
  review app. `error-discovery` may suggest building its own interface. Don't. Its
  agent-side review loop still applies: cluster the notes into failure modes, working
  from `error_analysis/labeling_sheet.csv`.
- Some skills mention optional libraries, for example judgy or numpy in
  `validate-evaluator`. They are not project dependencies. Ask before adding any.
