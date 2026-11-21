# Techniques and tools that help LLMs and coding agents understand and analyse code (state as of November 2026)

Labels used below: **[measured-peer]** = peer-reviewed or arXiv study with stated method; **[measured-vendor]** = number measured by the vendor on its own product or benchmark; **[vendor-claim]** = marketing or unquantified statement; **[secondary]** = reported by a third party, primary source not read in this session. Every finding carries the source's date.

## 1. Repository-level context techniques and the evidence for them

### Takeaway
The field moved from pre-computed context (repo maps, embeddings, graphs pushed into the prompt) to just-in-time context that the agent pulls through tools. Structural indexes still give measurable gains, but mainly as a tool the agent queries (localisation, multi-hop navigation), on large repositories, and with small or cheap models; pushed repository overviews show no measured benefit.

### Cited Findings

**Repo maps**
- Aider's repo map lists files with the key symbols and their "critical lines" (signatures, types). Files are graph nodes, dependencies are edges, a graph-ranking algorithm picks what fits a token budget. Default budget is 1k tokens (`--map-tokens`), and the map grows when no files are in the chat. Documentation, undated, read Nov 2026. [vendor-claim, design description] ([Aider repo map docs](https://aider.chat/docs/repomap.html))
- Aider publishes no controlled ablation of the repo map in that documentation page. See Gaps.

