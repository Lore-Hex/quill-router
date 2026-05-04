# Codex Revenue Loop

## Rules

- Do not write long-form marketing copy from scratch.
- Interview the founder in audio.
- Transcribe, lightly clean, chop, tag, and reorder the founder's words.
- Outreach messages must be short, approved, quote-based, and linked to context.
- Google Sheets is the CRM source of truth.
- Do not send outreach without human approval.
- Do not post on X or LinkedIn without human approval at action time.
- Do not automate migration-credit grants in v1.

## Live CRM

- Google Sheet: https://docs.google.com/spreadsheets/d/1UVa4RcM8DIlJynxYIIAB6paDcFbUpoBJcQt2Zfl3KTo/edit
- `Sheet1`: lead CRM with the columns from `docs/revenue-loop-google-sheet.csv`.
- `Sheet2`: quote bank seeded from `docs/outreach-quote-bank.csv`.
- Social queue rows use `docs/social-approval-queue.csv`.

## Live Channels

- Firefox is logged into X and LinkedIn for manual, approval-only posting and outreach.
- Treat all X and LinkedIn actions as representational communication.
- Never like, comment, repost, connect, DM, publish, or submit forms without explicit approval for the exact text/action.

## Daily Run

1. Browser-check `trustedrouter.com`, `/compare/openrouter`, `/docs/migrate-from-openrouter`, `/security`, and `/models`.
2. Add or update leads in the Google Sheet.
3. Score leads.
4. Attach one relevant quote from `docs/outreach-quote-bank.csv`.
5. Attach the matching `context_link`.
6. Draft X/LinkedIn posts or replies only as approval-packet items, using founder quotes and context links.
7. Wait for approval.
8. Send only approved messages and record `sent_at`.

## Quote Candidates

Use `docs/outreach-quote-bank.csv`.

## Approval Packet

Use `docs/outreach-approval-packet.md`.

## CRM Columns

Use `docs/revenue-loop-google-sheet.csv` as the Google Sheet header row.
