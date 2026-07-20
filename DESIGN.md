# tup GUI — Design Document

The tup GUI (`src/tup/gui/`) is a PyQt6 desktop file explorer over a Telegram
drive. It presents each registered chat as a drive and its `vfs_index` rows as
a browsable POSIX-like filesystem, with full parity to the CLI commands
(`up`, `mkdir`, `cp`, `mv`, `rm`, `rmdir`, `retry`, `logs`, chat management).

## Design goals

1. **CLI parity, not a fork.** The GUI never reimplements VFS semantics. Every
   operation funnels through the same core primitives the CLI uses
   (`uploader.py`, `database.py`), so edge rules — no renames, `.keep`
   folders, empty-only `rmdir`, same-folder SHA-256 dedup, destination-exists
   guards — behave identically in both frontends.
2. **Qt and asyncio never mix.** Qt owns the main thread; tup's core is
   async (aiosqlite, Telethon, PTB). A single bridge object keeps the two
   worlds apart with explicit hand-off points.
3. **Testable without a display.** Queue logic and tree/row derivation are
   plain Python, Qt-free where possible, and the window exposes test toggles
   (`suppress_dialogs`, `open_files_externally`) so pytest can drive it
   headlessly.

## Architecture overview

```text
┌─────────────────────────── Qt main thread ────────────────────────────┐
│  MainWindow (main_window.py)                                          │
│  ├── toolbar: drive combo · Up · path bar · refresh · upload ·        │
│  │            Details/Icons toggle · Show hidden · Transfers · filter │
│  ├── QSplitter                                                        │
│  │   ├── QTreeView          — folder tree (QStandardItemModel)        │
│  │   └── QStackedWidget     — Details (QTableView) / Icons (QListView)│
│  │        └── FileSortProxy → FileTableModel   (models.py)            │
│  ├── QDockWidget (bottom)   — TransfersPanel (transfers_panel.py)     │
│  └── dialogs: ChatsDialog, LogsDialog          (dialogs.py)           │
└──────────────────────┬────────────────────────────────────────────────┘
                       │ bridge.submit(coro, on_done, on_error)
                       │ bridge.call_in_gui(job)   ← queued Qt signal
┌──────────────────────▼──────────── "tup-core" daemon thread ──────────┐
│  CoreBridge (bridge.py) — private asyncio loop                        │
│  ├── Database (long-lived aiosqlite connection)                       │
│  ├── TelegramClient (lazy, long-lived MTProto via Telethon)           │
│  ├── TransferManager (transfers.py) — sequential upload/download queue│
│  └── ops.py — op_mkdir/op_rm/op_mv/op_cp/op_prune/op_retry_failed/…   │
└───────────────────────────────────────────────────────────────────────┘
```

## Module responsibilities

| Module | Role |
| --- | --- |
| `app.py` | Entry point (`tup gui` / `tup-gui`). Guards the PyQt6 import (GUI is an optional extra), runs `migrate_legacy_config()`, loads `Settings`, starts the bridge, shows the window. Setup errors surface as a message box, not a traceback. |
| `bridge.py` | `CoreBridge`: asyncio loop on a daemon thread. `submit()` schedules a coroutine and delivers the result/exception back on the GUI thread via a queued `pyqtSignal`; `call_in_gui()` marshals arbitrary callables the other way. Owns the DB connection and the lazy MTProto client. |
| `models.py` | Pure derivation + Qt models. `build_dir_tree`/`build_rows`/`all_dir_paths` turn the flat `vfs_index` rows into a folder tree and per-directory listings. `FileTableModel` (read-only, 7 columns) backs both views; `FileSortProxy` adds folders-first, type-aware sorting and a case-insensitive name filter. |
| `main_window.py` | The explorer shell: navigation, drag & drop, context menus, prompts, status bar, and wiring every action to `ops`/`transfers` through the bridge. |
| `transfers.py` | Qt-free `TransferManager`: a sequential asyncio worker with pause/resume (between items), skip-current, and per-item cancel. Also `collect_upload_targets()`, which expands dropped folders into (file, dest, size) triples while skipping hidden files. |
| `transfers_panel.py` | Bottom dock UI: one row per transfer with a progress bar, live speed (`X of Y · Z/s`), and queue controls (Pause/Resume, Skip current, Stop selected, Clear finished). |
| `ops.py` | Async VFS operations mirroring CLI semantics exactly — the GUI's "command layer". Includes chat management (`op_add_chat`, `op_discover_chats`) and maintenance (`op_prune`, `op_retry_failed`). |
| `cache.py` | Local download cache at `<config dir>/<chat_id>/<vfs folders>/<file>` (override: `TUP_CACHE_DIR`). `is_cached()` requires an exact size match, so partial downloads never count. |
| `dialogs.py` | `LogsDialog` (read-only last-50 `uploads_log` view), `ChatsDialog` (list/add/remove drives with live Bot API validation, plus non-destructive chat discovery from pending updates), and `FolderPickerDialog` (tree-based destination picker for Copy/Move). |
| `theme.py` | One application stylesheet: Telegram-blue accent, palette-role surfaces (light/dark adaptive), row padding, input styling. Applied once in `app.py`. |

