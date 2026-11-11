# Local model hints (optional)

PerFin reads statements with deterministic parsers first. The optional `ml`
containers add a model running **on your own machine** that suggests what a
document is when those parsers are unsure: its kind, its institution, the
accounts it is about, and what kind of activity an unfamiliar wording on a
brokerage statement is. Suggestions show up as proposed accounts ("Accounts in
this file"), account choices and suggested kinds of activity in the review
queue, marked as coming from the model. Nothing is imported on the model's word alone (ADR-0010).

It works like Immich's machine-learning container: start it or not, PerFin
works the same either way.

## What you need

| Hardware | Text model (statements, exports) | Vision model (scans, photos) |
| --- | --- | --- |
| NVIDIA GPU, 8 GB VRAM (e.g. RTX 3070 Ti) | `qwen2.5:7b-instruct` (4-bit, about 5 GB), 1-3 s per document | `qwen2.5vl:3b` fits next to nothing else; load one model at a time |
| NVIDIA GPU, 12 GB+ | the same, or an 8B-14B model | `qwen2.5vl:7b` |
| CPU only | `qwen2.5:3b-instruct`; expect 10-60 s per document | not recommended |

For the GPU: the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host (`docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04
nvidia-smi` should list the card).

## Set up

1. Add to `deploy/.env`:

   ```sh
   PERFIN_ML_URL=http://ml:3003
   PERFIN_ML_TOKEN=<openssl rand -hex 32>
   # optional: ML_MODEL=qwen2.5:7b-instruct  ML_VISION_MODEL=
   ```

   `PERFIN_ML_URL` must be local: PerFin refuses to start with a public
   address.

2. Start the profile (`ml-cuda` for an NVIDIA GPU, `ml` for CPU):

   ```sh
   docker compose --profile ml-cuda up -d
   ```

3. Pull the model once. The runtime has no internet access of its own: start
   the `egress` profile, uncomment the two Ollama lines at the end of
   `deploy/egress-allowlist.txt`, then

   ```sh
   docker compose --profile ml-cuda --profile egress up -d egress
   docker compose --profile ml-cuda --profile egress exec ml-runtime-cuda \
     ollama pull qwen2.5:7b-instruct
   ```

   Comment the allowlist lines out again afterwards. (Alternatively, copy an
   Ollama models directory into the `ml_models` volume.)

4. Restart the worker so it reads the settings: `docker compose up -d worker`.

5. Check: `docker compose exec ml python -I /opt/perfin-ml/perfin_ml.py
   healthcheck && echo ok`, and `nvidia-smi` on the host shows the model
   loaded after the first document (under 8 GB).

## What it is asked, and what is done with the answer

- Only the worker calls it, only for a document whose kind is unknown or
  whose account nothing matched, with the document's extracted text (at most
  `PERFIN_ML_MAX_CHARS` characters). Never the file itself, never your ledger.
- The answer is checked before use: known document kinds only; an account's
  last four digits only if exactly those digits are printed in the document;
  names, currencies and dates shape-checked. It is stored on the document and
  used to fill in proposed accounts and account suggestions. You confirm
  everything.
- **Words PerFin's vocabulary does not know** (brokerage statements). When a
  statement words a line or a section in a way the parser has no rule for
  ("Periodic Investment Plan", a heading like "Sweep Program Activity"), the
  worker asks the model what kind of activity that phrase is, from a fixed
  list (purchase, sale, dividend, interest, fee, deposit, withdrawal, ...).
  It sends those phrases together with the statement's text, so the model
  can read each phrase on the rows it stands on (up to
  `PERFIN_ML_MAX_CHARS`; raise it, with `ML_CONTEXT` on the adapter, if
  your statements are long and your GPU has room). The model cannot supply
  a figure or say a table is not activity; it can only pick a kind or
  decline, and a kind per phrase is all that comes back. The statement is then read again with the
  suggested kinds: the dates and amounts are the page's own, and the
  statement's totals are checked as always. A line typed this way is never
  imported on its own. It shows up in the review queue as "kind suggested
  by the local model" with the kind read ("Read as"), and **Import as
  read** confirms it. Lines that only move cash wait while the rest of the
  statement is imported; a line that moves shares holds its statement until
  you confirm. The answer is kept with the document, so confirming does not
  ask the model again.
- **What is remembered, and what is not.** The model itself remembers
  nothing: PerFin never trains or tunes it, every request stands alone with
  no history, and the adapter stores nothing and logs no content. When you
  confirm suggested kinds, PerFin keeps the *wording* for your household so
  the next statement of that layout is read with no model and no question:
  a section's heading or a broker's action words, and what kind it is. It
  never keeps a payee, merchant or memo, any digit, a phrase containing a
  household member's name, an address or one of your account numbers, or
  any amount, date, security or account from the statement. You can see
  every phrase and remove it under **Documents, What PerFin has learned,
  Statement wording**.
- If the model is slow, down or wrong, documents are processed as if it were
  not there; after three failures in a row it is not asked for five minutes.

## Turning it off

Empty `PERFIN_ML_URL` and restart the worker, or stop the profile
(`docker compose --profile ml-cuda stop ml ml-runtime-cuda`).
