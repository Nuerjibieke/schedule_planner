# Agent Runtime Diagram

This figure presents the runtime flow in an abstract way and highlights two stages:

- The upper part is for understanding and orchestration.
- The lower part is for confirmation, execution, and state persistence.

```mermaid
flowchart TB
    classDef user fill:#f6efe6,stroke:#8b6f47,color:#2b2116,stroke-width:1.2px;
    classDef stage fill:#eef3f8,stroke:#5d748a,color:#1f2d3a,stroke-width:1.2px;
    classDef decision fill:#fff7df,stroke:#b6922e,color:#3a2f0f,stroke-width:1.2px;
    classDef store fill:#eaf4ea,stroke:#5b8a61,color:#1e3521,stroke-width:1.2px;
    classDef output fill:#f3edf7,stroke:#7b6593,color:#2b2136,stroke-width:1.2px;

    U["User Request"]:::user

    subgraph S1["Understanding and Orchestration"]
        O["Orchestration Entry"]:::stage
        P["Planning and Tool Use"]:::stage
        A["Action Generation"]:::stage
    end

    subgraph S2["Confirmation and Execution"]
        D{"Confirmation Needed?"}:::decision
        E["Execution and Validation"]:::stage
    end

    DB[("State Storage")]:::store
    PRE["Preview and Confirmation"]:::output
    OUT["Result Feedback"]:::output

    U -->|Submit request| O
    O -->|Organize processing| P
    P <--> |Query, parse, validate, and retrieve current state| DB
    P -->|Produce structured plan| A
    A -->|Form candidate actions| D
    D <--> |Preview display and user confirmation| PRE
    D -->|Proceed when direct execution is allowed| E
    E <--> |Write and update state| DB
    E -->|Return execution result| OUT
```

## How to Read the Figure

- Read from top to bottom: the system first understands the request, then decides how to execute it.
- Read each arrow as one explicit interaction or transition.
- The decision node in the middle splits the flow into two paths:
  - go to preview and confirmation first
  - or proceed directly to execution

## Why It Is Not Just "Planning and Querying"

This stage does more than querying.

During planning, the system can also invoke deterministic capabilities such as:

- task lookup
- natural language time parsing
- free-slot search
- conflict checking
- candidate-plan validation

So "Planning and Tool Use" is more accurate than "Planning and Querying".
