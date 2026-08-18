# orbit/config/ — what each YAML governs

Eight files. Each is read at call time by a named loader; none is cached across calls, so an edit
takes effect on the next tool call without a restart (the voice runtime is the exception — it loads
its config once at startup).

| File | Read by | Consumed by |
| --- | --- | --- |
| `risk_tiers.yaml` | `policy.load_risk_tiers`, `policy.load_tool_registry` | `SafetyPlugin.before_tool_callback` |
| `chrome_profiles.yaml` | `policy.load_chrome_profiles` | `policy.resolve_profile`, `OpenSessionTool`, `GetPolicyTool` |
| `url_policy.yaml` | `browser_policy_tools._load_blocklist` | `_check_url_policy` |
| `filesystem_policy.yaml` | `policy.load_filesystem_policy` | `filesystem_tools._resolve_scoped_path`, `DeleteTool`, `ReadFileTool`, `SearchTool` |
| `windows_control_policy.yaml` | `policy.load_windows_control_policy` | `windows_control_tools._require_confidence`, `_is_blocked_combo` |
| `communication_policy.yaml` | `policy.load_communication_policy` | `communication_tools._resolve_account` |
| `voice.yaml` | `voice.config.load_voice_config` | `VoiceRuntime`, `load_transcriber`, `VoiceSpeaker` |
| `keyterms.yaml` | `voice.config.load_voice_config` | `DeepgramTranscriber._build_url` |

**screen-perception has no policy YAML, deliberately** — every tool it exposes is a pure read (a
screenshot, a UIA tree, foreground-window info) with nothing to scope, allowlist, or deny. Not an
oversight; there's genuinely nothing here for a policy file to govern, unlike windows-control's
confidence floor/key-combo denylist or filesystem's scoped roots.

## risk_tiers.yaml has two jobs

It assigns tiers **and** it is the tool registry. `load_tool_registry` returns the union of
`low` + `medium` + `high` + `allowed`; anything outside that union is refused before tier logic
runs at all.

The soft default this replaced — unlisted names falling through to tier `medium`, logged but still
executed — is what let raw Playwright MCP bypass the whole policy layer while producing clean event
rows: the raw server's tool names happened to match the ones in this file, so the tier lookup
"matched" and approved tools nobody had reviewed. If a name needs to run, add it here deliberately
with a real tier. Never add a fallback.

`allowed:` is for tools genuinely risk-neutral enough that assigning a tier would be arbitrary. It
is empty today and is not a shortcut around the thinking the rest of the file does.

Two entries are **documentation, not active gates**: `browser_evaluate` and `browser_extract` are
listed `medium` to record the risk of those internal primitives, but neither is in any skill's
`tool_filter`, so `SafetyPlugin` never sees them. Listing something here does not expose it.

The `planned:` block at the bottom is commented out on purpose. Those names (`windows.click`,
`files.write`, `gmail.send`, `web.search`, …) have no implementation anywhere. Live entries for
unimplemented tools made the config look more complete than the system is — exactly the gap this
file exists to prevent. Uncomment a name only when its tool actually exists.

`high` is currently empty, and that is consistent: nothing reachable is high-tier, and a high-tier
tool would be blocked outright anyway since no confirmation channel exists.

## chrome_profiles.yaml is a consent boundary

`mom`, `dad` and `sister` carry `owner_confirmation_required: true` and **no `contexts:` list**.
Both resolvers check by contexts *and* by profile name so either phrasing is refused — the
name-only path is what was missing when `context="mom"` silently resolved to `dev`.

This is not merely a technical gate: those people did not agree to have software touch their saved
logins or autofill, so the rule has no low/medium-tier exception and no "the user asked nicely"
path. Adding a `contexts:` list to any of those three would make it reachable — do not.

`dev` is `default: true` with contexts `[personal, general, research]`; `dev_college` covers
`[college, assignments]`. `profile_dir` is resolved relative to the project root and created on
demand.

## url_policy.yaml

`blocklist_keywords` are matched as plain lowercased **substrings against the whole URL**, not
domains — so `bank` matches `mybank.com`, and would also match a path segment containing "bank".
That bluntness is deliberate for a fail-closed check; widen entries carefully.

