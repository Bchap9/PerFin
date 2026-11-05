# Local model hints (optional)

PerFin reads statements with deterministic parsers first. The optional `ml`
containers add a model running **on your own machine** that suggests what a
document is when those parsers are unsure: its kind, its institution, the
accounts it is about. Suggestions show up as proposed accounts ("Accounts in
this file") and account choices in the review queue, marked as coming from the
model. Nothing is imported on the model's word alone (ADR-0010).

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
- If the model is slow, down or wrong, documents are processed as if it were
  not there; after three failures in a row it is not asked for five minutes.

## Turning it off

Empty `PERFIN_ML_URL` and restart the worker, or stop the profile
(`docker compose --profile ml-cuda stop ml ml-runtime-cuda`).
