---
name: duplicate-detection-guidelines
description: "Shared functional-judgment knowledge base for both agents: step counting from actual content, contact/channel type hard boundaries, the specific patterns that distinguish a true duplicate from a superficially-similar but intentionally distinct test case, recognizing real test source vs. non-test repo content, and professional naming conventions. Both agents read this once before grouping and duplicate detection, instead of each carrying its own copy of these rules. Not a runnable script - guidance only."
---

# Duplicate Detection Guidelines

**This knowledge base exists only to be read by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as part of their documented workflow.** It has no runnable action and no side effects on its own, but the judgment rules below are only meaningful applied within those two agents' full workflow (grouping, then duplicate detection, then the cross-agent checks) - not as a standalone reference for ad hoc duplicate-spotting outside that process.

Read this in full before Step 3 (grouping) and Step 4 (duplicate detection) of either agent's workflow.

## 1. Step counting is content-based, never the raw field/row count

A test's real step count comes from reading what the content actually says, not from counting entries in a structured field:

- If a single `steps` entry (or a single automation code block) contains multiple described actions - numbered, bulleted, or otherwise clearly separate instructions - count each as its own step. A field that looks like "1 row" can genuinely be 3-4 real steps once you read it.
- Preconditions and assumptions (e.g. "Prerequisite: FT flag is ON", "Assumption: agent has activity in last 24h") are context, never steps - exclude them from the count entirely.
- If the structured field is empty, look for step-like content in the description/comments (a "Steps:" header, a numbered list) before concluding there are no steps.
- **When a test method's body is mostly a call into a shared class in another file** (a common pattern: `_supervisorLogic.Supervisor_ProvideTheAbilityTo...()`, a `Logic`/`Driver`/`Workflow` helper), the real steps live inside that call, not in the 1-3 lines of the test method itself - counting "this test has 1 line, so 1 step" is exactly the mistake this rule exists to prevent. Follow the call into its real implementation (even though it's a different file) and read *that* content to judge steps/flow, stopping once you reach code that does something concrete (a UI action, an API call, an assertion) rather than delegating further - normally 1-2 levels deep, never an unbounded chase. Most tests being analyzed together call into the *same small set* of shared helper files - fetch each one once and reuse it for every test that calls into it, exactly like a test's own content is already cached and never re-fetched for the same test.

## 2. Contact/channel type is a hard boundary - never crossed, regardless of step diff

Some categories represent intentionally distinct test cases by design, not incidental variations. These are **never merged with each other**, even at stepDiff 0 (identical steps):

- **IBPhone, OBPhone, IBEmail, OBEmail, Chat, Workitem** - each is its own type. A set can only ever contain tests of the *same* type.
- A same-type pair is a completely normal, eligible merge candidate: e.g. IBPhone-with-IBPhone differing by 2 steps is fine to combine. It is only *across* these types (IBPhone with OBPhone, Chat with Workitem, etc.) that merging is disallowed - no exceptions, even if the steps look nearly identical.
- Apply the same hard-boundary principle to any other org-specific type distinction that works the same way: ACD vs DFO, coach vs join vs monitor, agent vs supervisor, insights vs live-monitoring, focused view vs overall view. If two tests differ primarily by *which mode/surface/channel/perspective* they exercise rather than by incidental step wording, treat that as a type boundary, not a mergeable difference. A strong signal that a distinction is this kind of hard boundary rather than an incidental detail: the codebase or test suite consistently maintains it as its own dedicated top-level grouping (e.g. a distinct folder per variant) rather than mixing variants together - that consistency is itself evidence the org treats it as deliberately separate coverage.
- Read the actual test title/summary/content for the type signal - a single functional group can legitimately contain multiple types side by side, and only individual test pairs need the type check.

## 3. Shared title prefix or similar step count is never sufficient evidence of duplication

Always compare the actual assertion/expected outcome between two tests before considering them for the same set - not the bracketed screen/component tag in their titles, and not how close their step counts happen to be. Two tests can share an identical prefix (same screen, same component, same feature area) and a near-identical step count while testing completely unrelated behavior. Grouping or flagging tests together on tag-and-count similarity alone produces false positives; every rule below is a specific case of checking the real content instead.

