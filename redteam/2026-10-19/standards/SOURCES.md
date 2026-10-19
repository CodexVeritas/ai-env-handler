# Standards this red-team is anchored to — sources, licenses, and how we use each

This exercise is modeled on how professional security teams (Anthropic, Microsoft, OWASP, NIST,
MITRE) run AI red teaming. We **cite and link** these standards and write our own mappings against
them; we do **not** reproduce their text. Licenses differ, so the "how we use it" column says what
we may copy vs. only reference.

Ranked by relevance to `envh` (a Linux, human-approval **secrets broker** that defends against AI
agents and supply-chain dependencies).

| # | Standard | Maintainer | License | How we use it (copy vs reference) |
|---|---|---|---|---|
| 1 | [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/) + [GenAI Red Teaming Guide](https://genai.owasp.org/resource/genai-red-teaming-guide/) | OWASP GenAI Security Project | CC BY-SA 4.0 | **Reference + our own mapping only.** Its risk IDs (LLM01 prompt injection, LLM02 sensitive-info disclosure, LLM03 supply chain, LLM06 excessive agency, LLM07 system-prompt leakage) tag our surfaces/findings. We do not paste OWASP text; if we ever did, that file would have to carry the CC BY-SA 4.0 notice and share-alike, kept out of the MIT source tree. |
| 2 | [MITRE ATLAS](https://atlas.mitre.org/) | MITRE | ATLAS/ATT&CK Terms of Use (free license, attribution required) | **Reference + IDs only.** We tag AI-specific findings with ATLAS technique IDs; any copied ATLAS text would need MITRE's attribution/permission block. |
| 3 | [MITRE ATT&CK](https://attack.mitre.org/) ([Terms of Use](https://attack.mitre.org/resources/legal-and-branding/terms-of-use/)) | MITRE | ATT&CK Terms of Use (free license, attribution required) | **Reference + IDs only.** `envh` is a Credential-Access (TA0006 / T1552 Unsecured Credentials) defense; we tag findings and the threat model with ATT&CK IDs. |
| 4 | [NIST AI RMF (AI 100-1)](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf) + [Generative AI Profile (AI 600-1)](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf) | NIST | US public domain (17 U.S.C. §105) | **May copy verbatim with attribution courtesy.** The Govern/Map/Measure/Manage functions frame our governance wrapper; we summarize rather than bulk-paste to keep this light. |
| 5 | [OWASP ASVS v5.0](https://owasp.org/www-project-application-security-verification-standard/) | OWASP | CC BY-SA 4.0 | **Reference + our checklist mapping.** Verifies the broker itself (secrets mgmt, access control, crypto storage, logging). We write our own checklist citing ASVS section numbers. |
| 6 | [NIST SP 800-115](https://csrc.nist.gov/pubs/sp/800/115/final) | NIST | US public domain | **May copy; we summarize.** Its planning→discovery→attack→reporting phase discipline backs our process. |
| 7 | [Microsoft "Lessons from Red Teaming 100 GenAI Products"](https://arxiv.org/abs/2501.07238) + [PyRIT](https://github.com/Azure/PyRIT) | Microsoft | paper: arXiv (quote+attribute); PyRIT: MIT | **Reference the paper's ontology/lessons; PyRIT is MIT and may be vendored as tooling** for automated probing of the hook/agent paths. |
| 8 | Anthropic [Challenges in red teaming](https://www.anthropic.com/news/challenges-in-red-teaming-ai-systems), [Frontier threats RT](https://www.anthropic.com/news/frontier-threats-red-teaming-for-ai-safety), [RSP](https://www.anthropic.com/rsp), [Best-of-N Jailbreaking](https://arxiv.org/abs/2412.03556) | Anthropic | proprietary / arXiv | **Reference only.** Shapes our method: diverse+expert attackers, iterate qualitative→automated, large-N effort-scaled reporting, pre-committed gates. |
| 9 | Academic: [Perez 2022](https://arxiv.org/abs/2202.03286), [Feffer AIES 2024 "Silver Bullet or Security Theater?"](https://arxiv.org/abs/2401.15897), [PersonaTeaming 2025](https://arxiv.org/html/2509.03728v3), [Ganguli 2022](https://arxiv.org/abs/2209.07858) | various | arXiv | **Reference only.** Evidence for attacker diversity, mandatory threat model/scope, dedup/clustering, automated+human blend. |
| 10 | Threat-intel-led RT: [TIBER-EU](https://www.ecb.europa.eu/paym/cyber-resilience/tiber-eu/html/index.en.html), [CBEST](https://www.bankofengland.co.uk/), [CREST](https://www.crest-approved.org/) | ECB / BoE / CREST | institutional | **Reference only.** The regulator-mandated gold standard; we borrow its principles (intel-led scenarios, independent test management, mandatory remediation/closure). |
| — | [NIST SP 800-53 SI-6](https://csrc.nist.gov/) ; [OWASP SAMM](https://owaspsamm.org/model/verification/requirements-driven-testing/) | NIST / OWASP | public domain / CC BY-SA | **Reference.** Back the Tier-2 negative/assurance tests ("a security function never degrades silently"). |

## Note on "validated by real-world results"

The strongest authority signal is **government/regulator mandate** (EU DORA Art. 26 requires
threat-led pen-testing; TIBER-EU/CBEST are how regulators deliver it) and **empirical grounding in
observed incidents** (ATT&CK/ATLAS are built from real attacks; Microsoft's lessons from 100 real
products; Anthropic's large-N bounty results). We rank by how many of those signals each standard
satisfies — see `METHODOLOGY.md`.

**Confidence / verification:** exact ATLAS technique counts, the ATLAS `atlas-data` repo license,
and the precise OWASP agentic numbering should be confirmed on the primary pages before being
relied on in any published artifact; they were gathered via search summaries and are used here only
as reference IDs.
