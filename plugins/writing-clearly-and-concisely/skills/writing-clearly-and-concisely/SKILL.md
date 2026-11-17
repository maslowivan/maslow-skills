---
name: writing-clearly-and-concisely
description: Use when writing prose humans will read—documentation, commit messages, error messages, explanations, reports, or UI text. Write in simple, natural language with a clear, logical flow (просто, понятно и логично). Preserves English references and adds separate Russian examples.
---

# Writing Clearly and Concisely

## Overview

Write so the reader understands the point on the first reading. Use familiar
words, explain how ideas connect, and sound like a person talking to the reader.
Clarity and logical flow take priority over making the text shorter.

## Simple, Natural, Logical Speech

- Lead with the answer or main point. Then give the reason, needed details, and
  the next step when there is one. Adapt the order to the reader's question.
- Use ordinary words and direct verbs. Keep a technical term when it adds
  precision; explain it when the reader may not know it.
- Make the connection between sentences clear. Explain what caused a result,
  what a pronoun refers to, and how a conclusion follows from the facts.
  Do not invent a causal link just to make the story sound coherent.
- Keep enough explanation for the reader to follow. Do not turn connected prose
  into clipped fragments or a list of facts whose relationship is left unstated.
- Match the reader's tone. Everyday words such as «нормально», «сейчас», «можно»
  and «поможет» are welcome when they express the meaning. Do not automatically
  replace them with formal wording or force slang into a formal document.
- Read the result as something you would say aloud to this reader. Rewrite
  phrases that sound stiff, pompous, translated, or need a second reading.

For Russian, the goal is: «Пиши нормально: простыми словами, понятно и логично.
Объясняй, как одна мысль связана с другой. Сохраняй смысл и естественный тон».

## When to Use This Skill

Use this skill whenever you write prose for humans:

- Documentation, README files, technical explanations
- Commit messages, pull request descriptions
- Error messages, UI copy, help text, comments
- Reports, summaries, or any explanation
- Editing to improve clarity

**If you're writing sentences for a human to read, use this skill.**

## Language and Meaning

Use the language, tone, terminology, and format requested by the user. Preserve
facts, uncertainty, exceptions, and technical identifiers when editing. Examples
are fictional: do not import their numbers, causes, or guarantees into real text.

- **English:** use the original references in `elements-of-style/` below. The
  original English examples also remain in [README.md](README.md).
- **Russian:** read [russian-examples.md](russian-examples.md) for separate
  before/after examples, punctuation, word order, and common wording problems.
  Composition principles transfer; English possessives, comma rules, title case,
  and usage distinctions do not define Russian grammar.
- **Mixed text:** apply each language's conventions to its prose; keep code,
  commands, product names, and quotations intact unless asked to edit them.

Treat preferences such as active voice or positive phrasing as tools, not bans.
Keep passive voice when the actor is unknown or irrelevant, and keep negation
when it carries the meaning. Do not replace uncertainty with invented certainty.

## Limited Context Strategy

When context is tight:

1. Write your draft using judgment
2. Load only the relevant section file (or `russian-examples.md` for Russian)
3. Copyedit the draft and return the revision

Loading a single section (~1,000-4,500 tokens) instead of everything saves significant context.

## Elements of Style

William Strunk Jr.'s *The Elements of Style* (1918) teaches you to write clearly and cut ruthlessly.

### Rules

**Elementary Rules of Usage (Grammar/Punctuation)**:

1. Form possessive singular by adding 's
2. Use comma after each term in series except last
3. Enclose parenthetic expressions between commas
4. Comma before conjunction introducing co-ordinate clause
5. Don't join independent clauses by comma
6. Don't break sentences in two
7. Participial phrase at beginning refers to grammatical subject

**Elementary Principles of Composition**:

8. One paragraph per topic
9. Begin paragraph with topic sentence
10. **Use active voice**
11. **Put statements in positive form**
12. **Use definite, specific, concrete language**
13. **Omit needless words**
14. Avoid succession of loose sentences
15. Express co-ordinate ideas in similar form
16. **Keep related words together**
17. Keep to one tense in summaries
18. **Place emphatic words at end of sentence**

### Reference Files

The rules above are summarized from Strunk's original text. For complete explanations with examples:

| Section | File | ~Tokens |
|---------|------|---------|
| Grammar, punctuation, comma rules | [elements-of-style/02-elementary-rules-of-usage.md](elements-of-style/02-elementary-rules-of-usage.md) | 2,500 |
| Paragraph structure, active voice, concision | [elements-of-style/03-elementary-principles-of-composition.md](elements-of-style/03-elementary-principles-of-composition.md) | 4,500 |
| Headings, quotations, formatting | [elements-of-style/04-a-few-matters-of-form.md](elements-of-style/04-a-few-matters-of-form.md) | 1,000 |
| Word choice, common errors | [elements-of-style/05-words-and-expressions-commonly-misused.md](elements-of-style/05-words-and-expressions-commonly-misused.md) | 4,000 |

**Most English tasks need only `03-elementary-principles-of-composition.md`** — it covers active voice, positive form, concrete language, and omitting needless words.

## AI Writing Patterns to Avoid

LLMs regress to statistical means, producing generic, puffy prose. Avoid:

- **Puffery:** pivotal, crucial, vital, testament, enduring legacy
- **Empty "-ing" phrases:** ensuring reliability, showcasing features, highlighting capabilities
- **Promotional adjectives:** groundbreaking, seamless, robust, cutting-edge
- **Overused AI vocabulary:** delve, leverage, multifaceted, foster, realm, tapestry
- **Formatting overuse:** excessive bullets, emoji decorations, bold on every other word

Be specific, not grandiose. Say what it actually does.

For a descriptive, Wikipedia-specific collection of patterns and sources, see
[signs-of-ai-writing.md](signs-of-ai-writing.md). Use it as an editing reference,
not as proof of AI authorship or a universal banned-word list.

## Apply the Guidance

Load the reference relevant to the language and problem. For English composition,
start with `elements-of-style/03-elementary-principles-of-composition.md`. For
Russian, start with [russian-examples.md](russian-examples.md). Return prose that
preserves the user's meaning and follows the requested format.


## Sources and Local Additions

Adapted from [softaworks/agent-toolkit](https://github.com/softaworks/agent-toolkit/tree/main/skills/writing-clearly-and-concisely).
Upstream credits [joshuadavidthomas/agent-skills](https://github.com/joshuadavidthomas/agent-skills)
and [obra/the-elements-of-style](https://github.com/obra/the-elements-of-style),
based on William Strunk Jr.'s *The Elements of Style* (1918).
See [SOURCES.md](SOURCES.md) for the pinned source revision, licenses, and changes.
The Russian examples are original maslow-skills additions, not quotations from Strunk.
