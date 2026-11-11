# Fable Brief

Status: DRAFT for review (2026-11-11).

This file has two parts. Part 1 is for the project owner: how the work is split across models and how to run the Fable sessions. Part 2 holds the prompts given to Fable, written to follow Anthropic's prompting guide for Claude Fable 5.2.

---

## Part 1: Notes for the project owner

### How the work is split

Fable usage is limited, so Fable does only the work where judgment decides the outcome. Everything else goes to cheaper models.

| Phase | Model | Work |
|---|---|---|
| A. Design | Fable 5.2, effort `high` | Architecture, semantic model schema, answers to the open questions, sample domain metrics, eval design, and a work plan split into packages. Small code spikes only where a design risk needs proof. |
| B. Build | Opus 5.5 orchestrates; Sonnet 5.5 does most packages; Haiku 5.5 does well-specified, high-volume pieces (data generation, fixtures, boilerplate) | Implement the packages in `docs/PLAN.md`, each on its own branch with tests. |
| C. Review | Fable 5.2, fresh context | Review at two milestones: after the core (model, search, compiler) and before the MVP release. Fable reviews against the spec; it does not build. |

Why Fable is not the orchestrator for phase B: orchestration runs for the whole build, so it would spend most of the Fable allowance on coordination. Opus 5.5 orchestrates well at a lower cost. Fable's judgment goes where it pays most: the decisions everything else depends on, and independent review.

### Running a Fable session

1. Make sure the repository contains this context pack (`CLAUDE.md` and everything in `docs/`).
2. Start the session in a new conversation or as a subagent with the Fable model. Use effort `high` if the setting is available.
3. Paste the prompt from Part 2 as the first message. Do not add instructions to "explain your reasoning in the response". On Fable 5.2 that can trigger a refusal. Ask for decisions and their reasons instead, as the prompts below do.
4. Do not interrupt for small things. Fable is built for long, unattended runs. Check back on the result.
5. If you need to change instructions partway, put the change in your newest message. Do not edit earlier messages.

---

## Part 2: Prompts

### Prompt A: Design session

