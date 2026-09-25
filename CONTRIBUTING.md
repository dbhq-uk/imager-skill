# Contributing

Thanks for your interest - contributions are welcome.

## Ways to help

- Report a bug or request a feature via [issues](https://github.com/dbhq-uk/imager-skill/issues)
- Add a preset, add a platform size, sharpen the interactive flow, or improve the skill instructions via a pull request

## Local development

```bash
git clone https://github.com/dbhq-uk/imager-skill.git
cd imager-skill
./install.sh          # symlinks into ~/.claude/skills (edits are live)
```

The whole skill directory is symlinked, so edits - including to `SKILL.md`, the CLI and the catalogues - are live immediately. For Codex, re-run `./install-codex.sh` after editing a `SKILL.md`, since that path is rewritten at install time. Full walkthrough in [`docs/dev-setup.md`](docs/dev-setup.md).

You need an OpenAI API key to generate anything, but not to run the tests: nothing in the suite makes a network call, and `--dry-run` returns before a request is built.

## Before opening a PR

- `ruff check . && ruff format --check .`
- `cd skills/imager && OPENAI_API_KEY=test-key-not-real python3 -m pytest tests/ -v` - all green (pytest is the only thing to install, and only for the tests)
- `shellcheck ./install.sh ./install-codex.sh ./skills/*/scripts/*.sh`
- `claude plugin validate .`
- If you touched prompt assembly or a preset, generate one real draft and look at it. It costs about $0.006, and no test can tell you the image got worse
- British English, plain hyphens, no trailing full stops on headings

## The bar for a new preset

A preset is not a style word. It is a full prompt fragment that has to work on a subject it has never seen.

**Show the output.** A new preset needs at least two generated examples in the pull request, on deliberately different subjects - a person and an object, or a diagram and a landscape. A preset tuned on one subject and shipped is how a catalogue fills up with entries nobody uses twice.

**Both fields, and know which is which.** `description` is what the user picks from; `prompt` is what is sent. Editing a description changes a label. Editing a prompt changes every image that preset will ever produce, including for people who have been happy with it. Treat a prompt edit on an existing preset as a breaking change and say so.

**Say what it excludes.** Most of the visual presets end with `no text, no labels`, and that is doing real work - GPT Image 2 will happily letter an image you wanted clean. If your preset is meant to be text-free, say so in the prompt.

## What we will not accept

**A runtime dependency, or a vendor SDK.** The standard library only. `/plugin install` copies the directory and runs nothing, so a dependency is a skill that does not start. This skill holds an API key, and the argument that it is safe to hand one over rests on the code between the key and the wire being short enough to read. `openai` and `requests` both buy convenience this does not need.

**Anywhere the key gets written down.** Not into config, not into the history log, not into a last-run record so `again` can replay it, not into a debug line. It is read from the environment on every run and that is the whole of it.

**A default that spends without asking.** The confirmation threshold, the draft-first flow, and `-y` being explicit are not tunables. Raising the threshold, defaulting `-y` on, or having the interactive flow pass it silently all convert a cheap tool into an expensive surprise.

## Licence

By contributing you agree your work is licensed under the [MIT licence](LICENSE).