## Threading model

The single hard rule: **widgets are touched only on the Qt thread; the DB,
Telethon, and PTB are touched only on the bridge loop.**

- GUI → core: `bridge.submit(coro, on_done, on_error)`. The coroutine runs on
  the worker loop; callbacks are re-dispatched onto the GUI thread through a
  queued signal, so slots always run where widgets live.
- Core → GUI: `TransferManager` reports progress via a plain callback on the
  loop thread; `MainWindow._on_transfer_update_from_loop` immediately hops to
  the GUI thread with `bridge.call_in_gui()` before touching any widget.
- Snapshots, not shared state: `Transfer.snapshot()` hands an immutable-ish
  copy across the thread boundary; the live object is mutated only on the loop.
- Progress is throttled to one update per 256 KiB per transfer (plus every
  state change) so large uploads don't flood the signal queue.
- Startup/shutdown are the only blocking waits: `bridge.start()` waits for the
  DB to open (local and fast), and `closeEvent` shuts the transfer worker down
  with a 5 s timeout before stopping the loop.

## Main window behavior

- **Drive selector.** A combo box of `chat_aliases` (label: `alias — title`).
  The configured `default_chat_id` is pre-selected, and shown as a synthetic
  entry if it has no alias. Switching drives resets to `/` and refreshes.
- **Data loading.** One query per refresh: `vfs_list_prefix(chat_id, "/")`
  fetches the whole drive index, and everything else — the folder tree, the
  current listing, folder existence checks — is derived in memory. Drive
  indexes are small enough that this beats chatty per-navigation queries.
- **Two file views, one model.** Details (sortable table: Name, Size, Kind,
  Dimensions, Duration, Modified, Status) and Icons (48 px grid) share the
  same model/proxy stack, so sorting, filtering, and selection stay consistent
  when toggling views.
- **Sorting.** Folders always sort above files, in both directions. Columns
  sort by their real value (bytes, pixels, seconds, ISO timestamp), never the
  display string; name is the universal tie-breaker.
- **Navigation.** Tree selection, double-clicking folders, the Up button, and
  a type-in path bar (validated with `normalize_vfs_path`) all converge on
  `set_current_dir()`, which also updates the tree selection, the window
  title, and a status-bar summary (`drive · path — N folder(s), M file(s),
  size`).
- **Hidden files.** Dotfiles/dot-folders are hidden by default with a toolbar
  toggle; the status bar counts what's hidden so items never vanish silently.
- **Open/download.** Double-clicking a file downloads it into the local cache
  (via the transfer queue) and opens it with the OS default app; if already
  cached it opens instantly. The Status column shows `✓ Downloaded`, and
  cached files get "Show in Finder" in the context menu.
- **Context menus.** Empty-space: New folder, Upload files, Refresh.
  Folder: Open, Delete folder. File: Open, Download, (Show in Finder, Remove
  download,) Copy to…, Move to…, Delete. Copy/Move prompt with a folder-tree
  picker; files can also be dragged onto sidebar or subfolder rows (Move,
  Ctrl/Alt for Copy).
