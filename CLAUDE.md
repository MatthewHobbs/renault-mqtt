# renault-mqtt

Global rules (signed commits, trunk/merge policy, Conventional Commits, tiers) live in
`~/.claude/CLAUDE.md`; this file is renault-mqtt specifics only.

This is the shared core behind the sibling add-ons **`MatthewHobbs/a290-ha-addon`** and
**`MatthewHobbs/r5-ha-addon`**, which pin it by immutable commit SHA in their Dockerfiles
(`ARG CORE_REF`). A change here reaches users only once those repos bump that pin and cut their
own release, so "merged here" is never "shipped".

## Releasing

`__version__` in `renault_mqtt/__init__.py` is the single source of truth — `pyproject.toml`
reads it via `version = { attr = "renault_mqtt.__version__" }`. Bump it in the PR that ships the
change; nothing else needs editing.

`.github/workflows/release.yaml` does the rest, triggered by **CI completing successfully on
`main`** rather than by the push itself, so a tag can only ever land on a commit that has
validated. It no-ops unless `__version__` names a tag that does not exist yet, so every merge can
reach it safely.

Do not tag by hand. 0.13.0 and 0.13.1 shipped untagged while this was a manual step, and because
the add-ons' Renovate reads this repo with the `github-tags` datasource, a missing tag silently
stops the bump ever being offered downstream. That failure is invisible rather than loud, which
is exactly why the tagging is automated.

## Tag signing, and the dry run that guards it

Tags are SSH-signed on the runner with a **signing-only** key: private half in the
`TAG_SIGNING_KEY` repo secret, public half registered on the account as a *signing* key
(`renault-mqtt release tagging`). It cannot authenticate a push, so a leaked secret can vouch for
tags and nothing more. A missing secret fails the job rather than quietly publishing an unsigned
tag.

The tagger is `Matthew Hobbs <matt@matthobbs.net>` because GitHub only marks a signature verified
when the tagger email is a verified address on the account owning the key — a bot identity cannot
be used here. A consequence worth knowing: a tag cut by CI looks like one cut by hand, and the
signing-only key is what distinguishes them.

**If that key is rotated or removed from the account, the first symptom is a failed release, not
a warning.** After any key change, prove the path before you need it:

```sh
gh workflow run release.yaml --ref main
```

That signs a throwaway tag, asserts the API reports `verification.verified`, and deletes it from
an `EXIT` trap so a mid-way failure cannot leave one behind. It cannot publish a release: the tag
job is gated on `workflow_run.conclusion`, which is null for a manual dispatch. The tag name is
deliberately non-semver (`signing-check-<run_id>`) so the add-ons' Renovate cannot mistake it for
a release during the seconds it exists.
