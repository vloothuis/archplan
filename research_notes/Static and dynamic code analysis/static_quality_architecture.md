# Static analysis for code quality, maintainability and architecture (state: November 2026)

Reading note: items marked **[bk]** come from the researcher's background knowledge (cutoff mid-2026) and cite the canonical primary URL, but the page was not re-fetched in this session. Unmarked items were confirmed by a search or fetch on 2026-11-17. Figures marked "derived" were computed from counts in the source.

## 1. Which parsing and program-representation foundations matter?

### Takeaway
Tree-sitter (syntax) and SCIP (resolved symbols and references) are the two actively maintained, permissively licensed foundations that cover Python, TypeScript and Go; Elixir has a tree-sitter grammar but no SCIP indexer. Stack graphs are archived, Kythe is in reduced maintenance, and the graph-database style systems (CodeQL, Joern, Glean, Soufflé) are heavy runtimes whose query ideas are easier to imitate than to embed.

### Cited Findings

Tree-sitter and LSP
- Tree-sitter is an incremental parser generator with error recovery, MIT licensed, with a C runtime and bindings for many languages. **[bk]** ([tree-sitter repo](https://github.com/tree-sitter/tree-sitter))
- An official Elixir grammar exists (`elixir-lang/tree-sitter-elixir`), alongside the grammars for Python, TypeScript and Go. **[bk]** ([tree-sitter-elixir](https://github.com/elixir-lang/tree-sitter-elixir))
- `tree-sitter-graph`, the DSL that builds graphs from tree-sitter parse trees, was not archived as of an ecosyste.ms record synced September 2025; its last push was 2024-12-11. ([ecosyste.ms](https://issues.ecosyste.ms/hosts/GitHub/repositories/tree-sitter%2Ftree-sitter-graph))
- The Language Server Protocol defines call-hierarchy and type-hierarchy requests, so a language server can answer "who calls this" per symbol, but it is a query protocol, not a bulk export format. **[bk]** ([LSP specification](https://microsoft.github.io/language-server-protocol/))

SCIP and LSIF
- SCIP is a Protocol Buffers based, language-agnostic index format that Sourcegraph introduced as the successor to LSIF. ([Sourcegraph announcement](https://sourcegraph.com/blog/announcing-scip))
- The SCIP repository now lives in a `scip-code` GitHub organisation with a project site at scip-code.org, under Apache-2.0. The page does not state who governs it. ([scip-code/scip](https://github.com/scip-code/scip))
- Listed indexers: scip-java (Java, Scala, Kotlin), scip-typescript (TypeScript, JavaScript), rust-analyzer, scip-clang (C, C++), scip-ruby, scip-python, scip-dotnet (C#, VB), scip-dart, scip-php. No Elixir indexer is listed. ([scip-code/scip](https://github.com/scip-code/scip))
- Sourcegraph's documentation lists scip-typescript, scip-python and scip-go as generally available. The page is undated. ([Sourcegraph precise code navigation](https://sourcegraph.com/docs/code-search/code-navigation/precise_code_navigation))

Stack graphs
- `github/stack-graphs` was archived by its owner on 2025-09-09 and is read-only. ([Wayback snapshot of the repo](https://proxy-ga.blitzz.co/proxy/123456/web.archive.org/web/20260601045401/https:/github.com/github/stack-graphs); [ecosyste.ms](https://issues.ecosyste.ms/hosts/GitHub/repositories/github%2Fstack-graphs))
- Stack graphs resolve names file-incrementally, without a build, by path finding in a graph built from tree-sitter trees. The design is described in a 2022 paper. ([Creager and van Antwerpen, arXiv 2211.01224](https://www.arxiv.org/pdf/2211.01224))

Code property graphs, CodeQL, Glean, Kythe, srcML, Datalog
- Joern is Apache-2.0 and was last pushed in April 2026 per a metadata listing. Its docs rate frontends for C/C++ and Java as "Very High" maturity, and JavaScript and Python as "High". ([ecosyste.ms Joern record](https://awesome.ecosyste.ms/projects/github.com%2Fjoernio%2Fjoern); [Joern docs](https://docs.joern.io/))
- The code property graph merges the abstract syntax tree, control-flow graph and program-dependence graph into one queryable graph (Yamaguchi et al., IEEE S&P 2014). **[bk]** ([paper](https://www.sec.cs.tu-bs.de/pubs/2014-ieeesp.pdf))
- The CodeQL CLI is under GitHub's proprietary terms. Free use is tied to codebases released under an OSI-approved licence; other use needs a commercial licence. The query sources in `github/codeql` are MIT. ([GitHub CodeQL licence](https://securitylab.github.com/tools/codeql/license); [ScanCode entry "Proprietary Free"](https://scancode-licensedb.aboutcode.org/github-codeql-terms-2020.html))
- Meta's Glean stores "facts" about code and is queried with its own language; Meta open-sourced it in 2021 and described its production use in a December 2024 post. ([Meta engineering blog](https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/))
- Kythe is Apache-2.0 with indexers for C++, Go and Java. A Wikipedia mirror states that Google laid off the US Kythe team in April 2024 and moved maintenance to a team in India. This is a secondary source. ([Wikipedia: Google Kythe](https://en.wikipedia.com/wiki/Google_Kythe))
- srcML wraps source code in XML that preserves all text, and supports C, C++, C# and Java only. GPL licensed. **[bk]** ([srcml.org](https://www.srcml.org/))
- Soufflé compiles Datalog to parallel C++ and is the engine behind the Doop points-to framework. Universal Permissive Licence. **[bk]** ([Soufflé](https://souffle-lang.github.io/))

### Inferences
- For a zero-dependency Python tool the realistic options are: (a) keep per-language extractors and borrow tree-sitter query patterns as a specification, (b) optionally ingest a SCIP index when the user has one, since SCIP gives compiler-accurate definitions and references for Python, TypeScript and Go. SCIP is protobuf, so ingestion needs either a small hand-written decoder or an optional dependency.
- SCIP carries symbols and occurrences, not call edges or component boundaries. A call graph would have to be derived by mapping reference occurrences to their enclosing definition.
- Stack graphs are not a safe dependency after the 2025 archive, but the idea (file-local graph fragments joined later) fits an incremental, cached model export.
- Datalog is a natural formulation for archplan detectors (cycles, layer violations and reachability are recursive relations). Imitating the rule style with plain Python set operations is more realistic than bundling Soufflé or CodeQL.
- CodeQL's licence rules out embedding it in a tool that is run on private code.

### Gaps
- Latest release versions and dates for scip-python, scip-typescript and scip-go were not confirmed. The governance of the `scip-code` organisation is not stated on its page.
- Current upstream state of `kythe/kythe` and `facebookincubator/Glean`, and the current list of open-source Glean indexers, were not verified on GitHub directly.
- Whether `tree-sitter-graph` has had commits after December 2024 is unknown.
- No source was found for an Elixir SCIP or LSIF indexer.

## 2. Which quality and architecture tools exist, and what do they detect?

### Takeaway
Tools split into three groups: rule linters that work per file (Ruff, ESLint, Pylint, Credo, staticcheck, PMD, Semgrep), architecture-conformance checkers that compare imports to declared rules (import-linter, Tach, dependency-cruiser, ArchUnit, Elixir Boundary, and since 2026 SonarQube's Architecture feature), and architecture-smell or behavioural analysers (Arcan, Designite, DV8, CodeScene, Sonargraph, NDepend). Only the last group combines structure with history, and almost none of it covers Go or Elixir.

### Cited Findings

Conformance checkers (declared rules against actual dependencies)
- import-linter (Python) offers contract types `forbidden`, `independence`, `layers`, a newer acyclic-siblings type, and custom contracts. It builds its import graph with the `grimp` library. ([import-linter contract types](https://import-linter.readthedocs.io/en/stable/contract_types/))
- Tach (Python, written in Rust, MIT) enforces declared module dependencies, public interfaces, layers and "no cycles", and can emit a JSON dependency map. The original `gauge-sh/tach` went unmaintained and was transferred to a `tach-org` organisation with a new maintainer; the repo is not archived. ([tach-org/tach](https://github.com/tach-org/tach); [dtach fork note](https://github.com/DetachHead/dtach))
- dependency-cruiser (JavaScript and TypeScript, MIT) validates `forbidden`, `allowed` and `required` rules, including `no-circular`, `no-orphans`, and a `moreUnstable` rule that applies Martin's Stable Dependencies Principle using computed instability. **[bk]** ([rules reference](https://github.com/sverweij/dependency-cruiser/blob/main/doc/rules-reference.md))
- ArchUnit (Java bytecode, Apache-2.0) expresses layer, onion and slice-cycle rules as unit tests and has "frozen" rules that record existing violations as a baseline. **[bk]** ([ArchUnit user guide](https://www.archunit.org/userguide/html/000_Index.html))
- pytestarch brings ArchUnit-style rules to Python tests. **[bk]** ([pytestarch](https://github.com/zyskarch/pytestarch))
- Boundary (Elixir) is a Mix compiler that groups modules into named boundaries with exports and allowed dependencies, and reports forbidden cross-boundary calls as compiler warnings. It can also restrict calls to third-party apps. ([sasa1977/boundary](https://www.github.com/sasa1977/boundary))
- deptry (Python, MIT) checks declared against used third-party packages: missing, unused, transitive and misplaced dev dependencies. **[bk]** ([deptry docs](https://deptry.com/))
- jQAssistant scans artefacts into Neo4j and expresses constraints as Cypher queries. GPL-3.0, JVM-centred. **[bk]** ([jQAssistant](https://jqassistant.org/))

SonarQube
- Sonar deprecated its earlier Java-only cycle detection and "architecture as code" features in 2025, with removal planned for January 2026. ([SonarQube Server 2025.6 docs](https://docs.sonarsource.com/sonarqube-server/2025.6/design-and-architecture/cycle-detection))
- The replacement, "Architecture", recovers the current architecture from code and compares it with an intended architecture defined by tech leads. Deviations are structural (code in the wrong place) or relational (a relationship that should not exist). Anything not explicitly allowed between modelled components is forbidden. ([SonarQube Server architecture docs](https://docs.sonarsource.com/sonarqube-server/architecture))
- Supported languages in SonarQube Server 2026.4: Java, C#, JavaScript, TypeScript, Python. It is in commercial editions only, not Community Build. ([Sonar community announcement](https://community.sonarsource.com/t/architecture-analysis-in-sonarqube-server-2026-4/186120))
- Sonar describes tangle detection that works without configuration. ([Sonar blog](https://www.sonarsource.com/blog/introducing-architecture-in-sonarqube/))
- Sonar acquired Structure101 in October 2024, which is the origin of this capability. **[bk]** ([Sonar press release](https://www.sonarsource.com/company/press-releases/sonar-acquires-structure101/))

CodeQL and GitHub Code Quality
- From June 2025 CodeQL quality queries are categorised as `reliability` or `maintainability`, with sub-tags such as readability, useless-code, complexity, performance, correctness, error-handling and concurrency. ([CodeQL CLI 2.22.1 changelog](https://codeql.github.com/docs/codeql-overview/codeql-changelog/codeql-cli-2.22.1/))
- GitHub Code Quality reached general availability on Enterprise Cloud and Team plans, with maintainability and reliability scores and quality-gate rulesets. ([InfoQ, August 2026](https://infoq.com/news/2026/08/github-code-quality))

Per-file linters (all **[bk]** unless noted)
- Ruff (Python, MIT, Rust) reimplements rules from Flake8, Pylint and others, including McCabe complexity (`C901`) and Pylint's "too many" design checks. It analyses one file at a time. ([Ruff rules](https://docs.astral.sh/ruff/rules/))
- Pylint (GPL-2.0) is one of the few Python linters with cross-module checks: `cyclic-import` (R0401) and `duplicate-code` (R0801). ([Pylint messages](https://pylint.readthedocs.io/en/stable/user_guide/messages/messages_overview.html))
- Credo (Elixir, MIT) groups checks into consistency, design, readability, refactor and warning. It includes cyclomatic complexity, ABC size, nesting depth and duplicated-code checks. ([Credo](https://hexdocs.pm/credo/overview.html))
- staticcheck (Go, MIT) provides correctness, simplification and unused-code (`U1000`) checks. golangci-lint (GPL-3.0) aggregates it with gocyclo, gocognit, dupl, depguard and others. ([staticcheck](https://staticcheck.dev/docs/checks/); [golangci-lint linters](https://golangci-lint.run/usage/linters/))
- PMD (BSD-style) covers Java and a dozen other languages with design rules (God Class, Law of Demeter, coupling counts) and ships the CPD clone detector. ([PMD](https://pmd.github.io/))
- Semgrep matches syntactic patterns across 30+ languages. In December 2024 Semgrep moved its maintained rules to a restrictive licence and renamed the open engine "Community Edition"; a consortium forked the engine as Opengrep in January 2025. ([Semgrep announcement](https://semgrep.dev/blog/2024/important-updates-to-semgrep-oss/); [Opengrep](https://www.opengrep.dev/))

Architecture-smell and behavioural analysers
- Arcan detects Cyclic Dependency, Unstable Dependency and Hub-Like Dependency on a dependency graph, and was used in industrial studies on C/C++ at ASML. ([Sas et al., EMSE 2022](https://link.springer.com/article/10.1007/s10664-022-10132-7))
- DV8 (Drexel, commercialised) detects six history-aware architecture anti-patterns, among them Unstable Interface, Crossing, Clique and Package Cycle. Its default Unstable Interface rule: a file on which at least 1% of project files depend and which co-changed at least twice with at least 10 of them. ([Mo et al., TSE 2019](https://www.cs.drexel.edu/~yc349/papers/2019/tse2019.pdf))
- CodeScene (commercial) scores files with a "Code Health" metric that aggregates about 25 design properties into Green, Yellow and Red, and combines it with hotspots and change coupling from git history. ([Tornhill and Borg, arXiv 2203.04374](https://arxiv.org/abs/2203.04374v1))
- Tornhill's open-source `code-maat` computes churn, change coupling and author metrics from version-control logs. GPL-3.0. **[bk]** ([code-maat](https://github.com/adamtornhill/code-maat))
- Designite (Java, C#; a Python variant "DPy" exists) detects architecture smells such as cyclic dependency, unstable dependency, god component, feature concentration, scattered functionality and dense structure, plus design and implementation smells. **[bk]** ([Designite](https://www.designite-tools.com/))
- NDepend (.NET, commercial) provides a dependency structure matrix, Martin's metrics and LINQ-based code rules. **[bk]** ([NDepend](https://www.ndepend.com/))
- Emerge (MIT, Python) builds dependency graphs for a dozen languages, computes fan-in, fan-out and Louvain modularity, and renders an interactive graph. **[bk]** ([glato/emerge](https://github.com/glato/emerge))

### Inferences
- archplan's combination (structure model plus change coupling plus hotspots, across Python, TypeScript, Go and Elixir) overlaps most with DV8 and CodeScene, both commercial, and neither supports Elixir. The open-source field has no direct equivalent.
- SonarQube's 2026 Architecture feature is the closest mainstream product to archplan's "current versus target" model. Its vocabulary (structural versus relational deviation, allow-list semantics for modelled components, tangles without configuration) is worth mirroring so users can map concepts.
- Features worth imitating: ArchUnit's frozen-violation baseline (adopt rules on a legacy codebase without failing on old violations); dependency-cruiser's `moreUnstable` rule (cheap to compute from the existing dependency graph); import-linter's distinction between direct and indirect forbidden imports; DV8's Unstable Interface rule, which only needs fan-in plus co-change counts that archplan already has.
- Exporting to or importing from import-linter, Tach and dependency-cruiser rule formats would let archplan reuse boundaries that teams have already declared.

### Gaps
- Sonargraph's and Designite's current language lists and prices were not verified. Whether Sonargraph supports Go or TypeScript in 2026 is unconfirmed.
- Arcan's current commercial status and language coverage were not verified.
- Latest release dates for Tach, import-linter and dependency-cruiser were not retrieved.
- A report that OpenAI agreed to acquire Astral (the maker of Ruff) in 2026 could not be verified in this session; treat Ruff's governance as unconfirmed.
- One Sonar 2026.5 release-note snippet mentions "initial support" for Java, Python and C# only, which conflicts with the five-language list; the context of that snippet was not checked.

## 3. What does research say about architecture smells, code smells and metrics, and their validity?

### Takeaway
Evidence is strongest for history-based measures (churn, change coupling, hotspots) and for dependency-structure smells combined with history; it is weak or contested for single-snapshot metrics, most of which lose power once file size is controlled. Architecture recovery by clustering has low absolute accuracy and should be offered as a suggestion, never as ground truth.

### Cited Findings

Architecture smells
- A 2020 systematic mapping study found that many architecture smells have no tool support and that empirical validation of detection tools is limited. ([cited in Sas et al., arXiv 2203.08702](https://arxiv.org/pdf/2203.08702))
- In an industrial C/C++ study at ASML, smells tended to grow over time, and their presence correlated with frequently changed components and more change propagation. Of 15,578 cyclic-dependency instances, most grew or stayed stable. Hub-like dependencies tended to appear in components that already had other smells. ([Sas, Avgeriou, Uyumaz, EMSE 2022](https://link.springer.com/article/10.1007/s10664-022-10132-7))
- Change frequency tends to rise after an architecture smell is introduced, and smelly artefacts receive larger changes; the effect is strongest in medium-large and large artefacts. ([Pigazzini and Arcelli Fontana, JSEP 2022](https://zendy.io/title/10.1002/smr.2398))
- Cycles between parent and child packages are less critical than cycles between unrelated packages. ([Al-Mutawa et al., cited in unimib thesis](https://boa.unimib.it/retrieve/e39773b4-e2a1-35a3-e053-3a05fe0aac26/phd_unimib_723299.pdf))
- Across 19 projects, files involved in architecture anti-patterns were more bug-prone and change-prone, and the rates rose with the number of anti-patterns a file took part in. Unstable Interface and Crossing contributed most. ([Mo, Cai, Kazman, Xiao, Feng, TSE 2019](https://www.cs.drexel.edu/~yc349/papers/2019/tse2019.pdf))
- Architectural debt can be quantified by grouping files connected through design-rule hierarchies and co-change, then fitting cost growth models. ([Xiao et al., ICSE 2016](https://personal.stevens.edu/~lxiao6/papers/ICSE-16-Debt.pdf))

Behavioural analysis: hotspots and change coupling
- In 39 proprietary codebases (30,737 files), code that CodeScene rated low quality had 15 times more defects, took 124% longer to resolve issues, and had up to 9 times longer maximum cycle times than healthy code. The authors work for CodeScene and the metric is proprietary. ([Tornhill and Borg, TechDebt 2022](https://arxiv.org/abs/2203.04374v1))
- Relative code churn measures discriminated fault-prone from non-fault-prone binaries in Windows Server 2003 with 89% accuracy (older result, 2005). **[bk]** ([Nagappan and Ball, ICSE 2005](https://dl.acm.org/doi/10.1145/1062455.1062514))
- Process metrics (changes, authorship) outperform static code metrics for defect prediction and are more stable across releases (2013). **[bk]** ([Rahman and Devanbu, ICSE 2013](https://dl.acm.org/doi/10.5555/2486788.2486846))
- Change coupling correlates with defects more strongly than complexity metrics in the systems studied (older result, 2009). **[bk]** ([D'Ambros, Lanza, Robbes, WCRE 2009](https://ieeexplore.ieee.org/document/5328803))
- At Google, showing developers bug-prediction scores in code review did not change their behaviour; developers wanted actionable output (2013). **[bk]** ([Lewis et al., ICSE 2013](https://research.google/pubs/does-bug-prediction-support-human-developers-findings-from-a-google-case-study/))

Complexity and size metrics
- A meta-analysis of 10 studies (about 24,000 evaluations of 427 snippets) found that Cognitive Complexity correlates with comprehension time and subjective ratings, with mixed results for comprehension correctness and physiological measures. ([Muñoz Barón, Wyrich, Wagner, ESEM 2020](https://arxiv.org/abs/2007.12520))
- A 2023 study found Cognitive Complexity only a modest predictor of perceived understandability for early-career developers. ([Esposito et al.](https://arxiv.deeppaper.ai/papers/2303.07722v2))
- After controlling for class size, object-oriented metrics lost their apparent association with fault-proneness (El Emam et al., 2001). Evanco (2003) disputed treating size as a confounder; Zhou et al. (2014) confirmed a significant size effect for most metrics; Tahir et al. (2021) found it not always significant. ([summary of El Emam](https://neverworkintheory.org/2011/07/07/the-confounding-effect-of-class-size-on-the-validity-of-object-oriented-metrics.html); [Evanco](https://search.isc.ac/inventory/49/746276.htm); [Tahir et al.](https://ar5iv.labs.arxiv.org/html/2106.04687))
- Cyclomatic complexity and lines of code are correlated, but in a large Java corpus the relation was not strong enough to treat them as interchangeable at method level. **[bk]** ([Landman et al., JSEP 2016](https://onlinelibrary.wiley.com/doi/10.1002/smr.1760))
- In a controlled maintenance study, code smells did not significantly explain effort once file size and number of changes were accounted for (2013). **[bk]** ([Sjøberg et al., TSE 2013](https://ieeexplore.ieee.org/document/6392174))
- Network measures on the dependency graph (centrality, ego networks) predicted defects in Windows Server 2003 with recall about 10 points higher than complexity metrics (older result, 2008). **[bk]** ([Zimmermann and Nagappan, ICSE 2008](https://dl.acm.org/doi/10.1145/1368088.1368161))

Martin's package metrics
- Instability, abstractness and distance from the main sequence are implemented in many tools (JDepend, NDepend, PDepend, dependency-cruiser), but the search found only one defect-oriented study and no direct maintainability validation; the maintainability link is asserted in tool documentation. ([PDepend handbook](https://pdepend.org/documentation/handbook/reports/abstraction-instability-chart.html); [defect study, unverified details](https://core.ac.uk/download/335076413.pdf))

Design Structure Matrices, reflexion models and architecture recovery
- A dependency structure matrix shows modules as rows and columns, so layering appears as a triangular matrix and cycles as entries above the diagonal (Sangal et al., OOPSLA 2005, the basis of Lattix). **[bk]** ([paper](https://dl.acm.org/doi/10.1145/1094811.1094824))
- Reflexion models map source entities to a high-level model that the engineer draws, then classify each dependency as convergence, divergence or absence (Murphy, Notkin, Sullivan, 1995 and TSE 2001). **[bk]** ([paper](https://ieeexplore.ieee.org/document/917525))
- Against ground-truth architectures of eight systems, nine variants of six recovery techniques were compared; ARC and ACDC routinely did best. ([Garcia et al., summarised in MSR 2015 paper](https://jgarcia.ics.uci.edu//wp-content/uploads/arcade_msr_2015.pdf))
- With better dependency inputs and larger systems, overall accuracy stayed low for all recovery techniques. ACDC, Bunch-SAHC, WCA and ARC scaled to the 10 million line range. The kind of dependencies fed in changed the results substantially. ([Lutellier et al., ICSE 2015](https://www.cs.purdue.edu/homes/lintan/publications/archrec-icse15.pdf); [thesis](https://uwspace.uwaterloo.ca/handle/10012/9554))
- A 2023 comparison reports average MoJoFM of roughly 47 for ACDC, 41 for ARC and 38 for Bunch-NAHC. The table was garbled in extraction; verify before quoting. ([SARIF paper, arXiv 2311.04643](https://arxiv.org/pdf/2311.04643))

### Inferences
- archplan's hotspot and change-coupling detectors rest on the best-supported evidence. Structural smells are best ranked by combining them with change data (as DV8 does), because a cycle or hub in code that never changes has little measured cost.
- Report size next to any complexity or smell count, or normalise by it, since size explains much of what those metrics appear to predict.
- Cycle findings should distinguish parent-child package cycles from cycles between unrelated components, and report cycle size and growth over time, since those properties carried signal in the studies.
- Martin's instability and distance metrics are cheap to add but should be labelled as heuristics without strong empirical backing.
- Reflexion models are the academic name for archplan's current-versus-target comparison; the convergence, divergence, absence vocabulary is established and citable.
- Clustering (ACDC-style pattern rules or Louvain) can propose component groupings for a repository without declared boundaries, but with MoJoFM around 40 to 50% the output needs human confirmation.
- The Google result argues for few, actionable findings over scores.

### Gaps
- No peer-reviewed validation of "god component" or "hub-like dependency" thresholds was found; tools use fixed or percentile thresholds.
- No study was found that validates architecture smells or these metrics on Go or Elixir codebases. Nearly all evidence is from Java, C# and C/C++.
- No independent replication of the CodeScene "15 times" result was found.
- The exact share of bug-prone files covered by DV8 anti-patterns was not extracted from the TSE paper.
- No research was found on primitive-obsession or "concept" smells at architecture level.

## 4. How accurate are static call-graph builders for dynamic languages?

### Takeaway
For Python, application-only call graphs reach near-perfect precision but only 61 to 82% recall, and whole-program graphs that include libraries drop to 19 to 35% precision. JavaScript tools reach about 76 to 88% recall against dynamic ground truth. Go's typed algorithms are sound apart from reflection, and no published precision or recall figures were found for TypeScript-specific tools or for Elixir.

### Cited Findings

Python
- PyCG's own evaluation (ICSE 2021) reports about 99.2% precision and 69.9% recall. ([Salis et al., arXiv 2103.00587](https://arxiv.org/pdf/2103.00587))
- PyCG was archived on 2023-11-26; the author plans no further development. Forks exist (`pycg-fork`, `onecode-pycg`, `nline/PyCG`). ([vitsalis/PyCG](https://github.com/vitsalis/PyCG); [pycg-fork](https://pypi.org/project/pycg-fork))
- Jarvis macro-benchmark (6 applications, ground truth of 5,653 functions and 20,085 call relations), precision / recall: application-only with all entry functions, PyCG 0.99 / 0.76 and Jarvis 0.99 / 0.82; application-only from application entry points, PyCG 1.00 / 0.61 and Jarvis 1.00 / 0.66; whole-program including libraries, PyCG 0.19 / 0.48 to 0.52 and Jarvis 0.35 / 0.60. ([Jarvis, arXiv 2305.05949v5](https://arxiv.org/html/2305.05949v5))
- Jarvis micro-benchmark counts give derived precision and recall of about 92% / 92% for PyCG (317 true positives, 27 false positives, 27 false negatives) and 98% / 97% for Jarvis (335, 7, 9). ([Jarvis, arXiv 2305.05949v5](https://arxiv.org/html/2305.05949v5))
- Whole-program analysis cost: PyCG took about 503 s and 963 MB at one iteration and ran out of memory or hit recursion errors on some projects; Jarvis took about 301 s and 1.2 GB. Application-only runs took under a second for both. ([Jarvis, arXiv 2305.05949v5](https://arxiv.org/html/2305.05949v5))
- The Jarvis paper states that PyCG outperforms pyan and Depends but gives no figures for them. ([Jarvis, arXiv 2305.05949v5](https://arxiv.org/html/2305.05949v5))
- Scalpel is a Python analysis framework whose call-graph module wraps PyCG. ([Scalpel docs](https://python-scalpel.readthedocs.io/en/latest/api/scalpel.call_graph.html))

JavaScript and TypeScript
- On 36 projects with dynamic call graphs, Jelly's baseline static analysis had 75.9% average recall; approximate interpretation raised it to 88.1% with negligible precision loss, adding 55.1% more call edges. ([Laursen, Xu, Møller, PLDI 2024](https://cs.au.dk/~amoeller/papers/approx))
- In a five-tool comparison with 348 manually checked edges, ACG had over 99% precision and found over 90% of the true edges that any tool found; most tools found edges that all others missed (2018 study, re-posted 2024). ([Antal et al., arXiv 2405.07206](https://arxiv.org/html/2405.07206v1))
- Measured against dynamic call graphs on reachable nodes, optimistic ACG had 80.9% recall and 51.9% precision, pessimistic ACG 43.2% and 53.0%; on reachable edges precision fell to 11.7% and 22.4%. ([Root-cause study, arXiv 2205.06780](https://arxiv.org/pdf/2205.06780))
- A 2025 study found that static tools (PyCG, Jelly) consistently outperform language models at call-graph generation. ([Venkatesh et al., EMSE 2025](https://arxiv.org/abs/2410.00603))

Go
- `golang.org/x/tools/go/callgraph` offers static, CHA, RTA and VTA builders. The static builder is unsound and may miss edges; all are unsound for reflection. VTA is sound apart from reflection and `unsafe` if its initial graph is sound. ([callgraph_test.go](https://go.googlesource.com/tools/+/HEAD/go/callgraph/callgraph_test.go); [vta.go](https://sources.debian.org/src/golang-golang-x-tools/1:0.25.0+ds-1/go/callgraph/vta/vta.go))
- The project's own benchmark (one program, one machine) lists CHA at 131,717 edges and 7,640 reachable functions in 86 ms; RTA at 42,291 edges and 5,099 reachable in 110 ms; VTA at 44,861 edges and 4,919 reachable in 600 ms. Fewer edges means more precise; no ground truth is involved. ([callgraph_test.go](https://go.googlesource.com/tools/+/HEAD/go/callgraph/callgraph_test.go))
- RTA starts from `main` and so cannot analyse a library package on its own. ([Go blog: deadcode](https://go.dev/blog/deadcode))

Elixir
- `mix xref` reports compile-time, export and runtime dependencies between modules and files, can list cycles, and is driven by compiler tracers, so it sees code after macro expansion. **[bk]** ([mix xref docs](https://hexdocs.pm/mix/Mix.Tasks.Xref.html))

### Inferences
- An application-only call graph, which is what an architecture model needs, can be highly precise in Python. The expected failure mode is missing edges (roughly one fifth to one third), so archplan sequences should be presented as incomplete lower bounds.
- CHA over-approximates Go calls about threefold compared with RTA or VTA in the one benchmark. For component-level dependency edges CHA is acceptable; for call sequences RTA or VTA is preferable, and RTA needs a `main` package.
- For TypeScript, type-directed resolution through the compiler API or a scip-typescript index is the practical route; the published JavaScript numbers (Jelly, ACG) describe untyped analysis and are a pessimistic bound for typed code.
- For Elixir, compiler tracers give exact static remote calls, but `apply/3`, protocols, behaviours and message passing (GenServer calls) are invisible. That last gap matters most for sequence models of OTP systems.
- PyCG's assignment-graph algorithm is documented in the paper and implementable in pure Python; given the archive status, imitating it is safer than depending on it.

### Gaps
- No published precision or recall study was found for TypeScript call-graph construction using type information.
- No peer-reviewed empirical study with ground truth was found for Go call-graph algorithms; only the project's own edge-count benchmark.
- No published accuracy figures were found for `mix xref` or any Elixir call-graph tool.
- No absolute precision or recall figures were found for pyan; its maintenance status was not verified.
- The status of the `go/pointer` package (believed deprecated) was not confirmed.

## 5. Clone detection and dead-code detection

### Takeaway
Practical clone detectors are token based and find exact and renamed clones well; near-miss (Type-3) detection is solved in research tools whose precision depends heavily on configuration. Dead-code tools are reachability analyses over a module or call graph from declared entry points, and their accuracy is bounded by how well entry points and dynamic uses are known.

### Cited Findings

Clone detection
- SourcererCC uses a bag-of-tokens index to scale Type-3 detection to very large corpora; its precision was estimated by manually validating 390 sampled pairs from its BigCloneBench run. ([Sajnani et al., arXiv 1512.06448](https://arxiv.org/pdf/1512.06448))
- NiCad's reported precision ranges from 89 to 96%, but the SourcererCC authors note it can fall to about 60% under aggressive normalisation. ([Sajnani et al., arXiv 1512.06448](https://arxiv.org/pdf/1512.06448))
- In an independent run on BigCloneBench, NiCad (70% threshold, blind renaming) had the best recall on "strongly Type-3" clones at 95%. ([LVMapper paper, arXiv 1909.04238](https://arxiv.org/pdf/1909.04238))
- MSCCD generates token-based clone detectors from ANTLR grammars for any language and reports recall comparable to SourcererCC. ([MSCCD, arXiv 2204.01028](https://ar5iv.labs.arxiv.org/html/2204.01028))
- jscpd (MIT) detects copy-paste in 150+ formats with a Rabin-Karp token matcher; PMD CPD is the comparable token-based tool on the JVM. **[bk]** ([jscpd](https://github.com/kucherenko/jscpd); [CPD docs](https://docs.pmd-code.org/latest/pmd_userdocs_cpd.html))
- Clones are classed as Type-1 (identical apart from layout), Type-2 (renamed identifiers or literals), Type-3 (added or removed statements) and Type-4 (same behaviour, different syntax). **[bk]** ([Roy, Cordy, Koschke 2009](https://www.sciencedirect.com/science/article/pii/S0167642309000367))

Dead-code detection
- Knip (JavaScript and TypeScript) builds a module graph from entry files and reports unreachable files, unused exports and unused dependencies; it can remove unused exports with `--fix`. Exports of a published library appear unused unless declared as entry points. ([How Knip works](https://knip.dev/explanations/how-knip-works); [Knip unused exports](https://knip.dev/typescript/unused-exports))
- Vulture (Python) assigns each finding a confidence: 100% for unreachable code and unused arguments, 90% for unused imports, 60% for unused functions, classes, attributes and variables. It supports whitelists for false positives and warns that implicitly called code is reported as unused. ([vulture on PyPI](https://pypi.org/project/vulture/))
- Go's `deadcode` command uses RTA from `main` and `init` functions to report unreachable functions, and `-whylive` prints the call chain that keeps a function alive. It cannot see calls from non-Go code. ([Go blog: deadcode](https://go.dev/blog/deadcode))

### Inferences
- Both capabilities fit archplan's existing model. Dead-component detection is reachability over the dependency or call graph from entry points, and archplan already needs entry points for sequences. Vulture's per-category confidence and Go's "why live" explanation are worth copying.
- A token-based Rabin-Karp clone detector over a normalised token stream is small enough to write with no dependencies, and covers Type-1 and Type-2 clones in all four languages. Cross-component clones are the architecturally interesting subset (a sign of a missing shared abstraction), which per-file tools do not single out.
- Change coupling without a structural dependency is a known indicator of hidden duplication, so clone findings and co-change findings can corroborate each other.

### Gaps
- SourcererCC's exact precision and recall per clone type were not extracted from the paper.
- No published accuracy evaluation was found for jscpd, PMD CPD, vulture or knip; the evidence is their documentation.
- No dedicated Elixir dead-code or clone tool was verified beyond the compiler's unused-function warnings and Credo's duplicated-code check.
- Two versions of the Go `deadcode` blog post disagree on whether the tool is described as sound; check the current post before quoting.
