# Static analysis for security (SAST and related techniques), state as of November 2026

Reading guide for the report writer:

- **[V]** marks a finding checked in this session (web search result, fetched page, or GitHub API query on 2026-11-17).
- **[M]** marks a finding taken from the researcher's prior knowledge and cited to the canonical source URL. These URLs were not re-fetched in this session. Treat exact numbers under [M] as needing a spot check before publication.
- Repository facts (licence, archive flag, latest release, last push) come from the GitHub REST API on 2026-11-17. "Last push" can reflect bot branches, so the latest release date is the better activity signal.

## 1. Core techniques and their trade-offs

### Takeaway
Every practical SAST engine is deliberately unsound: it trades missed flows for speed and fewer false alarms. The choices that matter most are (a) syntactic matching versus inter-procedural data flow and (b) how the call graph and framework "glue" are modelled. Dynamic languages and frameworks break the call graph first, so recall drops there before anything else does.

### Cited Findings

Pattern and AST matching
- Semgrep-style rules match code patterns on a generic AST with metavariables and are fast and easy to write. Semgrep's own documentation states that its data-flow engine has no path sensitivity, no pointer or alias analysis, and soundness is not a goal. [V/M] | Source: [Semgrep data-flow overview](https://docs.semgrep.dev/writing-rules/data-flow/data-flow-overview)
- Bandit visits the Python AST and runs per-node plugin checks (for example `B602 subprocess_popen_with_shell_equals_true`). It does no data-flow tracking. [M] | Source: [Bandit docs](https://bandit.readthedocs.io/en/latest/)
- Sobelow (Elixir/Phoenix) and Brakeman (Rails) are framework-aware pattern checkers. They know the framework's router, controllers, templates and config, which lets them report configuration and missing-protection findings (CSRF, HSTS, CSP) that generic engines miss. [M] | Source: [Sobelow repo](https://github.com/nccgroup/sobelow); [Brakeman docs](https://brakemanscanner.org/docs/)

Taint tracking and data-flow analysis
- IFDS reduces inter-procedural, finite, distributive subset problems to graph reachability on an "exploded supergraph" and solves them precisely (context-sensitive, flow-sensitive) in polynomial time, O(E D^3). [M] | Source: [Reps, Horwitz, Sagiv, POPL 1995](https://dl.acm.org/doi/10.1145/199448.199462)
- IDE generalises IFDS to environment problems (values attached to facts, for example constants or type-state). [M] | Source: [Sagiv, Reps, Horwitz, TCS 1996](https://doi.org/10.1016/0304-3975(96)00072-2)
- FlowDroid is the reference IFDS-based taint analysis (context, flow, field and object-sensitive, with lifecycle modelling for Android). It shows that the framework model (entry points, callbacks) is as important as the solver. [M] | Source: [Arzt et al., PLDI 2014](https://dl.acm.org/doi/10.1145/2594291.2594299)
- Taint analysis is specified by sources, sinks, sanitizers and propagators (summaries for library code). Pysa computes per-function summaries bottom-up over the call graph and takes these specifications as `.pysa` model files plus a `taint.config` rule file. [M] | Source: [Pysa basics](https://pyre-check.org/docs/pysa-basics/)
- Meta reported that in the first half of 2020 Pysa found 44% of all security issues detected in the Instagram server code base. This is a vendor self-report from 2020. [M] | Source: [Meta engineering blog, 2020](https://engineering.fb.com/2020/08/07/security/pysa/)
- Semgrep Community Edition taint mode is intra-procedural (single function). Cross-function and cross-file taint are Pro engine features. [V] | Source: [Semgrep Pro engine intro](https://docs.semgrep.dev/semgrep-code/semgrep-pro-engine-intro)
- Opengrep 2.0.0 (released 2026-11-13) added inter-file taint analysis to the open-source fork, enabled with `--enable-interfile`, and removed the Python wrapper from the architecture. [V] | Source: [Opengrep releases](https://github.com/opengrep/opengrep/releases)

Abstract interpretation
- Abstract interpretation computes a sound over-approximation of program behaviour in an abstract domain, with widening to force termination. Precision depends on the domain. [M] | Source: [Cousot and Cousot, POPL 1977](https://dl.acm.org/doi/10.1145/512950.512973)
- Meta's Infer uses compositional (bi-abductive, summary-based) analysis so that it can run on code changes. Meta reported a fix rate near 0% when the same findings were shown as a batch bug list and about 70% when shown at code-review ("diff") time. [M] | Source: [Distefano et al., "Scaling Static Analyses at Facebook", CACM 2019](https://dl.acm.org/doi/10.1145/3338112)

Symbolic execution
- Symbolic execution explores paths with symbolic inputs and uses a constraint solver to decide feasibility. It gives few false positives and concrete witnesses, but suffers from path explosion, solver cost and environment modelling. [M] | Source: [Baldoni et al., ACM Computing Surveys 2018](https://dl.acm.org/doi/10.1145/3182657)

Code property graphs
- A code property graph merges AST, control-flow graph and program dependence graph into one property graph. Vulnerability patterns become graph traversals. The original paper found 18 previously unknown Linux kernel vulnerabilities with such traversals. [M] | Source: [Yamaguchi et al., IEEE S&P 2014](https://ieeexplore.ieee.org/document/6956589)
- Joern is the open-source CPG implementation, with a Scala-based query DSL and frontends for C/C++, Java, JavaScript/TypeScript, Python, Go, PHP, Ruby, Kotlin, Swift, C# and binaries. [M] | Source: [Joern docs](https://docs.joern.io/)

Datalog and logic queries
- Doop showed that expressing points-to analysis declaratively in Datalog is both concise and fast. [M] | Source: [Bravenboer and Smaragdakis, OOPSLA 2009](https://dl.acm.org/doi/10.1145/1640089.1640108)
- CodeQL extracts a relational database from the code and evaluates QL, an object-oriented Datalog dialect. [M] | Source: [Avgustinov et al., ECOOP 2016](https://doi.org/10.4230/LIPIcs.ECOOP.2016.2)
- Soufflé is the main open-source Datalog engine for program analysis (UPL-1.0, latest release 2.5 on 2025-03-24). [V] | Source: [Soufflé repo](https://github.com/souffle-lang/souffle)

Type-based checks
- Information-flow type systems label data with security levels and reject illegal flows at type-check time. [M] | Source: [Sabelfeld and Myers, 2003](https://ieeexplore.ieee.org/document/1159651)
- Python's `LiteralString` (PEP 675) lets an API demand a string built only from literals, which blocks injection by type at the sink. [M] | Source: [PEP 675](https://peps.python.org/pep-0675/)

Where soundness and precision break down
- The "soundiness" position paper states that virtually all whole-program analyses are unsound on purpose for reflection, `eval`, dynamic loading, native code and similar features, and asks authors to document these gaps. [M] | Source: [Livshits et al., CACM 2015](https://dl.acm.org/doi/10.1145/2644805)
- Python call graphs: PyCG reports about 99.2% precision and about 69.9% recall on its benchmark, so roughly 30% of real call edges are missed by a state-of-the-art static generator. [M] | Source: [Salis et al., ICSE 2021](https://arxiv.org/abs/2103.00587)
- JavaScript call graphs: the field-based "approximate call graph" approach is intentionally unsound (ignores dynamic property names and `eval`) to stay scalable. [M] | Source: [Feldthaus et al., ICSE 2013](https://dl.acm.org/doi/10.5555/2486788.2486887)
- Go offers several call-graph algorithms with different precision (static, CHA, RTA, VTA) in `golang.org/x/tools/go/callgraph`. [M] | Source: [Go callgraph package](https://pkg.go.dev/golang.org/x/tools/go/callgraph)
- In the ICSE 2026 Python study, pattern-matching tools kept high precision but low recall on real CVEs, and Pysa reported nothing on the real-world set (as summarised by a later paper). [V, secondary] | Source: [PyFlow, arXiv 2608.07026](https://arxiv.org/pdf/2608.07026)
- In the Java study by Li et al., the vulnerability classes most often missed were those outside "resource control" and "insufficiently neutralized input/output", that is, logic, authorization and design flaws rather than injection. [V] | Source: [Li et al., ESEC/FSE 2023](https://dblp.uni-trier.de/rec/conf/sigsoft/LiCFFLLLC23.html)

### Inferences
- archplan already holds what most taint engines lack and must reconstruct: entry points, component boundaries, call sequences and trust zones. Its detectors are graph-reachability queries over a summarised model, which is the CPG/Datalog style at architecture granularity. It should present itself as that, not as a taint engine.
- The cheapest reusable idea is the specification split used by Pysa, CodeQL and Semgrep: a small data file per framework that lists sources (entry points), sinks, sanitizers or guards, and propagators. archplan could keep its detectors generic and ship such files for Django, FastAPI, Express, NestJS, net/http, Gin and Phoenix.
- A zero-dependency tool cannot run IFDS with alias analysis across four languages. A realistic target is function-summary taint (parameter-to-sink, parameter-to-return, source-to-return) propagated bottom-up over the call sequences archplan already extracts. That matches Pysa's architecture in miniature.
- archplan should publish a "soundiness statement" per language: which dynamic features (decorators, metaclasses, `getattr`, dynamic `import`, Elixir macros and `apply/3`, Go reflection and interface dispatch, JS dynamic property access) are not resolved. This follows the soundiness paper and sets expectations.
- Framework-aware checks in the style of Sobelow and Brakeman fit archplan's model well, because they depend on router and middleware structure rather than on data flow.

### Gaps
- No independent measurement was found of Elixir static-analysis precision or of Elixir call-graph recall. Macro expansion and dynamic dispatch are the likely weak points, but this is unquantified.
- Exact complexity and precision claims above come from the original papers and were not re-read in this session.

## 2. Tools: engines, rule languages, coverage, licences, maintenance

### Takeaway
For archplan's four languages, CodeQL, Semgrep/Opengrep and Joern cover Python, TypeScript and Go with data flow. Elixir is served only by Sobelow (no release since 2023) and by Semgrep's limited support. Several named tools changed status: Pyre is archived and Pysa moved, Semgrep's rules were relicensed and Opengrep forked, tfsec folded into Trivy, Horusec is dormant, Bearer is under the Elastic License.

### Cited Findings

Status table (GitHub API, 2026-11-17) [V]

| Tool | Engine type | Licence | Latest release | Status note |
|---|---|---|---|---|
| CodeQL | Relational DB + QL (Datalog), global data flow and taint | Queries/libraries MIT; CLI and engine proprietary | continuous | Active. [repo](https://github.com/github/codeql) |
| Semgrep CE | Generic AST patterns, intra-procedural taint | Engine LGPL-2.1; Semgrep-maintained rules under Semgrep Rules License | v1.185.0, 2026-11-10 | Active. [repo](https://github.com/semgrep/semgrep) |
| Opengrep | Fork of Semgrep CE; inter-file taint since 2.0 | LGPL-2.1 | v2.0.0, 2026-11-13 | Active. [repo](https://github.com/opengrep/opengrep) |
| Joern | Code property graph, Scala query DSL | Apache-2.0 | v4.0.684, 2026-11-16 | Active. [repo](https://github.com/joernio/joern) |
| Bandit | Python AST plugins | Apache-2.0 | 1.9.4, 2026-02-25 | Active. [repo](https://github.com/PyCQA/bandit) |
| Pyre | Python type checker | MIT | v0.9.23, 2024-10-29 | Archived. Replaced by Pyrefly. [repo](https://github.com/facebook/pyre-check) |
| Pysa | Summary-based taint for Python | MIT | n/a | Moved to its own repo, pushed 2026-11-13. [repo](https://github.com/facebook/Pysa) |
| gosec | Go AST rules plus SSA analyzers | Apache-2.0 | v2.30.0, 2026-10-23 | Active. [repo](https://github.com/securego/gosec) |
| govulncheck | Call-graph reachability against Go vuln DB | BSD-3-Clause | v1.1.4, 2025-01-13 | Maintained by Go team, repo pushed 2026-10. [repo](https://github.com/golang/vuln) |
| Sobelow | Elixir/Phoenix AST checks | Apache-2.0 | v0.13.0, 2023-08-06 | Last commit 2024-05-19. Effectively dormant. [repo](https://github.com/nccgroup/sobelow) |
| eslint-plugin-security | ESLint AST rules | Apache-2.0 | v4.2.1, 2026-10-30 | Active under eslint-community. [repo](https://github.com/eslint-community/eslint-plugin-security) |
| Bearer CLI | AST + data-flow, data-type (PII) classification | Elastic License 2.0 (not OSI open source) | v2.1.3, 2026-10-28 | Active. Owned by Cycode. [repo](https://github.com/Bearer/bearer) |
| Infer | Abstract interpretation, bi-abduction, Pulse | MIT | v1.3.0, 2026-05-12 | Active. Java, C/C++, ObjC, Erlang. [repo](https://github.com/facebook/infer) |
| Mariana Trench | Summary-based taint for Android/Java bytecode | MIT | tag v1.0.8 | Active. [repo](https://github.com/facebook/mariana-trench) |
| Horusec | Orchestrator of other scanners | Apache-2.0 | v2.8.0, 2022-06-08 | Last commit on default branch 2023-08-07. Dormant. [repo](https://github.com/ZupIT/horusec) |
| Trivy | SCA, IaC misconfig, secrets, licence | Apache-2.0 | v0.76.2, 2026-11-05 | Active. [repo](https://github.com/aquasecurity/trivy) |
| tfsec | Terraform checks | MIT | v1.28.14, 2025-05-02 | Superseded by `trivy config`. [repo](https://github.com/aquasecurity/tfsec) |
| Brakeman | Rails-aware AST + light data flow | Brakeman Public Use License (Synopsys text, not OSI) | v8.1.0, 2026-09-30 | Active. [repo](https://github.com/presidentbeef/brakeman) |

Details per tool
- Semgrep relicensing: on 2024-12-13 Semgrep moved its maintained rules from LGPL 2.1 with Commons Clause to the Semgrep Rules License, which restricts use in competing commercial products. The engine licence did not change. Semgrep OSS was renamed Semgrep Community Edition. [V] | Source: [InfoQ, Feb 2025](https://www.infoq.com/news/2025/02/semgrep-forked-opengrep); [Semgrep FAQ on Opengrep](https://docs.semgrep.dev/faq/comparisons/opengrep.md)
- Opengrep was launched on 2025-01-23 by a group of more than ten security vendors, including Aikido, Endor Labs, Amplify, Arnica, Kodem and Mobb, as an LGPL-2.1 fork of Semgrep CE. [V] | Source: [The New Stack](https://thenewstack.io/opengrep-launches-as-free-fork-after-semgrep-license-shift/); [SecurityWeek](https://www.securityweek.com/endor-labs-and-allies-launch-opengrep-reviving-true-oss-for-sast/)
- Semgrep rule format: YAML with `pattern`, `patterns`, `pattern-either`, `pattern-not`, `metavariable-regex`, and `mode: taint` with `pattern-sources`, `pattern-sinks`, `pattern-sanitizers`, `pattern-propagators`. [M] | Source: [Semgrep rule syntax](https://semgrep.dev/docs/writing-rules/rule-syntax)
- CodeQL supports C/C++, C#, Go, Java/Kotlin, JavaScript/TypeScript, Python, Ruby, Swift, Rust and GitHub Actions workflows. Elixir is not supported. [M] | Source: [CodeQL supported languages](https://codeql.github.com/docs/codeql-overview/supported-languages-and-frameworks/)
- CodeQL CLI licence: free for open-source code bases and academic research. Use on private code needs a GitHub code-security licence. [M] | Source: [CodeQL CLI licence](https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md)
- CodeQL "models as data": sources, sinks and summaries for libraries can be supplied as YAML data extensions without writing QL. [M] | Source: [CodeQL: customizing library models for Python](https://codeql.github.com/docs/codeql-language-guides/customizing-library-models-for-python/)
- Pyre's README states that the repository is archived, that type checking moved to Pyrefly, and that Pysa moved to `facebook/Pysa`. [V] | Source: [pyre-check README](https://github.com/facebook/pyre-check)
- govulncheck reports only vulnerabilities whose vulnerable symbols are reachable from the program's call graph, and it can analyse source or compiled binaries. It reads the Go vulnerability database, which uses the OSV schema. [M] | Source: [Go blog: govulncheck](https://go.dev/blog/govulncheck)
- Capslock (Google) is a Go capability analysis: it reports which privileged operations (network, file system, exec, unsafe, reflection) each package can reach through the transitive call graph. BSD-3-Clause, active on 2026-11-17. [V for status, M for description] | Source: [Capslock repo](https://github.com/google/capslock)
- Elixir supply-chain checks: `mix_audit` scans `mix.lock` against advisories (BSD-3-Clause, pushed 2026-02-23). [V] | Source: [mix_audit repo](https://github.com/mirego/mix_audit)
- Bearer was acquired by Cycode in 2024 and integrated into Cycode's platform. [V, secondary] | Source: [Wiz Academy 2026 guide](https://www.wiz.io/academy/application-security/open-source-code-security-tools)
- Trivy's GitHub Actions and Docker Hub images were compromised in March 2026 and later remediated, according to one 2026 guide that names `trivy-action` v0.35.0 and `setup-trivy` v0.2.6 as safe versions. Single secondary source. [V, secondary] | Source: [Wiz Academy 2026 guide](https://www.wiz.io/academy/application-security/open-source-code-security-tools)
- Commercial engines (Snyk Code, SonarQube taint analysis in paid editions, Checkmarx, OpenText Fortify, Black Duck Coverity) are closed source with proprietary rule formats. [M] | Source: [Snyk Code docs](https://docs.snyk.io/scan-with-snyk/snyk-code); [SonarQube docs](https://docs.sonarsource.com/)
- Coverity's founders reported that users start to distrust and ignore a tool when the false-positive rate passes roughly 30%. Dated 2010. [M] | Source: [Bessey et al., "A Few Billion Lines of Code Later", CACM 2010](https://dl.acm.org/doi/10.1145/1646353.1646374)
- Older Python taint tool PyT is abandoned (last release 0.42 in 2018, last push 2020-12-25). [V] | Source: [PyT repo](https://github.com/python-security/pyt)

### Inferences
- Rule content has become the contested asset, as the Semgrep relicensing shows. archplan should not copy Semgrep-maintained rules. Community rules, CodeQL's MIT-licensed model files and CWE identifiers are safer references for source/sink lists. Check each file's licence before reuse.
- Brakeman and Sobelow are the closest design references for archplan because they build a framework model first (routes, controllers, plugs or filters) and then check it. Sobelow's dormancy leaves Elixir/Phoenix as an underserved niche that archplan already parses.
- Capslock's per-package capability report maps directly to archplan's component model: "which components can reach network, file system, process execution or deserialisation" is a model-level detector that needs only a call graph and a list of capability-bearing APIs per language.
- The Semgrep `mode: taint` YAML shape and CodeQL's models-as-data are the two de facto formats for source/sink/sanitizer specifications. Imitating one (field names and semantics) lowers the learning cost for users.

### Gaps
- Semgrep's exact Elixir support level in November 2026 (Community versus Pro) was not verified.
- Current release cadence and language coverage of Snyk Code, Checkmarx, Fortify and Coverity were not verified in this session. No vendor-neutral 2026 comparison of these commercial engines was found.
- gosec's recent addition of taint-style analyzers was not verified.
- Whether gitleaks' author has moved to a successor project in 2026 was not verified (see section 4).

## 3. Independent benchmarks and empirical studies

### Takeaway
Across Java, C/C++, Python and JavaScript, peer-reviewed studies agree: single SAST tools find well under half of real-world vulnerabilities, combining tools raises recall to roughly 30 to 78% at a high cost in warnings, and synthetic benchmarks overstate real performance. Developer studies name false positives, poor explanations and bad workflow integration as the reasons warnings are ignored.

### Cited Findings

Real-world detection studies
- Java, Li et al., ESEC/FSE 2023: seven free or open-source tools were run on OWASP Benchmark (synthetic) and on a Java CVE Benchmark (real-world). The abstract reports that with all tools combined, 70.9% of real vulnerabilities remain undetected. [V] | Source: [paper record](https://researchr.org/publication/LiCFFLLLC23)
- The same authors' benchmark page states that only 12.7% of real-world vulnerabilities are detected by the selected tools. The two figures use different matching criteria, and the full paper was not retrieved to reconcile them. [V, unresolved conflict] | Source: [Java CVE Benchmark](https://marklee131.github.io/Java_CVE_Bench)
- C/C++, Lipp, Banescu and Pretschner, ISSTA 2022: five open-source analyzers and one commercial one on 27 projects (1.15 million lines, 192 known vulnerabilities). The tools missed between 47% and 80% of vulnerabilities. Combining tools lowered the miss rate to 30 to 69%, with 15 percentage points more functions flagged. Performance on synthetic benchmarks was much better. [V] | Source: [paper PDF](https://mediatum.ub.tum.de/doc/1659728/1659728.pdf)
- C/C++, Charoenwet et al., ISSTA 2024: 815 vulnerability-contributing commits from 92 projects (319 CVEs). The best single tool (Flawfinder) raised a warning in the vulnerable function for 52% of commits. All five tools combined reached 78%. 22% of commits got no warning. [V] | Source: [arXiv 2407.12241](https://arxiv.org/html/2407.12241v1)
- Python, "An Empirical Study on Static Application Security Testing (SAST) Tools for Python", ICSE 2026 (Distinguished Paper): eight tools chosen from 117. No single tool detected more than 40% of the real-world vulnerabilities. All tools combined detected 66.7%. [V] | Source: [ICSE 2026 programme](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/30/An-Empirical-Study-on-Static-Application-Security-Testing-SAST-Tools-for-Python)
- A later paper describes that Python real-world dataset as 108 CVEs from 62 projects and reports a best baseline recall of about 40.7%. [V, secondary] | Source: [PyFlow, arXiv 2608.07026](https://arxiv.org/pdf/2608.07026)
- JavaScript, Brito et al., IEEE Transactions on Reliability 2023: nine tools on 957 Node.js package vulnerabilities from npm advisories. The three best tools combined detected up to 57.6% at a precision of 0.11%. Many OWASP Top 10 classes were detected by no tool. [V] | Source: [arXiv 2301.05097](https://export.arxiv.org/abs/2301.05097)
- Java, C and Python, Zhou et al. 2024: 15 SAST tools compared with 12 open-source language models at repository level. Combined SAST tools were the best option for C and Python in that study. (Model results are out of scope here.) [V] | Source: [arXiv 2407.16235](https://ar5iv.labs.arxiv.org/html/2407.16235)
- Synthetic, Goseva-Popstojanova and Perhinschi, IST 2015: three commercial tools on Juliet. 27% of C/C++ and 11% of Java vulnerabilities were missed by all three. Dated. [V] | Source: [paper PDF](https://community.wvu.edu/~kagoseva/Papers/IST-2015.pdf)
- A longitudinal CodeQL study reports 171 CVEs identified, of which 83 could have been detected by a CodeQL version released before the fix. Only a secondary listing was found. [V, weak source] | Source: [paper listing](https://newx.sg/paper/detail/d62639c0-4ce8-11f1-b84c-00163e10baa7)
- Vendor and blog benchmarks from 2025 and 2026 claim that well-known scanners find fewer than one in five real vulnerabilities. Each has a conflict of interest because the author's own scanner is included. Treat as indicative only. [V] | Source: [DEV post](https://dev.to/muhammad_hasan/we-benchmarked-24-sast-tools-on-700-real-vulnerabilities-the-3-best-known-ones-came-last-8ne); [Endor Labs benchmark](https://www.endorlabs.com/learn/ai-sast-benchmark-2x-more-real-vulnerabilities)

Benchmarks and datasets
- OWASP Benchmark for Java v1.2 has 2,740 runnable test cases in 11 vulnerability categories. The score is true-positive rate minus false-positive rate. The repository is still pushed to (2026-11-13), but the last tagged release is "1.2beta" from 2016. [M for counts, V for repo status] | Source: [OWASP Benchmark](https://owasp.org/www-project-benchmark/); [BenchmarkJava repo](https://github.com/OWASP-Benchmark/BenchmarkJava)
- NIST Juliet 1.3: about 64,099 C/C++ test cases over 118 CWEs and about 28,881 Java test cases over 112 CWEs, hosted in the Software Assurance Reference Dataset. Synthetic. [M] | Source: [NIST SARD test suites](https://samate.nist.gov/SARD/test-suites)
- NIST SATE (Static Analysis Tool Exposition) is the recurring NIST evaluation of static analyzers. [M] | Source: [NIST SATE](https://www.nist.gov/itl/ssd/software-quality-group/static-analysis-tool-exposition-sate)
- OpenSSF CVE Benchmark: more than 200 real JavaScript/TypeScript CVEs, each with vulnerable and patched versions. Last push 2024-03-12, so it is stale. [V] | Source: [ossf-cve-benchmark repo](https://github.com/ossf-cve-benchmark/ossf-cve-benchmark)
- RealVuln (2026): 15 scanners on 26 intentionally vulnerable Python repositories, ranked by F3 score, with open labels and harness. [V] | Source: [arXiv 2604.13764](https://arxiv.org/html/2604.13764v1)
- CrossCommitVuln-Bench (2026): a dataset of Python vulnerabilities introduced over multiple commits, which per-commit static analysis does not see. [V, title only] | Source: [arXiv 2604.21917](https://arxiv.org/pdf/2604.21917)

Developer studies
- Johnson et al., ICSE 2013: interviews with 20 developers. All saw value in static analysis. Barriers were false positives, the way warnings are presented, lack of fix guidance and poor workflow integration. [V] | Source: [Google Research record](https://research.google/pubs/pub43477/)
- Christakis and Bird, ASE 2016 (Microsoft survey): developers tolerate false-positive rates up to roughly 15 to 20%, and most want results in the editor or code review, not in the build. The threshold is quoted from a citing paper. [V, secondary for the number] | Source: [paper PDF](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/07/ase-2016.pdf); [citing paper, ICSE 2020](https://net.cs.uni-bonn.de/fileadmin/ag/smith/publications/2020_ICSE_Warning.pdf)
- Google Tricorder, ICSE 2015: a check is kept only if its "effective false positive" rate (developer did not act on it) stays at or below about 10%. Findings are shown in code review. [M] | Source: [Tricorder paper](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/)
- Sadowski et al., CACM 2018: Google integrates analysis into the compile and review workflow and found that findings shown outside the workflow are ignored. [M] | Source: ["Lessons from Building Static Analysis Tools at Google"](https://dl.acm.org/doi/10.1145/3188720)
- Meta: about 70% fix rate at code-review time versus near 0% for batch bug lists (see section 1). [M] | Source: [CACM 2019](https://dl.acm.org/doi/10.1145/3338112)

### Inferences
- The classes that taint-based SAST misses most (authorization, logic and design flaws) are the classes archplan's model-level detectors target. This is the strongest positioning argument for archplan: it complements, and does not duplicate, CodeQL or Semgrep.
- archplan should measure itself on real CVEs, not only on synthetic cases. Reusable ground truth: the ICSE 2026 Python CVE set, the OpenSSF JS/TS CVE benchmark, the Java CVE Benchmark method (run on vulnerable and patched versions, count a hit only if the finding disappears after the fix), and the Go vulnerability database (entries name the vulnerable symbols).
- A design budget follows from the developer studies: aim for an effective false-positive rate of at most 10 to 20% per detector, report new findings on changed code first, and give each finding a concrete path and a fix hint.
- Function-level matching (as in Charoenwet et al.) inflates recall compared with line-level or flow-level matching. archplan findings are component-level or path-level, so any reported recall should state the matching granularity.

### Gaps
- The full text of Li et al. (FSE 2023) was not retrieved, so the relation between the 12.7% and 70.9% figures and the per-tool OWASP Benchmark scores are unconfirmed.
- Per-tool numbers from the ICSE 2026 Python study (CodeQL, Semgrep, Bandit individually) were not retrieved.
- No peer-reviewed real-world SAST evaluation for Go or for Elixir was found.
- No current SATE report with tool-level numbers was located in this session.

## 4. Supply-chain and adjacent static checks

### Takeaway
Software composition analysis is converging on the OSV schema for advisories and on call-graph reachability to cut noise. Secrets and infrastructure-as-code scanning are mature pattern-matching domains with active open-source tools. Reachability claims are mostly vendor-reported and assume a buildable project and a good call graph.

### Cited Findings

Composition analysis and reachability
- govulncheck reports a vulnerability only when a vulnerable symbol is reachable in the call graph, in source or binary mode. [M] | Source: [Go blog](https://go.dev/blog/govulncheck)
- OSV-Scanner (Google) is Apache-2.0, v2.7.1 released 2026-11-09. pip-audit is Apache-2.0, v2.10.1 released 2026-06-10. [V] | Source: [osv-scanner](https://github.com/google/osv-scanner); [pip-audit](https://github.com/pypa/pip-audit)
- The OSV schema records affected packages, version ranges and, optionally, affected symbols in ecosystem-specific fields. [M] | Source: [OSV schema](https://ossf.github.io/osv-schema/)
- Independent evidence, VEX-Bench (2026): reachability tools only check whether a vulnerable function appears in the call graph and cannot judge path feasibility. govulncheck failed to build 11 of 30 Go cases. About 70% of tool false positives were cases labelled "code not reachable". [V] | Source: [arXiv 2609.08040](https://arxiv.org/pdf/2609.08040)
- Vendor claims: Endor Labs states that function-level reachability removes about 92% of SCA findings. Pixee cites a 60 to 95% range. These are self-reported. [V] | Source: [Endor Labs](https://www.endorlabs.com/use-case/sca-with-reachability); [Pixee](https://www.pixee.ai/resource-center/software-supply-chain-security/reachability-analysis)
- A 2025 KAUST study of 2,414 repositories reportedly found a 92.0% false-positive rate in dependency scanners and that function-call analysis pruned 61.9% of them. Quoted second-hand by a vendor blog. The original was not located. [V, secondary] | Source: [OX Security blog](https://www.ox.security/blog/reachability-analysis-eliminating-vulnerability-backlogs/)
- Semgrep Supply Chain classifies findings by reachability but reportedly does not analyse reachability through transitive dependencies. Second-hand. [V, secondary] | Source: [Sacra summary](https://sacra.com/chat/h/6da11295-e0cf-433f-afbd-319f105386ca/)

Secrets detection
- gitleaks: MIT, v8.30.1 (2026-03-21), regex plus entropy rules in TOML. [V] | Source: [gitleaks](https://github.com/gitleaks/gitleaks)
- TruffleHog: AGPL-3.0, v3.99.9 (2026-11-12), detectors that verify candidate secrets against the issuing service. [V for status, M for verification feature] | Source: [trufflehog](https://github.com/trufflesecurity/trufflehog)
- detect-secrets (Yelp): Apache-2.0, last release v1.5.0 on 2024-05-06, built around a committed `.secrets.baseline` file so that only new secrets fail the build. [V for status, M for baseline design] | Source: [detect-secrets](https://github.com/Yelp/detect-secrets)

Infrastructure as code
- Checkov: Apache-2.0, v3.4.5 (2026-11-12). KICS: Apache-2.0, v2.2.1 (2026-11-09). Both active. [V] | Source: [checkov](https://github.com/bridgecrewio/checkov); [kics](https://github.com/Checkmarx/kics)
- tfsec was absorbed into Trivy in 2023. Its checks run under `trivy config`, and no new features are planned for tfsec. [V] | Source: [AppSecSanta overview](https://appsecsanta.com/iac-security-tools/best-open-source-iac-security-tools)

SBOM formats
- CycloneDX (OWASP, also Ecma standard ECMA-424) and SPDX (Linux Foundation, ISO/IEC 5962:2021 for version 2.2.1, version 3.0 current) are the two SBOM formats in use. [M] | Source: [CycloneDX specification](https://cyclonedx.org/specification/overview/); [SPDX specifications](https://spdx.dev/use/specifications/)
- OWASP Top 10:2025 ranks "A03 Software Supply Chain Failures" third. It is new and expands the 2021 category "Vulnerable and Outdated Components". [V] | Source: [OWASP Top 10:2025 introduction](https://top10.owasp.org/2025/0x00_2025-Introduction)

### Inferences
- archplan already has the call sequences and the component-to-dependency edges needed for a reachability overlay: read OSV advisories (plain JSON, no dependency needed), match affected package and symbol, and mark which components and entry points reach it. That would answer "which exposed entry point reaches the vulnerable dependency", which package-level scanners do not show.
- Unlike govulncheck, archplan does not need a successful build. That is an advantage in coverage and a cost in precision, and should be stated.
- Symbol-level advisory data is good for Go and thin for PyPI, npm and Hex. Package-level reachability ("is the package imported from a reachable component") is the realistic fallback.
- The detect-secrets baseline file is a simple, proven pattern for "fail only on new findings" and transfers directly to archplan findings.
- Secrets and IaC scanning are out of archplan's scope. The useful connection is to ingest their SARIF output and attach findings to components.

### Gaps
- No peer-reviewed head-to-head comparison of reachability tools (govulncheck, Endor Labs, Semgrep Supply Chain, OSV-Scanner call analysis) on one dataset was found.
- Current version numbers of CycloneDX and SPDX were not re-verified.
- Hex (Elixir) advisory coverage in OSV and its symbol-level detail were not checked.

## 5. Architecture-level and design-level security analysis

### Takeaway
Design-level analysis is far less tooled than code-level SAST. The existing pieces are threat-modelling-as-code tools that need a hand-written model, a small research line on missing-authorization detection, and catalogues that map architectural tactics to CWEs. A tool that derives the model from code, as archplan does, fills the gap between them.

### Cited Findings

Threat modelling as code
- pytm (now at `OWASP/pytm`, MIT, v1.4.1 released 2026-11-01): the system is described in Python objects (actors, servers, datastores, dataflows, boundaries). The tool generates data-flow diagrams, sequence diagrams and a threat list from a rule library. [V for status, M for description] | Source: [pytm](https://github.com/OWASP/pytm)
- Threagile (MIT, last release v0.9.1 on 2024-07-30, last push 2026-04): YAML model of assets, trust boundaries and communication links, with built-in risk rules and generated diagrams and reports. Slow release cadence. [V for status, M for description] | Source: [Threagile](https://github.com/Threagile/threagile)
- OWASP Threat Dragon (Apache-2.0, v2.6.2 on 2026-05-10): diagram editor with STRIDE-style threat suggestions, JSON model files. [V for status, M for description] | Source: [Threat Dragon](https://github.com/OWASP/threat-dragon)

Data-flow diagram extraction from code
- Code2DFD extracts security-annotated data-flow diagrams from Java (Spring) microservice applications by keyword and framework detection, including properties such as encryption and authentication on flows. [M] | Source: [Schneider and Scandariato, 2023, arXiv 2304.12769](https://arxiv.org/abs/2304.12769)

Missing-authorization and access-control checking
- Sun, Xu and Su, USENIX Security 2011: infer per-role sitemaps from PHP applications and report pages reachable by forced browsing that a less privileged role should not reach. [M] | Source: [paper page](https://www.usenix.org/conference/usenix-security-11/static-detection-access-control-vulnerabilities-web-applications)
- RoleCast, OOPSLA 2011: finds missing security checks without a specification, by learning which checks consistently guard security-sensitive operations (database writes) and flagging paths that lack them. [M] | Source: [Son, McKinley, Shmatikov](https://dl.acm.org/doi/10.1145/2048066.2048146)
- MACE, CCS 2014: detects privilege escalation by comparing the authorization context (user, role, session, ownership) across all paths that reach the same resource and flagging inconsistencies. [M] | Source: [Monshizadeh et al.](https://dl.acm.org/doi/10.1145/2660267.2660337)
- Near and Jackson, ICSE 2016 (SPACE): extracts data exposures from Rails applications and checks them against a catalogue of access-control patterns (ownership, public objects, authentication, explicit permission and others). [M] | Source: [paper](https://dl.acm.org/doi/10.1145/2884781.2884836)
- AuthCheck, 2019: static analysis for Spring that tracks authorization state with a finite-state machine and reports CWE-862 (missing authorization) and CWE-863 (incorrect authorization). [V] | Source: [Piskachev et al., TAPAS 2019](https://staticanalysis.org/tapas2019/talks/TAPAS_2019_paper_8.pdf)
- Muñoz (HP Software Security Research, 2016): static detection of missing and inconsistent authorization checks from configuration, annotations and programmatic APIs. A related US patent uses Spring Security as its example. [V] | Source: [talk slides](https://speakerdeck.com/pwntester/automatic-detection-of-inadequate-authorization-checks-in-web-applications); [US 9171168](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/9171168)
- route-detect: Semgrep-based tool that enumerates web routes per framework and marks each as authenticated, unauthenticated or with a given role, to surface unguarded routes. BSD-3-Clause, last push 2025-09-11. [V] | Source: [route-detect](https://github.com/mschwager/route-detect); [Zscaler blog](https://www.zscaler.com/de/blogs/cybersecurity-best-practices/route-detect-appsec-tool)
- Commercial example: Klocwork's `SPRING.AUTHZ.MISSING` checker. [V] | Source: [Klocwork reference](https://help.klocwork.com/2024/en-us/reference/spring.authz.missing.htm)
- Broken Access Control is ranked first in OWASP Top 10:2025, as in 2021. It maps to 40 CWEs, and Server-Side Request Forgery was merged into it. [V] | Source: [OWASP Top 10:2025 introduction](https://top10.owasp.org/2025/0x00_2025-Introduction)

Architectural weakness catalogues and smells
- CAWE (Common Architectural Weakness Enumeration): Santos, Tarrit and Mirakhorli, ICSA Workshops 2017. It catalogues 224 CWE weaknesses that are rooted in architecture, organised by security tactic (for example "Authorize Actors", "Validate Inputs", "Limit Exposure"). A later dissertation counts 223. [V] | Source: [catalogue record](https://archive.cps-vo.org/node/44464); [Santos et al., JSS 2019 preprint](https://joannacss.github.io/preprints/jss19-preprint.pdf)
- MITRE incorporated this work as CWE view 1008 "Architectural Concepts", with one category per tactic (for example CWE-1012 "Cross Cutting"). [V for 1012, M for 1008] | Source: [CWE-1008](https://cwe.mitre.org/data/definitions/1008.html); [CWE-1012](https://cwe.mitre.org/data/definitions/1012.html)
- Microservice security smells: a multivocal review identifies ten smells (for example insufficient access control, publicly accessible microservices, unnecessary privileges, non-encrypted data exposure, hardcoded secrets, centralized authorization, multiple user authentication) and matching refactorings. [M] | Source: [Ponce et al., JSS 2022, arXiv 2104.13303](https://arxiv.org/abs/2104.13303)
- Relevant CWE identifiers for model-level detectors: CWE-306 (missing authentication for critical function), CWE-862 (missing authorization), CWE-863 (incorrect authorization), CWE-639 (authorization bypass through user-controlled key), CWE-20 (improper input validation), CWE-501 (trust boundary violation), CWE-284 (improper access control). [M] | Source: [MITRE CWE](https://cwe.mitre.org/)

### Inferences
- Mapping of archplan's existing detectors to prior art:
  - Open or unguarded entry points: route-detect, Sun et al. 2011, CWE-306 and CWE-862, CAWE tactic "Authorize Actors".
  - Unguarded paths to sinks: RoleCast (a path to a sensitive operation with no check), CWE-862.
  - Unchecked input: CAWE tactic "Validate Inputs", CWE-20.
  - Trust-zone dependency violations: Threagile and pytm trust boundaries, CWE-501.
  - Untested denial paths: no direct prior tool was found. This appears to be a distinguishing feature.
- Two techniques from the research are cheap to add on top of archplan's model and need no user specification:
  - Consistency checking (RoleCast, MACE): if most paths to a sink or most sibling handlers on a router pass a given guard and one does not, flag the outlier. This uses majority behaviour as the specification and tends to give high-precision findings.
  - Authorization-context comparison (MACE): compare which guards protect read versus write access to the same type or datastore.
- archplan could emit pytm or Threagile models (and a data-flow diagram) from its extracted architecture. That would give threat-modelling tools a generated, always current input and give archplan a threat rule library for free. Code2DFD shows this is publishable work for Java. No equivalent was found for Python, TypeScript, Go or Elixir.
- Adding CWE identifiers and CAWE tactic names to each archplan finding makes the output comparable with SAST tools and with OWASP Top 10 categories.

### Gaps
- No independent precision or recall figures were found for route-detect, AuthCheck or commercial missing-authorization checkers on real applications.
- Recent (2024 to 2026) peer-reviewed static broken-access-control detection for Django, FastAPI, Express/NestJS, Go routers or Phoenix was not found in this session. The cited research targets PHP, Rails and Spring and is mostly from 2011 to 2019. A dedicated search may find newer work.
- Data-flow diagram extraction tools for languages other than Java were not found.
- No benchmark of design-level or architectural security findings exists that could serve as ground truth.

## 6. Output and interchange

### Takeaway
SARIF 2.1.0 is the interchange format every relevant tool and platform accepts. Version 2.2 is still in drafting. Stable fingerprints plus a baseline are the standard way to deduplicate and to report only new findings. CWE identifiers are the join key to OWASP Top 10:2021 and 2025.

### Cited Findings

SARIF
- SARIF 2.1.0 has been an OASIS Standard since 2020, with a later errata release. [V] | Source: [OASIS announcement](https://oasis-open.org/?p=1786); [SARIF 2.1.0 errata 01](https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/sarif-v2.1.0-errata01.pdf)
- SARIF 2.2 work started with a starter document in October 2023. No evidence was found that it has been finalised. Open proposals include a standard security-severity property and a precision property. [V] | Source: [OASIS SARIF list, Oct 2023](https://lists.oasis-open.org/archives/sarif/202310/msg00006.html); [sarif-spec issue 612](https://github.com/oasis-tcs/sarif-spec/issues/612)
- Relevant SARIF 2.1.0 objects: `result.ruleId`, `level`, `locations`, `codeFlows`/`threadFlows` (ordered path steps, used for taint paths), `relatedLocations`, `fingerprints` and `partialFingerprints`, `baselineState` (`new`, `unchanged`, `updated`, `absent`), `suppressions`, and `taxonomies` for CWE references. [M] | Source: [SARIF 2.1.0 specification](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
- GitHub code scanning deduplicates alerts across runs with `partialFingerprints` (it computes `primaryLocationLineHash` when the tool supplies none), reads a `security-severity` rule property (0.0 to 10.0) to rank security findings, and reads CWE tags of the form `external/cwe/cwe-862` from rule properties. [M] | Source: [GitHub: SARIF support for code scanning](https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/sarif-support-for-code-scanning)

OWASP Top 10 and CWE mapping
- OWASP Top 10:2025 categories: A01 Broken Access Control; A02 Security Misconfiguration (up from fifth); A03 Software Supply Chain Failures (new, expands 2021 A06); A04 Cryptographic Failures; A05 Injection; A06 Insecure Design; A07 Authentication Failures (renamed); A08 Software or Data Integrity Failures; A09 Security Logging and Alerting Failures (renamed); A10 Mishandling of Exceptional Conditions (new). [V] | Source: [OWASP Top 10:2025 introduction](https://top10.owasp.org/2025/0x00_2025-Introduction)
- Changes from 2021: SSRF (2021 A10) is merged into A01. Cryptographic Failures, Injection and Insecure Design each moved down two places. [V] | Source: [OWASP Top 10:2025 introduction](https://top10.owasp.org/2025/0x00_2025-Introduction)
- Method: 248 CWEs across the ten categories, at most 40 per category. Data from more than 2.8 million applications and about 175,000 CVE-to-CWE records. Eight categories come from data and two from a community survey. Per-category CWE counts given: A01 40, A02 16, A03 5, A04 32, A05 38, A07 36, A09 5, A10 24. [V] | Source: [OWASP Top 10:2025 introduction](https://top10.owasp.org/2025/0x00_2025-Introduction)
- Release timing: presented in November 2025 at OWASP Global AppSec. Sources disagree on when it became final (one says the list was final in November 2025 with the write-up in preview, another says the final version came in January 2026). [V, conflicting] | Source: [The Register, 2025-11-11](https://www.theregister.com/2025/11/11/new_owasp_top_ten_broken/); [Qualys blog, 2026](https://blog.qualys.com/qualys-insights/2026/06/15/what-changed-in-owasp-top-10-2025-and-recommendations-for-each-category)

Deduplication and baselining practice
- detect-secrets commits a baseline file and fails only on findings not in it. [M] | Source: [detect-secrets](https://github.com/Yelp/detect-secrets)
- Google and Meta report that showing only findings on changed code at review time is what makes developers act on them. [M] | Source: [Sadowski et al., CACM 2018](https://dl.acm.org/doi/10.1145/3188720); [Distefano et al., CACM 2019](https://dl.acm.org/doi/10.1145/3338112)

### Inferences
- archplan can emit SARIF 2.1.0 with only the standard library, since it is plain JSON. A call-sequence path from entry point to sink fits `codeFlows`/`threadFlows` directly, so GitHub would render archplan's path findings step by step.
- Line-based fingerprints are a poor fit for model-level findings because they change with unrelated edits. archplan should set its own `partialFingerprints` from stable model identities, for example a hash of detector id, entry-point qualified name, sink qualified name and the missing guard kind. That keeps a finding stable across refactors that do not change the architecture.
- Each detector should carry a CWE tag and both OWASP mappings (2021 and 2025), because compliance users still reference 2021. The mapping table is small: most archplan detectors land in A01 (CWE-862, CWE-306, CWE-284), A06 Insecure Design (CWE-501) and A05 Injection or input validation (CWE-20).
- A committed baseline file plus SARIF `baselineState` gives "new findings only" behaviour in CI with no service dependency.
- A reachability overlay on dependencies (section 4) would map to the new 2025 A03 category.

### Gaps
- The per-category CWE lists for OWASP Top 10:2025 (including counts for A06 and A08) were not retrieved. They are on the individual category pages.
- The final publication date of OWASP Top 10:2025 is disputed between sources.
- Whether SARIF 2.2 reached Committee Specification status by November 2026 could not be confirmed.
- No empirical study on fingerprint stability or deduplication accuracy across SAST tools was found.
