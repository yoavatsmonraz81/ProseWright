# Projects

ProseWright can hold more than one story. Each story is a **project**: a folder (a "home") with its own working log, canon, lorebooks and workspace state. Projects never read or write each other's data.

## Choosing the home

The editor opens one project at a time. In order:

1. `STORY_EDITOR_HOME`, if set.
2. The **active project** in the project registry (see below).
3. A `project.json` at the repository root.
4. The bundled example, `examples/lantern-quay/`.

| Home | How it's configured |
|---|---|
| **A folder with `project.json`** | Only the integrations its manifest names. |
| **A folder without a manifest** | No external integrations at all: no SillyTavern chat, no character folder, no authored spine file, no Drive folder. |

## Switching between projects

The registry lists the projects on this machine and which one is active. It lives at `~/.config/story-editor/projects.json` (`STORY_EDITOR_REGISTRY` overrides). It only holds pointers: adding, removing or switching never copies, moves or deletes a project's files.

```bash
python -m story_editor project add ~/stories/my-story    # register a folder
python -m story_editor project list                      # * marks the active one
python -m story_editor project switch my-story           # used the next time the server starts
python -m story_editor project current                   # what would open now, and why
python -m story_editor project remove my-story           # forget it (files untouched)
```

In the GUI, the **project** chip in the status bar lists the registered projects. Opening one switches the registry, restarts the engine on it and reloads the page. A switch is refused while a Drive sync or any other write request (a proposal, a novelize run, an export) is still running.

## Saving and loading a whole project

A bundle (`.sebundle`) is one file holding everything a project needs, with a checksum for every file. Use it to back a project up, move it to another machine, or set it aside while you work on another story.

```bash
python -m story_editor project save                     # the open project → ./<id>-<UTC stamp>.sebundle
python -m story_editor project save my-story --out ~/backups
python -m story_editor project verify ~/backups/my-story-20261002-101500Z.sebundle
python -m story_editor project load ~/backups/my-story-20261002-101500Z.sebundle --into ~/stories/my-story-restored
```

In the GUI's **project** panel, **save bundle** downloads a bundle of the open project. **load bundle…** takes a `.sebundle`, checks it, shows what it holds and suggests a free name, then restores it as a new project in `~/ProseWright-projects/<name>` (`STORY_EDITOR_PROJECTS_DIR` overrides) and adds it to the list; **open now** switches to it. A loaded project can be edited, saved again and loaded again like any other — each load is a new project, and nothing is ever loaded over an existing one. A copy loaded under a new name is titled "… (copy)". Switching away from a project that isn't in the list adds it first, so the panel always offers a way back.

**What goes in.** The project folder, plus the files it reaches outside that folder: its SillyTavern chat, lorebooks kept elsewhere, the authored spine, and a working log kept elsewhere. Each is recorded with its original path. Left out by default: `workspace/backups/` (add `--with-backups`), the search index (rebuildable; add `--with-index`), imported font files in `workspace/fonts/` (licensed per machine; the font choices in `fonts.json` are kept), Drive sync state, `.git/` and caches. A shared SillyTavern character folder is never bundled. When the home is the code checkout itself, only `project.json`, `mini_spine*.json`, `workspace/`, `canon/`, `cards/` and `worlds/` are bundled.

**Guarantees.**

- Saving never changes the project, and never leaves a half-written bundle under the final name. If any file changes while it's being saved, nothing is written; save again when the project is idle.
- `verify` checks every file against its checksum and the archive's own CRC. Truncated or corrupted archives, files missing from or added to the list, and unsafe paths are all refused.
- `load` only restores into a folder that doesn't exist yet. It checks every file as it's written into a staging folder and moves the result into place only at the end, so a failed load leaves nothing behind.
- The loaded copy is isolated. Its external files point at the copies inside it (`external/…`), and its Drive folder and SillyTavern character folder are switched off. It can't write into the original's chat, lorebooks or Drive folder. `load` lists every such rewrite.
- `load` registers the copy unless you pass `--no-register`. Ids must be unique, so load a second copy of a project with `--id NEW-ID`.

