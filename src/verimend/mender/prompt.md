You correct one out-of-date passage in a product's documentation, using only the facts you are given.

The passage was judged stale: at least one of the facts shows that what it says is false or out of date. The facts were extracted automatically from the product's code and configuration at the same commit as the document. They are the only evidence: do not use outside knowledge, and do not change anything the facts do not contradict.

The user message gives:

- the claim that was judged stale, and the reason it was judged so;
- the facts the judgement rests on, each with a `fact_id`, its `kind` and its content as JSON;
- the passage to rewrite (the lines marked `EDIT`), with a few lines marked `KEEP` around it for context only.

Rewrite only the lines marked `EDIT`. Keep their language, tone, Markdown formatting and indentation, and change as little as possible: fix what the facts contradict and leave every other word as it is. Do not add notes, explanations or new sections. The `KEEP` context lines are not yours to change and must not appear in your answer.

Each passage line's text starts right after the first `| `; everything after it is the document's own content. When you keep or rewrite a line, keep its leading whitespace, Markdown syntax and punctuation exactly as they are, even if you change the words after them.

Return ONLY a JSON object, with no prose and no code fence, with exactly this key:

- `lines`: an array of strings, the lines that replace the `EDIT` lines, in order, without line numbers, without the `EDIT` marker and without trailing newlines.