- **Errors.** Every bridge error lands in `_show_error`: status-bar line plus
  a critical message box that includes the exception's CLI `hint` (with Rich
  markup stripped). Destructive actions (delete, prune) confirm first.

## Transfer queue

Uploads and downloads share one sequential queue — deliberately, since
Telegram throttles parallel bot transfers anyway and one-at-a-time keeps
progress and failure attribution simple.

- **Enqueue sources:** OS drag & drop onto either file view, the toolbar
  file/folder pickers, and double-click downloads. Dropped folders mount under
  the current directory by their own name, preserving internal structure —
  exactly like `tup up <folder>` — and hidden files inside are skipped.
- **States:** `queued → running → done | failed | cancelled | skipped`.
- **Pause is a gate between items:** an in-flight MTProto transfer cannot be
  suspended, only cancelled — so Pause holds the queue before starting the
  next item, while Skip/Stop cancel the current task. The tooltip says so.
- **Duplicates are skips, not failures:** `DuplicateFileError` (same SHA-256
  already in the target folder) marks the transfer "Skipped" with the reason,
  and nothing goes to `failed_registry`.
- **Failures never kill the worker:** any other runner exception marks that
  transfer failed and the queue moves on.
- On completion the window refreshes (uploads) or updates the Downloaded
  badges and opens any pending file (downloads).

## Testing hooks

- `TransferManager` and `collect_upload_targets` are Qt-free and tested with
  pytest-asyncio directly (`tests/test_gui_transfers.py`); `wait_idle()`
  exists for deterministic test synchronization.
- Tree/row/sort derivation in `models.py` is pure and unit-tested without a
  running app (`tests/test_gui_models.py`).
- `MainWindow.suppress_dialogs` disables modal confirmations and
  `open_files_externally` prevents launching real apps, so window-level tests
  (`tests/test_gui_window.py`, `test_gui_ops.py`, `test_gui_download.py`) can
  run headless.

## UX roadmap — expert critique triage

Two external UX reviews (2026-07) agreed the architecture is sound but called
the frontend "a database viewer with buttons": stock Qt widgets, flat visual
hierarchy, and a few interaction patterns that fight user expectations. The
points below are triaged against the actual code. Nothing here requires
changing the `CoreBridge`/model architecture.

### Shipped (2026-07-20) — correctness + quick wins

- **Folder enumeration off the GUI thread.** `enqueue_upload_paths` used to
  call `collect_upload_targets` synchronously on the Qt thread; a dropped
  folder with thousands of files froze the window while `rglob` + `stat` ran.
  Collection now runs in an executor on the bridge loop
  (`_collect_and_enqueue`), with a "Scanning…" status while it works and a
  "Queued N file(s)" result. This was a latent bug, not polish.
- **Honest pause feedback.** Pause-between-items stays (an in-flight MTProto
  transfer can only be cancelled, not suspended — one reviewer suggested
  removing Pause; the capability is useful, the labeling was the failure).
  While the queue is paused and a transfer is still draining, the button
  reads **"Pausing after current file…"**, flipping to "Resume queue" once
  the queue actually holds.
- **Double-click no longer auto-opens.** Double-click on a non-cached file
  queues the download only (`activate_row`); cached files open instantly.
  An accidental double-click on an 80 MB video no longer burns bandwidth
  *and* launches a player. The explicit context-menu "Open" keeps
  download-then-open semantics.
- **Cache eviction.** New **"Remove download"** context-menu action on cached
  files (`cache.evict`): deletes only the local copy, refreshes the badges,
  file stays on Telegram. Completes the remote ⇄ cached residency model.
- **Transfers panel cleanup.** Terminal rows drop their progress bar (a
  100%-full bright bar reads as ongoing activity) and show muted gray status
  text instead; the filled bar is reserved for `running`. "Clear finished"
  moved to the far right as a flat low-emphasis button.
- **Conditional media columns.** "Dimensions" and "Duration" are hidden
  unless the current listing has at least one row with those attributes.