## 4. A binary-state (flag/toggle/mode) pair is a duplicate only when both tests assert the same single variable, inverted

When two tests exercise the same two-valued setting, check whether they observe the *same specific outcome variable* in each of its two states (one assertion point, two expected values) - that is a valid merge candidate, since it's really one check parameterized by the setting. If each test instead asserts a *different* outcome variable that merely happens to sit behind the same setting, they are functionally independent checks and are not duplicates, regardless of matching step counts. Judge this by counting how many distinct things are being asserted, not by whether the setting name matches.

## 5. Same operation from two access paths is mergeable; the same operation performed in reverse is not

If a single operation is verified through two different navigation paths or starting points, and the only difference between the two tests is the step(s) needed to reach that starting point, treat it as one operation under test twice - a real duplicate. The path taken to reach an operation is incidental to what the operation itself does.

If instead the two tests exercise the *same category of operation but with its subject and object swapped* (X acting on Y vs. Y acting on X), that is a distinct scenario even when the step sequences look structurally parallel - a reversed subject/object relationship is never a duplicate of its mirror, because each direction can fail independently of the other.

## 6. The same expected outcome reached via two interchangeable mechanisms is mergeable

If two tests arrive at an identical, fully-matching expected outcome and differ only in *which of two equivalent user-facing controls* was used to get there, the choice of control is incidental - merge them. This only applies when the outcome assertions match exactly; if the outcomes differ even slightly, treat it as two separate mechanisms under test, not an incidental choice.

## 7. Repeating one operation across several pre-existing data subsets is usually mergeable - unless the subset choice changes the outcome

A single operation re-tested once per pre-existing data partition (e.g. the same action run against several different starting subsets of the same list) is typically redundant if the operation's mechanics and success assertion are identical regardless of which subset it started from - the partition only determines the input, not the behavior. Before merging, confirm the different partitions don't correspond to different code paths (see rule 8) - if they do, they are not interchangeable and must stay separate.

## 8. Distinct triggering conditions on a stateful feature are deliberate, non-overlapping coverage - never merge these

Be most skeptical of a low step-diff here. A feature that changes or maintains some ongoing state based on *which specific event occurs* (out of several possible events), *combined with* which of several settings/permissions is active, requires one test per condition→outcome pairing to have real coverage - each pairing can fail independently of the others even though the tests look structurally identical. A low step-diff between two tests that each name a different specific triggering condition, event, or permission value in their titles is a signal of deliberate, exhaustive branch coverage, not incidental duplication - do not merge across different named conditions even at stepDiff 0-1.

## 9. An automatically-triggered behavior is not a duplicate of its manually-triggered equivalent

Where a system-driven trigger (time-based, event-based, or otherwise not initiated by direct user action) and a user-initiated trigger both lead to a similar visible end state, they still exercise different invocation paths and must be tested and reported separately - never merge purely because the end state looks the same.

## 10. Different validation depth on the same subject is not duplication

A test whose assertion is a single shallow check (does the expected element/state appear or not) and a test whose assertion spans many data points or conditions for that same subject serve different verification purposes - a shallow presence/absence check and a thorough data-correctness check are not interchangeable, even when they're about the same underlying feature.

## 11. A narrow-scope compatibility/meta check may be subsumed by a broader one covering the same surface - verify the overlap before treating it as redundant