The **scheme allowlist is not in this file** — `ALLOWED_SCHEMES = {"http", "https"}` is hardcoded in
`browser_policy_tools.py`. Do not look for it here. Blocklisted URLs are refused outright rather
than queued for confirmation, matching the rest of the build's fail-closed default.

## filesystem_policy.yaml

`allowed_roots` defaults to a single entry, `data/fs_workspace` — deliberately not the user's real
Documents/Desktop/etc. This build runs directly on the user's Windows machine with no container
between the agent and the real filesystem, so the safe default is a sandbox the agent owns.
Widening this is an operator decision made by editing the list, never something a tool call can do.

`denylist_keywords` mirrors `url_policy.yaml`'s `blocklist_keywords`: matched as lowercased
substrings against the **resolved absolute path**, and it wins over `allowed_roots` — a path inside
an allowed root is still refused if it matches here. Mostly defense-in-depth today since
`allowed_roots` is narrow, but it's what keeps a later-widened root from silently exposing `.env`,
`.git`, or system directories.

`quarantine_dir` / `quarantine_ttl_hours` back `fs_delete`'s quarantine-not-destroy behavior. No code
currently sweeps entries past their TTL — treat `quarantine_ttl_hours` as documentation of intent,
same as `db.purge_old_events` having no caller.

`max_read_bytes` caps both `fs_read_file` and `fs_search`'s content-matching mode, so neither can be
used to flood the model's context (or exfiltrate a huge file one call at a time).

## windows_control_policy.yaml

There is no scoped-roots equivalent here — mouse/keyboard input lands wherever real OS focus is,
which can't be sandboxed the way a file path can. Two knobs stand in for that:

`blocked_key_combos` is matched after canonicalizing (lowercase, sort, `+`-join), so `"Alt+F4"`,
`"alt+f4"`, and `"f4+alt"` all match one entry. Refused outright (`permission_denied`) rather than
queued for confirmation, same fail-closed default as `url_policy.yaml`'s blocklist — there's no
confirm channel to route these through instead. Widen carefully; it's specifically what stops
`windows_key` from closing an app or locking/interrupting the session out from under the user.

`min_actuation_confidence` (default 0.70) reuses `orbit.tools.foundation.Confidence`'s existing
"surface" boundary rather than introducing a second number. Any `ElementRef` — UIA, OCR, vision, or
the raw-`{x, y}` path (always scored at `Confidence.VISION_INFERRED`) — below this is refused by
`windows_click`/`windows_drag`. Since no confirm channel exists, this is a hard stop today, not
Section 7's softer "reverify" state.

## communication_policy.yaml

Shape mirrors `chrome_profiles.yaml` deliberately — no wrapper key, account context names are the
YAML root keys directly, `mom`/`dad`/`sister` carry `owner_confirmation_required` with no `contexts:`
list, same consent-boundary rule ("those people did not agree to have software touch their account,"
same as the Chrome-profile version of this rule — no low/medium-tier exception, no "the user asked
nicely" path).

`backend:` (only on owner-permitted accounts) names an implementation
`communication_backend.get_backend` knows how to construct — today only `"local"` exists. Requesting
an unregistered name is a hard `ValueError`, not a fallback to `"local"`.

## voice.yaml and keyterms.yaml

`load_voice_config` merges: `_DEFAULTS` in `voice/config.py` ← `voice.yaml` ← `keyterms.yaml` for
the `keyterms` key ← `DEEPGRAM_API_KEY` from the environment. Any key present in `_DEFAULTS` but
absent from the YAML still resolves; a key in neither raises `KeyError` at use.

**`DEEPGRAM_API_KEY` is not in `.env.example`** even though the voice runtime needs it and warns at
startup when it is missing. It is a real gap — the key lives in `.env` only. Add it to the example
when touching that file.

`deepgram_cost_per_minute_usd` is a hardcoded list price, never fetched live; it feeds the `cost_usd`
column and the daily cap, so a stale value silently mis-sizes both. `tts_disable_espeak_fallback`
must stay `true` — the reason is a licensing one, see `orbit/voice/CLAUDE.md`.

Keyterms use repeated `?keyterm=` params with no weights or intensifiers — that syntax belongs to
the older deprecated `keywords` feature and will not work here. The list is loaded fresh on every
voice runtime start, so it grows by adding a line, no code change.
