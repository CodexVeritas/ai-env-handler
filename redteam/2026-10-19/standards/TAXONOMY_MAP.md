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
| F1 socket-protocol | T1190 | — | — | V4 (API), V2 (validation) |
| F2 request-lifecycle | T1499 | — | — | V2 (validation & business logic) |
| F3 session-tokens | T1528, T1552 | — | — | V7 (session), V9 (self-contained tokens) |
| F4 preset-and-import | T1565 (data manip.) | AML.T0024 (exfil via ML inference API — oracle analogue) | LLM05 (improper output), LLM02 | V2 (validation), V13 (config) |
| F5 config-parsing | T1499.004 | — | — | V2 (validation), V13 (config) |
| F6 peer-creds-identity | T1134 (token/identity) | — | — | V8 (authorization) |
| F7 client-side-run | T1055, T1057 | — | — | V15 (secure coding) |
| F8 info-leak | T1552 | AML.T0024 | LLM02 | V14 (data protection) |
| F9 dos-resilience | T1499 | AML.T0029 (denial of ML service) | LLM10 (unbounded consumption) | V2 (business logic / anti-automation) |
| F10 hook-bypass | T1562 (impair defenses) | AML.T0051 (LLM prompt injection) | LLM01 | V2 (validation) |
| F11 supply-chain | T1195 | AML.T0010 (ML supply chain) | LLM03 | V15 (secure coding) |
| F12 cross-user | T1078 (valid accounts) | — | — | V8 (authorization), V7 (session) |
| F13 excessive-agency / confused-deputy | T1548 (abuse elevation) | AML.T0053 | LLM06 | V8 (authorization), V2 (validation) |
| F14 detection / defense-in-depth | T1562 | — | — | V16 (security logging) |
| F15 host-config / hardening-gate matrix | T1200 / T1556 (per gate) | — | — | V13 (config); NIST SP 800-53 SI-6 |
| F16 automated fuzz sweep | T1190 | — | — | V4 (API), V2 (validation) |

> ASVS references are OWASP ASVS **5.0** chapters (V2 Validation & Business Logic, V4 API & Web
> Service, V7 Session Management, V8 Authorization, V9 Self-contained Tokens, V13 Configuration,
> V14 Data Protection, V15 Secure Coding, V16 Security Logging).

## Finding tags

Each confirmed finding in `FINDINGS.md` carries a `taxonomy` list using these IDs (schema field in
`CHARTER.md` §8), e.g. a new import oracle → `["ATTACK:T1552","ATLAS:AML.T0024","OWASP-LLM:LLM02","ASVS:V14"]`.
This lets a reader cross-reference every finding to the public body of knowledge and lets us state
coverage in a shared vocabulary rather than ad-hoc labels.

> Note: a few ATLAS IDs above are approximate analogues (ATLAS targets ML systems; `envh` is a
> classical tool that *defends* agents), chosen for the closest adversary intent. Confirm exact
> ATLAS IDs on atlas.mitre.org before external publication.
