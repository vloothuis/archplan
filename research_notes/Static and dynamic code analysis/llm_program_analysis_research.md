# LLMs combined with static and dynamic program analysis: research and industrial evidence (state as of November 2026)

Notes on reading these findings:

- Each finding gives the year and, where known, the model and benchmark. Numbers from different papers are not comparable unless stated.
- "Verified" means I read the primary page. "Snippet only" means the number comes from a search summary and should be checked before it is quoted in a report.
- Several papers changed their headline numbers between arXiv versions. The version is named where this matters.

## 1. How well do LLMs detect vulnerabilities on their own?

### Takeaway
On function-level benchmarks with clean labels, LLMs asked for a yes/no vulnerability verdict perform near chance, and the older benchmarks that showed high scores had wrong labels and duplicated data. Results improve when the model gets inter-procedural context, and agentic benchmarks with execution (CyberGym, BountyBench) show low but fast-rising discovery rates, with patching much easier than detection.

### Cited Findings

Label quality and leakage in datasets

- PrimeVul (Ding et al., 2024, arXiv 2403.18624, later ICSE 2025) reports manually checked label accuracy of 25.0% for BigVul, 51.7% for CVEFixes, 60.0% for DiverseVul and 47.8% for CrossVul (figures cited from Chen et al.), against 86.0% and 92.0% for PrimeVul's two labelling methods. Verified. - [PrimeVul v2](https://arxiv.org/html/2403.18624v2)
- The same paper reports exact-copy duplication of 12.7% in BigVul, 18.9% in CVEFixes, 3.3% in DiverseVul and 0.0% in PrimeVul. Verified. - [PrimeVul v2](https://arxiv.org/html/2403.18624v2)
- StarCoder2 scored 68.26% F1 on BigVul and 3.09% F1 on PrimeVul (2024). Verified. - [PrimeVul v2](https://arxiv.org/html/2403.18624v2)
- On PrimeVul paired samples (vulnerable function and its fix), the share of pairs judged correctly on both sides was 5.67% for GPT-3.5 two-shot, 12.94% for GPT-4 chain-of-thought, and 22.70% for random guessing. The authors conclude these models are "no better than a random guess". These are 2024 models and are superseded as point estimates. Verified. - [PrimeVul v2](https://arxiv.org/html/2403.18624v2)

Newer models on the same benchmarks

- TREAT (2025, arXiv 2510.17163) reports Claude Sonnet 4 at 69.5% accuracy and 73.7% F1 on single-function PrimeVul, with GPT-4-turbo and GPT-5 near 69 to 70% F1. Performance on the paired task stayed much weaker. Snippet only. - [TREAT](https://arxiv.org/pdf/2510.17163)
- A 2026 multi-agent paper reports that LLM baselines still reach only 8.28% to 10.57% pair accuracy on the PrimeVul test set. Snippet only. - [Three Heads Are Better Than One](https://arxiv.org/pdf/2605.18153)
- SecVulEval (2025, arXiv 2505.19828) is a statement-level C/C++ benchmark with five levels of context including called functions. The best model, Claude 3.7 Sonnet, reached 23.83% F1 for finding the vulnerable statements with correct reasoning, with GPT-4.1 close behind. Snippet only. - [SecVulEval](https://arxiv.org/pdf/2505.19828)
- Vul-RAG (2024, arXiv 2406.11147) built PairVul and found existing techniques at 0.50 to 0.54 accuracy on vulnerable versus similar-but-correct code. Retrieval of vulnerability knowledge raised accuracy by 12.96% and pairwise accuracy by 110% relative. In a user study the generated knowledge raised human detection accuracy from 0.60 to 0.77. Snippet only. - [Vul-RAG](https://arxiv.org/html/2406.11147v2)

Sensitivity to context

- "Everything You Wanted to Know About LLM-based Vulnerability Detection But Were Afraid to Ask" (Li et al., 2025, arXiv 2504.13474, preprint) tested 13 LLMs on 2,000 vulnerable and patched pairs across 99 CWEs with callee and type context attached. The strongest models reached about 0.7 F1 on key CWEs at about 0.8 precision. The authors argue that earlier pessimistic results came from context-deprived evaluation, and that the hard part is naming the root cause, not the binary verdict. They also report limited generalisation and "overthinking" bias. No independent replication found. Snippet only. - [arXiv 2504.13474](https://arxiv.org/abs/2504.13474)
- VulnSage (2025, arXiv 2503.17885) reports 67.5% accuracy on vulnerabilities disclosed after the models' training cutoffs and finds that multi-function and multi-file context improves detection. Snippet only. - [VulnSage](https://arxiv.org/html/2503.17885v1)

Contamination

- A 2026 study built on 834 Linux kernel samples (CWE-Trace, arXiv 2606.20502) reports that contamination gave no measurable advantage, because most nominally contaminated samples carried no usable memorisation signal. Abstract-level, snippet only. - [CWE-Trace summary](https://www.emergentmind.com/papers/2606.20502)
- A 2026 study of LLM program repair uses metamorphic testing plus negative log-likelihood and reports evidence of leakage in repair benchmarks. Snippet only. - [arXiv 2604.21579](https://www.alphaxiv.org/abs/2604.21579)

Agentic and execution-based benchmarks

- CyberGym (UC Berkeley, 2025, ICLR 2026 oral) has 1,507 instances from real vulnerabilities in 188 projects. The agent must produce a proof-of-concept input that reproduces the bug. The first version reported a best result of 11.9% (OpenHands with Claude 3.7 Sonnet). Version 2 reported about 17.9% (OpenHands with Claude Sonnet 4), and thinking mode raised GPT-5 from 7.7% to 22.0%. The Berkeley blog later reports about 30% in one trial and about 67% across 30 trials for top agents. The runs also found previously unknown bugs: 35 zero-days and 17 incomplete historical patches in v2. Snippet only; numbers differ by version. - [CyberGym v2](https://arxiv.org/pdf/2506.02548v2); [Berkeley RDI blog](https://rdi.berkeley.edu/blog/cybergym/)
- BountyBench (2025, arXiv 2505.15216) covers 25 real systems and 40 bug bounties. Codex CLI with o3-high scored 12.5% on Detect, 47.5% on Exploit and 90% on Patch. Claude Code scored 57.5% on Exploit and 87.5% on Patch. Custom agents ranged 17.5 to 67.5% on Exploit and 25 to 60% on Patch. Verified from the abstract. - [BountyBench](https://arxiv.org/abs/2505.15216)
- CVE-Bench (ICML 2025) uses 40 critical web-application CVEs. The best agent framework exploited up to 13%. Snippet only. - [CVE-Bench](https://proceedings.mlr.press/v267/zhu25i.html)
- Meta's AutoPatchBench (2025, part of CyberSecEval 4) has 136 fuzzing-found C/C++ bugs from ARVO for testing AI patching. Model pass rates were not retrieved. - [Meta engineering](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)

### Inferences
- Any report of "LLM vulnerability detection F1" on BigVul, CVEFixes or DiverseVul should be discounted, because half or more of the labels are wrong.
- The paired-sample result is the most relevant one for a triage design: models tend to give the same verdict for a vulnerable function and its fixed version. A judge that sees only one function cannot be trusted to separate real findings from look-alikes. Giving the judge the surrounding callers, callees and types is the measured remedy.
- Detect is far harder than Exploit or Patch once the location is known (BountyBench 12.5% versus 47.5% and 90%). This supports a split where a deterministic detector proposes the location and the LLM reasons about a specific candidate.
- Single-trial success rates understate what repeated runs achieve (CyberGym 30% versus 67% over 30 trials). Repetition raises recall and cost together.

### Gaps
- I did not retrieve numbers for VulDetectBench, SecBench or CyberSecEval 3. They are not covered here.
- I found no systematisation-of-knowledge paper dedicated to LLM vulnerability detection with consolidated numbers for 2026 frontier models.
- Almost all benchmark work is on C/C++ and Java. I found no Python-specific vulnerability detection benchmark results of comparable quality.
- TREAT and SecVulEval numbers come from search summaries and need a check against the papers.

## 2. Neuro-symbolic and hybrid systems that pair LLMs with static analysis

### Takeaway
Hybrid designs outperform both pure LLM detection and unmodified static analysers in their own papers, mostly by using the LLM for three narrow jobs: inferring project-specific specifications (sources, sinks, sanitisers), judging path feasibility for a specific warning, and writing checkers. An independent 2026 replication on real projects found that these tools still have low recall and false-discovery rates above 85%, and that static analysers have the same problem. LLM triage of analyser alerts is the best-supported use: several studies and vendors report removing most false positives, with a measurable risk of dismissing true positives.

### Cited Findings

Specification inference and LLM-driven dataflow

- IRIS (Li, Dutta, Naik, ICLR 2025) uses GPT-4 to infer CWE-specific taint specifications for third-party APIs, feeds them to CodeQL, then uses the LLM again to triage the resulting paths. On CWE-Bench-Java (120 validated vulnerabilities in real Java projects averaging about 300,000 lines) it detected 55, against 27 for CodeQL alone, with a false discovery rate about 5 percentage points lower. It found 4 previously unknown vulnerabilities. An earlier arXiv version reported 69 detections; the published figure is 55. Snippet only. - [IRIS v3](https://arxiv.org/html/2405.17238v3)
- LLift (Li et al., OOPSLA 2024) post-processes use-before-initialisation warnings from the UBITect analyser on the Linux kernel. On nearly 1,000 cases it reported about 50% precision with no missed bugs in its test set, and 13 previously unknown bugs. Model: GPT-4 era. Snippet only. - [LLift](https://arxiv.org/abs/2308.00245v3)
- BugLens (Li et al., ASE 2025, "Hitchhiker's Guide Part II") refines taint-style warnings from Suture and CodeQL on the Linux kernel. Precision rose from 0.10 raw, and 0.50 with semi-automated refinement, to 0.72. It found 4 unreported vulnerabilities. The authors report that naive prompting ("is this warning a true positive?") was highly unreliable and that structured, step-by-step analysis guidance was needed to curb overconfidence. Snippet only. A search summary wrongly attributed the 0.10 to 0.72 figure to KNighter. - [BugLens](https://arxiv.org/html/2504.11711v3)
- LLMSA (2024, arXiv 2412.14399, preprint) decomposes an analysis into Datalog-style relations, some computed by parsing and some by the LLM. It reports 66.27% precision and 78.57% recall for taint detection on TaintBench (55 of 70 found), 72.37% precision and 85.94% recall for alias analysis, and 82.77% precision and 85.00% recall on Juliet. Snippet only. - [LLMSA](https://arxiv.org/html/2412.14399v1)
- LLMDFA (NeurIPS 2024) is a summary-based, compilation-free dataflow analysis driven by an LLM. Its project page claims precision and recall comparable to symbolic tools, but the 2026 replication below measured a 93.3% sampled false discovery rate and over 33 hours per Java project. - [Li et al. 2026](https://arxiv.org/html/2601.19239v1)
- RepoAudit (ICML 2025; Claude 3.5 Sonnet) is an agent that explores call paths on demand and checks path conditions. The ICML version reports 40 true bugs in 15 projects at 78.43% precision, and 185 new issues in a later deployment, of which 174 were confirmed or fixed. Other versions report 38 bugs at 65% precision. Snippet only. - [RepoAudit, ICML](https://proceedings.mlr.press/v267/guo25n.html); [RepoAudit v3](https://arxiv.org/html/2501.18160v3)
- GPTScan (ICSE 2024; GPT-3.5) matches smart-contract logic-bug scenarios with the LLM, then confirms with static analysis. Precision was over 90% on token contracts and 57.14% on large Web3Bugs projects, with recall over 70%. The static confirmation step removed about two thirds of false positives. Snippet only. - [GPTScan](https://arxiv.org/abs/2308.03314)

LLM-written checkers

- KNighter (Yang et al., 2025, arXiv 2503.09002) has the LLM synthesise Clang Static Analyzer checkers from historical kernel patches, then runs the checkers deterministically. The first version reported 70 new Linux kernel bugs (56 confirmed, 41 fixed, 11 CVEs). Version 3 reports 92 new bugs, 77 confirmed, 57 fixed, 30 CVEs. The venue (SOSP 2025) was not confirmed in my search. Snippet only. - [KNighter v3](https://arxiv.org/abs/2503.09002v3)

Independent replication

- Li, Jiang, Chen and Xiong (2026, arXiv 2601.19239) re-ran RepoAudit (Claude 3.5 Sonnet), KNighter (o3-mini), IRIS (GPT-4), LLMDFA (GPT-4) and INFERROI (GPT-4) against CodeQL and Semgrep. Verified. - [arXiv 2601.19239](https://arxiv.org/html/2601.19239v1)
  - On 222 known vulnerabilities, average recall of the LLM-based tools was 21.09% for C/C++ and about 33% for Java. KNighter, CodeQL and Semgrep found none of the C/C++ cases.
  - On 24 active projects, with 385 warnings hand-labelled, the sampled false discovery rate was 97.0% for RepoAudit, 94.4% for IRIS, 93.3% for LLMDFA, 92.3% for Semgrep and 85.3% for CodeQL (the best).
  - Of 363 false positives, 37.47% came from shallow inter-procedural or dataflow reasoning and 19.00% from imprecise source or sink identification. LLM-specific causes included mishandled control flow, missed implicit sanitisation and not following the prompt or CWE definition.
  - Nearly all misses by CodeQL, Semgrep and KNighter came from source/sink specifications that did not match project-specific APIs.
  - Cost: RepoAudit used up to 225M input tokens on one project and averaged 448 minutes. CodeQL averaged 21 minutes and Semgrep under 1 minute.
  - The authors conclude that LLM-based detectors are not yet reliable at project scale and recommend hybrid workflows, better context retrieval, hierarchical analysis and caching.

False-positive triage of static analysis alerts

- Tencent study (Du et al., 2026, arXiv 2601.18844): 433 alarms from an in-house analyser (328 false, 105 true). Hybrid LLM plus static analysis techniques removed 94 to 98% of false positives with high recall. Cost was 2.1 to 109.5 seconds and $0.0011 to $0.12 per alarm, against 10 to 20 minutes for manual inspection. Verified from the abstract. - [arXiv 2601.18844](https://arxiv.org/abs/2601.18844)
- "Sifting the Noise" (Xiong and Zhang, 2026, arXiv 2601.22952) compared three agent frameworks. On the OWASP Benchmark the false-positive rate fell from over 92% to 6.3% in the best configuration. On real CodeQL alerts the best setup identified up to 93.3% of false positives. Results varied with agent design, model, vulnerability category and cost. Snippet only. - [arXiv 2601.22952](https://arxiv.org/abs/2601.22952)
- ZeroFalse (2025, arXiv 2510.02534) enriches CodeQL alerts with flow traces and CWE-specific knowledge before LLM adjudication. Best F1 was 0.912 on the OWASP Java Benchmark and 0.955 on a real-world set. Reasoning models with CWE-specific prompts did best. Snippet only. - [ZeroFalse](https://arxiv.org/html/2510.02534v1)
- LLM4PFA (2025, arXiv 2506.10322) uses agent-planned constraint reasoning to judge inter-procedural path feasibility for warnings. Numbers not retrieved. - [LLM4PFA](https://arxiv.org/html/2506.10322v1)

Industrial reports

- Semgrep (2025, vendor blog): on over 2,000 findings labelled by its researchers, Assistant agreed with 96% of true-positive calls but only 41% of false-positive calls. An earlier implementation scored 91% and 25%. Semgrep states the design is deliberately conservative. A second post says Assistant handles about 60% of incoming triage for customers. Snippet only; vendor-reported. - [Semgrep, 96% post](https://semgrep.dev/blog/2025/building-an-appsec-ai-that-security-researchers-agree-with-96-of-the-time/); [Semgrep, 60% post](https://semgrep.dev/blog/2025/semgrep-is-confidently-handling-60-of-all-triage-for-users-without-reducing-coverage)
- Datadog (vendor blog): its SAST false-positive filter labels findings as likely true or false and explains why. Datadog reports a trade-off: prompts tuned to keep true positives let more false positives through, and prompts tuned to filter false positives missed real issues. Evaluated on the OWASP Benchmark. Snippet only. - [Datadog](https://www.datadoghq.com/blog/using-llms-to-filter-out-false-positives)
- GitHub Copilot Autofix (2024 beta data, vendor): median time to commit a fix for a CodeQL pull-request alert was 28 minutes with Autofix against 1.5 hours manually. A 2025 dashboard metric showed a much smaller gap for a different measure (about 15.6 hours with autofix against 17.4 hours without). Snippet only. - [GitHub blog](https://github.blog/news-insights/product-news/secure-code-more-than-three-times-faster-with-copilot-autofix/); [GitHub changelog](https://github.blog/changelog/2025-06-16-mean-time-to-remediate-metric-for-codeql-pull-request-alerts-now-included-on-the-security-overview-dashboard)

### Inferences
- The evidence favours the archplan design direction (deterministic candidates, LLM judgement) over LLM-only scanning. The three LLM roles with repeated support are: (1) supplying project-specific knowledge the analyser lacks, (2) judging a specific candidate with its surrounding code, and (3) writing a deterministic checker that is then run without the LLM.
- Headline precision in tool papers does not transfer to other projects. RepoAudit went from 78% in its own paper to a 97% sampled false discovery rate in the replication. Any detector-plus-judge design needs its own labelled sample from the target repositories.
- The dominant cause of misses in static tools is a specification that does not fit the project (62 to 64 of 64 misses). This is the gap an LLM with repository knowledge can close, and IRIS's doubling of CodeQL detections is the measured example.
- The dominant cause of LLM false positives is shallow inter-procedural reasoning (37%). A tool that already holds a call graph and call sequences can supply that structure deterministically, which the replication authors recommend.
- Triage quality is asymmetric and tunable. Semgrep chose to keep true positives (96%) at the cost of filtering fewer false positives (41%). Datadog reports the same trade-off. A design should state which error it prefers.
- Structured guidance beats an open "is this real?" question (BugLens). Triage prompts should walk through explicit checks tied to the finding type.
- OWASP Benchmark results are much better than results on real alerts and should not be used to predict field performance.

### Gaps
- I found no published measured results for Snyk, Meta or Google on LLM triage of static analysis alerts.
- I found no peer-reviewed study of LLM-generated CodeQL or Semgrep rules with precision and recall numbers; KNighter (Clang checkers) is the closest.
- I did not read the limitations section of the Tencent paper; the abstract says limitations exist but does not list them.
- Almost all hybrid work targets C/C++, Java or Solidity security bugs. I found no equivalent work on Python or on architecture-level findings.

## 3. LLMs with dynamic analysis

### Takeaway
The strongest real-world results come from pairing LLMs with execution: a crash, a sanitiser report or a passing test proves a finding and removes hallucinations by construction. Between late 2024 and mid 2026 the documented scale went from single bugs (Big Sleep) to hundreds (Claude Opus 4.6) to a vendor-reported ten thousand or more (Claude Mythos Preview), and the bottleneck moved from discovery to validation and patching capacity.

### Cited Findings

Fuzzing and harness generation

- Google OSS-Fuzz-Gen (reported November 2024): AI-generated and AI-improved fuzz targets found 26 vulnerabilities, including CVE-2024-9143 in OpenSSL, which had probably been present for about two decades. Coverage rose across 272 C/C++ projects by more than 370,000 lines. Secondary coverage of Google's post. - [The Hacker News](https://thehackernews.com/2024/11/googles-ai-powered-oss-fuzz-tool-finds.html)
- PromptFuzz (CCS 2024): generated drivers reached about 1.61 times the branch coverage of OSS-Fuzz's human-written drivers on 14 libraries and found 33 new bugs from 49 crashes, 30 confirmed. Snippet only. - [PromptFuzz](https://arxiv.org/html/2312.17677v1)
- Fuzz4All (ICSE 2024): LLM as input generator for compilers and solvers. 98 bugs in GCC, Clang, Z3, CVC5, OpenJDK and Qiskit, 64 confirmed as new, and 36.8% higher average coverage than prior fuzzers. Snippet only. - [Fuzz4All](https://arxiv.org/abs/2308.04748)
- ChatAFL (NDSS 2024): LLM-guided protocol fuzzing. A secondary source reports 9 new vulnerabilities against 4 for NSFuzz and 3 for AFLNet. Snippet only, secondary. - [LLM fuzzing survey](https://arxiv.org/pdf/2402.00350)
- TitanFuzz (ISSTA 2023): LLM-generated programs for fuzzing deep-learning libraries, with 20.98% and 39.97% API coverage on PyTorch and TensorFlow per a secondary source. Bug counts not retrieved. - [LLM fuzzing survey](https://arxiv.org/pdf/2402.00350)

Test generation

- CodaMosa (ICSE 2023; Codex): calls the LLM when search-based test generation stalls. On 486 Python benchmarks it gave significantly higher coverage on 173 and 279 benchmarks (against search-based and LLM-only baselines) and lower coverage on 10 and 4. Old model; the design pattern is the lasting result. Snippet only. - [CodaMosa](https://www.microsoft.com/en-us/research/wp-json/wp/v2/msr-research-item/925401)
- TestGen-LLM (Meta, FSE 2024): generated tests pass through filters that require the test to build, pass reliably and add coverage. On Instagram Reels and Stories, 75% built, 57% passed reliably and 25% increased coverage. It improved 11.5% of the classes it was applied to, and engineers accepted 73% of its recommendations. Snippet only. - [TestGen-LLM](https://arxiv.org/abs/2402.09171)
- ACH (Meta, 2025, arXiv 2501.12862): the LLM generates mutants that represent a specific concern (privacy), then generates tests that kill them. Engineers accepted 73% of tests (140 of 191) at WhatsApp and Messenger test-a-thons. Of 571 generated tests, 277 would have been discarded by a line-coverage criterion. The LLM-based equivalent-mutant detector had precision 0.79 and recall 0.47, rising to 0.95 and 0.96 after simple preprocessing. Snippet only; self-reported. - [ACH](https://arxiv.org/abs/2501.12862)

Agentic vulnerability discovery

- Sean Heelan, o3 on Linux ksmbd (May 2025). Verified. - [Heelan](https://sean.heelan.io/2025/05/22/how-i-used-o3-to-find-cve-2025-37899/)
  - Benchmark on a known bug (CVE-2025-37778) with about 3.3k lines of context, 100 runs: o3 found it in 8 runs, reported no bug in 66 and gave false positives in 28. Claude 3.7 Sonnet found it in 3 of 100 and Claude 3.5 Sonnet in 0.
  - With about 12k lines (100k tokens), o3 found the known bug in 1 of 100 runs, and some runs reported a new bug, CVE-2025-37899. The author gives a true-to-false ratio of about 1:50 for this setting.
  - The author states that a verification step is needed to filter output.
- Google Big Sleep (Project Zero and DeepMind). In late 2024 the agent, built on Gemini 1.5 Pro and seeded with a prior bug for variant analysis, found an exploitable stack buffer underflow in SQLite before release. 150 CPU-hours of fuzzing did not rediscover it. In August 2025 Google reported a first batch of about 20 flaws in open-source software such as FFmpeg. Secondary coverage. - [The Register, 2024](https://www.theregister.com/2024/11/05/google_ai_vulnerability_hunting); [TechCrunch, 2025](https://techcrunch.com/2025/08/04/google-says-its-ai-based-bug-hunter-found-20-security-vulnerabilities/)
- Google CodeMender (announced 6 October 2025) uses Gemini Deep Think models to patch vulnerabilities and had upstreamed 72 security fixes at announcement. - [DeepMind](https://deepmind.google/blog/introducing-codemender-an-ai-agent-for-code-security/)
- OpenAI Aardvark (announced October 2025, private beta) had found at least ten issues that received CVE identifiers at announcement. Aggregators say it was renamed Codex Security in 2026; I did not confirm this from OpenAI. - [The Register](https://theregister.com/2025/10/31/openai_aardvark_agentic_security)
- Anthropic, Claude Opus 4.6 (5 February 2026). Verified. - [Anthropic](https://www.anthropic.com/research/zero-days)
  - More than 500 high-severity vulnerabilities found and validated in open-source software.
  - Setup: the model ran in a virtual machine with standard utilities, debuggers and fuzzers, with no custom harness.
  - Methods shown: reading Git history to find a fix and then finding a caller missing the same check (Ghostscript); searching for commonly misused functions after fuzzing failed (OpenSC); reasoning about an algorithm's invariants (CGIF).
  - Validation: the work focused on memory corruption because crashes and sanitisers make it verifiable. Claude critiqued, de-duplicated and re-prioritised crashes, and human researchers validated every finding before reporting.
- Anthropic, Claude Mythos Preview and Project Glasswing (update dated 22 May 2026, vendor-reported): Anthropic and about 50 partners found more than 10,000 high or critical vulnerabilities. In open-source projects the model estimated 6,202 high or critical out of 23,019 total. Mozilla fixed 271 vulnerabilities in Firefox 150 found with Mythos Preview. Snippet only. - [Anthropic Glasswing update](https://www.anthropic.com/research/glasswing-initial-update)
- XBOW (2025, vendor-reported): reached the top of HackerOne's US leaderboard in June 2025 with about 1,060 submissions. Findings were automated, and its security team reviewed them before submission. - [XBOW](https://xbow.com/blog/top-1-how-xbow-did-it/)

DARPA AI Cyber Challenge (final at DEF CON 33, August 2025)

- DARPA reported that finalist systems discovered 54 of 63 synthetic vulnerabilities (86%) and patched 43 (68%), and found 18 real vulnerabilities. Early coverage used an uncorrected denominator of 70 (77%). - [DARPA](https://darpa.mil/news/2025/aixcc-results)
- Team Atlanta's Atlantis won. Its technical report (2025, arXiv 2509.14589) describes LLMs combined with symbolic execution, directed fuzzing and static analysis. Verified from the abstract. A paper summary gives 61% vulnerability coverage and a 72.1% patch success rate; snippet only. - [Atlantis report](https://arxiv.org/abs/2509.14589)
- Trail of Bits' Buttercup placed second, found 28 vulnerabilities and applied 19 patches, and reported 90% accuracy because every candidate was reproduced before submission. - [Trail of Bits](https://blog.trailofbits.com/2025/08/09/trail-of-bits-buttercup-wins-2nd-place-in-aixcc-challenge/)
- Trail of Bits' pre-final review states that Theori (third) and the fourth-placed team used LLMs as the main reasoning engine with traditional tools for validation, and that Team Atlanta required a proof-of-vulnerability or a patch as evidence before accepting a static-analysis report. Based on interviews before code release. - [Trail of Bits, Tale of the tape](https://blog.trailofbits.com/2025/08/07/aixcc-finals-tale-of-the-tape/)
- The fourth-placed team's paper (2025, arXiv 2509.07225) reports that LLM-based strategies found nearly all of its proofs-of-vulnerability and libFuzzer contributed one or two. It found 28 vulnerabilities (6 zero-days) and patched 14. Snippet only. - [FuzzingBrain](https://arxiv.org/abs/2509.07225)
- A systematisation paper on AIxCC exists (2026, arXiv 2602.07666). I read only its abstract, which gives no numbers. - [SoK AIxCC](https://arxiv.org/abs/2602.07666)

### Inferences
- Every system with a low false-positive rate has an executable oracle: a crash, a sanitiser report, a passing or failing test, or a killed mutant. Buttercup's 90% accuracy and the validated Anthropic findings follow from that gate, not from model judgement.
- The Heelan numbers show what the same class of model does without an oracle: 8 true reports against 28 false ones in 100 runs, and about 1:50 with more context. Read-only LLM review needs a verification step or a ranking step before a human or agent acts on it.
- For findings that cannot crash (design smells, architecture violations), the nearest equivalents to an oracle are a deterministic re-check of the claim against the model, or a generated test. ACH's pattern (state the concern as a mutant, then prove a test catches it) is a tested way to make a quality concern executable.
- Reading version-control history for fixes and then searching for unfixed siblings is a documented high-yield strategy (Big Sleep variant analysis, Opus 4.6 on Ghostscript, KNighter's patch-derived checkers).
- Filter chains turn a weak generator into an acceptable one: TestGen-LLM kept 25% of generated tests and engineers accepted 73% of what survived.

### Gaps
- The Mythos Preview and XBOW figures are vendor-reported and I found no independent audit. The Mythos counts include the model's own severity estimates.
- I did not retrieve the per-technique attribution (LLM versus fuzzing versus static analysis) from the Atlantis report or the AIxCC systematisation paper.
- Aardvark's 2026 results and rename come from aggregators and news sites and are unconfirmed.
- TitanFuzz and ChatAFL numbers come from a secondary survey.

## 4. LLMs for code quality rather than security

### Takeaway
LLMs detect simple, local smells (long method, large class) acceptably and do poorly on smells that depend on relationships between classes (feature envy, god class) unless metrics or dependency data are added to the prompt. Industrial code review deployments report that developers act on roughly a quarter to three quarters of comments depending on the measure, and grounding comments in explicit rules or analyser output is the factor most often linked to higher adoption.

### Cited Findings

Code and design smells

- Souza et al. (IEEE TSE, arXiv 2601.09873, 2026): four LLMs, nine smell types, 30 Java projects, ground truth from 76 developers reviewing 268 candidates. LLMs worked well on simple smells such as Large Class and Long Method. Voting between LLMs and static tools raised recall and added false positives on complex smells. Snippet only. - [arXiv 2601.09873](https://arxiv.org/pdf/2601.09873)
- A zero-shot Java benchmark over 20 projects (Ghadimi et al.) found feature envy (6.1 to 42.6%) and god class (2.0 to 38.9%) largely undetectable at snippet level, with long method the easiest. Snippet only. - [arXiv 2504.16027](https://arxiv.org/pdf/2504.16027)
- "Attentionsmelling" (ICSE 2026 showcase; GPT-4o) found that adding computed metrics to the prompt let the model classify God Class correctly, while Feature Envy stayed hard because it depends on cross-class context. Abstract only. - [ICSE 2026 listing](https://conf.researchr.org/details/icse-2026/icse-2026-brazilian-software-engineering-showcase/2/Attentionsmelling-Using-Large-Language-Models-to-Identify-Code-Smells)
- "Are We SOLID Yet?" (2025, arXiv 2509.03093): 240 snippets, four models, four languages, four prompt strategies. No prompt strategy was best across all SOLID principles. Snippet only. - [arXiv 2509.03093](https://arxiv.org/html/2509.03093v1)

Architecture recovery and conformance

- An ICSA 2026 paper (arXiv 2602.07609) checks code against architectural decision records with one LLM screening and three validating, across 980 records in 109 repositories. Results not retrieved. - [arXiv 2602.07609](https://arxiv.org/html/2602.07609v1)
- An ECSA 2025 paper evaluates off-the-shelf LLMs for architecture recovery from source code and analyses error types. Related work summarises a four-LLM study as strong on broad structure and styles and weak on fine-grained patterns and inter-class relations. Snippet only. - [ECSA 2025 listing](https://conf.researchr.org/details/ecsa-2025/ecsa-2025-call-for-research-papers/7/Automated-Software-Architecture-Design-Recovery-from-Source-Code-Using-LLMs); [SAKE](https://arxiv.org/pdf/2606.29520)
- An ESEM 2026 study of an agent recovering architecture for 17 Java microservice systems concludes that current capabilities are insufficient for fully reliable reverse engineering. Snippet only. - [ESEM 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ESEM.2026.62)
- ArchAgent (2026, arXiv 2601.13007) combines static analysis with LLM synthesis; its ablation found that dependency context improved the accuracy of recovered architectures. Snippet only. - [ArchAgent](https://arxiv.org/html/2601.13007v1)

Refactoring

- A study of GPT-4o on 100 Python classes (2026, arXiv 2601.13139) found a mean test pass rate of 0.844 across ten runs (standard deviation 0.01) after refactoring, and concludes hybrid approaches with program analysis and testing are needed. Snippet only. - [arXiv 2601.13139](https://arxiv.org/html/2601.13139v1)
- A 2025 review (arXiv 2510.03914) cites a study of 1,752 Extract Method scenarios in which up to 76.3% of LLM suggestions were wrong because of hallucinations, and notes that LLMs did better when given IDE static analysis. EM-Assist filters LLM suggestions with IDE checks and applies them with the IDE's refactoring engine. Snippet only. - [arXiv 2510.03914](https://arxiv.org/pdf/2510.03914)

Automated code review in industry

- Google AutoCommenter (2024): developers resolved about 40% of its best-practice comments, and the "useful" rating plateaued near 54%. Snippet only. - [arXiv 2405.13565](https://arxiv.org/html/2405.13565v1)
- Google, resolving reviewer comments with ML (2024): authors addressed 7.5% of all reviewer comments by applying a suggested edit. Snippet only. - [Google/UW paper](https://homes.cs.washington.edu/~rjust/publ/code_review_automation_aiware_2024.pdf)
- ByteDance BitsAI-CR (2025): 75.0% precision for review comments using a rule taxonomy and a second filtering model; the "outdated rate" (code changed after the comment) for Go was 26.7%. Snippet only. - [BitsAI-CR](https://arxiv.org/pdf/2501.15134)
- SGCR (HiThink, 2025): grounding review in explicit specifications gave 42% developer adoption against 22% for a baseline LLM. Snippet only. - [SGCR](https://arxiv.org/html/2512.17540v1)
- Ubisoft RevMate (2025): appreciation ratio of 28.3% for generated review comments. - [Ubisoft La Forge](https://www.ubisoft.com/en-us/studio/laforge/news/3SsR9KkpyUCBvy1Wyw3lTS/impact-of-an-llmbased-code-review-assistant-in-practice)
- An industrial study (arXiv 2412.18531) found 73.8% of automated comments marked resolved, while average pull-request closure time rose from 5 hours 52 minutes to 8 hours 20 minutes. Snippet only. - [arXiv 2412.18531](https://arxiv.org/pdf/2412.18531v1)
- A vendor-run 2026 benchmark (50 pull requests, 122 real bugs) shows the recall and precision trade-off: one tool caught 68.9% of bugs at 0.30 precision. Vendor source, LLM-judged. - [Tenki](https://tenki.cloud/benchmarks/code-reviewer)

Warning repair and analysis-guided repair

- Microsoft CORE (FSE 2024): a proposer LLM fixes static-analysis findings and a ranker LLM rejects bad fixes. 59.2% of Python files (52 checks) passed both the tool and human review; 76.8% of Java files passed the tool. A known failure is a "fix" that deletes code or suppresses the warning. Snippet only. - [CORE](https://www.microsoft.com/en-us/research/publication/core-resolving-code-quality-issues-using-llms/)
- CodeCureAgent (2025, arXiv 2509.11787): an agent classifies and repairs warnings, then builds, checks the warning is gone with no new warnings, and runs tests. Manual inspection of 291 cases found 86.3% correct fixes. Snippet only. - [CodeCureAgent](https://arxiv.org/abs/2509.11787v1)
- Google (2024 report): Gemini-generated fixes for sanitiser bugs were accepted for 15% of bugs, after automated tests filtered out invalid patches. - [Google Research](https://research.google/pubs/ai-powered-patching-the-future-of-automated-vulnerability-fixes/)

### Inferences
- The smell results match the security results: LLMs fail where the answer depends on relationships outside the snippet. An architecture model that supplies coupling, dependency and call data covers the input the studies found missing, and the metric-enriched prompt result suggests a detector should hand the LLM its computed evidence, not only the code.
- Review comments grounded in a named rule are adopted about twice as often as free-form LLM comments (SGCR 42% against 22%; BitsAI-CR's rule taxonomy). Findings from deterministic detectors are already rule-grounded.
- Repair and refactoring need a deterministic acceptance check: re-run the detector, confirm no new findings, run tests, and reject fixes that only delete or suppress. CORE and CodeCureAgent both rely on this.
- More comments are not better. One deployment saw high resolution rates and longer pull-request closure times at once.
- Adoption measures (resolved, outdated, appreciated, accepted) are not interchangeable across studies.

### Gaps
- I found no study measuring LLM detection of architecture-level smells (cyclic dependencies, layering violations, hub components) against ground truth.
- I did not retrieve results for the ICSA 2026 decision-violation paper or the ECSA 2025 recovery paper.
- I found no verified figures for Meta or Microsoft code review deployments; a LinkedIn figure (63.9% acceptance) came only through a secondary summary.
- Most smell studies use Java. Python-specific evidence is thin.

## 5. Known failure modes and methodological cautions

### Takeaway
The documented failure modes are fabricated or unprovable findings, different answers on repeated runs, shallow inter-procedural reasoning, degradation as context grows, and benchmarks with bad labels. Reasoning models and agents with tools raised capability sharply in 2025 and 2026, but the systems that report low false-positive rates all add an external check, so the caution about unverified LLM findings still holds.

### Cited Findings

- Fabricated findings at scale: curl ended its bug bounty at the end of January 2026 after a surge of low-quality AI-generated reports; by mid-2025 about 20% of submissions were such reports. Its maintainer also acknowledged valid bugs found with AI assistance. - [The Register](https://www.theregister.com/2026/01/21/curl_ends_bug_bounty/); [The New Stack](https://thenewstack.io/curls-daniel-stenberg-ai-is-ddosing-open-source-and-fixing-its-bugs/)
- Run-to-run inconsistency: Ullah et al. (IEEE S&P 2024, arXiv 2312.12575; GPT-4 era) found every tested LLM gave inconsistent answers on some tests at temperature 0.2, and some stayed inconsistent at 0.0. Superseded models, but later work reports the same effect. Snippet only. - [arXiv 2312.12575](https://arxiv.org/html/2312.12575v2)
- Heelan (2025; o3): the same prompt over 100 runs gave 8 correct reports, 66 "no bug" and 28 false positives. Verified. - [Heelan](https://sean.heelan.io/2025/05/22/how-i-used-o3-to-find-cve-2025-37899/)
- A 2026 study of 300 repeated scans of the same JavaScript code found the SAST baseline fully stable and LLM-only findings highly unstable, while the LLM found real issues SAST missed. Aggregator summary; check the paper before citing. - [Summary of arXiv 2606.15762](https://www.emergentmind.com/papers/2606.15762)
- A 2025 paper on bug injection and correction reports that instability persists at low temperature, with ensembles and after fine-tuning. Snippet only. - [arXiv 2509.06429](https://arxiv.org/pdf/2509.06429)
- Context-window effects: o3's detection of a known bug fell from 8 of 100 runs at about 3.3k lines to 1 of 100 at about 12k lines. Verified. - [Heelan](https://sean.heelan.io/2025/05/22/how-i-used-o3-to-find-cve-2025-37899/)
- Position effects: a 2025 paper finds LLMs localising vulnerabilities within a file do worse when the bug is near the end of a long file. Title and abstract only. - [arXiv 2502.06898](https://arxiv.org/pdf/2502.06898)
- Inter-procedural reasoning: 37.47% of 363 false positives from LLM-based and static tools came from shallow inter-procedural or dataflow reasoning. RepoAudit missed bugs behind call chains deeper than its three-layer default, complex control flow (13 cases) and concurrency (11 cases). Verified. - [arXiv 2601.19239](https://arxiv.org/html/2601.19239v1)
- Prompt adherence: the same study lists failure to follow the prompt or the CWE definition as a distinct cause of false positives. - [arXiv 2601.19239](https://arxiv.org/html/2601.19239v1)
- Overconfidence: BugLens reports that a direct true-or-false question on a warning frequently misclassifies real vulnerabilities, and that structured guidance is needed. Snippet only. - [BugLens](https://arxiv.org/html/2504.11711v3)
- Benchmark validity: label accuracy of 25% to 60% in BigVul, CVEFixes, DiverseVul and CrossVul. Verified. - [PrimeVul v2](https://arxiv.org/html/2403.18624v2)
- Tool papers overstate transfer: RepoAudit's own 78.43% precision against a 97.0% sampled false discovery rate in independent replication on other projects. - [RepoAudit](https://proceedings.mlr.press/v267/guo25n.html); [arXiv 2601.19239](https://arxiv.org/html/2601.19239v1)
- Version drift in headline numbers: IRIS (69 then 55), RepoAudit (38 or 40 bugs, 65% or 78% precision), KNighter (70 then 92 bugs), CyberGym (11.9% then 17.9%). - [IRIS v1](https://arxiv.org/html/2405.17238v1); [IRIS v3](https://arxiv.org/html/2405.17238v3)

Do reasoning models and agents change the conclusions?

- Capability rose: thinking mode moved GPT-5 from 7.7% to 22.0% on CyberGym (2025), and top agents later reached about 30% single-trial. - [CyberGym v2](https://arxiv.org/pdf/2506.02548v2); [Berkeley RDI blog](https://rdi.berkeley.edu/blog/cybergym/)
- Scale rose: 1 bug (Big Sleep, 2024), 20 (Big Sleep, 2025), over 500 validated (Opus 4.6, February 2026), over 10,000 vendor-reported (Mythos Preview, May 2026). Mozilla's count went up more than tenfold between Opus 4.6 and Mythos Preview. - [Anthropic zero-days](https://www.anthropic.com/research/zero-days); [Anthropic Glasswing update](https://www.anthropic.com/research/glasswing-initial-update)
- Verification stayed: Anthropic restricted the Opus 4.6 work to crash-verifiable bugs and had humans validate each one. Buttercup reproduced every candidate. Team Atlanta required a proof-of-vulnerability or patch. XBOW reviewed findings before submission. - [Anthropic zero-days](https://www.anthropic.com/research/zero-days); [Trail of Bits](https://blog.trailofbits.com/2025/08/09/trail-of-bits-buttercup-wins-2nd-place-in-aixcc-challenge/)
- The 2026 replication concluded LLM detectors were not reliable at project scale, but it used Claude 3.5 Sonnet, o3-mini and GPT-4, which are one to two generations behind the 2026 frontier. - [arXiv 2601.19239](https://arxiv.org/html/2601.19239v1)
- ZeroFalse (2025) found reasoning-oriented models with CWE-specific prompts gave the best triage results. Snippet only. - [ZeroFalse](https://arxiv.org/html/2510.02534v1)

### Inferences
- The stable conclusion across 2023 to 2026 is about system design, not model strength: unverified LLM findings are noisy, and findings gated by a deterministic or executable check are usable. Stronger models raise the yield of the gated design more than they remove the need for the gate.
- Because outputs vary between runs, a single LLM verdict on a finding is a sample. Designs should store the verdict with its evidence, prefer verdicts that can be re-checked deterministically, and treat repeated runs as a cost and recall decision.
- Keep the context for each judgement small and targeted. The measured drop from 8% to 1% detection when context grew fourfold argues for per-finding context slices (the candidate plus its callers, callees and types) over whole-module prompts.
- Academic replications lag the frontier by one to two model generations, and frontier results are mostly vendor-reported. Both should be labelled as such in a report.
- Evidence for quality and architecture findings is much thinner than for memory-safety bugs, which have a natural oracle. Conclusions about LLM triage of design findings are extrapolations from the security literature.

### Gaps
- I found no controlled study comparing 2026 frontier models with and without tools on the same triage task with independent labels.
- I found no measurement of hallucination rate for LLM triage explanations (as opposed to verdicts).
- The 300-scan variance study was seen only through an aggregator.
- I found no study of how well coding agents act on static-analysis findings at repository scale beyond warning-repair papers.
