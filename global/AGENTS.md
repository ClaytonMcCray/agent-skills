# Global Agent Rules

## Workflow: Clean Root Context & Independent Review

- **One root agent, clean context**: Keep high-level orchestration in the root thread and delegate token-heavy reading and writing to swarm workers or read-only subagents. Read only final reports and verified summaries at the root.
- **Small tasks**: Handle small, localized changes (a few edits, no research phase) directly in the root thread.
- **Substantial tasks**: For tasks requiring a research phase, multi-file edits, or more than ~15 minutes of agent work, use the `dev-swarm` skill (or `dev-swarm-zircon` when porting Zircon C++ to Rust). Follow its fixed roles, explicit `swarm` model pins (never leave `model` unset, and do not spawn `narrator` or `monitor` workers unless explicitly requested), and failure escalation rules. Use the `review` skill for standalone code reviews.
- **Independent diagnosis when stuck**: At any task size, if you are stuck or want an independent second opinion, spawn a fresh `gemini-4-carbon-high` or `claude-opus-5-5-high` worker given only the problem statement, then compare its diagnosis with the root's own view.
- **Independent review**: Non-trivial diffs must be reviewed before they are reported done by an agent that did not write them, in a fresh context, on a stronger or different model tier where one exists (`gemini-4-neon-xhigh` writes, `gemini-4-carbon-high` reviews). Whenever agents disagree, resolve the disagreement first by reading the cited lines directly.
- **One writer at a time per workspace**: Any worker or subagent with edit access is a writer. Parallelize concurrent edits across tasks using separate `git` worktrees (`manage_worktree.py`) or `workspace: "share"` / `"branch"`—never run concurrent writers in the same working copy.

## Commit and PR Attribution

- Never add session trailers, `conversation://` links, session links, or `Co-Authored-By` lines to commit messages or PR/CL descriptions. Those links expose private chat transcripts and reviewers do not need them.
