# table-talk

A conversational analytics agent over a BigQuery poker dataset. Source data is ingested from final-table poker video replays via a video-to-JSON pipeline.

**Status**: in progress. Migrating from a Colab prototype to a structured Python project with IaC, dbt, and an agentic interface.

## Data Model

The schema captures poker hand histories extracted from recorded broadcasts. A `Video` is segmented into `Clips`, each clip holds 1-N `Hands`, and each hand decomposes into per-street state (`Hands_Streets`), per-player state (`Hands_Positions`), and the individual `Actions` taken. The `*_LU` tables are lookups for streets, seat positions, and action types.

```mermaid
%%{init: {"layout": "elk"}}%%
erDiagram
    VIDEOS           ||--o{ CLIPS           : "has"
    CLIPS            ||--o{ HANDS           : "contains"
    HANDS            ||--o{ HANDS_STREETS   : "played over"
    STREETS_LU       ||--o{ HANDS_STREETS   : "classifies"
    HANDS            ||--o{ HANDS_POSITIONS : "seats"
    POSITIONS_LU     ||--o{ HANDS_POSITIONS : "classifies"
    HANDS_STREETS    ||--o{ ACTIONS         : "occurs on"
    HANDS_POSITIONS  ||--o{ ACTIONS         : "performs"
    ACTIONS_LU       ||--o{ ACTIONS         : "classifies"

    VIDEOS {
        int  video_id          PK
        char video_url
        char video_title
        time video_start_time
        time video_end_time
        date upload_date
    }
    CLIPS {
        int  clip_id           PK
        int  video_id          FK
        time clip_start_time
        time clip_end_time
    }
    HANDS {
        int  hand_instance_id  PK
        int  clip_id           FK
        time hand_start_time
    }
    HANDS_STREETS {
        int     street_instance_id  PK
        int     hand_instance_id     FK
        int     street_id            FK
        varchar community_cards "array, NOT NULL"
    }
    STREETS_LU {
        int  street_id         PK
        char street_name
        int  street_order
    }
    HANDS_POSITIONS {
        int  hand_position_id  PK
        int  position_id       FK
        int  hand_instance_id  FK
        int  starting_stack
        char hole_cards
        char denomination
        char limit_type
        char game_format_type
    }
    POSITIONS_LU {
        int  position_id           PK
        char seat_position_label
    }
    ACTIONS {
        int   action_instance_id  PK
        int   hand_position_id     FK
        int   street_instance_id   FK
        int   action_id            FK
        float bet_amount
        int   action_order
    }
    ACTIONS_LU {
        int  action_id     PK
        char action_name
    }
```

## Long-term architecture

1. **Ingestion**: `yt-dlp` downloads poker broadcasts; videos cached in GCS
2. **Extraction**: Gemini (via Vertex AI) extracts hand records as JSON
3. **Validation & shredding**: dbt validates JSON structure and shreds into a BigQuery star schema
4. **Analytics**: a conversational agent queries BigQuery via natural language

## Documentation

- [ARCHITECTURE.md](./ARCHITECTURE.md) — pipeline phases, file organization, BQ tables, failure handling per phase
- [CLAUDE.md](./CLAUDE.md) — behavioral guidelines for AI coding agents and project conventions that new code must follow

## Getting started

This project is under active development and not yet usable end-to-end. See `terrm/README.md` for infrastructure setup.
