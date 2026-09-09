# Phase Commit Discipline Rule

1. **Commit at Phase Boundary**: Every implementation phase defined in `TASKS.md` must be completed, verified, and committed to git at the end of that phase. Do not begin work on subsequent phases with uncommitted changes from prior phases.
2. **Verification Gate**: Before committing a phase, the full test suite (`pytest`) must pass cleanly with zero failures or errors.
3. **Artifact Standards**: Each phase must maintain its corresponding test plan (`PHASE<N>_TEST_PLAN.md`) and completion report (`PHASE<N>_COMPLETION_REPORT.md`), documenting what was delivered and verified.
4. **Clean Working Tree**: Ensure `git status` shows no untracked files or unstaged modifications before advancing to the next phase.
