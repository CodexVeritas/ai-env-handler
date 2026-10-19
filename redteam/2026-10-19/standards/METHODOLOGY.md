# Methodology — how this exercise maps to professional, research-backed practice

This pass is run the way security-focused teams run AI red teaming. Below, each element of our
process is tied to the standard or research finding that justifies it. We describe the standards in
our own words and link them (`SOURCES.md`); we do not reproduce their text.

## Process backbone (NIST SP 800-115 + PTES phases)

Our waves follow the classic planning → discovery → attack → reporting arc, instantiated as:

1. **Planning / pre-commitment** — a frozen, SHA-pinned `CHARTER.md` fixes scope, win conditions,
   out-of-scope, and the grading rubric *before* any attacker runs. (NIST SP 800-115 rules of
   engagement; Feffer AIES 2024: an unstated threat model turns red teaming into "security
   theater.")
2. **Discovery** — `SURFACE_MAP.md` partitions the attack surface into disjoint families (F1–F16)
   so coverage is explicit and work doesn't overlap.
3. **Attack** — waves of diverse attacker agents plus an automated large-N fuzz sweep.
4. **Reporting / closure** — independent grading with mandatory reproduction, then remediation with
   regression tests and a coverage/residual-risk statement.

## Governance wrapper (NIST AI RMF: Govern / Map / Measure / Manage)

- **Govern**: the charter's anti-drift commitments and the rule that only independently reproduced
  CONFIRMED findings ship.
- **Map**: the surface map and the AI-agent threat scenarios (§4a) locate where risk lives.
- **Measure**: structured JSON findings, severity rubric, taxonomy tags, and effort-scaled
  reporting make results comparable.
- **Manage**: remediation + regression tests + re-test; "security is never done."

## AI-red-teaming structure (OWASP GenAI Red Teaming Guide + Microsoft ontology)

- We test across the Guide's evaluation areas that apply to a local tool: **implementation**
  (the broker's guardrails), **system/infrastructure** (the socket, protocol, files, installer),
  and **runtime/agentic behavior** (the hook, excessive agency). Model evaluation (of an LLM
  itself) does not apply — `envh` is the *defense*, not a model.
- Findings use Microsoft's **System / Actor / TTP / Weakness / Impact** ontology, and we model
  **benign** over-reach as well as adversaries (Microsoft Lesson: benign actors hitting failures
  matter too). "Procedure = steps to reproduce" is why every finding carries exact repro commands.

## Evidence-based practices we adopted (and the research behind them)

| Practice | Why (evidence) |
|---|---|
| Pre-committed scope + the decisions findings drive | Feffer AIES 2024; OWASP planning phase |
| Diverse, independent attackers (personas/specializations) | PersonaTeaming 2025; Perez 2022 diversity frontier; Anthropic domain-expert + multilingual-gap findings |
| Independent adversarial verification with mandatory non-root reproduction | DEF CON GRT2 accept/reject; Microsoft "Procedure"; bug-bounty practice |
| Automated large-N fuzzing alongside reasoning agents | Best-of-N Jailbreaking (power law in samples); Microsoft Lesson 4 (automation broadens coverage) |
| Novelty / de-duplication tracking | Ganguli 2022 (dedup/clustering of ~39k attacks) |
| Severity anchored to impact; taxonomy mapping | Microsoft impact dimension; comparability (Anthropic's "incomparable results" concern) |
| Blue-team / detection validation (F14), not just "did the gate hold" | Anthropic layered RT + monitoring; NIST SP 800-53 SI-6; OWASP runtime phase |
| Coverage + explicit "what was NOT covered" | Microsoft Lesson 3 ("RT ≠ benchmarking") & 8 ("never done"); NIST field-testing beyond RT |
| Pre-committed gates; external-review path for high-severity | Anthropic RSP + third-party-evals initiative; DEF CON independent testing |

## Threat-intel-led principles we borrow (TIBER-EU / CBEST / CREST)

Too heavyweight to adopt wholesale for a solo OSS tool, but we borrow: intelligence-led scenario
design (model the real adversary — an AI agent / malicious dependency), strict scoping and
independent "test management" (the grader role is separate from attackers), and a mandatory
remediation/closure step. These frameworks are the clearest "validated by real-world results"
benchmark because financial regulators mandate them.

## What the prior (2026-10-05) method already did well, and what this pass adds

**Already best-in-class** (many real exercises lack these): pre-committed charter; structured
machine-analyzable findings; independent grading with mandatory non-root reproduction; remediation
with regression tests.

**Added this pass** (the evidence-based gaps): attacker **diversity**; an **automated fuzz** family
(F16); explicit **benign-actor / excessive-agency** modeling (F13); **blue-team/detection**
validation (F14); **multi-config hardening-gate** assurance via VM/CI (F15 / `../tiers/`);
**taxonomy mapping** (`TAXONOMY_MAP.md`); **world-novelty** cross-check; and a **coverage/residual-
risk** report. Internal LLM graders are independent of the attackers but not a third party, so
HIGH/CRITICAL findings are written for optional external human review.
