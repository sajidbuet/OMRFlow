# Merge notes — `roadmap/0.1.1-alpha.0-live-intake`

**Status of this branch:** planning and documentation only. Not merged.
Created 2026-09-27 from `main` at `2d18ccb`, while a separate, substantial
update to the repository was in progress on another computer.

This file says how to bring the branch into `main` **after** that other update
has landed, without losing either piece of work.

---

## 1. What the branch contains

Only Markdown:

| Path | Kind |
|---|---|
| `development/releases/0.1.1-alpha.0/**` | New files — the release-specific roadmap, architecture notes, acceptance criteria, these notes and the implementation prompt pack |
| `docs/wiki/Development-Roadmap.md` | One small additive section near the top, pointing at the release roadmap |

Nothing under `src/`, `tests/`, `scripts/`, `packaging/`, `.github/`, and no
migration, model, version or packaging file is touched. In particular
`src/omr_scanner/_version.py` still reads `__version__ = "0.1.0-alpha.2"`.

Because almost everything is a new file in a new directory, the only realistic
conflict is the pointer section in `docs/wiki/Development-Roadmap.md`, and only
if the other update edits the top of that page.

---

## 2. Preconditions

Do not start until **all** of these are true:

1. The home-computer update has been pushed and is on `origin/main`.
2. The working tree on the machine doing the merge is clean
   (`git status` reports nothing to commit). Commit or stash your own work
   first; do not merge on top of uncommitted changes.
3. Nobody else is mid-way through pushing to `main`.

---

## 3. Procedure (PowerShell)

```powershell
git fetch origin

# Bring local main up to the remote, refusing anything but a fast-forward.
git switch main
git pull --ff-only origin main

# Replay the planning commit(s) on top of the new main.
git switch roadmap/0.1.1-alpha.0-live-intake
git rebase origin/main
```

### If the rebase stops on a conflict

The likely file is `docs/wiki/Development-Roadmap.md`. Resolve it **by hand**:

- keep everything the home-computer update wrote;
- re-add the short "Next development line: `0.1.1-alpha.0`" pointer section
  where it still makes sense (just after the status legend / phase table);
- if the other update already renamed or restructured phases (for example,
  changed Phase 11B's target version), adjust the wording of the pointer to
  match — do not revert their change.

Then:

```powershell
git add docs/wiki/Development-Roadmap.md
git rebase --continue
```

If the rebase goes wrong and you want to start over, `git rebase --abort`
returns the branch exactly to its pre-rebase state. That is safe; nothing is
lost.

**Do not** resolve conflicts with `git checkout --theirs`/`--ours` on whole
files, and do not use `git push --force` against `main`.

### Re-check the planning documents against the new `main`

The home-computer update may have changed things these documents cite. Before
merging, re-read at least:

- `development/releases/0.1.1-alpha.0/ARCHITECTURE_NOTES.md` §2
  ("What exists today") against the new `src/omr_scanner/database/models.py`,
  `database/migrations.py` (the latest schema version),
  `services/batch_store.py`, `services/review_store.py`,
  `services/conflict_policy.py` and `services/scan_provenance.py`;
- the "Preserve" list at the top of each prompt in `prompts/`.

Any fact that changed should be corrected in these documents in a follow-up
commit on this branch **before** merging. Every prompt already instructs the
implementer to re-inspect the repository first, so a stale detail is not
fatal — but a planning document that contradicts the code it plans against is
worse than none.

### Verify

```powershell
git status
git diff --name-only origin/main...HEAD
git diff origin/main...HEAD
```

`git diff --name-only origin/main...HEAD` must list only files under
`development/releases/0.1.1-alpha.0/` and `docs/wiki/Development-Roadmap.md`.
If anything under `src/`, `tests/`, `scripts/`, `packaging/` or `.github/`
appears, stop and find out why before going further.

There is no Markdown linter in this repository's CI; the relevant automated
check is that the suite still passes on the rebased tree, which it must
because no code changed. Running it is still cheap insurance:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

### Publish the rebased branch

The rebase rewrote the branch's commits, so the remote copy of the branch
needs updating. Use the lease form, which refuses if somebody else pushed to
the same branch meanwhile:

```powershell
git push --force-with-lease origin roadmap/0.1.1-alpha.0-live-intake
```

This rewrites only the planning branch, never `main`.

### Merge

```powershell
git switch main
git merge --ff-only roadmap/0.1.1-alpha.0-live-intake
git push origin main
```

---

## 4. If a fast-forward is refused

`--ff-only` fails if `main` moved again after the rebase. That is the safe
outcome: nothing was changed. Choose one of:

1. **Preferred — rebase again.** Repeat §3 from `git fetch origin`. The
   repository's history is linear (no merge commits at the time of writing),
   and this keeps it that way.
2. **If rebasing is undesirable** (for example the branch has been shared and
   others have built on it): make an explicit merge commit instead, which
   rewrites nothing:

   ```powershell
   git switch main
   git pull --ff-only origin main
   git merge --no-ff roadmap/0.1.1-alpha.0-live-intake `
       -m "Merge planning branch for 0.1.1-alpha.0 live intake"
   git push origin main
   ```

   Or open a pull request from the branch and merge it on GitHub.

Never use `git reset --hard`, `git push --force` to `main`, or delete the
branch until the merge is confirmed on `origin/main`.

---

## 5. After the merge — the version bump

The branch deliberately does **not** change the version. `0.1.1-alpha.0` is a
target, not the running application.

The version changes from `0.1.0-alpha.2` to `0.1.1-alpha.0` **only when
implementation of the new development line begins on the merged codebase** —
that is, as the first commit of prompt `01-architecture-persistence.md`, or as
a very small preparatory commit immediately before it. Rules:

- edit `__version__` in `src/omr_scanner/_version.py` **and nowhere else** —
  it is the single source of truth; `pyproject.toml`, the About dialog, the
  installer and the diagnostics all derive from it. Do not introduce a second
  constant;
- run the existing version tests (`tests/unit/test_version.py` and
  `tests/unit/test_release_automation.py`, or whatever replaces them by then) — `numeric_version("0.1.1-alpha.0")`
  gives `(0, 1, 1, 0)`, which sorts after `0.1.0-alpha.2`'s `(0, 1, 0, 2)`;
- add an `## [Unreleased]` note in `CHANGELOG.md` saying the 0.1.1 line has
  begun; do not create a `[0.1.1-alpha.0]` release heading until it is
  actually released;
- do **not** tag, build or publish anything as part of the bump. Releasing
  `0.1.1-alpha.0` is gated by `ACCEPTANCE_CRITERIA.md` §7 and prompt `06`.

---

## 6. Deleting the branch

Only after `origin/main` contains the planning commit(s):

```powershell
git branch -d roadmap/0.1.1-alpha.0-live-intake          # -d, not -D: refuses if unmerged
git push origin --delete roadmap/0.1.1-alpha.0-live-intake
```
