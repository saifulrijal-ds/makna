# Landscape: What Exists and How This Fits

Status: DRAFT for review (2026-11-11).

This document lists the tools and prior work the platform builds on or competes with. Facts about outside tools reflect general knowledge as of mid-2026. The design session must check current versions, licenses, and capabilities before relying on any of them.

## The problem in one picture

A typical analytics stack has three layers that do not talk to each other well:

- A **data catalog** knows physical metadata: tables, columns, descriptions, owners, tags, lineage.
- A **query engine** (Trino in our reference setup) knows how to run SQL across sources.
- **AI agents** need business meaning: what a metric is, how to compute it, which joins are valid, and what is sensitive.

The semantic layer sits between the catalog and engine on one side and people and agents on the other.

## Existing semantic layer tools

Assess each for reuse (as a library, a format, or a reference design). Do not adopt any of them without a written reason in `docs/DECISIONS.md`.

- **dbt MetricFlow / dbt Semantic Layer.** A mature metric definition format (semantic models, measures, dimensions, entities) and a SQL compiler. Ties closely to dbt projects.
- **Cube.** A semantic layer server with its own data model, caching, and APIs. Strong for BI. Heavy to run.
- **Malloy.** A semantic modeling language that compiles to SQL. Expressive and well designed, smaller ecosystem.
- **Open Semantic Interchange (OSI).** An industry effort to define a shared, vendor-neutral format for semantic metadata. Check its current status. Compatibility with an open format would make the platform easier to adopt.
- **OpenMetadata and DataHub.** Open-source data catalogs with glossary and metric features. Useful as the first catalog connector targets, since an open-source catalog can stand in for any organization's in-house catalog during development.
- **MCP Toolbox for Databases (Google).** Exposes predefined database tools to agents through MCP. A possible companion for query execution, not a semantic layer by itself.

The project owner has evaluated some of these before (OKF, Malloy, OpenMetadata, and OrionBelt were part of earlier research). The design session should form its own view and record it.

## The shape of the adopting organization

The platform is open and generic, but it is designed with one realistic adopter in mind: a regulated consumer lending company. That adopter has:

- An **in-house data catalog** that will be the source of truth for physical metadata and, by design, a home for metric definitions. The platform must connect to it through a connector, not replace it.
- **Trino** as the query engine, with LDAP username and password authentication for clients.
- A **feature store** (Feast) where feature definitions live as YAML in a git repository, reviewed through pull requests. This shows the governance style the organization already trusts: definitions as code, changes through review.
- A **data governance team** that may draft terms and metrics with AI help. Drafts stay drafts until an authorized owner confirms them.
- **Personal data protection rules** (Indonesia's UU PDP in the reference case). Personal fields must be tagged, masked by default, and never exposed to agents in raw form.
- **Several agent projects** that would consume the layer: a self-service analytics agent on Trino, a feature store assistant, and analytics agents on other engines. They use different frameworks, so the layer must be agent-agnostic.

None of this organization's real data, schemas, or code is in this repository. Use it only as a source of realistic requirements.

## What this platform adds

The existing tools cover parts of the problem. The gap this platform fills:

1. **Catalog-first.** It layers meaning on top of an existing catalog instead of owning the metadata.
2. **Agent-first serving.** Hybrid search and an MCP server are core features, not add-ons.
3. **Governance built in.** Draft, review, and approved states; owners; sensitivity tags; masking rules.
4. **A curator workflow and a business discovery experience in one app**, with a test bench that shows what an agent would do with a question.