**Agent-queried structure: AST search and graph tools**
- AutoCodeRover (ISSTA 2024, arXiv Apr 2024) searches by program structure (classes, methods) on an AST instead of files, optionally narrowed by spectrum-based fault localisation from tests. It resolved 19% of SWE-bench Lite at USD 0.43 average cost. [measured-peer] ([arXiv 2404.05427](https://arxiv.org/abs/2404.05427))
- SWE-agent (arXiv May 2024, rev. Nov 2024) introduced the term agent-computer interface (ACI). A purpose-built interface for viewing, searching and editing gave 12.5% pass@1 on SWE-bench and 87.7% on HumanEvalFix, and the paper studies how interface design changes agent behaviour. [measured-peer] ([arXiv 2405.15793](https://arxiv.org/abs/2405.15793))
- RepoGraph (ICLR 2025, arXiv Oct 2024) builds a line-level graph of definitions and references and plugs into existing systems. Reported average relative improvement of 32.8% in resolve rate across four methods on SWE-bench Lite; for Agentless with GPT-4o the absolute change is small, 27.33% to 29.67%. [measured-peer, self-reported] ([arXiv 2410.14684](https://arxiv.org/abs/2410.14684), [HTML](https://arxiv.org/html/2410.14684v2))
- CodexGraph (NAACL 2025, arXiv Aug 2024) lets the agent write graph-database queries against a code graph. Evaluated on CrossCodeEval, SWE-bench and EvoCodeBench; described as competitive, stronger with stronger LLMs. [measured-peer; exact figures not read] ([arXiv 2408.03910](https://arxiv.org/html/2408.03910v2))
- LocAgent (arXiv Mar 2025, rev. Apr 2025) parses a repo into a directed heterogeneous graph (files, classes, functions; imports, invocations, inheritance) for multi-hop search. Up to 92.7% file-level localisation accuracy; a fine-tuned Qwen-2.5-Coder-32B matches proprietary models at about 86% lower cost; downstream issue resolution improves 12% at Pass@10. [measured-peer] ([arXiv 2503.09089](https://arxiv.org/abs/2503.09089))
- A 2026 monograph on coding-agent reliability (Jarmak, arXiv Aug 2026) is reported to note that LocAgent's accuracy declines below file level and that serialising a graph to text can erase distinctions that are explicit in storage. [secondary: seen only in a search snippet] ([arXiv 2608.13867](https://arxiv.org/abs/2608.13867))

**Embedding retrieval versus agentic search**
- Anthropic (29 Sep 2025): Claude Code uses a hybrid. CLAUDE.md is loaded up front; glob and grep retrieve files just in time, "bypassing the issues of stale indexing and complex syntax trees". Runtime exploration is slower and needs good tools and heuristics. [vendor position] ([Anthropic, Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents))
- Cursor (6 Nov 2025): semantic search raised answer accuracy on its Cursor Context Bench by 12.5% on average (6.5% to 23.5% by model). In an online A/B test code retention rose 0.3% overall and 2.6% on codebases with 1,000 or more files; removing semantic search raised dissatisfied follow-ups by 2.2%. Cursor states grep plus semantic search together is best. [measured-vendor] ([Cursor, semsearch](https://cursor.com/blog/semsearch))
- Cognition (Oct 2025): agent trajectories spent more than 60% of the first turn retrieving context. SWE-grep and SWE-grep-mini are small models trained for retrieval only: up to 8 parallel tool calls per turn, at most 4 turns, tools limited to grep, read and glob. Windsurf exposes this as a "Fast Context" subagent. [measured-vendor / vendor-claim] ([Cognition, SWE-grep](https://cognition.ai/blog/swe-grep), [Windsurf docs](https://docs.windsurf.com/context-awareness/fast-context))
- Augment (talk write-up, 11 Sep 2025): grep and find were enough on SWE-bench's small repositories; embeddings gain value in larger and messier codebases. [secondary] ([jxnl.co](https://jxnl.co/writing/2025/09/11/why-grep-beat-embeddings-in-our-swe-bench-agent-lessons-from-augment/))
- Sourcegraph: Amp's "Librarian" is a search subagent over public and private GitHub code. A quoted Cody statement says embeddings were replaced by Sourcegraph Search in Cody Enterprise. [vendor announcement; Cody quote secondary] ([Amp, Librarian](https://ampcode.com/news/librarian), [jacar.es](https://jacar.es/en/code-agents-without-vector-search/))
- "Is Grep All You Need?" (arXiv 2605.15184, 14 May 2026): on a 116-question LongMemEval sample across a custom harness and Claude Code, Codex and Gemini CLI, grep generally gave higher accuracy than vector retrieval. This is a memory benchmark, not a code benchmark. [measured-peer, preprint; numbers not read] ([summary](https://academy.dair.ai/papers/is-grep-all-you-need))

**What to retrieve, and long context versus retrieval**
- CodeRAG-Bench (arXiv Jun 2024, rev. Feb 2025): high-quality retrieved context helps code generation, but retrievers fail when query and document share little lexical overlap, and generators fail to improve with limited context or when they must integrate several pieces. [measured-peer] ([arXiv 2406.14497](https://arxiv.org/abs/2406.14497))
- "What to Retrieve for Effective Retrieval-Augmented Code Generation?" (arXiv Mar 2025): in-context code and API information help; retrieved "similar code" often adds noise and lowers results by up to 15%. [measured-peer] ([arXiv 2503.20589](https://arxiv.org/pdf/2503.20589))
- "The Limits of Long-Context Reasoning in Automated Bug Fixing" (arXiv Feb 2026): putting all relevant files in context for SWE-bench Verified degraded results sharply; Qwen3-Coder-30B resolved 7% at 64k tokens and GPT-5-nano resolved none. [measured-peer] ([arXiv 2602.16069](https://arxiv.org/html/2602.16069v1))
- Anthropic (29 Sep 2025) names "context rot": recall accuracy falls as the window grows, so context is a finite budget. Sub-agents may spend tens of thousands of tokens and return a 1,000 to 2,000 token summary. [vendor position] ([Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents))

**Pushed repository overviews**
- "Evaluating AGENTS.md" (ETH Zurich, arXiv 12 Feb 2026, presented at ICLR 2026): across several agents and models, context files did not improve task success and raised inference cost by more than 20%. Agents do follow the instructions in them. Repository overviews, though popular and recommended by providers, were not helpful. LLM-generated files helped only when existing documentation was removed. Advice: human-written files should state only minimal requirements. [measured-peer] ([arXiv 2602.11988](https://arxiv.org/abs/2602.11988))

**Code-graph MCP servers (community)**
- A crowded class of tree-sitter to SQLite graph servers exists (CodeGraph, codebase-memory-mcp, cgraphy and others). Self-reported results: CodeGraph 59% fewer tokens, 70% fewer tool calls, 35% lower cost over seven projects with Claude Opus 4.7; codebase-memory-mcp claims about 120x token reduction. None is independently verified, and none reports task success. 2026. [vendor-claim] ([CodeGraph write-up](https://pasqualepillitteri.it/en/news/11125/codegraph-cut-coding-agent-tokens), [codebase-memory-mcp](https://dev.to/deusdata/how-i-cut-my-ai-coding-agents-token-usage-by-120x-with-a-code-knowledge-graph-4a3d))

### Inferences
- A graph is most defensible as a query target for questions grep answers badly: "who depends on X", "what does changing X reach", "which component owns this". These are archplan's `get_blast_radius` and `find`. Evidence for graphs is strongest on localisation (LocAgent, RepoGraph) and weakest on end-to-end resolve rate with frontier models.
- The AGENTS.md study argues against pushing a full `get_architecture` dump into every session. An architecture model should be pulled on demand and scoped (one component, one neighbourhood), and any always-loaded text should hold constraints, not overviews.
- Token-saving claims from graph servers measure cost, not correctness. archplan should evaluate on task outcome (did the agent pick the right files, did it respect a boundary) if it wants evidence of value.
- Freshness is the recurring objection to indexes (Anthropic's "stale indexing"). A model that is cheap to rebuild and reports its own age (commit, timestamp) addresses that objection directly.
- Lower granularity is where graphs lose accuracy (reported for LocAgent). Component and type level, where archplan operates, is the stable end.

### Gaps
- Not verified in this session and therefore not cited: RepoCoder (EMNLP 2023), RepoBench (ICLR 2024), CrossCodeEval (NeurIPS 2023), Long Code Arena, GraphCodeBERT (ICLR 2021), Agentless, Moatless, CodeRAG, Code Graph RAG, Potpie and Greptile's graph design. I found no 2025 or 2026 source giving new measured effects for them.
- No controlled ablation of Aider's repo map was found.
- No public statement from Amp that confirms or denies embedding use; one blog marks this "unconfirmed".
- No independent head-to-head of graph MCP servers against plain agentic search on a task-success metric was found.
- CodexGraph's exact SWE-bench and CrossCodeEval figures were not read.

## 2. Giving models compiler-grade facts (LSP, indexes, types, slices)

### Takeaway
LSP access is now a standard, first-party feature of coding agents, mostly justified by diagnostics after edits and exact reference lookups; there is still no rigorous public benchmark of its effect. For bug finding, more structural context is not automatically better: selected and abstracted data-flow context helps, raw callers and callees often do not.

### Cited Findings
- Claude Code plugins can ship language servers through `.lsp.json` or `lspServers` in the plugin manifest. Fields include `command`, `extensionToLanguage`, `requestTimeout` (default 60,000 ms), and `diagnostics`, which defaults to `true` and pushes diagnostics into context after edits. Docs read Nov 2026. [vendor documentation] ([Claude Code plugin manifest reference](https://code.claude.com/docs/en/plugins-reference))
- LSP support arrived in Claude Code v2.0.74 (Dec 2025) with operations such as goToDefinition, findReferences and hover, plus automatic diagnostics. [secondary] ([how2shout](https://www.how2shout.com/news/claude-code-v2-0-74-lsp-language-server-protocol-update.html), [azukiazusa.dev](https://azukiazusa.dev/en/blog/claude-code-lsp-support))
- Serena (oraios) is an MCP server that exposes LSP-backed symbol search, references, rename and diagnostics; v1.0.0 released 3 Apr 2026, 40+ languages, 22k+ stars. Its tools are typically off by default inside harnesses that already have file and search tools. [secondary] ([GitHub](https://github.com/oraios/serena), [reference note](https://pantheon-org.github.io/agentic-context/references/oraios-serena/))
- A March 2026 developer analysis states that no vendor-neutral benchmark of Serena against Claude Code's native tools exists; its own comparison is qualitative (Serena better at symbol-level exploration and safe refactoring, native tools better at regex search). "Up to 70% token savings" is anecdotal. [secondary, no measurement] ([aledlie report, 28 Mar 2026](https://aledlie.vercel.app/reports/2026-serena-mcp-vs-claude-code-native-benchmark/))
- Monitor-guided decoding (NeurIPS 2023): a static-analysis monitor constrains decoding to type-consistent identifiers. It consistently improves compilation rate; SantaCoder-1.1B with the monitor beats text-davinci-003 on compilation rate and next-identifier match; also evaluated on Java, C# and Rust. [measured-peer] ([arXiv 2306.10763](https://arxiv.org/abs/2306.10763))
- Interprocedural context for vulnerability detection (arXiv Apr 2026): four modern LLMs on 509 ReposVul cases in C, C++ and Python. Function-level context often gave the best balance of accuracy, cost and explanation quality; interprocedural context did not consistently improve detection and sensitivity varied by model and language. [measured-peer] ([arXiv 2604.08417](https://arxiv.org/pdf/2604.08417))
- Context-enhanced vulnerability detection (arXiv Apr 2025): abstracted context built from control-flow, data-flow and call-graph analysis significantly improved detection with GPT-4, DeepSeek and CodeLlama; the best abstraction level depends on the model. [measured-peer] ([arXiv 2504.16877](https://arxiv.org/html/2504.16877v1))
- A related paper (CPRVUL, 2026) reports that appending raw callers, callees and globals did not help and often degraded strong fine-tuned models; selection and structure matter more than inclusion. [measured-peer; seen via search summary] ([arXiv 2602.06751](https://www.arxiv.org/pdf/2602.06751))
- LLM4PFA (arXiv Jun 2025) and a Tencent industrial study (arXiv Jan 2026): keep the analyzer's source-to-sink path and ask the LLM to judge path feasibility. Reported 94 to 98% of false positives removed at high recall, and inspection time cut from 10 to 20 minutes per alarm to seconds. [measured-peer, self-reported] ([arXiv 2506.10322](https://arxiv.org/pdf/2506.10322), [review of arXiv 2601.18844](https://www.themoonlight.io/review/reducing-false-positives-in-static-bug-detection-with-llms-an-empirical-study-in-industry))
- LLMSAN (Wang et al.): treats a data-flow path as a certificate of a bug report that several "sanitizers" can check, giving a cross-checking effect. [measured-peer] ([NSF PAR](https://par.nsf.gov/servlets/purl/10588102))

### Inferences
- The consistent result is "curated facts beat raw neighbourhoods". archplan's pre-computed call sequences and dependency edges are curated facts; they should be returned as short, typed paths (A calls B calls C, with file and line per hop), not as code dumps.
- LSP and archplan are complementary. LSP answers symbol-level questions on demand; archplan answers component-level and cross-language questions that no single language server covers. Overlapping with LSP at symbol level has low marginal value in harnesses that already have it.
- A path is a checkable certificate. Any archplan finding that rests on a dependency or call path should include that path so an agent, or a second deterministic check, can verify each hop.

### Gaps
- No rigorous public benchmark of LSP tools in agents (task success, edit accuracy) was found; the claim that LSP helps agents rests on vendor design choices and anecdote.
- multilspy, SCIP and LSIF: no 2025 or 2026 source with measured effect on agents was found in this session. Sourcegraph's MCP server exposes search and navigation, but I did not confirm that it is SCIP-backed.
- Program slicing for prompts: only the path-feasibility papers above were found; no general study of slice-based prompt construction for agents.

## 3. MCP servers and agent tools for analysis; guidance on tool design

### Takeaway
Published guidance from Anthropic converges on a small number of workflow-level tools, natural-language identifiers, a concise/detailed switch, pagination with steering messages on truncation, and errors that say how to recover. Tool definitions and tool results are both context costs, and clients increasingly defer tool loading or call tools from code, which makes names, descriptions and documented return shapes more important.

### Cited Findings

**Anthropic, "Writing effective tools for agents" (11 Sep 2025)** [vendor guidance, partly measured] ([source](https://www.anthropic.com/engineering/writing-tools-for-agents))
- Build few tools that match high-impact workflows; do not wrap every API endpoint. Consolidate chained operations (for example one `get_customer_context` instead of three lookups).
- Namespace tools with prefixes; prefix versus suffix choice had measurable effects that differ by model.
- Return high-signal fields (names) and omit low-level ones. Resolving UUIDs to natural-language names or short ids reduced hallucination and improved retrieval precision (no figure given).
- Offer a `response_format` enum of `concise` and `detailed`. In the Slack example concise used 72 tokens against 206, about one third. Detailed carries the ids needed for follow-up calls.
- Use pagination, range selection, filtering and truncation with sensible defaults. Claude Code caps tool responses at 25,000 tokens by default. When truncating, tell the agent how to narrow the request.
- Errors should state what was wrong and show a correctly formatted input, not a code or traceback.
- Tool descriptions are prompts: write them as for a new hire, name parameters unambiguously (`user_id`, not `user`). Small description changes produced large gains; this is credited for Claude Sonnet 3.5's SWE-bench Verified result at the time.
- Method: build realistic multi-step evaluation tasks, measure accuracy, runtime, tool-call count, tokens and tool errors, and use held-out tasks. Many redundant calls suggest pagination tuning; many parameter errors suggest unclear descriptions.

**Anthropic, "Effective context engineering" (29 Sep 2025)** [vendor guidance] ([source](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents))
- Keep tool sets minimal with little overlap. If a human cannot say which tool applies, the agent cannot either. Tools should return token-efficient results.

**Anthropic, "Code execution with MCP" (4 Nov 2025)** [vendor guidance, single example] ([source](https://www.anthropic.com/engineering/code-execution-with-mcp))
- Loading all tool definitions and passing every intermediate result through the model wastes context. Presenting servers as code APIs with progressive disclosure cut one example from 150,000 to 2,000 tokens (98.7%). A `search_tools` tool with a detail-level parameter is an alternative. Costs: sandboxing and operations.

**Anthropic, "Advanced tool use" (24 Nov 2025)** [measured-vendor] ([source](https://www.anthropic.com/engineering/advanced-tool-use))
- Five common MCP servers (58 tools) cost about 55K tokens before any work; Anthropic saw 134K internally.
- Tool Search Tool (deferred loading): 85% fewer tokens; accuracy on MCP evaluations rose from 49% to 74% (Opus 4) and 79.5% to 88.1% (Opus 4.5).
- Programmatic Tool Calling: average tokens 43,588 to 27,297 (37% less) on research tasks; small accuracy gains.
- Tool use examples: complex parameter handling accuracy 72% to 90%.
- Implications stated for server authors: distinct descriptive names (search matches on them), compact definitions, precisely documented return formats, input examples for non-obvious parameters, parallel-safe and idempotent tools.

**MCP specification** [standard] ([tools, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools))
- `tools/list` is paginated. Tools may declare `outputSchema` and return `structuredContent` (added in revision 2025-06-18). Tools carry optional `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). A tool may return resource links that the client can fetch later.
- A reported Claude Code bug (2026) dropped all tools from a server that sent `outputSchema`; a later spec revision (2026-07-28) is reported to relax the root-object rule. Both are [secondary] and unverified. ([issue mirror](https://claudeissues.com/issue/25081-bug-mcp-client-silently-drops-all-tools-when-third-party-server-includes-outputs), [draft changelog](https://modelcontextprotocol.io/specification/draft/changelog.md))

**Claude Code hook limits relevant to tool output** [vendor documentation, read Nov 2026] ([hooks reference](https://code.claude.com/docs/en/hooks))
- Hook `additionalContext` is capped at 10,000 characters; longer output is saved to a file and replaced by a path plus a 2,000-character preview. The docs advise writing injected context as factual statements, not imperatives, because instruction-like text can trigger prompt-injection defences.

**Analysis MCP servers**
- Semgrep (12 Nov 2026): hosted MCP server in beta, ten capability areas (findings and triage, fixes, scans, projects, supply chain, rules, reports, workflows, automations, settings), plus a `discover` tool that finds the right tool for a task. Every call uses the platform API with the user's credential and token scope. Earlier local server ran via `semgrep mcp`; the standalone repo was folded into the main binary. No metrics published. [vendor announcement] ([Semgrep blog](https://semgrep.dev/blog/2026/semgrep-mcp-server), [directory note](https://mcpservers.org/th/servers/semgrep/mcp))
- SonarQube: official `SonarSource/sonarqube-mcp-server`, and a managed cloud endpoint reported in April 2026. Snyk: official `snyk/studio-mcp` (Go, Apache 2.0, 11 tools across SAST, SCA, IaC, containers, SBOM). CodeQL alerts are reached through the GitHub MCP server. [secondary] ([dev.to overview](https://dev.to/grove_chatforest/code-security-mcp-servers-snyk-sonarqube-semgrep-trivy-codeql-and-beyond-4nln), [chatforest review](https://chatforest.com/reviews/security-scanning-mcp-servers/))
- Joern: `codebadger` (also listed as joern-mcp) is a containerised MCP server over Joern's code property graph; needs Docker and a running Joern server. [open-source README] ([GitHub](https://github.com/Lekssays/codebadger))
- Sourcegraph publishes an MCP server for code search, navigation and a Deep Search endpoint. [vendor] ([Sourcegraph MCP](https://sourcegraph.com/mcp))

### Inferences
- archplan's tool list (`get_architecture`, `get_findings`, `get_blast_radius`, `get_hotspots`, `find`, `get_item` and more) is workflow-level, which matches the guidance. The risks are count and overlap: about 25 tools is past the point where deferred loading helps, so each tool's name and first description sentence must be distinct enough to be found by tool search.
- Concrete interface properties the guidance supports: (a) stable, human-readable ids (component path or qualified name, not opaque hashes) accepted by every tool; (b) a concise default with a `detail` or `response_format` switch; (c) `limit` and cursor on every list, with a truncation line that says how to narrow; (d) errors that list the nearest valid ids; (e) documented return shapes, since Codex and Claude may call tools from code; (f) read-only annotations on query tools.
- A 25,000-token response cap in Claude Code means `get_architecture` for a large repository must be scoped or summarised by default.
- Semgrep's `discover` tool and Anthropic's `search_tools` suggest that large servers add a discovery step. A smaller alternative for archplan is fewer, more general tools.
- Findings and annotations returned by a tool are untrusted text from the model's view. Phrase them as facts with evidence, not instructions.

### Gaps
- OpenAI's own guidance could not be read: `openai.com/index/harness-engineering` returned HTTP 403, and searches did not return OpenAI's function-calling best-practice text or Codex's MCP output limits. Only secondary summaries were available (see section 6).
- No verified source for GhidraMCP or a GitHub-published CodeQL MCP server was found in this session.
- No measured comparison of analysis MCP servers (finding quality when driven by an agent) was found.
- Anthropic's posts give few numbers for the effect of individual design choices (identifier format, error text); the direction is stated, the size is not.

## 4. Presenting findings to an LLM

### Takeaway
The best-documented finding format is GitHub's Autofix prompt: rule rationale with an example, alert message and location, the full flow path, and line-numbered snippets around every path step. For triage, agents that can read the codebase beat single prompts by a wide margin with strong models, and the reliable controls against hallucinated findings are deterministic re-checks and execution, not prompt wording.

### Cited Findings
- GitHub Copilot Autofix prompt (14 Feb 2024, updated 7 Apr 2025) contains: general background on the vulnerability type with a sample and its fix, taken from CodeQL query help; the alert message and location; the flow path for data-flow issues; other locations named in the message; code snippets around each flow-path step and referenced location, with imports and top-level definitions, merged per file, trimmed to a target length, and with line numbers added. The model may edit only code present in the prompt. [vendor engineering post] ([GitHub blog](https://github.blog/engineering/platform-security/fixing-security-vulnerabilities-with-ai/))
- Same source, output and checks: a strict three-part format (instructions, before/after blocks, needed dependencies). Before/after blocks replaced diffs because the model was poor at line-number arithmetic. Post-processing does fuzzy matching, syntax parsing, name resolution and type checks, and verifies new dependencies exist in the registry and have no known vulnerabilities.
- Same source, evaluation: 1,400+ alerts with test coverage from 63 CodeQL queries. A fix counts only if the alert disappears, no new alert appears, there is no syntax error, and no test outcome changes. Iterating against this harness tripled the success rate and cut compute sixfold. [measured-vendor]
- "Sifting the Noise" (ISSTA 2026, arXiv Jan 2026): given a static warning and codebase access, Aider, OpenHands and SWE-agent were compared with plain prompting. On the OWASP Benchmark the false-positive rate fell from above 92% to 6.3% in the best configuration. On CodeQL alerts in real Java projects the best configuration identified up to 93.3% of false positives. On a post-cutoff OSS-Fuzz set, SWE-agent with Claude Sonnet 4 identified 95.5% of false positives at 95.5% precision, against 36.4% for plain prompting. Agents help little with weaker models; aggressive suppression can hide true vulnerabilities; cost varies widely by framework. [measured-peer] ([arXiv 2601.22952](https://arxiv.org/abs/2601.22952))
- SAST-Genius (arXiv Sep 2025): Semgrep finding plus code snippet, data-flow path and surrounding context in a structured JSON prompt to a fine-tuned model cut false positives from 225 to 20 and triage time by 91%. [measured-peer, self-reported, small scale] ([arXiv 2509.15433](https://arxiv.org/pdf/2509.15433))
- Semgrep Assistant (Jan 2025): agreement with Semgrep's security researchers was about 55% in early tests, then 96% on true positives but 41% on false positives after iteration. The asymmetry is deliberate: it is tuned to keep a false positive sooner than drop a true positive. Later posts report 95% user agreement with auto-triage decisions and about 60% of new SAST findings auto-triaged (Sep 2025). "Memories" reuse human triage decisions to suppress repeats. [measured-vendor] ([Semgrep, 96%](https://semgrep.dev/blog/2025/building-an-appsec-ai-that-security-researchers-agree-with-96-of-the-time/), [Semgrep, 60%](https://semgrep.dev/blog/2025/semgrep-is-confidently-handling-60-of-all-triage-for-users-without-reducing-coverage), [Semgrep, memories](https://semgrep.dev/blog/2025/announcing-ai-noise-filtering-and-triage-memories))
- Cursor Bugbot (15 Jan 2026): the first design ran eight parallel passes with shuffled diff order, kept findings by majority vote, merged them, and passed them to a validator model. Self-consistency by voting helped; the later move to a single agent helped more. [measured-vendor] ([Cursor](https://cursor.com/blog/building-bugbot))
- Anthropic's `claude-code-security-review` action: findings carry severity and remediation, then pass a separate false-positive filter step that accepts custom instructions. The README warns it is not hardened against prompt injection and should review trusted PRs only. [vendor README] ([GitHub](https://github.com/anthropics/claude-code-security-review))
- Hallucinated findings in the wild: curl's maintainer reported that none of 20 AI-assisted reports in one period were real vulnerabilities, with invented function names and generic `strcpy` claims as typical patterns; curl ended its HackerOne bounty with effect from 1 Feb 2026. [secondary news reports] ([The New Stack](https://thenewstack.io/curl-fights-a-flood-of-ai-generated-bug-reports-from-hackerone/), [Hackaday, 26 Jan 2026](https://hackaday.com/2026/01/26/the-curl-project-drops-bug-bounties-due-to-ai-slop))

### Inferences
- A finding meant for an agent should carry, in this order of evidence: rule id with a one-paragraph rationale and a good/bad example; message; primary location with file and line; the evidence path (dependency chain or call sequence) with file and line per step; a short line-numbered snippet; and a machine-checkable "resolved when" condition. This mirrors Autofix and the path-as-certificate papers.
- "Resolved when the detector no longer fires and no new finding appears" is the strongest verification archplan can offer, and it is deterministic. Exposing re-check as a tool (refresh, then `get_findings` filtered to the same stable finding id) gives the agent the same loop GitHub used to triple fix success.
- Stable finding ids matter for this loop and for memory of human triage (Semgrep's "memories", archplan's `label_finding`). Ids should survive line shifts.
- Asking the model for line citations is useful only if something checks them. Since archplan owns the model, it can validate any file, line or symbol an agent cites back.
- Confidence fields have no measured support in the sources read. Severity and the conservative bias (prefer keeping a real finding) do.
- Voting and validator stages are cheap ways to cut noise when no deterministic check exists; an agent with code access is better when the model is strong.

### Gaps
- No study was found that feeds SARIF itself to an LLM and ablates its fields. The evidence on "which fields matter" is indirect (Autofix design, SAST-Genius, path-feasibility work).
- No controlled evidence was found for multi-agent debate, for requiring an exploit or reproducing test as proof, or for line-citation requirements as hallucination controls in code review. Products describe verification steps without ablations.
- No measured effect of confidence scores or fix examples in the prompt was found.
- The CR-Bench paper (arXiv 2603.11078, Mar 2026) on signal-to-noise in review agents was found but not read.

## 5. AI code review and agentic security review products

### Takeaway
During 2025 and 2026 the leading review products moved from fixed pipelines to agents that fetch their own context, usually followed by a verification stage and sometimes combined with deterministic analyzers. Published precision figures are mostly vendor-measured and disagree across benchmarks; the one in-the-wild academic study finds that most review agents produce more noise than signal.

### Cited Findings
- Anthropic Code Review for Claude Code (9 Mar 2026): parallel agents search for bugs, a verification step filters false positives, findings are ranked by severity and posted as one overview plus inline comments; depth scales with PR size. On Anthropic's own code: PRs with substantive comments rose from 16% to 54%; 84% of PRs over 1,000 lines get findings (7.5 on average); 31% of PRs under 50 lines (0.5 on average); fewer than 1% of findings marked incorrect by engineers; about 20 minutes and USD 15 to 25 per review. [measured-vendor, internal use] ([Claude blog](https://claude.com/blog/code-review))
- GitHub Copilot code review (5 Mar 2026): generally available on an agentic tool-calling architecture that fetches related code, directory structure and references as needed; runs on GitHub Actions. The linked post title cites 60 million reviews. Since Oct 2025 it combines LLM detection with deterministic tools; CodeQL is on by default, ESLint and PMD are off by default. [vendor changelog] ([agentic architecture](https://github.blog/changelog/2026-03-05-copilot-code-review-now-runs-on-an-agentic-architecture/), [Oct 2025 preview](https://github.blog/changelog/2025-10-28-new-public-preview-features-in-copilot-code-review-ai-reviews-that-see-the-full-picture/), [docs](https://docs.github.com/en/copilot/concepts/agents/code-review))
- Cursor Bugbot (15 Jan 2026): pipeline (eight passes, vote, validator, dedupe) replaced in autumn 2025 by a full agent. Resolution rate (share of flagged bugs the author fixed before merge, judged by an LLM) rose from 52% to above 70%; bugs flagged per run from 0.4 to 0.7; over two million PRs per month; 40 major experiments. Restraining the model to cut false positives backfired once agentic; dynamic context and tool-design iteration helped most. [measured-vendor] ([Cursor](https://cursor.com/blog/building-bugbot))
- Augment's benchmark (11 Dec 2025, updated 18 Jun 2026), 50 PRs across Sentry, Grafana, Cal.com, Discourse and Keycloak, golden comments corrected by Augment. Precision / recall / F-score: Augment 65/55/59, Cursor Bugbot 60/41/49, Greptile 45/45/45, Codex Code Review 68/29/41, CodeRabbit 36/43/39, Claude Code 23/51/31, GitHub Copilot 20/34/25. The post's chart differs slightly from its table. [measured-vendor, by the top-ranked vendor] ([Augment](https://www.augmentcode.com/blog/we-benchmarked-7-ai-code-review-tools-on-real-world-prs-here-are-the-results))
- Greptile's own benchmark (2025): catch rate Greptile 82%, Bugbot 58%, Copilot 54%, CodeRabbit 44%; false positives were not scored. [measured-vendor] ([Greptile](https://greptile.com/benchmarks))
- Martian's Code Review Bench is reported to rank Greptile first at 60.8% F1 (76.2% precision) and CodeRabbit at 57.5%; its independence is disputed. [secondary] ([softwareseni](https://www.softwareseni.com/how-ai-code-review-tools-compare-on-real-bug-detection-and-noise))
- "From Industry Claims to Empirical Reality" (MSR 2026, arXiv 3 Apr 2026): 60.2% of closed agent-only-reviewed PRs fall in the 0 to 30% signal range and 12 of 13 review agents average below 60% signal. Agent-only review is reported to show lower merge and higher abandonment rates than human review. [measured-peer; dataset details via secondary summary] ([arXiv 2604.03196](https://arxiv.org/abs/2604.03196))
- Google AutoCommenter (arXiv May 2024): LLM flags best-practice violations in review; about 40% of its comments were resolved by developers. [measured-peer, industrial] ([arXiv 2405.13565](https://arxiv.org/html/2405.13565v1))
- Meta MetaMateCR (arXiv Jul 2025): AI fixes for reviewer comments; the fine-tuned model reached 68% offline exact match and a 19.75% actionable-to-applied rate in production, about 9 points above GPT-4o. [measured-peer, industrial] ([arXiv 2507.13499](https://arxiv.org/abs/2507.13499))
- Copilot Autofix: beta data reported median remediation time of 28 minutes against 90 minutes manually (2024). No merge-rate figure is published. [vendor-claim via press] ([TechTarget](https://www.techtarget.com/cybersecurity/news/366603045/GitHub-Copilot-Autofix-tackles-vulnerabilities-with-AI?amp=1))
- Semgrep Assistant figures are in section 4. A third-party article says the AI layer was renamed "Multimodal" on 19 Mar 2026. [secondary] ([Augment tools page](https://www.augmentcode.com/tools/semgrep-ai-code-review))
- Snyk Agent Fix (formerly DeepCode AI Fix) offers up to five fixes per finding and rescans to confirm; no production merge rate published. Sonar reports a 3.2% false-positive rate over 137 million reviewed issues in tuned deployments. ZeroPath claims 75% fewer false positives and twice the real findings of traditional SAST, with exploitability validation in the scan. Corgea describes scan, agent analysis with codebase context, fix, PR. All four are vendor or competitor statements. [vendor-claim / secondary] ([Pixee comparison](https://www.pixee.ai/blog/best-automated-remediation-tools-2026), [tech-insider](https://tech-insider.org/snyk-vs-checkmarx-vs-sonarqube-sast-2026/), [ZeroPath](https://zeropath.com/articles/code-security-platforms-reducing-false-positives), [Corgea](https://corgea.com/blog/top-ai-sast-tools-for-2026))

### Inferences
- The common architecture is: agent with tools gathers context, candidate findings pass a verification stage, deterministic analyzers add high-precision signals. A tool like archplan fits two slots: a context tool the review agent calls (blast radius, ownership, boundary rules) and a deterministic detector whose findings need no LLM verification.
- Resolution or acceptance rate (did the author act on it) is the metric vendors converge on. archplan can measure the same thing for its findings through `label_finding` and through findings that disappear after a change.
- Precision numbers are not comparable across sources. Claude Code scores 23% precision in Augment's benchmark and "under 1% marked incorrect" in Anthropic's internal data; the metrics, populations and judges differ.
- Bugbot's experience suggests that over-restricting an agent to avoid noise costs recall; filtering after generation works better than suppressing during it.

### Gaps
- Qodo, Graphite and CodeRabbit: no primary architecture post or measured result was read in this session.
- Google's "resolving code review comments" figure (about 7% of comments) was seen only in a newsletter summary and is not cited as a finding.
- Sonar AI CodeFix, Snyk, ZeroPath and Corgea publish no independently verified precision or acceptance figures that I could find.
- Anthropic's "Claude Code Security" research preview: no primary source was found in this session.
- Microsoft's reported "one third of 1.5M AI review comments not useful" was seen only on an aggregator page and is not cited as a finding.

## 6. Feedback loops and guardrails for agents

### Takeaway
Deterministic feedback (linters, type checkers, tests, static analysis) fed back into the agent loop measurably improves generated code, and enforced rules outperform written instructions. Studies of AI-generated code show persistent security and quality gaps, which is the motivation for such guardrails.

### Cited Findings
- "Static Analysis as a Feedback Loop" (arXiv Aug 2025): Bandit and Pylint results fed back to the LLM over up to ten iterations cut security issues from over 40% to 13%, readability violations from over 80% to 11%, and reliability warnings from over 50% to 11%. [measured-peer] ([arXiv 2508.14419](https://arxiv.org/abs/2508.14419v1))
- An earlier study with Bandit in a similar loop reported a 30.8% reduction in issues flagged by CodeQL; a Jan 2025 study on C found LLMs fix flawed code well when given failed tests or vulnerability reports. [measured-peer; seen via search summary] ([arXiv 2508.14419 related work](https://www.alphaxiv.org/abs/2508.14419.md))
- Claude Code hooks (docs read Nov 2026): events include `PreToolUse`, `PostToolUse`, `PostToolBatch`, `Stop`, `SubagentStop`, `SessionStart`, `UserPromptSubmit`, `FileChanged`. Exit code 2 feeds stderr to Claude: it blocks the call on `PreToolUse`, prevents stopping on `Stop`, and is shown to Claude after the fact on `PostToolUse`. JSON output supports `decision: "block"` with a reason and `additionalContext`. Matchers address MCP tools as `mcp__<server>__<tool>` and plugin servers as `mcp__plugin_<plugin>_<server>__<tool>`. [vendor documentation] ([hooks reference](https://code.claude.com/docs/en/hooks))
- Claude Code LSP plugins push diagnostics into context after every edit by default (`diagnostics: true`). [vendor documentation] ([plugin manifest reference](https://code.claude.com/docs/en/plugins-reference))
- A community playbook warns that agents may edit linter configuration to silence rules, so a lint hook needs a guard on the config files. [secondary, anecdotal] ([proxify playbook](https://agentic.proxify.io/setup/hooks-playbook.md))
- OpenAI "Harness engineering" (11 Feb 2026), as summarised by third parties: AGENTS.md kept to about 100 lines as a table of contents into a `docs/` tree; a fixed layering (Types, Config, Repo, Service, Runtime, UI) with forward-only dependencies enforced by custom linters and structural tests; lint error messages written as remediation instructions for the agent; background agents that scan for violations and open refactoring PRs. [secondary; primary returned HTTP 403] ([OpenAI post](https://openai.com/index/harness-engineering/), [summary](https://www.ignorance.ai/p/the-emerging-harness-engineering))
- "Evaluating AGENTS.md" (arXiv Feb 2026, ICLR 2026): agents follow instructions in context files, but the files did not raise success rates and cost over 20% more; unnecessary requirements make tasks harder. [measured-peer] ([arXiv 2602.11988](https://arxiv.org/abs/2602.11988))
- GitHub Autofix evaluation uses the analyzer itself plus the repository's tests as the acceptance gate (section 4). [measured-vendor] ([GitHub blog](https://github.blog/engineering/platform-security/fixing-security-vulnerabilities-with-ai/))
- Veracode (Spring and Summer 2026 updates): average security pass rate of AI-generated code is about 56% while syntax pass rate is near 100%; best model 68% (GPT-5.5), then 62% (GPT-5.3-Codex, Claude Opus 4.8); Python 63%, Java 30%; larger and coding-specific models show little advantage. [measured-vendor] ([Veracode](https://veracode.com/blog/spring-2026-genai-code-security), [SD Times](https://sdtimes.com/agentic-security/veracode-finds-ai-generated-code-security-has-barely-improved-since-last-year/))
- CodeRabbit (17 Dec 2025): in 470 open-source PRs (320 labelled AI co-authored, 150 human), AI PRs had 10.83 issues against 6.45 (1.7x); logic and correctness 1.75x, maintainability 1.64x, security 1.57x, performance 1.42x; readability over 3x. Authorship labels are heuristic and the vendor sells review. [measured-vendor] ([CodeRabbit](https://www.coderabbit.ai/blog/state-of-ai-vs-human-code-generation-report), [The Register](https://www.theregister.com/2025/12/17/ai_code_bugs/))

### Inferences
- Architecture rules work as agent constraints when they are executable: a detector that fails with a message naming the violated rule, the offending edge, and the allowed alternatives. This is the "fitness function" pattern, and archplan's detectors plus a target architecture are a direct fit.
- The two integration points with evidence behind them are a `PostToolUse` hook (inform after an edit) and a `Stop` hook (do not finish while new violations exist). Both can call archplan and return only findings introduced by the session's changes. Reporting only new findings avoids flooding context with existing debt.
- Finding messages should double as remediation instructions (OpenAI's practice), stated as facts about the code and the rule, which also satisfies Claude Code's advice on injected context.
- Instructions in AGENTS.md or CLAUDE.md are followed but do not improve outcomes on their own and cost tokens. For archplan this favours a short pointer ("architecture rules are enforced by archplan; call X before adding a cross-component dependency") over an embedded architecture description.
- Since agents may disable checks, archplan's rule and target files should be treated as protected, with changes routed through the human (which matches its editor-approval design).

### Gaps
- No controlled study of hooks or of architecture-rule enforcement on agent outcomes was found; support is from practitioner reports and the static-analysis feedback papers.
- OpenAI's harness engineering post was not readable directly, so its numbers (code volume, PR counts, team size) are not reported here. A claim of "88 AGENTS.md files" appears in one weak secondary source and conflicts with the single-map description.
- No 2026 academic measurement of architectural erosion or dependency violations in agent-written code was found; the quality evidence is about bugs, security and maintainability issues per PR.
- Veracode's and CodeRabbit's figures are vendor studies; a rival study cited by The Register reached different conclusions on maintainability and was not read.
