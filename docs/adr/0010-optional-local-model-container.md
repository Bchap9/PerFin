# ADR-0010: An optional local-model container suggests what documents are; it never decides

Status: Accepted. Date: 2026-11-05. Partly supersedes ADR-0007 (its first
decision only: "PerFin contains no LLM client"). The MCP design in ADR-0007
is unchanged.

## Context
After 1.0.0-rc.2 the deterministic readers still miss documents that a person
reads at a glance: unfamiliar statement layouts, accounts whose number is
printed in an unusual way, exports with no institution name. Each miss is a
review item a person resolves by hand. The owner runs PerFin on a host with an
NVIDIA GPU (8 GB of VRAM), enough to run a 7-8B text model or a 3B-7B vision
model at 4-bit quantisation locally, and asked for this to be used, as an
optional container in the stack like Immich's machine-learning container.

ADR-0007 removed the in-app model fallback because "local only" is one
configuration change away from "remote", and because a model's answer must
never become a ledger entry on its own. Both concerns still hold; this ADR
keeps them as hard rules rather than as the absence of code.

## Decision
- **Optional and separate.** The model runs in its own containers, started
  only by the `ml` (CPU) or `ml-cuda` (NVIDIA GPU) Compose profile: an adapter
  (`deploy/ml/perfin_ml.py`: standard library only, prompt, JSON schema,
  bearer token) and a model runtime (Ollama). PerFin works exactly as before
  without them; `PERFIN_ML_URL` empty (the default) means no call is ever made.
- **Local only, enforced in code.** `PERFIN_ML_URL` must be an http(s) URL
  whose host is a loopback or private address or an internal-only name (one
  label such as `ml`, or `.local`/`.lan`/`.internal`/`.home.arpa`); settings
  refuse anything else, so the application does not start. Before every call
  the worker resolves the name and refuses when any address is not local. The
  HTTP client ignores proxy variables. In Compose the adapter and runtime sit
  on an internal network with no route out; the runtime reaches the internet
  only through the egress allowlist proxy, for model downloads.
- **Minimal input.** Only the worker calls it, only for a document whose kind
  is unknown or whose account nothing matched, and only with the document's
  extracted text (capped, `PERFIN_ML_MAX_CHARS`). Never the file, never ledger
  data, never another document.
- **Untrusted output** (as in ADR-0004). The answer must match a JSON schema
  and is checked again in the worker (`app.ml.client.validated`): known kinds
  only, shape-checked names and currencies, plausible dates, and an account's
  last four digits only when exactly those digits are printed in the text.
  What survives is stored on the document (`confidence.ml`) and used only as
  a suggestion: a proposed new account's name, type and institution, an
  account choice in the review queue, an unknown document kind (which only
  selects a parser; the parser's own checks still apply). Nothing is written
  to the ledger without a person's confirmation, and suggestions are marked
  as coming from the model.
- **Failure is silent.** A timeout, error or malformed answer leaves the
  document as it would have been without the model; repeated failures stop
  calls for five minutes.

## Amendment (2026-11-11): the model may be asked what kind of activity a phrase is

Brokerage statements are read row by row by action words and section
headings from fixed lists (INGESTION.md "Brokerage statements"). A row whose
words are in none of them was left unread and reported, and the statement
then did not add up. Naming such words is what a language model does well
and a rule table does not, so the owner asked for the model to help there.
The rules above hold unchanged; this adds one question under them.

- **A second, narrower question.** When a brokerage parser could not type
  some activity rows, the worker sends just the action phrases of those rows
  and the headings above them (`POST /v1/vocabulary`): normalised, every
  digit masked, at most 40 of each, 80 characters each. Not the document's
  text, no amounts, no account numbers, never ledger data. It is asked only
  when the parser's own vocabulary failed, and an answer kept on the
  document (`confidence.ml_vocabulary`) is used again without asking.
- **Closed lists, matched by position.** For each numbered phrase the model
  picks an activity type, for each heading a section kind, or `unknown`. The
  adapter and then the worker (`app.ml.client.validated_vocabulary`) keep
  only listed kinds and match them to the phrases PerFin sent by their
  number, never by text the model returns. The lists hold no way to say "this
  is not activity": a model cannot hide a table or a row, it can only
  decline, and what it declines stays reported as unread, as without a model.
- **A kind, never a figure.** The pages are read again by the same parser
  with the suggested kinds. Dates, amounts, quantities, prices and
  securities are the page's own, read exactly as for any other row, and the
  statement's reconciliation runs over the result as always.
- **Held until a person confirms.** A row typed this way carries the
  `suggested_type` doubt, scored below any confidence threshold a deployment
  may set. The importer never writes it: a line moving only cash waits in an
  `investment_lines_held` item while the rest of its statement is imported,
  a line moving units holds its statement. The review queue shows each such
  line with the kind suggested, marked as the model's, and "Import as read"
  is the person's confirmation.

Rejected for this step: sending the whole statement and letting the model
extract rows (its figures could not be told from the page's), free-text
explanations shown in the review queue (a crafted statement could put
advice there), and letting an arithmetic match alone stand in for a
person's confirmation.

## Alternatives considered
- **Keep no model at all (ADR-0007).** The owner's hardware and the review
  load argue for the option; the rules above keep its risks bounded.
- **A model client inside the worker image** (llama.cpp bindings). Heavier
  image for everyone, GPU drivers in the worker, and no clean on/off switch.
- **Let the model write transactions or accounts directly.** Rejected: a
  model's guess must not become a financial record without a person.
- **Hosted model APIs.** Rejected outright: documents never leave the host.

## Consequences
- Two optional services (`ml`, `ml-runtime`/`ml-runtime-cuda`), one internal
  network (`ml`), a model volume and a one-shot volume initialiser, all held to
  the same hardening checks as the rest of the stack
  (`tests/unit/test_compose_hardening.py`).
- A new image (`perfin-ml`) in the release workflow.
- docs/ML.md is the setup guide; README, INGESTION.md, SECURITY.md and
  security/what-leaves-the-host.md say what changed.
