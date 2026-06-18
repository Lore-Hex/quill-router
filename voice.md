# TrustedRouter blog voice

The style guide for posts in `src/trusted_router/blog.py`. Read it before writing
or editing a post. The goal: writing that earns trust by being concrete and
honest, not by sounding impressive.

## Voice

- **"We," the TrustedRouter research team.** Never first-person "I." Never a brand
  voice talking about itself in the third person.
- **Evidence first, claim second.** Lead with the number, the test, the thing we
  ran. A sentence that could have been written without doing the work doesn't go in.
- **Plain and declarative.** Short sentences. Say the thing. No hedging clouds, no
  throat-clearing intros.
- **Anti-hype.** No superlatives we can't back with a number. State limitations in
  the open — a disclosed weakness is more convincing than a hidden one.
- **Reproducibility is the spine.** Point at the open code, the data, the way a
  reader can rerun it and get our number. "Don't trust us, check it."
- **Flowing paragraphs, no section headers.** A post is an argument read top to
  bottom, not a doc with `##` signposts.
- **Link generously and accurately.** Every model, benchmark, tool, and sibling
  post that's named should usually link to its real source. Verify URLs resolve.

## Hard bans

- **Never the word "quietly."** It's an LLM tell. Also avoid its cousins as filler:
  "silently"/"seamlessly"/"effortlessly" when they're doing no work.
- **No "not X but Y" cleverness** as a rhetorical reflex ("it isn't a quiz, it's a
  research assignment"). Just say what it is.
- **No LLM filler vocabulary:** delve, tapestry, testament, moreover, furthermore,
  "it's worth noting," "in today's landscape," "ever-evolving," "boasts,"
  "elevate," "navigate the complexities," "at the end of the day."
- **No em-dash overuse.** One or two per paragraph is plenty.
- **No OpenRouter links.** Mention them by name where the work is theirs; do not
  hyperlink to them.

## Mechanics

- `body_html` is HTML inside a Python triple-quoted string: `<p>` paragraphs,
  `<a href="...">`, `<strong>`, `<em>`, `<span class="mono">` for ids, and
  `<table class="data-table">` for data. No `<h2>`/`<h3>`.
- Numbers are exact and match the published artifacts. If a number has a caveat
  (an excluded task, a single judge pass), state it.
