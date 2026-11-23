# Repository instructions

## Branch names

Name branches `<type>/<short-kebab-topic>`, such as `docs/agents-md-standardization` or `fix/retry-timeout`.

- Use one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`, `perf`, `release` or `research` for the type.
- Use one to five lowercase words made of `a-z` and `0-9`, joined by hyphens. Keep the topic to 40 characters and describe the change.
- Omit tool, agent, model and user names, including prefixes like `claude/`, `codex/`, `antigravity/`, `cursor/` and `crew-`. Omit dates, random or hash suffixes, and bare ticket IDs.
- If a tool creates a branch with its own prefix, run `git branch -m <type>/<topic>` before pushing.
- The shape matches `^(feat|fix|chore|docs|refactor|test|ci|perf|release|research)/[a-z0-9]+(-[a-z0-9]+){0,4}$`. The 40-character topic limit also applies.

## GitHub Actions

Pin every `uses:` to a full 40-character commit hash, followed by the version as a comment (`actions/checkout@<sha> # v7.0.1`), never a tag or branch. Tags can be moved to different code after review; CI fails on an unpinned action. Dependabot keeps the hashes and comments current.

## No tautological work

Add a test, document, comment, label or UI element only when you can name the concrete failure or question it addresses. Zero new tests or docs are acceptable when no concrete regression or question calls for them.

Exercise the real behavior and identify the plausible regression in the test name or a brief comment. A test is tautological if it would still pass with that behavior broken. Avoid tests that only assert a mock returns its configured answer, compare a constant to itself, use expected values copied from the implementation, check that something exists or runs without error, or reimplement production logic to compute the expected result.

Document intent, constraints, reasoning and procedures the reader cannot infer from the code, filenames, signatures or Git history. Avoid comments that narrate code and labels, headings or UI elements that repeat adjacent information. Flag existing tautological work you encounter in files you change. Remove it only within the task's scope.
