# Archive

These documents describe the **pre-rework SaaS design** and are kept here
for historical reference only. They were reversed by design decision **D4**
in [`../REWORK_BRIEF.md`](../REWORK_BRIEF.md).

| File | What it described | Why it's archived |
|---|---|---|
| `telegram-bot-plan.md` | Customer-facing Telegram bot with invite codes, subscription expiry, paid-tier admin features, broadcast | D4 reversal — bot is now a private tool for 3 known users; SaaS layer ripped in commit `c366c55` |
| `telegram-implementation-steps.md` | Step-by-step plan for building the above | Same — superseded by the rework brief |

**Do not implement anything from these files** — they describe a project shape
that no longer exists. Refer to `docs/REWORK_BRIEF.md` for the current spec.