When one test's scope is a strict superset of another's (the broader test's execution path necessarily passes through everything the narrower test checks), the narrower one may be redundant. This is subsumption, not step-content similarity - only apply it after confirming the broader test's actual path covers the narrower test's exact surface, not merely because both tests share the same category label (e.g. both being labeled as compatibility/smoke/meta checks).

## 12. Grouping accuracy directly gates duplicate-detection quality

A true duplicate pair split across two different functional groups will never be compared against its real match, no matter how good the within-group judgment is. Treat Step 3 (grouping) as equally important to Step 4, not preliminary busywork.

## 12a. An oversized functional group must be split into name-similar batches, never sampled

A group with more tests than can be fully read and compared (over ~110) cannot be handed to Step 4 as-is - reading only some of it and calling the rest "checked" is exactly the sampling failure that has caused real duplicates to be missed at scale (a 716-test group produced a false "0 duplicates" result this way). The fix is not to read less; it's to split the group into batches of at most 100 by test-name similarity - tests that look like they cover the same or closely related scenarios go in the same batch, filled in order until each batch reaches 100, with the last batch taking whatever remains.

Duplicate detection then happens **within one batch only, never across batches split from the same original group** - and this is intentional, not a shortcut to apologize for. The name-similarity split *is* the judgment call that two tests aren't related enough to be worth comparing; if a test landed in batch 1 and another in batch 2, they were already judged dissimilar enough by name to not need a cross-check. Batches are named `<original group name> (batch 1)`, `(batch 2)`, etc., zero-padded to the width of the total batch count for that group (e.g. `(batch 01)` .. `(batch 12)`) so a plain alphabetical sort in the report keeps them in the right order instead of `1, 10, 11, 2, 3...`.

## 13. Separate non-test infrastructure from test source before judging an automation repo's folder structure

A repository can contain a large volume of content that is not test code at all - environment/region configuration, CI pipeline definitions, infrastructure-as-code, credential/user-provisioning tooling - sitting at the top level alongside the real test source, sometimes outnumbering the actual test files by a wide margin. Before judging whether top-level folders represent genuine functional groups (or fall back to content-based grouping), first identify which directory actually contains the test suites and confine that judgment to it. Never treat a configuration/environment/pipeline/tooling directory as a candidate functional group, and don't let its file count distort any read of how "big" or "well-organized" the test suite itself is.

Don't infer a folder's purpose from its name alone - a folder literally named "automation" is not guaranteed to contain the automated tests; it can just as easily be setup/provisioning tooling (e.g. scripts that create or activate test accounts) that happens to share that name. Confirm by what the files actually contain (test declarations vs. setup scripts/config), not by the folder label. The same caution applies to shared/support code directories (page objects, common utilities, fixtures, workflow/orchestration helpers) that sit alongside real test folders - these hold code the tests depend on, not test cases themselves, and should never be scanned or counted as a functional test group even though they're clearly test-related.

A line of code matching a test-declaration pattern (`test(...)`, `it(...)`, a `def test_...` function, an `@Test`-annotated method) is not automatically a real test case just because the syntax matches - confirm the body actually drives and asserts application behavior. A support script that merely happens to follow a test-naming convention (e.g. a setup/provisioning routine named `test_something` because it lives in a test-runner-compatible file) is not a functional test and must not be counted, grouped, or proposed as a merge candidate - judge by what the body does, not by whether its declaration syntax matched.

A folder or filename convention that marks tests as retired/deprecated/obsolete/legacy (e.g. a dedicated "deprecated" or "legacy" test folder) signals those tests are no longer active - exclude them from grouping and duplicate-candidate analysis entirely, the same way a Removed status excludes a manual test on the Jira side. Suggesting a merge for a test nobody runs anymore is not useful output.

## 14. A test framework's typical reputation doesn't determine what's actually under test

A framework commonly associated with one kind of testing (e.g. browser/UI automation) can be used purely to drive a different kind of system (e.g. API/backend requests with no browser involved), and the reverse is equally possible. Determine the actual system under test by reading the test body's own setup, imports, and assertions - not by assuming from the test runner's typical use case. This matters for functional grouping (e.g. grouping by API domain/endpoint/controller rather than by UI screen or component when the tests are API-driven) and for judging what a "step" or a meaningful difference even is in that context.

## 15. Prefer the cheapest available signal before reading full content, and only escalate when it's genuinely ambiguous

A test's name/title is available essentially for free once a source has been listed or scanned - full step/body content is not. Attempt functional grouping from names alone first; only read full content for the specific tests whose name doesn't give enough signal to place with confidence. This matters most at scale: when no structural shortcut (a genuinely functional folder layout, a reliable field-level filter) is available, the fallback becomes "read everything," and on a large source that is expensive enough to actively avoid wherever a cheaper signal already answers the question. This is a general escalation order to apply anywhere a judgment call is needed, not a one-off optimization for a single step.

## 16. A scanner reporting zero matches is a signal to investigate, never a license to substitute a cruder count

`scan-automation-repo`'s test-declaration patterns cover the common frameworks (Playwright/Jest, Cypress, pytest, Cucumber, JUnit/TestNG, NUnit/MSTest/xUnit) but cannot cover every custom in-house test framework that exists. When it reports zero (or a suspiciously low) match count against a repo that otherwise looks like it should contain many tests, that is genuinely ambiguous - it could mean the repo really has no tests, or it could mean this repo's real test-declaration convention (a custom attribute, a naming-convention-only pattern, a config-driven registration, reflection over a base class) isn't one of the known patterns yet. Resolve the ambiguity by reading a small representative sample of files directly and learning the real convention from their actual content - never by falling back to a cruder proxy like "count every method" or "count every file in the folder" as a stand-in for a real test count. A support/helper method (page-object navigation, click/wait utilities, shared setup) sits in the exact same files as real tests and is trivially miscounted as one by any proxy that isn't actually checking for the repo's real test marker - this is the same file/method-vs-real-test distinction rule 13 makes for folder structure, applied here to counting.

This also means: never hardcode a fix narrowly scoped to one specific repo or company's codebase. Recognizing a well-known language/framework convention (an NUnit `[Test]` attribute, a pytest `def test_` prefix) is legitimate general framework support, available to any repo using that framework - but a repo-specific keyword, project name, or file path baked into a pattern is not, and must never be added even under real-run pressure to "just make this one repo work."

## 17. Execution-tier tags can live inside a test's own title, not just in a folder name

The tier-vs-functional distinction rule 13 makes for folder names (`SanityTest/`, `SmokeTest/`, `RegressionTest/`) applies equally when the same concept is tagged inline instead - e.g. a test titled `"CX-1234 : @sanity @sm - Verify login succeeds"` carries the same "which suite(s) this runs in" metadata as a tier folder would, just embedded as tokens in the title string rather than as a directory. Strip these tags (and the Jira ID) before judging two tests' actual functional content or step similarity - two tests tagged `@sanity` and `@regression` respectively can still be genuine duplicates, and two tests both tagged `@sanity` are not automatically related just because they share that tag. Never let an execution-tier tag leak into a `suggestedName` (rule 18) any more than a Jira key would - it is scheduling/execution metadata, not part of the test's real identity.

## 18. Naming and rationale must be professional and grounded in the set's real content

- `suggestedName` is required on every set, no exceptions, and must read like a real test title - never a Jira key, ticket number, an execution-tier tag (`@sanity`, `@smoke`, `@regression`, etc.), or the words "manual"/"automated" anywhere in it, and never a "consolidate X into Y" narrative.
- For a manual-only or automation-only set: read the actual titles of the tests in the set and write a name that generalizes what they verify together.
- For a **combined** (manual + automation) set: the outcome is "extend the existing automation script to also cover the manual steps" - so the suggested name is really the automation test's *new* name. Read that automation test's actual name and write the suggestion in the same technical style/pattern it already uses (lowercase natural-language title, hyphenated, camelCase - whatever that codebase uses), extended to describe the added step. Do not switch it to a differently-styled "professional English" sentence that wouldn't fit alongside the rest of that repo's test names.
- `criteria` ("Difference") and `mergeRationale` ("How to combine") must name the *specific* real difference (the actual assertion, channel, or extra step involved) - never restate the stepDiff number as if that were an explanation on its own.
- For a combined set specifically, `mergeRationale` must explain concretely how the *existing automated script* would be extended to also cover the manual test's flow - not a vague "these are similar" statement.
