# Taxonomy map — surfaces & findings to recognized classes

We tag each attack-surface family (and every confirmed finding) with identifiers from recognized
taxonomies so results are comparable and auditable. These are reference IDs only; the authoritative
descriptions live at the linked sources in `SOURCES.md`. Mapping and all prose here are ours.

Legend: **ATT&CK** = MITRE ATT&CK technique; **ATLAS** = MITRE ATLAS technique; **LLM** = OWASP Top
10 for LLM Applications 2025; **ASVS** = OWASP ASVS v5.0 chapter.

## Product-level threat model

`envh` is fundamentally a **Credential Access** defense. Its reason to exist is to deny the
following to an unprivileged agent/dependency:

- **ATT&CK T1552 Unsecured Credentials** (esp. T1552.001 credentials in files) — the `.env`-on-disk
  problem envh removes.
- **ATT&CK T1555 Credentials from Password Stores** / **T1528 Steal Application Access Token** — the
  vault is the store; a session id is a bearer token.
- Out of scope by design (acknowledged, root-flavored): **T1003 OS Credential Dumping**,
  **T1055 Process Injection**, **T1552.007 /proc** memory reads.

On the AI side: **ATLAS** adversary goals around model/agent abuse, and OWASP **LLM06 Excessive
Agency**, **LLM01 Prompt Injection**, **LLM02 Sensitive Information Disclosure**, **LLM03 Supply
Chain**.

## Surface family → taxonomy

| Family | ATT&CK | ATLAS | OWASP LLM | ASVS |
|---|---|---|---|---|
| F1 socket-protocol | T1190 | — | — | V13 (API), V5 (validation) |
| F2 request-lifecycle | T1499 | — | — | V11 (business logic) |
| F3 session-tokens | T1528, T1552 | — | — | V3 (session), V7 (tokens) |
| F4 preset-and-import | T1565 (data manip.) | AML.T0024 (exfil via ML inference API — oracle analogue) | LLM05 (improper output), LLM02 | V1 (architecture), V5 |
| F5 config-parsing | T1499.004 | — | — | V5 |
| F6 peer-creds-identity | T1134 (token/identity) | — | — | V3, V13 |
| F7 client-side-run | T1055, T1057 | — | — | V13 |
| F8 info-leak | T1552 | AML.T0024 | LLM02 | V8 (data protection) |
| F9 dos-resilience | T1499 | AML.T0029 (denial of ML service) | LLM10 (unbounded consumption) | V11 |
| F10 hook-bypass | T1562 (impair defenses) | AML.T0051 (LLM prompt injection) | LLM01 | V5 |
| F11 supply-chain | T1195 | AML.T0010 (ML supply chain) | LLM03 | V10 (malicious code) |
| F12 cross-user | T1078 (valid accounts) | — | — | V3, V8 |
| F13 excessive-agency / confused-deputy | T1548 (abuse elevation) | AML.T0053 | LLM06, LLM08 | V1, V11 |
| F14 detection / defense-in-depth | T1562 | — | — | V7 (logging) |
| F15 host-config / hardening-gate matrix | T1200 / T1556 (per gate) | — | — | V1, V14 (config); NIST SP 800-53 SI-6 |
| F16 automated fuzz sweep | T1190 | — | — | V13 |

## Finding tags

Each confirmed finding in `FINDINGS.md` carries a `taxonomy` list using these IDs (schema field in
`CHARTER.md` §8), e.g. a new import oracle → `["ATTACK:T1552","ATLAS:AML.T0024","OWASP-LLM:LLM02","ASVS:V8"]`.
This lets a reader cross-reference every finding to the public body of knowledge and lets us state
coverage in a shared vocabulary rather than ad-hoc labels.

> Note: a few ATLAS IDs above are approximate analogues (ATLAS targets ML systems; `envh` is a
> classical tool that *defends* agents), chosen for the closest adversary intent. Confirm exact
> ATLAS IDs on atlas.mitre.org before external publication.
