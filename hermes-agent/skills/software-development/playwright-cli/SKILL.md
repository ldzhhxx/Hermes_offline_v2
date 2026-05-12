---
name: playwright-cli
description: Use Playwright CLI for browser automation tasks from Hermes. Covers installation detection, workspace setup, core commands, snapshots, storage/network tooling, and common environment quirks.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [playwright, browser-automation, e2e, testing, cli]
---

# Playwright CLI

Use this skill when the user wants browser automation through Playwright CLI rather than Hermes browser tools or Playwright test code.

## When to use

- User asks to use `playwright-cli`
- User wants a token-efficient browser automation CLI workflow
- User refers to the repo-provided `.claude` Playwright skill
- User wants manual browser actions like open/click/type/snapshot with Playwright

## Local environment facts for this machine

- The Playwright repo is cloned at `/home/liu/hermes-projects/playwright`
- The official CLI is installed at `/root/.hermes/node/bin/playwright-cli`
- In non-login shells, that directory may not be on `PATH`, so prefer the absolute path if `playwright-cli` is not found
- Repo-local upstream skills are installed at `/home/liu/hermes-projects/playwright/.claude/skills/playwright-cli`

## Quick verification

Run these checks first:

```bash
/root/.hermes/node/bin/playwright-cli --help
cd /home/liu/hermes-projects/playwright && test -d .claude/skills/playwright-cli && echo SKILL_OK
```

## Install / reinstall

Clone repo and install dependencies:

```bash
git clone https://github.com/microsoft/playwright.git /home/liu/hermes-projects/playwright
cd /home/liu/hermes-projects/playwright
npm install
```

Install the official CLI:

```bash
npm install -g @playwright/cli@latest
```

If the command is not on `PATH`, use the installed binary directly:

```bash
/root/.hermes/node/bin/playwright-cli --help
```

Install the repo-provided local agent skill bundle into the repo workspace:

```bash
cd /home/liu/hermes-projects/playwright
/root/.hermes/node/bin/playwright-cli install --skills
```

Expected results:
- `.playwright/` workspace initialized in the repo
- `.claude/skills/playwright-cli/` created in the repo
- default browser auto-detected or installed

## Core workflow

Open a session and navigate:

```bash
/root/.hermes/node/bin/playwright-cli open https://example.com --browser=chrome
```

Interact:

```bash
/root/.hermes/node/bin/playwright-cli click e15
/root/.hermes/node/bin/playwright-cli type "hello"
/root/.hermes/node/bin/playwright-cli press Enter
/root/.hermes/node/bin/playwright-cli fill e5 "user@example.com" --submit
```

Inspect page state:

```bash
/root/.hermes/node/bin/playwright-cli snapshot
/root/.hermes/node/bin/playwright-cli --raw snapshot
/root/.hermes/node/bin/playwright-cli eval "document.title"
/root/.hermes/node/bin/playwright-cli console
/root/.hermes/node/bin/playwright-cli network
```

Save artifacts:

```bash
/root/.hermes/node/bin/playwright-cli screenshot
/root/.hermes/node/bin/playwright-cli screenshot --filename=page.png
/root/.hermes/node/bin/playwright-cli pdf --filename=page.pdf
```

Close:

```bash
/root/.hermes/node/bin/playwright-cli close
```

## Common commands

Navigation:

```bash
/root/.hermes/node/bin/playwright-cli go-back
/root/.hermes/node/bin/playwright-cli go-forward
/root/.hermes/node/bin/playwright-cli reload
```

Tabs:

```bash
/root/.hermes/node/bin/playwright-cli tab-list
/root/.hermes/node/bin/playwright-cli tab-new https://example.com
/root/.hermes/node/bin/playwright-cli tab-select 0
/root/.hermes/node/bin/playwright-cli tab-close
```

Storage:

```bash
/root/.hermes/node/bin/playwright-cli state-save auth.json
/root/.hermes/node/bin/playwright-cli state-load auth.json
/root/.hermes/node/bin/playwright-cli cookie-list
/root/.hermes/node/bin/playwright-cli localstorage-list
/root/.hermes/node/bin/playwright-cli sessionstorage-list
```

Network mocking:

```bash
/root/.hermes/node/bin/playwright-cli route "**/*.jpg" --status=404
/root/.hermes/node/bin/playwright-cli route-list
/root/.hermes/node/bin/playwright-cli unroute
```

Tracing and debugging:

```bash
/root/.hermes/node/bin/playwright-cli tracing-start
/root/.hermes/node/bin/playwright-cli tracing-stop
/root/.hermes/node/bin/playwright-cli show --annotate
/root/.hermes/node/bin/playwright-cli generate-locator e5 --raw
```

## Good verification tasks

Minimal smoke test:

```bash
cd /home/liu/hermes-projects/playwright
CLI=/root/.hermes/node/bin/playwright-cli
$CLI open https://demo.playwright.dev/todomvc/ --browser=chrome
$CLI type "Buy milk"
$CLI press Enter
$CLI --raw snapshot | grep "Buy milk"
$CLI close
```

If the final grep finds the text, the real browser automation loop is working.

## Pitfalls

1. `playwright-cli: command not found`
- Use `/root/.hermes/node/bin/playwright-cli`
- Check `npm root -g` and `find /root/.hermes/node -name playwright-cli`

2. Browser command blocked by approval layer
- This is not a Playwright failure; the environment denied running a browser-opening command
- Ask the user for permission or have them run the smoke test locally

3. Running as root can break Chromium launch with sandbox errors
- Symptom: `Running as root without --no-sandbox is not supported` or `Chromium sandboxing failed`
- Playwright CLI may fail to open/pdf in this environment unless Chromium is launched without sandbox
- Reliable fallback for local HTML -> PDF export:

```bash
/opt/google/chrome/chrome --headless --no-sandbox --disable-gpu \
  --print-to-pdf=/absolute/output.pdf file:///absolute/input.html
```

- Use this fallback especially when exporting self-contained local presentations to PDF

4. Cloudflare / anti-bot verification blocks real browsing
- Some targets such as `grok.com` can present Cloudflare human verification
- Clicking the checkbox may still fail to pass automatically, and the session can even fall through to `about:blank` or return to the verification page
- This is a site-side anti-automation gate, not evidence that Playwright CLI or the Hermes skill is broken
- Best fallback: have the user complete verification manually in a normal browser, provide an already-authenticated/verified entry point, or switch to another news/data source

4. Missing browser installation
- Re-run:

```bash
/root/.hermes/node/bin/playwright-cli install --skills
```

- Or install a browser explicitly if prompted by Playwright CLI

4. Want Hermes-native usage vs repo-local `.claude` skill
- The repo’s `install --skills` installs a local `.claude` skill bundle for agent ecosystems like Claude Code
- This Hermes skill is the native wrapper/translation for Hermes usage

## Related files

- Repo root: `/home/liu/hermes-projects/playwright`
- Repo upstream skill: `/home/liu/hermes-projects/playwright/.claude/skills/playwright-cli/SKILL.md`
- Upstream dev skills:
  - `/home/liu/hermes-projects/playwright/.claude/skills/playwright-dev/SKILL.md`
  - `/home/liu/hermes-projects/playwright/.claude/skills/playwright-devops/SKILL.md`
