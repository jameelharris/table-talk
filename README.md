# table-talk

A conversational analytics agent over a BigQuery poker dataset. Source data is ingested from final-table poker video replays via a video-to-JSON pipeline.

**Status**: in progress. Migrating from a Colab prototype to a structured Python project with IaC, dbt, and an agentic interface.

## Architecture (planned)

1. **Ingestion**: `yt-dlp` downloads poker broadcasts; videos cached in GCS
2. **Extraction**: Gemini (via Vertex AI) extracts hand records as JSON
3. **Validation & shredding**: dbt validates JSON structure and shreds into a BigQuery star schema
4. **Analytics**: a conversational agent queries BigQuery via natural language

## Components

| Component | Status | Location |
|-----------|--------|----------|
| Infrastructure (Terraform) | In progress | `terraform/` |
| Video-to-JSON pipeline | Migrating from Colab | TBD |
| dbt models | Not started | TBD |
| Conversational agent | Not started | TBD |

## Getting started

This project is under active development and not yet usable end-to-end. See `terraform/README.md` for infrastructure setup.

## Repository conventions

- `CLAUDE.md` defines behavioral guidelines for AI coding agents working on this repo
- IaC is incremental; each commit adds infrastructure tied to a specific capability
- The original Colab notebook is preserved as a behavioral reference, not a translation source