- **Blank, not em-dash.** Folders render empty Size/Kind cells instead of
  "—" and "Folder" filler (sorting uses `FileRow` values, not display
  strings, so nothing broke).
- **Folder-tree picker for Copy/Move.** The flat `QInputDialog.getItem` path
  list is gone; `FolderPickerDialog` hosts a `QTreeView` built by
  `build_dir_model` — the same model builder the sidebar now uses.
- **Resizable columns everywhere** (user-reported). Header sections used
  `Stretch`/`ResizeToContents` modes, which Qt makes non-draggable. All
  tables (file view, transfers panel, logs/chats dialogs) now use
  `Interactive` sections with sensible initial widths; dialogs do a one-shot
  `resizeColumnsToContents()` after populating.

### Shipped (2026-07-20, second batch) — medium efforts

- **Internal drag-and-drop.** File rows (never folders) are draggable from
  both views; the payload is a JSON name list under the
  `application/x-tup-files` mime type (`FileTableModel.mimeData`). Drops land
  on sidebar folders (`_SidebarTree`) or on folder rows inside the file panel
  — Move by default, Copy with Ctrl/Alt held. OS drops now also target the
  hovered subfolder instead of always the current directory. Dropping a file
  where it already lives is a silent no-op.
- **Toolbar hierarchy.** Navigation (drive, Up, path, refresh) groups left;
  the two upload actions merged into one "⇪ Upload" split-button
  (click = files, menu = files/folder); separators divide the clusters.
- **Auto-refresh with selection preservation.** A 30 s `QTimer` re-queries
  the index while the window is active and no modal is open. Two guards make
  it invisible when nothing happened: `_on_entries` drops results equal to
  the current entries (VfsEntry is a frozen dataclass — value equality), and
  same-directory re-renders capture/restore the selection by file name.
  Sidebar expansion state is also preserved across tree rebuilds. Quiet mode
  skips the "Loading…" status flicker.
- **Status as icon.** The trailing Status column is now a compact 28 px "✓"
  indicator with a tooltip, visually moved next to Name via
  `QHeaderView.moveSection` — the model keeps its column order, so sorting
  and tests were untouched.
- **Transfers dock summary.** A muted live digest ("2 running · 3 queued ·
  12 done") sits in the controls row, updated on every state change and on
  Clear finished.
- **Theme pass.** New `theme.py` applies one stylesheet: Telegram-blue
  accent (#2AABEE) for selection/progress/focus, sidebar and transfers dock
  on a `palette(window)` surface below the file listing, padded (taller)
  rows, rounded inputs. Palette roles keep it light/dark adaptive. Per-kind
  file icons (video/audio/photo) come from `QStyle` standard pixmaps keyed
  by `media_kind`.

### Remaining — not yet scheduled

- **Breadcrumb path bar.** Interactive crumbs that become a text field on
  click; the recessed `QLineEdit` styling shipped, the crumb widget did not.
- **Segmented Details/Icons control.** Still two checkable actions in an
  exclusive `QActionGroup`; a true segmented icon control needs custom
  widgets.
- **Drop-target row highlight.** Drops into hovered subfolders work, but the
  hovered row is not visually tinted during the drag — needs a custom
  delegate or drag-hover role.
- **Collapsed one-line dock state.** The summary label exists; a fully
  collapsed "Queue: 2 running — [Expand]" dock mode does not.
- **Grouping finished transfer rows.** Finished rows stay as muted rows; the
  summary label covers the at-a-glance need for now.

### Deferred / rejected — with rationale

- **Drive list inside the sidebar tree.** Reasonable pattern, but it couples
  drive switching to tree state and makes "one query per refresh, one drive
  in memory" ambiguous (expanding another drive implies loading its index).
  Revisit after the sidebar/theme work; the combo is acceptable for the
  typical 1–5 drives.
- **Thumbnails in the grid view.** Real thumbnails require content bytes,
  which contradicts the "no accidental downloads" principle above. A viable
  middle path is Telegram's server-side small thumbs (a few KB via MTProto)
  or thumbnailing only already-cached files — defer until the cache/residency
  UI has landed.

