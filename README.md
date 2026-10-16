# nocfo-sync

One-way sync of a Google Drive folder tree into [NoCFO](https://developers.nocfo.io)
files and folders.

It reads Drive through the **Google Drive for Desktop** mount on macOS — no
Google Cloud project, no OAuth client, no service account. NoCFO is reached
with a personal access token.

```
Google Drive for Desktop mount          NoCFO
~/Library/CloudStorage/GoogleDrive-…/   ┌────────────────────────────┐
  My Drive/Receipts/2026/inv.pdf   ──►  │ <destination>/Receipts/2026│
                                        │   inv.pdf                  │
                                        └────────────────────────────┘
```

## What it does

- Walks the selected Drive folder recursively (breadth-first).
- Mirrors the folder tree into NoCFO under a chosen destination folder,
  reusing folders that already exist by name and creating the missing ones.
- Uploads every file; uploads with a size/mtime fingerprint stored in a
  local SQLite DB so re-runs skip unchanged files.
- **Never deletes anything in NoCFO.** Files removed from Drive are only
  reported. Files *changed* in Drive are re-uploaded and the previous NoCFO
  copy is deleted **only if it is not attached to a document**.
- Skips Google-native stubs (`.gsheet`, `.gdoc`, `.gslides`, …) — the mount
  only stores a pointer for those, not the document content.
- Skips dotfiles (`.DS_Store`, `.Trash`, …) and symlinks.
- Optionally sends `Idempotency-Key` on uploads (see caveat below).

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Configure

Copy the example and fill it in:

```sh
cp config.example.toml config.toml
```

```toml
[nocfo]
base_url = "https://api.nocfo.io"   # or https://api-tst.nocfo.io
token = "..."                        # personal access token
business_slug = "your-business"
idempotency = true

[gdrive]
mount = "~/Library/CloudStorage/GoogleDrive-you@example.com/My Drive"
root_folder = ""                     # relative to mount; picked via TUI if empty
destination_folder_id = 1234         # omit for the NoCFO root
```

The token may also live in a plain `config` file (`token=…`) or in the
`NOCFO_TOKEN` environment variable. `config.toml` and `config` are
git-ignored.

### Where the config is found

`nocfo-sync` is **not** tied to the directory you run it from. It looks for
`config.toml` in this order and uses the first one it finds:

1. the path passed with `--config`
2. the `NOCFO_SYNC_CONFIG` environment variable
3. the current working directory
4. `~/.config/nocfo-sync/config.toml`
5. the repository root (so `.venv/bin/nocfo-sync` works from anywhere)

A plain `config` file in the same locations is merged in as a fallback. The
sync database (`.state/sync.db`) is stored **next to the config file**, so
runs from different directories share the same state. `nocfo-sync status`
prints exactly which config and state file are in use.

## Use

```sh
# pick the Drive source folder and the NoCFO destination interactively
.venv/bin/nocfo-sync pick-drive-root
.venv/bin/nocfo-sync pick-destination

# run one pass (opens the pickers if nothing is configured yet)
.venv/bin/nocfo-sync sync

# non-interactive / scriptable
.venv/bin/nocfo-sync sync --plain --dry-run --root-destination

# inspect
.venv/bin/nocfo-sync status
.venv/bin/nocfo-sync discover-folders --depth 4
```

`sync` shows a Textual progress bar and live log. `--plain` prints to stdout
instead (useful for cron); `--dry-run` computes the plan without touching
NoCFO or the state DB.

### Pickers

Both pickers lazily load children as you expand, so deep trees stay fast.
The keys are the same in both:

| Key | Action |
|---|---|
| `↑` / `↓` | move the cursor |
| `→` | open the folder (press again to step into the first child) |
| `←` | close the folder, or jump to its parent |
| `space` | toggle open/closed |
| `enter` | **choose** the folder under the cursor |
| `esc` / `q` | cancel |

- **Drive picker** — browses the local mount, directories only. Choosing the
  top `My Drive` node syncs the whole mount.
- **NoCFO picker** — browses the NoCFO folder tree; `n` creates a new folder
  under the cursor. Choose the `NoCFO root` node to sync into the root.

## Scheduling

With the plain output, cron/launchd can run it directly:

```cron
0 7 * * *  cd ~/dev/nocfo_sync && .venv/bin/nocfo-sync sync --plain --root-destination >> sync.log 2>&1
```

## The undocumented NoCFO folders API

The public OpenAPI schema has **no folder endpoints** — `folder_id` appears
only on the files API. Probing the API revealed a working folder resource,
which this tool relies on:

| Call | Behaviour |
|---|---|
| `POST /v1/business/{slug}/folders/` `{name, parent}` | create (201) |
| `GET /v1/business/{slug}/folders/` | list root folders (paginated) |
| `GET /v1/business/{slug}/folders/{id}/` | detail: `children`, `breadcrumbs`, `has_content` |
| `PATCH /v1/business/{slug}/folders/{id}/` | rename / move |
| `DELETE /v1/business/{slug}/folders/{id}/` | 204 if empty; **500 HTML error if non-empty** |

Folder names are **not** returned by the files listing, so a folder cannot be
discovered from `GET /files/` alone — hence the dedicated picker. Because
this resource is undocumented it may change without notice; the sync fails
loudly rather than silently misplacing files.

## Caveats

- **`Idempotency-Key` + `file_upload`**: `api-tst.nocfo.io` returns `500` for
  any upload carrying that header. On such a 500 the client retries once
  without the header and disables idempotency for the rest of that run
  (a note is printed). Set `idempotency = false` to skip it entirely.
- **File identity is the relative path.** Renaming/moving a file in Drive
  looks like a new file plus a vanished one, so NoCFO gets a new copy and the
  old one is reported as left behind (never deleted).
- **Placeholder files**: in streamed mode Drive for Desktop only stores a
  stub until you open a file. The sync reads every file, so a first run over
  a large folder downloads everything locally.
- **Change detection** uses size + mtime, the only signals the mount exposes.
  Editing metadata without changing size/mtime will be missed.
- Native Google Docs are skipped rather than exported.

## Tests

```sh
.venv/bin/python -m pytest
```

Unit tests run entirely against fakes (no network, no Drive mount).