**Format** (`story-editor/bundle@1`): a gzip tar archive whose first member is `MANIFEST.json` (project id and title, created, machine, code commit, options, what was excluded, external files with their original paths, and `{path, size, sha256}` for every file), followed by `home/…` and `external/…`. Empty folders aren't stored.

## `project.json` (`story-editor/project@1`)

```json
{
  "schema": "story-editor/project@1",
  "id": "lantern-quay",
  "title": "Lantern Quay",
  "log": "workspace/lantern_quay.jsonl",
  "integrations": {
    "sillytavern": {"chat": null, "characters_dir": null},
    "lorebooks": ["worlds/lantern_quay_lorebook.json"],
    "drive": {"remote": null},
    "authored_spine_md": null,
    "player_mode": {"home": null}
  }
}
```

- `id`: lowercase letters, digits and dashes.
- `log`: the working log (a SillyTavern-format `.jsonl` chat). Relative paths resolve against the home; absolute paths are kept as they are.
- `sillytavern.chat`: the SillyTavern chat file this project pushes edits back to. `characters_dir`: where its character card portraits live.
- `lorebooks`: missing means every `worlds/*.json`; `[]` means none.
- `drive.remote`: an rclone remote for laptop/workstation sync, e.g. `"gdrive:stories/my-story"`.
- `authored_spine_md`: an optional hand-written story plan in Markdown.
- `player_mode.home`: a Player-mode play folder; the editor's **play** view plays from it and keeps the working log in step (see [PLAYER_MODE.md](PLAYER_MODE.md)). A bundle doesn't carry the play folder, and a loaded copy has this switched off.
- `null` turns an integration **off**. There is no fallback to another project's.
- An invalid manifest stops the editor at startup rather than falling back.

## A project's folder

```
my-story/
  project.json
  mini_spine.json          the beat plan (Author mode)
  canon/
    characters.json        character bibles (aliases, voice rules, pronouns)
    story_primer.txt       a paragraph or two every model call can see
    dossiers/*.json        dated per-character case files
  worlds/*.json            SillyTavern-format lorebooks
  workspace/               the working log, manuscript, backups and editor state
```

`examples/lantern-quay/` is a complete three-scene example.

## Guards

- **Drive sync.** Every snapshot pushed to Drive records its `project`. Push, pull and background jobs refuse a Drive folder that holds another project's snapshot; `force` does not override this. A project without `drive.remote` reports "no Drive folder".
- **SillyTavern push-back** goes only to the open project's own `sillytavern.chat`. With none configured, push-back is refused.
- **Disabled integrations** (no chat, no spine file, no characters folder) are handled explicitly by every consumer.

## Tests

| Test | What it proves |
|---|---|
| `test_project_manifest.py` | Validation, parsing, path resolution; invalid manifests raise |
| `test_project_isolation.py` | In fresh interpreters: the default home is the example; the example resolves nothing outside its home, even with a Drive remote in the environment; a bare home has no integrations; the example segments into its three scenes |
| `test_drive_projects.py` | Cross-project push, pull and jobs are refused, forced or not, and nothing moves on either side |
| `test_project_battery.py` | Real operations (log, scenes, backup and restore, spine progress, dossier, SillyTavern and Drive refusals) against the example under a write fence that allows writes only inside its home |
| `test_write_fence.py` | The fence sees every write route (open, rename, copy, delete, mkdir, rmtree, SQLite) and doesn't mistake directory-relative deletes |
| `test_registry.py` | Add, switch, remove; ids unique; a broken registry is never overwritten; atomic writes; in fresh interpreters, the home is chosen in the documented order and a lost active folder falls back with a warning |
| `test_bundle.py` | Save → load is byte-identical with mtimes kept; exclusions and opt-ins; externals travel and the loaded copy reaches nothing of the original; truncated, corrupted, tampered, unlisted, missing, unsafe and symlink members are refused; load never overwrites; a file changing mid-save writes nothing |
| `test_project_switching.py` | A switch is refused while a Drive sync or another request is running |
| `test_project_load_api.py` | Through the API, as the GUI does: save → load → edit → save → load again; suggested names; bad files, bad names, taken folders and stale uploads are refused and leave nothing behind |
| `conftest.py` | Every session runs on a scratch copy of the example and a scratch registry; the suite fails if any test writes into the repository's `examples/` or the machine's real registry |