```
I'm building Makna, an open semantic layer platform: a web app that sits on top of a data catalog and a query engine, and gives business users, data curators, and AI agents one governed place to find and use the meaning of data. I'm a data scientist who builds AI agents in a regulated consumer lending company, and I want this platform to become the shared foundation for several agents that today each re-learn the data on their own. The repository you are in holds the context pack. A team of cheaper models will build the platform from the design you produce, so the quality of your design decides the quality of the product.

Read these first, in this order: CLAUDE.md, docs/PROJECT_BRIEF.md, docs/LANDSCAPE.md, docs/REQUIREMENTS.md, docs/DECISIONS.md.

<goal>
Produce a design that a team of Sonnet-class coding agents can build from with few questions, and that makes the MVP succeed as defined in docs/PROJECT_BRIEF.md.
</goal>

<deliverables>
1. docs/ARCHITECTURE.md: components, how they talk to each other, the data flow for a search and for a compiled query, the internal metadata model, and where each requirement ID is met. Include one diagram in Mermaid.
2. Answers to every open question Q-1 to Q-8 in docs/DECISIONS.md, as decision records with the options you considered, the choice, and the reason. Revise a proposed default P-1 to P-8 only if you find a real reason, and record it.
3. semantic/schema/: the semantic model schema (JSON Schema), plus three example definitions from the sample domain that validate against it: one simple metric, one ratio metric, and one metric that needs a non-obvious join path.
4. docs/SAMPLE_DOMAIN.md: the synthetic lending domain. Tables, columns, the quirks from DAT-2, and the 15 to 25 metrics from DAT-3 with plain-language definitions.
5. docs/EVALS.md: the golden question set design (EVL-1 to EVL-4), with the first 20 questions written out, including ambiguous ones and ones with no valid answer, and how each eval is scored.
6. docs/PLAN.md: the build plan as work packages. Each package has an ID, the requirement IDs it covers, its dependencies, acceptance criteria that a test can check, and a suggested model (Sonnet 5.5 for most, Haiku 5.5 for well-specified high-volume work). Order the packages so the system runs end to end as early as possible. Also create docs/progress.json listing every package with status "not_started".
</deliverables>

<how_to_work>
This design is yours: what the platform is made of and how it fits together are your call, within the decisions in docs/DECISIONS.md marked as decided.

When you have enough information to act, act. Do not re-litigate decisions D-1 to D-9. Python as the primary language (D-8) matters to me: the people who will maintain this work in Python, so design the web app to stay in Python too. If you are weighing a choice, give a recommendation, not an exhaustive survey.

Check current facts about outside tools (versions, licenses, formats such as MetricFlow and Open Semantic Interchange) with web search before you rely on them, and cite what you used in the decision records.

Where a design risk could sink the plan, prove it with a small spike and keep the spike in spikes/ with a short README. Likely candidates: compiling a ratio metric with a multi-hop join into Trino SQL with sqlglot, and hybrid search ranking on PostgreSQL. Do not build the product itself.

Use subagents when a subtask is independent and can run in parallel, for example checking outside tools or drafting the sample domain while you work on the schema. Each subagent adds cost, so do not delegate small or sequential steps. Brief each subagent with the files it needs and the exact output you expect.

Before you finish, have a fresh-context subagent review your deliverables against docs/REQUIREMENTS.md and the goal above. It should list requirements with no clear home in the design, contradictions between documents, and work packages whose acceptance criteria a test could not check. Fix what it finds.

Commit your work on a branch named design/architecture in small steps, push the branch, and open a pull request. These actions do not need my approval. Ask me before anything destructive or before changing repository settings.

Store lessons that future agents in this repository should know in docs/notes/, one lesson per file with a one-line summary at the top.
</how_to_work>

<finishing>
Please avoid stopping while you still owe work I asked for. Carry on with work that does not depend on my answer. When you meet ordinary obstacles such as errors, timeouts, or failing tools, diagnose them and work through them. Stop only when the deliverables are complete or you are blocked on input only I can provide. If you are blocked, ask and end the turn, rather than ending on a promise.

Before reporting progress, audit each claim against a tool result from this session. Only report work you can point to evidence for. If something is not yet verified, say so.
</finishing>

<final_report>
Write for an intelligent reader who has not seen your tool calls or the files you opened. Open with the outcome in one or two sentences. Then give: the three to five decisions that matter most and why, the risks you see for the build, anything you need from me, and a link to the pull request. Use plain words and full sentences. Do not use terms you made up during the work without explaining them. Keep bullets for lists of separate items.
</final_report>
```

### Prompt C: Milestone review

Use this in a new conversation each time, so the reviewer has not seen the work being built.

```
You are reviewing Makna, a semantic layer platform built by a team of coding agents from a design that is in this repository. I need an independent, honest assessment before the project moves to the next milestone: [core complete | MVP release]. A clear list of real problems is worth more to me than reassurance.

Read CLAUDE.md, docs/PROJECT_BRIEF.md, docs/REQUIREMENTS.md, docs/ARCHITECTURE.md, docs/PLAN.md, and docs/progress.json. Then review the code on the main branch.

<what_to_check>
- Does the code meet the requirement IDs that docs/progress.json marks as done? Check each one against the code and the tests, not against the progress file.
- Does the implementation follow the architecture? Where it does not, is the difference an improvement or a problem?
- Run the test suite and the evals. Report the actual numbers.
- Security and governance: can an agent reach raw personal data, approve a definition, or run SQL outside approved join paths? Can a non-approver approve?
- Code that will slow down the next phase: duplicated logic, unclear module boundaries, missing tests for core paths.
</what_to_check>

<how_to_work>
This is a review. Report your findings and stop. Do not fix the code yourself, except to write a failing test that proves a bug, which you may commit on a branch named review/<milestone>.

Use subagents for independent areas (for example security, search quality, compiler correctness) if that speeds the review. Brief each one with the files it needs and the exact output you expect.

Before reporting, audit each finding against something you observed in this session: a file, a test result, or a command output. If you suspect a problem but could not confirm it, mark it as unconfirmed.
</how_to_work>

<final_report>
Write docs/reviews/<milestone>.md and give me a summary. Open with your verdict in one sentence: ready to move on, ready after named fixes, or not ready. Then list findings in order of severity. For each finding, say what is wrong, where, the evidence, and the smallest fix. Use plain words and full sentences. Write for a reader who has not seen your tool calls.
</final_report>
```
