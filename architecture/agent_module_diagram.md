# Agent 运行示意图

这张图用较抽象的方式展示完整运行 flow，强调两个阶段：

- 上半部分是理解与编排。
- 下半部分是确认、执行与状态落地。

```mermaid
flowchart TB
    classDef user fill:#f6efe6,stroke:#8b6f47,color:#2b2116,stroke-width:1.2px;
    classDef stage fill:#eef3f8,stroke:#5d748a,color:#1f2d3a,stroke-width:1.2px;
    classDef decision fill:#fff7df,stroke:#b6922e,color:#3a2f0f,stroke-width:1.2px;
    classDef store fill:#eaf4ea,stroke:#5b8a61,color:#1e3521,stroke-width:1.2px;
    classDef output fill:#f3edf7,stroke:#7b6593,color:#2b2136,stroke-width:1.2px;

    U["用户请求"]:::user

    subgraph S1["理解与编排阶段"]
        O["编排入口"]:::stage
        P["规划与工具调用"]:::stage
        A["整理为可处理动作"]:::stage
    end

    subgraph S2["确认与执行阶段"]
        D{"是否需要确认"}:::decision
        E["执行与校验"]:::stage
    end

    DB[("状态存储")]:::store
    PRE["预览与确认"]:::output
    OUT["结果反馈"]:::output

    U -->|提交需求| O
    O -->|组织处理流程| P
    P <--> |查询、解析、校验并获取当前状态| DB
    P -->|产出结构化计划| A
    A -->|进入确认或执行判断| D
    D <--> |预览展示与用户确认| PRE
    D -->|可直接落地时继续执行| E
    E <--> |写入并更新状态| DB
    E -->|反馈执行结果| OUT
```

## 图的阅读方式

- 从上到下看：先理解请求，再决定如何执行。
- 左到右看：每条箭头都表示一次明确的流转关系。
- 中间的判断节点表示系统会在这里分成两种路径：
  - 先进入“预览与确认”。
  - 直接进入执行。

## 为什么不是“规划与查询”

这里不只是查询。

规划阶段除了读取当前任务状态，还会调用多种确定性能力，例如：

- 查询已有任务
- 解析自然语言时间
- 搜索空闲时段
- 检查时间冲突
- 校验候选计划

因此用“规划与工具调用”比“规划与查询”更准确。
