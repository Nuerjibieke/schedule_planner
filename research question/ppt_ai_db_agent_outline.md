# AI DB Agent 选题汇报 PPT 内容稿

## Slide 1. 标题

**题目：AI DB Agent 的可做研究问题：从 Text-to-SQL 到动态多轮数据库交互**

- 汇报目标：向导师说明一个现在可做、后续可扩展的 AI DB Agent 研究方向
- 当前建议问题：
  - **如何提升 AI DB agent 在动态多轮数据库交互中的正确性与稳定性？**
- 中长期延展：
  - 自然语言数据意图到可验证事务型状态变更

讲述要点：

- 我不想一步跳到“通用数据库智能体”
- 我希望先在现有 benchmark 上做出扎实结果
- 然后再升级到更强的问题

---

## Slide 2. 为什么值得做

- 数据库任务正在从单轮 `text-to-SQL` 变成真实工作流
- 真实用户会不断修改问题、增加约束、查看中间结果后再追问
- 企业场景需要的不只是“查对”，还需要“稳、可控、可扩展”

证据：

- [Spider 2.0](https://arxiv.org/abs/2411.07763)：真实 enterprise text-to-SQL workflow，632 个任务
- [DySQL-Bench](https://arxiv.org/abs/2510.26495)：动态多轮 SQL 交互，1,072 个任务
- [CIDR 2026 agent-first 数据系统论文](https://www.vldb.org/cidrdb/2026/supporting-our-ai-overlords-redesigning-data-systems-to-be-agent-first.html)：数据库社区已经开始把 agent 视为未来主导 workload

讲述要点：

- 这个方向不是“追热点”，而是 benchmark、工业界、数据库社区三条线都在推进

---

## Slide 3. 传统问题 vs 我想做的问题

### 传统问题

- `text-to-SQL`
- `semantic parsing`
- `text2CRUD`

默认假设：

- 用户意图固定
- 一次生成一条 SQL
- 评价标准主要是 execution accuracy

### 我想做的问题

- **动态多轮数据库交互**
- 用户在多轮过程中不断修正目标
- 模型需要持续追踪 intent、更新 query、保持正确性

一句话概括：

**从“生成一条 SQL”升级为“在交互中持续完成数据库探索任务”。**

---

## Slide 4. 最有价值且现在可做的问题

### 当前主问题

**如何提升 AI DB agent 在动态多轮数据库交互中的正确性与稳定性？**

英文：

**How can we improve the correctness and robustness of AI DB agents under dynamic multi-turn database interaction?**

为什么我选这个，而不是直接做更大的问题：

- 可以直接落到现有 benchmark
- 难度足够高，但边界还可控
- 既比传统 `text-to-SQL` 更新，也不会“大跃进”

讲述要点：

- 这是一个“现在就能开做”的问题
- 也是后续走向更强 DB agent 的合理第一步

---

## Slide 5. 这个问题的研究价值

### 学术价值

- 把 DB 任务从单轮解析推进到交互式 agent 问题
- 关注 correctness 与 robustness，而不只是单次生成
- 能连接 NLP、agent systems、database 三个方向

### 应用价值

- 更接近真实 analyst / data engineer 使用方式
- 更适用于 BI、企业数据探索、报表分析、业务排查
- 结果更容易迁移到真实数据库产品或 copilot 系统

### 方法价值

- 可以研究：
  - 多轮 intent tracking
  - schema grounding
  - query revision
  - verifier / self-correction
  - stopping strategy

---

## Slide 6. 和传统问题相比，挑战在哪里

### 挑战 1：意图是动态变化的

- 用户不是一开始就说清楚
- 会在中间不断加条件、改目标、换维度

### 挑战 2：正确性不是一次性的

- 前一轮生成正确，不代表下一轮还能保持一致
- 模型容易遗忘上下文、误改约束、累积错误

### 挑战 3：数据库交互是环境问题，不只是语言问题

- 要结合 schema、文档、历史 query、中间结果
- 有时还要决定是否继续问、是否停止、是否重写

### 挑战 4：现有能力强的模型仍然不稳

- [DySQL-Bench](https://arxiv.org/abs/2510.26495) 报告：`GPT-4o` 总体准确率 **58.34%**
- `Pass@5` 只有 **23.81%**

讲述要点：

- 这说明问题不是“已经快做完了”
- 仍然有明确而扎实的研究空间

---

## Slide 7. 可直接使用的 benchmark

### 1. DySQL-Bench

- 来源：[arXiv 2510.26495](https://arxiv.org/abs/2510.26495)
- 核心：dynamic multi-turn SQL interaction
- 规模：13 个领域，1,072 个任务
- 适合：
  - 多轮交互
  - query refinement
  - intent tracking
  - robustness

### 2. BIRD-Interact

- 来源：[官方站点](https://bird-interact.github.io/)
- ICLR 2026 Oral
- 600 个任务，覆盖 BI、CRUD、交互式探索
- 适合：
  - 用户模拟器
  - agentic interaction
  - 交互策略研究

### 3. Spider 2.0

- 来源：[arXiv 2411.07763](https://arxiv.org/abs/2411.07763)
- 632 个 enterprise workflow 任务
- 适合做外部补充验证：
  - 长上下文
  - 多步骤 workflow
  - 复杂环境泛化

讲述要点：

- 如果导师问“能不能马上做实验”，答案是可以
- 因为 benchmark 已经存在，不需要先造任务集

---

## Slide 8. 我的初步研究切入点

### 可以做的技术方向

1. **多轮意图状态建模**
   - 显式维护用户当前目标、约束和已确认条件

2. **交互式 query revision**
   - 不是每轮从头生成 SQL
   - 而是在已有 query 上局部修订

3. **verifier-backed correction**
   - 用 schema / execution feedback / intermediate result 检查错误
   - 再做定向修正

4. **上下文压缩与选择**
   - 不是把所有历史都塞给模型
   - 而是只保留当前轮最关键的 intent/state

讲述要点：

- 这些方向都能直接在 DySQL-Bench / BIRD-Interact 上验证
- 不需要先解决“写数据库回滚”这种更大的问题

---

## Slide 9. 中长期延展问题

如果第一阶段做顺利，第二阶段可以升级为：

**如何将自然语言数据意图编译为可验证、可回滚的事务型状态变更？**

这一步比当前问题更强，因为它开始涉及：

- update / insert / delete
- transaction boundary
- post-condition correctness
- rollback / repair
- policy-aware execution

讲述要点：

- 我当前不准备直接做这一步
- 但第一阶段的工作可以自然过渡到这个方向

---

## Slide 10. 最终建议

### 当前建议给导师的结论

我想做的不是传统 `text-to-SQL`，也不是一步跳到通用数据库 agent，而是一个中间且更稳的方向：

**如何提升 AI DB agent 在动态多轮数据库交互中的正确性与稳定性？**

### 选择这个问题的原因

- 有明确学术价值
- 有现成 benchmark
- 难度足够但不失控
- 能作为后续“事务型 DB agent”研究的第一步

### 一句话收尾

**这是一个“现在能做、以后能接着做大”的问题。**

---

## 参考资料

- [DySQL-Bench: Rethinking Text-to-SQL: Dynamic Multi-turn SQL Interaction for Real-world Database Exploration](https://arxiv.org/abs/2510.26495)
- [Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows](https://arxiv.org/abs/2411.07763)
- [BIRD: Can LLM Already Serve as A Database Interface?](https://arxiv.org/abs/2305.03111)
- [BIRD-Interact Official Site](https://bird-interact.github.io/)
- [CIDR 2026: Supporting Our AI Overlords: Redesigning Data Systems to be Agent-First](https://www.vldb.org/cidrdb/2026/supporting-our-ai-overlords-redesigning-data-systems-to-be-agent-first.html)
- [CIDR 2026: BridgeScope](https://www.vldb.org/cidrdb/2026/bridgescope-a-universal-toolkit-for-bridging-large-language-models-and-databases.html)
