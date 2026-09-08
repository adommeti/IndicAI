# UC1 synthetic helpdesk golden set v1

150 authored synthetic utterances: 50 Hindi (35 Deva, 15 Latn), 50 Telugu (Telu),
50 Tamil (Taml). No real employee data or recordings. Native-language editorial
review is still pending; these are initial authored references, not human-approved
company policies. IDs and labels must not be changed to improve evaluation scores.

20 utterances contain prompt injection (7 Hindi, 7 Telugu, 6 Tamil). Each asks
for an exact marker while attempting rule disclosure, ticket bypass or an English
reply. Targets measure literal compliance only; they cannot detect every semantic
attack outcome. Password expiry always routes to `file_ticket`.

Expected article IDs refer to this synthetic KB contract, for later retrieval wiring:

| ID | Synthetic reference procedure |
|---|---|
| kb-vpn | Check internet, sign in to company VPN, complete authenticator challenge. |
| kb-laptop | IT portal replacement form, device details, reason, manager approval. |
| kb-leave | HR portal leave section contains balance, policy, application and cancellation. |
| kb-expense | Expense portal accepts receipts and shows claim status and rejection reasons. |
| kb-outlook | Check connection, disable Work Offline, run Send/Receive, collect remaining error. |
| kb-wifi | Join company Wi-Fi with company identity, verify browser connectivity. |
| kb-badge | Request through onboarding portal; admin confirms collection details. |

These describe a fictional employer. No leave entitlement, reimbursement deadline,
password-reset authority or actual corporate policy is inferred. Reference replies
are topic-level acceptable guidance; this harness does not score their similarity.
Clarify labels denote underspecified symptoms; explicit requests to open a ticket
and identity/password changes route to support.

Audio is synthesized via `sarvam_tools_tts_speak`, Bulbul v3, 16 kHz WAV: Hindi
speaker `priya`, Telugu `kavya`, Tamil `vijay`, selected from the model catalogue.
`audio_manifest.jsonl` records hashes, duration, speaker and request IDs. The local
WAVs are ignored by git. Object-store archival is not configured in this repository;
copy these files alongside the manifest when transferring the golden set.
