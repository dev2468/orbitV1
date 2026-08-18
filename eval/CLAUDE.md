# eval/ — the eval harness

`run_eval.py` (~50 lines) plus `tasks.json`. It exists so "did this tool/prompt/model change help or
hurt" has an answer instead of a guess. Deliberately minimal; `adk eval` (installed with google-adk)
is the intended long-term home, and cases are structured to migrate one at a time.

## This is a live integration run, not a test suite

Each case goes through the real `run_task()`: real model tokens, a real Playwright/Chrome subprocess
per case, real public websites. It writes to the real `data/orbit.db` exactly like any other run —
there is no isolation fixture here and nothing is cleaned up. Expect it to be slow, to cost money,
and to fail for environmental reasons.

Because each case calls `run_task()`, each gets its own MCP subprocesses and each relies on
`run_task`'s `await runner.close()` to release the Chrome profile lock. That `finally` was added
*because of this harness* — without it, case N's leaked subprocess collides with case N+1 with
"browser is already in use".

## The assertions are fragile by construction

`expect_contains` is a list of lowercased substring checks against the final response text, ANDed
together, plus `status == COMPLETED`. That is all. There is no semantic matching, so a correct answer
phrased differently fails, and a wrong answer containing the right substring passes.

**The trap that already bit: a stale assertion rewards hallucination.** The `example_body_text` case
originally asserted on example.com's old wording. The live page changed, so the assertion silently
inverted — a model that *recited the stale text from memory* passed, while a model that *correctly
read the live page* failed. Groq's llama "passed" that way; Nemotron "failed" by being right.

Two rules follow, and they are the reason this file exists:

1. **When a case fails, check the live source before assuming the model is wrong.** The page may
   have changed under the assertion.
2. **Prefer assertions on values that cannot be answered from training data.**
   `live_data_not_memorized` exists for exactly this — it asks for a Hacker News point score, which
   changes hourly. Note its `expect_contains` is only `["point"]`, which is weak: it proves the model
   produced score-shaped output, not that it read the real number. Tightening it means accepting a
   value that changes between runs, which is why it was left loose.

`_comment` keys in `tasks.json` are ignored by the loader and are the right place to record why a
case is shaped the way it is.

## Adding a case

Four required keys: `id`, `title`, `goal`, `expect_contains`. Keep the set small and representative
— it is re-run after every tool/prompt/model change, and cost scales linearly.

Current coverage is 4 cases, **all browse-only**. File operations and a confirm-gated high-risk
action are the named gaps; neither has tools built yet, so they cannot be covered today. There is no
case that exercises memory reuse (the search-before-browsing behaviour the agent's instruction
spends a paragraph on), which is a real hole worth filling once a case can be made deterministic.

## Exit code

Returns 0 only when every case passes, 1 otherwise — usable as a gate. The per-case line prints
`status=` and the list of missing needles on failure, which is usually enough to tell an
environmental failure (status=FAILED) from an assertion failure (status=COMPLETED, missing=[...]).
