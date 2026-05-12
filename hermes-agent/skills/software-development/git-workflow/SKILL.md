---
name: git-workflow
description: "Use when doing standard Git repository work: clone, branch, commit, pull, push, remotes, tags, and common troubleshooting across any Git host."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [git, workflow, branches, remotes, commits, tags]
    related_skills: [writing-plans, requesting-code-review, systematic-debugging]
---

# Standard Git Workflow

## Overview

This is a host-agnostic Git skill for normal day-to-day repository work. It is not tied to GitHub, Gitee, GitLab, or any specific forge.

Use it for the common lifecycle:

- clone a repo
- inspect status and remotes
- create or switch branches
- add / commit changes
- pull and push safely
- manage remotes and tags
- troubleshoot common Git errors

Prefer plain `git` commands first. They work in public internet, LAN, and self-hosted environments.

## When to Use

Use this skill when:

- the user asks for standard Git operations
- the remote host does not matter
- you need generic Git commands rather than platform APIs
- you are working in a normal repo with branches, commits, and remotes

Do not use this skill for:

- GitHub issues / PR comments / Actions APIs
- forge-specific admin tasks
- code review workflows that depend on a web platform

## 1. Quick Discovery

Run these first:

```bash
git --version
git config --global --get user.name || true
git config --global --get user.email || true
```

If already inside a repo:

```bash
git rev-parse --is-inside-work-tree
git status --short --branch
git remote -v
git branch -vv
```

Recent history:

```bash
git log --oneline --decorate -10
```

## 2. Clone

Basic clone:

```bash
git clone <repo-url>
```

Clone into a target dir:

```bash
git clone <repo-url> <local-dir>
```

Shallow clone:

```bash
git clone --depth 1 <repo-url>
```

Clone a specific branch:

```bash
git clone --branch <branch> <repo-url>
```

## 3. Identity Setup

Set once if missing:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Verify:

```bash
git config --global --get user.name
git config --global --get user.email
```

## 4. Everyday Local Workflow

Check current state:

```bash
git status --short --branch
```

See changes:

```bash
git diff
git diff --cached
```

Stage files:

```bash
git add <file>
git add -A
```

Commit:

```bash
git commit -m "your message"
```

Amend the last commit:

```bash
git commit --amend
# or without editing the message
git commit --amend --no-edit
```

## 5. Branch Workflow

Create and switch to a branch:

```bash
git checkout -b <branch>
```

Or with newer syntax:

```bash
git switch -c <branch>
```

Switch branches:

```bash
git checkout <branch>
# or
git switch <branch>
```

List branches:

```bash
git branch
git branch -a
```

Track a remote branch locally:

```bash
git fetch origin
git checkout -b <branch> --track origin/<branch>
```

Delete a fully merged local branch:

```bash
git branch -d <branch>
```

Force delete a local branch:

```bash
git branch -D <branch>
```

## 6. Pull / Fetch / Push

Safe update flow:

```bash
git fetch --all --prune
git status --short --branch
git pull --ff-only
```

Push current branch first time:

```bash
git push -u origin <branch>
```

Push current branch after upstream exists:

```bash
git push
```

Delete a remote branch:

```bash
git push origin --delete <branch>
```

## 7. Remotes

Show remotes:

```bash
git remote -v
```

Add a remote:

```bash
git remote add origin <repo-url>
```

Change a remote URL:

```bash
git remote set-url origin <repo-url>
```

Rename a remote:

```bash
git remote rename origin upstream
```

Remove a remote:

```bash
git remote remove <name>
```

Fetch from all remotes:

```bash
git fetch --all --prune
```

## 8. Tags

List tags:

```bash
git tag
```

Create an annotated tag:

```bash
git tag -a v1.0.0 -m "v1.0.0"
```

Push one tag:

```bash
git push origin v1.0.0
```

Push all tags:

```bash
git push origin --tags
```

Delete a local tag:

```bash
git tag -d v1.0.0
```

Delete a remote tag:

```bash
git push origin :refs/tags/v1.0.0
```

## 9. Useful Inspection Commands

See one-line history graph:

```bash
git log --oneline --decorate --graph --all -20
```

See changed files in last commit:

```bash
git show --stat --oneline HEAD
```

See who changed a line:

```bash
git blame <file>
```

See unstaged file names only:

```bash
git diff --name-only
```

See staged file names only:

```bash
git diff --cached --name-only
```

## 10. Common Recovery Commands

Unstage a file:

```bash
git restore --staged <file>
```

Discard unstaged changes in a file:

```bash
git restore <file>
```

Restore all unstaged tracked changes:

```bash
git restore .
```

Stash current work:

```bash
git stash push -u -m "wip"
```

List stashes:

```bash
git stash list
```

Apply latest stash:

```bash
git stash pop
```

## Common Pitfalls

1. Running `git pull` before checking local changes and causing avoidable conflicts.
2. Committing on `main` when the work should be on a feature branch.
3. Forgetting `-u` on the first push, so the branch has no upstream.
4. Using destructive commands like `reset --hard` without confirming scope.
5. Assuming the default branch is always `main`; many repos still use `master`.
6. Confusing unstaged changes, staged changes, and untracked files.

## Troubleshooting

### `fatal: not a git repository`

Check location:

```bash
pwd
git rev-parse --is-inside-work-tree
```

### `src refspec <branch> does not match any`

Usually no commit yet or wrong branch name:

```bash
git branch
git status --short --branch
```

If needed:

```bash
git add -A
git commit -m "Initial commit"
```

### `Updates were rejected because the remote contains work that you do not have locally`

Inspect divergence first:

```bash
git fetch origin
git log --oneline --decorate --graph --all -20
```

### Merge conflicts

Inspect status:

```bash
git status
```

Then resolve files manually, stage them, and finish with:

```bash
git add <resolved-files>
```

### `Permission denied (publickey)` or auth failures

Check remote and auth path:

```bash
git remote -v
```

Then verify whether the repo uses SSH or HTTPS and fix credentials accordingly.

## Verification Checklist

- [ ] `git status --short --branch` matches the expected branch and cleanliness
- [ ] remotes shown by `git remote -v` are correct
- [ ] local changes are reviewed before commit or pull
- [ ] branch upstream is configured when push is required
- [ ] tags or remote deletions are only done intentionally
