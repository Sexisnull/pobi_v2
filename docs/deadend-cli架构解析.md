> This project is ongoing and available here: [https://github.com/xoxruns/deadend-cli](https://github.com/xoxruns/deadend-cli). If you are looking for the benchmarks results take a look here: [https://github.com/xoxruns/deadend-cli/tree/main/benchmarks-results/xbow](https://github.com/xoxruns/deadend-cli/tree/main/benchmarks-results/xbow).  
> If you want to know more this project, or just have a chat, you can contact on discord (xoxruns) or linkedin ([https://www.linkedin.com/in/yass-99637a105/](https://www.linkedin.com/in/yass-99637a105/))

![](https://miro.medium.com/v2/resize:fit:2000/format:webp/1*XoDybwz30VYiha2Bo8IFEA.png)

Architectural design, representing agentic paths to successful penetration testing

**Pentesting AI agents are becoming more capable,** and people are starting to see the advantages. But here’s the problem: analyzing the different solutions, many projectslack **verifiable results** based on **confirmed benchmarks**.

**XBOW** and **Google’s Project Zero** pioneered agentic offensive security. While proprietary, they’ve set the building blocks. **XBOW** reports **85%**. **Cyber-AutoAgent** hits **81%** using AWS Bedrock and **MAPTA** reaches **76.9%** — *both fail on blind SQL injection*. These are actual, comparable, measurable results.

I’ve been exploring this for the past 6 months. The last 2–3 months were mainly focused on building the most suitable AI agent architecture for webapp and API pentesting. On the 104-challenge **XBOW benchmarks**, **we’re achieving ~77.55% on 98 challenges**. Compared to the other agents, it was able to solve one of the blind SQL injections that others could not, but still struggles with other ones.

The approach uses **feedback-driven iteration**: when a task is not achieved, the agent doesn’t just give up — it refines the plan or expands it, observes what happened, changes the tool if needed, and keeps iterating, until breakthrough. Ultimately resolving some of the challenges that other implementations could not.

**And everything runs fully locally**. No strands requirements like Cyber-AutoAgent. MAPTA is primarily an academic paper with limited open-source implementation, while other solutions use proprietary benchmarks or outdated test suites. We also rely on **custom sandboxed tools** *(Playwright for HTTP, WebAssembly for Python, Docker for shell)*, **local or self-hosted models**, zero data exfiltration. If you can deploy the model, the agent works with it.

Here’s why this matters: if we’re not presented with **verifiable** and **reproducible** **benchmarks**, we don’t have **proof the agent does what it claims**. In cybersecurity it’s even more important — **it defines whether the agent we’re using can actually protect us by finding flaws before attackers do**.

***This article breaks down the architecture, the tooling decisions, and the actual results. All verifiable, all reproducible and available here:*** [*https://github.com/xoxruns/deadend-cli/tree/main/benchmarks-results/xbow*](https://github.com/xoxruns/deadend-cli/tree/main/benchmarks-results/xbow)***.***

## Evaluation and results

### Environment

The benchmarks that I’ve been trying to run are the [validation benchmarks](https://xbow.com/blog/benchmarks). XBOW designed these benchmarks to represent what an experienced pentester could achieve in a week — their proprietary system reports 85% success rate as the baseline.According to XBOW:

> a success rate of 85%, which is equivalent to what a experienced pentester could achieve within a week

All benchmarks were run on XBOW’s validation suite in black-box mode *(otherwise it defeats the purpose)* — no source code access, only target URLs and challenge descriptions. This mirrors real-world pentesting scenarios where you don’t have insider knowledge of the application.

**Agent Test Configuration:**

- ***Models tested: Kimi K2 Thinking, Anthropic Claude Sonnet 4.5***
- ***Evaluation period: January 2026***
- ***Max recursive depths for the planner: 2 — Defines how level the primary task should be decomposed into smaller ones.***

### Model Comparison

We tested two frontier models to validate first architectural designs: **Claude Sonnet 4.5** (**77.55%**) and **Kimi K2 Thinking** (~ **69%**) — Both achieving Blind SQL injections payloads.

Both models used identical architecture and tooling — the only variable was the LLM itself. While the model capabilities are important, the goal in the near futur is to achieve to same results with open-weight models.

### Overall results

From the results we can start by concluding good results in **XSS (91.3%)**, **IDOR (80%)**, **Business Logic (85.7%)**, **SQL injection (83.3%)**. And **perfect findings in graphQL, SSRF, HTTP method tamper, NoSQL injection, Brute-force (100%)**.

### Where and why it fails (For now…)

**LFI/Path Traversal**  
These vulnerabilities are well-documented, making the low success rate surprising. The agent struggled with directory traversal depth enumeration and finding the right path patterns.

**Blind SQL Injection**  
One successful exploit out of three attempts. While better than MAPTA’s and Cyber-AutoAgent’s results in this category, two challenges remain unsolved.

**SSTI**  
Template injection failures were unexpected given strong performance on other injection vulnerabilities.

**CVE-specific challenges**  
Lower success expected — the agent doesn’t have exhaustive CVE database access. These require specific version fingerprinting and known exploit adaptation.

## Architecture: Feedback-driven iteration

### Two-Phase Approach: Recon Before Attack

The architecture operates in two distinct phases, mirroring real-world penetration testing methodology.

![](https://miro.medium.com/v2/resize:fit:2000/format:webp/1*hjo0jieOfoFZN0Qh6bUO_Q.png)

overall workflow

**Phase 1:** Reconnaissance The first phase gathers intelligence about the target application. This includes identifying endpoints, technologies, authentication mechanisms, and potential attack surfaces. While reconnaissance can be partially automated with traditional tools (port scanners, directory bruteforcers), using a Language Model can ease the strategic oversight and further planning— determining which findings matter and how they relate to potential vulnerabilities.

The reconnaissance phase uses the same supervisor agent architecture but with a reconnaissance-specific goal prompt. It collects structured information that becomes context for the exploitation phase.

**Phase 2:** Exploitation The exploitation phase receives reconnaissance findings as context, enabling focused vulnerability discovery. The agent already knows what endpoints exist, which require authentication, what technologies are in use, and where to focus efforts. This targeted approach is more efficient than blind testing — the agent avoids wasting time on dead ends and concentrates on promising attack surfaces.

The dual-phase structure ensures thorough reconnaissance before exploitation begins, preventing a lack of context and divergence for the LLM.

### Agentic architecture: ADaPT-Based Recursive Decomposition

The exploitation phase implements an agentic architecture deeply inspired by the [ADaPT paper](https://arxiv.org/abs/2311.05772) (ADaPT: As-Needed Decomposition and Planning with Language Models).

The different component are the following:

- The planner
- Executor
- Validator
![](https://miro.medium.com/v2/resize:fit:1400/format:webp/1*klJcE_ipgTsdO07vsChm2g.png)

Agentic architecture: The handling of task in the plan

### Core components

**Planner:** The planner’s main goal is to decompose a task into subtasks. Track the tasks, their success and their failures. It has specifically two methods, `expand`, which expands a task into subtasks, and `update` which updates the subtasks according to a parent task. The main objective of the planner, is to keep track of the tasks, update them and create new ones if needed. To be able to know what action it needs to do, we define confidence scores (inspired by cyber-AutoAgent functioning). When execution confidence is low (20–60%), the Planner expands the task into more granular subtasks. When confidence is moderate (60–80%), it refines the existing plan based on what has already been achieved.

**Executor:** The executor component executes the task, sees to its completion. The executor relies on a supervisor agent to subagents (generic agents). The supervisor analysis the task and decides which generic agent is the more suitable for the task at hand. If it doesn’t work, the supervisor can recall another tooling to test another method and so on, until the task is completed (success or failure).

**Validator:** Verifies task completion through proof-of-concept validation. When execution confidence exceeds 80%, the Validator confirms whether exploits actually work, extracting validation tokens (flags) and ensuring discoveries are genuine rather than false positives.

### Recursive Task Solving with Feedback Loops

The core of our approach is the `_solve()` method, which implements recursive task decomposition with feedback-driven adaptation.

The recursion enables dynamic adaptation: if a subtask’s confidence drops below 60%, it can further decompose. If it rises above 80%, validation occurs. This creates a tree of tasks that grows and shrinks based on execution feedback — fundamentally different from static, predetermined exploitation sequences.

### Generic agents (subagents)

Each generic agent (or subagent) have a specific tooling.

- **Requester agent** — uses playwright: fined-grained testing, similar to what we do manually.
- **Shell agent** — Docker sandboxed image: Gives access to usual pentest tooling.
- **Python interpreter agent** — Webassembly sandbox: The python interpreter enables exploit and testing generation directly by the agent.

### Why Custom Tools Instead of Existing Solutions?

*Why not use existing frameworks like browser-use or e2b for sandboxing?* Two reasons: task-specific design and execution control.

**Task-Specific Tooling**

Each tool is built for a specific purpose. The requester agent has one job: generate and send binary HTTP payloads. This specificity matters because standard HTTP libraries enforce RFC compliance — they won’t send malformed requests needed for certain vulnerabilities (like HTTP request smuggling).

For example; well-written HTTP parsers reject packets with conflicting Content-Length and Transfer-Encoding headers. But that’s exactly what you need to test for request smuggling vulnerabilities. Building Playwright-based requester can craft these malformed requests because it operates at a lower level than standard libraries.

The custom implementation also handles authentication state properly — cookies and tokens are saved and shared across different tools, maintaining session context throughout multi-step exploitation.

**Execution control**

E2B and similar sandboxing services are well-built, but they run in the cloud.

The sandboxing we need is just containers — And it’s also what those companies do anyway. There’s no technical reason to add an external layer when laptops can run Docker and WebAssembly efficiently. Running locally also means:

- Full control over sandboxing policies
- No data exfiltration to external services
- No additional latency from cloud round-trips
- No vendor dependency or API costs

Custom tools give us precision for pentesting-specific needs while keeping everything on controlled infrastructure.

### Tech stack

We use [LiteLLM](https://github.com/BerriAI/litellm) to access models from different providers. After testing alternatives, LiteLLM was the obvious choice — it’s open-source and works seamlessly across providers. The goal is also to enable the possibility to run everything locally. **Including the model**.

We use [Instructor](https://python.useinstructor.com/) for LLM structured output.

[pgvector](https://github.com/pgvector/pgvector) as the vector database.

Webassembly and [pyodide](https://pyodide.org/en/stable/index.html) for the sandboxed python interpreter.

Playwright for handling the requester tooling.

### From normal agentic worklow to feedback driven iteration

*How the architectural changes resulted in the 78% findings?*

The first implementation used vulnerability-specific agents — one for reconnaissance, another for exploitation, sometimes specialized agents for each vulnerability type. This approach had two critical problems.

**Problem 1: Tool Confusion** Giving too many tools to a single agent caused confusion about which tool to use, even with explicit ordering. The solution: introduce a supervisor-subagent hierarchy where each subagent has a focused toolset (3–5 tools) for its domain (HTTP, shell, or Python).

**Problem 2: No Feedback** The linear architecture lacked feedback loops. When an approach failed, the agent couldn’t determine if it should refine its strategy or pivot entirely. The solution: confidence-based iteration where each execution produces a score *(0–1.0) that determines the next action — fail (<20%), expand (20–60%), refine (60–80%), or validate (>80%)*.

## Future Work

The current architecture demonstrates that competitive autonomous pentesting (77.55% on XBOW) is achievable without cloud dependencies. The next challenges:

**Open Models performance** Achieve comparable results using only open-weight models. Current implementation uses Claude Sonnet 4.5 and Kimi K2 — demonstrating this works with 80%+ of success with open-weights models is essential for a shift to competitive/low-cost results.

**Hybrid Testing** Add AST analysis for white-box code inspection alongside behavioral testing. Most real vulnerabilities require understanding both behavior AND implementation — pure black-box testing misses logic flaws and insecure coding patterns.

**Adversarial Robustness** Current benchmarks use static targets. Real systems employ adaptive defenses — WAFs, rate limiting, behavioral analysis. Training against adversarial scenarios would improve real-world effectiveness.

**Multi-Target Orchestration** Extend beyond single-target testing to coordinate attacks across related systems — API and web interface together, or distributed microservices. Complex environments require understanding relationships between components.

**Context Efficiency** Reduce token usage and tool calls through better information sharing between components. Current repetitive calling suggests suboptimal context management — addressing this would improve both speed and cost.

*The ultimate goal: autonomous pentesting that’s accessible (open-source models), comprehensive (hybrid testing), and robust (works against real defenses), not just benchmark targets.*

## References

1. ADaPT: As-Needed Decomposition and Planning with Language Models — [https://arxiv.org/abs/2311.05772](https://arxiv.org/abs/2311.05772)
2. XBOW Validation Benchmarks — [https://xbow.com/blog/benchmarks](https://xbow.com/blog/benchmarks)
3. Multi-Agent Penetration Testing AI for the Web (MAPTA) — [https://arxiv.org/abs/2508.20816](https://arxiv.org/abs/2508.20816)
4. Cyber-AutoAgent — [https://medium.com/data-science-collective/from-single-agent-to-meta-agent-building-the-leading-open-source-autonomous-cyber-agent-e1b704f81707](https://medium.com/data-science-collective/from-single-agent-to-meta-agent-building-the-leading-open-source-autonomous-cyber-agent-e1b704f81707)
5. From Naptime to Big Sleep — [https://projectzero.google/2024/10/from-naptime-to-big-sleep.html](https://projectzero.google/2024/10/from-naptime-to-big-sleep.html)
6. [https://cdn.prod.website-files.com/686c11d5bee0151a3f8021bf/689cb0e9212bdf4ed0efe44c\_US-25-Dolan-Gavitt-AI-Agents-for-Offsec-with-Zero-False-Positives.pdf](https://cdn.prod.website-files.com/686c11d5bee0151a3f8021bf/689cb0e9212bdf4ed0efe44c_US-25-Dolan-Gavitt-AI-Agents-for-Offsec-with-Zero-False-Positives.pdf)
7. Towards Effective Offensive Security LLM Agents: Hyperparameter Tuning, LLM as a Judge, and a Lightweight CTF Benchmark — [https://arxiv.org/pdf/2508.05674](https://arxiv.org/pdf/2508.05674)