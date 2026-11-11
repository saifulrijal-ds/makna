# Project Brief: Makna

Status: DRAFT for review (2026-11-11).

Makna is an open semantic layer platform. The name is Indonesian for "meaning", which is what the platform gives people and AI agents about their data.

## Why this exists

Organizations increasingly let AI agents answer business questions over their data. The agents fail in a predictable way: they find tables, but they do not know what the business means. They do not know which column is the "official" disbursement date, how a "non-performing" loan is defined, which joins are safe, or which fields hold personal data. Every team that builds an agent re-solves this, usually inside a prompt or a skill file that nobody reviews.

A semantic layer solves this once. It holds the agreed meaning of the data (entities, metrics, dimensions, join paths, synonyms, owners, sensitivity) in one governed place, and serves it to people and to agents.

This project builds that layer as a web platform. It sits on top of an existing data catalog and query engine. It does not replace them.

## Who it is for

Two groups, equally important in the MVP:

- **Curators** (data engineers, data scientists, data governance staff, analysts). They define, review, and approve metrics and entities. AI assistants may draft definitions, but a draft is never official until an authorized owner approves it.
- **Business users.** They search and browse to find the right metric, understand what it means in plain language, see who owns it, and trust that it is the approved version.

A third consumer is not a person: **AI agents** reach the layer through an MCP server and must work with any agent platform or framework (Claude Code, Claude Desktop, Codex, Google ADK, LangChain, and others).

## What it does (MVP)

1. Connects to a data catalog and a query engine through pluggable connectors, and normalizes their metadata.
2. Stores semantic definitions as versioned YAML files, with a database index for search and serving.
3. Provides hybrid search (keyword and vector; reranking in phase 2, D-23) over metrics, entities, columns, and glossary terms.
4. Compiles a metric request (metric, dimensions, filters, time grain) into governed SQL for the engine.
5. Exposes read-only tools to agents through an MCP server.
6. Gives curators and business users a web app: search, browse, metric pages, an editor, a review queue, and a test bench.

## What it is not

- Not a data catalog. Catalog data stays the source of truth for physical metadata.
- Not a BI or dashboard tool.
- Not a write path for agents in v1. Agents can search, read definitions, and compile queries. They do not create or approve definitions.
- Not tied to any one company. It ships with synthetic sample data; company-specific connectors and definitions are added later by the adopting organization.

## How we know it works

The MVP is successful when, on the synthetic sample domain:

- An agent using the MCP server answers a golden set of business questions more accurately than the same agent with raw schema access only. The golden set is specified in `docs/REQUIREMENTS.md` (section 8); its questions and scoring method are written in `docs/EVALS.md` during the design session.
- Search returns the correct metric in the top 3 results for at least 90% of the golden questions (target to confirm after the first eval run).
- Every compiled query for an approved metric runs on the engine and returns the expected result.
- A curator can take a drafted metric from draft to approved in the web app, and the change is visible to agents without a redeploy.

## Constraints

- Open, company-agnostic code. No real company data, schemas, or credentials enter this repository.
- Sample data is synthetic and follows a consumer lending domain, so the platform is tested on realistic finance concepts.
- Agent-agnostic by design: anything an agent needs goes through the MCP server or a documented HTTP API.
- Governance is built in, not added later: lifecycle states, ownership, sensitivity tags, and masking rules exist from the first version.

## Related documents

- `docs/LANDSCAPE.md`: existing tools and prior work this builds on.
- `docs/REQUIREMENTS.md`: functional and non-functional requirements by phase.
- `docs/DECISIONS.md`: decisions made so far and open questions.
- `CLAUDE.md`: working rules for every agent in this repository.
- `docs/FABLE_BRIEF.md`: the brief for the architecture and design session.
