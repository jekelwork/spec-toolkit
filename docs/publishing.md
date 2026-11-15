# Publishing the toolkit to the template

Maintainer notes for this repository only. This page is not part of the export, and no published
file links to it.

The toolkit is published to the public template repository, `jekelwork/spec-toolkit-template`, by
exporting the toolkit files from a reviewed commit on `main` and opening a pull request there (see
the constitution's "Publishing to the Template" section).

## Export

1. Update `main`, and make sure the commit you publish has been reviewed and merged:
   `git fetch origin && git checkout main && git pull --ff-only origin main`.
2. Check that the manifest classifies every file tracked at the ref:
   `python3 scripts/export_toolkit.py --check`. A new file that matches neither `include` nor
   `exclude` in `scripts/toolkit-export.toml` fails here, so add it to the right list first.
   `--check` does not see untracked or uncommitted files.
3. Export the commit into an empty directory outside the repository:
   `python3 scripts/export_toolkit.py --ref HEAD --output ../toolkit-export`
   (`--list` prints the paths, `--tarball FILE.tar.gz` writes an archive instead).
4. In a clone of the template repository, create a branch, replace its files with the export
   (delete files the export no longer has), and review the diff. Then open a pull request in the
   template repository. Do not push to its `main`.

The export reads git objects at the ref, never the working tree, so uncommitted changes are not
exported (the script warns about them).

## What is excluded, and why

`scripts/toolkit-export.toml` is the single list. In short:

- `project-context.toml`, `ROADMAP.md`, `.specify/memory/**` (the constitution) and `specs/**`
  are this repository's own governance and plan. The template must stay uninitialized: each adopting
  project creates its own through `docs/bootstrap.md`.
- The export tooling (`export_toolkit.py`, the manifest and their tests), the `Toolkit source`
  workflow and this page exist only here.

Everything else is exported. The `Toolkit source` workflow's `Toolkit export` job runs
`export_toolkit.py --check`; the export tests (`scripts/test_export_toolkit.py`) and
`scripts/test_fresh_export.py` run in the `Repository tools` job. The fresh-export test exports the
repository and checks that the export is uninitialized (`python3 scripts/project_context.py
--bootstrap-state` prints `uninitialized`), has none of the excluded files, has the files adopters
need (with `.claude/skills` as a symlink), passes its own unit tests and passes
`scripts/agent_setup.py check`.

The `Toolkit export` job should be a required status check in the `main` ruleset of this
repository. The maintainer adds it; the template does not have this workflow.

## Fixes made in the template

A fix contributed directly to the template must be brought back here, or the next publish undoes it.
Apply the same change in a pull request on an issue-backed branch in this repository, merge it,
then publish as above. Compare before publishing:
`diff -rq -x .git <template clone> <export directory>`.

## If the template repository moves

After moving the repository (for example to an organisation), open its settings and tick
**Template repository** again. Then confirm that the Toolkit workflow's "Keep the template
uninitialized" step runs on a pull request there. The step has the condition
`github.event.repository.is_template`, so it is skipped silently while the repository is not marked
as a template, and nothing then stops an initialized state from reaching the template. Also update
the template name in the documentation that links to it.
