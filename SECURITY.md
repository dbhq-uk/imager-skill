# Security

## Reporting a vulnerability

Email <dan@dbhq.uk> rather than opening a public issue. Include what you found,
how to reproduce it, and what an attacker could do with it. You will get a first
response within 48 hours.

## What this skill does

It sends prompts, and optionally images you nominate, to OpenAI's image API and
writes the result to a file. The sections below are the complete account of what
it touches.

### Network

**OpenAI, or OpenRouter if you ask for it, and nothing else.** Requests go out
over `urllib` from the standard library to `api.openai.com`, or to
`openrouter.ai` under `--provider openrouter`. There is no DBHQ endpoint, no
telemetry and no analytics.

`--dry-run` assembles and prints the full prompt without making a request.
`--estimate` prices a batch without making one either. Both are the honest way
to see exactly what would be sent before anything is.

**What leaves your machine:** the prompt text, and - when you use `--edit` or
pass reference images - the image files you named, base64 encoded. Treat that
the way you would treat any upload: OpenAI's data-usage terms apply, not ours.

### Credentials

- The API key is read from `OPENAI_API_KEY` (or `OPENROUTER_API_KEY`) in the
  environment **on every run**
- It is **never written to disk** by this skill. `~/.dbhq/imager/`
  holds `config.yaml` (your defaults), `history.jsonl` (a generation log) and
  `last.json` (the last run, so `again` can repeat it), and none of the three
  has a field for a key
- It is not logged, not printed, and not included in the history entry

If you put the key in your shell rc file, that file's permissions are the ones
protecting it.

### On disk

- Installs into `~/.claude/skills/imager` or `~/.codex/skills/imager`,
  depending on the agent
- Builds a virtualenv at `skills/imager/.venv`
- Writes generated images where you tell it to, and defaults to the working
  directory
- Writes config, history and last-run state to `~/.dbhq/imager/`

### Spend

Worth listing here because it is the loss an attacker or a mistake could
actually cause. Every run is priced before it is made, and at or above $0.50 it
stops and asks. `-y` skips that prompt and is an explicit flag with no default,
no config setting and no code path that passes it on your behalf.

### Third-party code

One runtime dependency, **PyYAML**, for the two catalogue files. The API calls
use the standard library rather than a vendor SDK, deliberately: this is a skill
that holds an API key, and the less code sits between the key and the wire, the
less there is to audit before trusting it. Dependabot keeps the pin current.

## Standing position on scanner findings

Automated skill scanners flag the sentences above that name environment
variables and the `~/.dbhq/imager/` directory as "sensitive file access".
That is accurate documentation, not a defect.

**We do not delete accurate documentation to clear a scanner finding.** A
reader who cannot tell where a tool keeps its state cannot audit it.
