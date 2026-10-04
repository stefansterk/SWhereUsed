# Changes

## 2.2.0
- Sort the columns of a file's lists (Structure, Used directly in, Top-level assemblies, Rename, Pack & Go): click a
  header for up, again for down, a third time for the original order. Names with numbers sort naturally; in the
  tree every level is sorted on its own (parts stay under their assembly); the main assembly stays on top.
  Remembered per view.

## 2.1.2
- Pack & Go: "Include drawings" (was "Drawings along", also in Rename files) and "Overwrite existing files" sit above
  the tabs, always in view. No summary line at the top any more (the count is on the button, the details in the
  confirmation); it only shows when files are marked in the list or something went wrong.

## 2.1.1
- Fixed: Start SWhereUsed.bat said it was started from inside the zip file, also when it was unzipped. The rename to
  SWhereUsed had left "WhereUsed.py" behind in the .bat files (also in Start SWhereUsed in background.bat and
  Test a file.bat). A test now checks that every file a .bat refers to is there.

## 2.1.0
- Pack & Go runs in a window with a progress bar (copying, pointing the copies to each other, properties); when it
  is done the window offers Undo, History and Go to the copy, instead of a line at the top of the Pack & Go bar.

## 2.0.0
- **WhereUsed is now called SWhereUsed.** The files are renamed (SWhereUsed.py, Start SWhereUsed.bat, ...) and the
  data folder is now %USERPROFILE%\\.SWhereUsed. Start SWhereUsed.bat moves the old .whereused folder there once
  (index, settings, key, history and Python stay as they are), and starting when you sign in is moved to the new
  name. Settings kept in the browser (theme, rules, profiles, pins) stay.

## 1.38.1
- Fixed: Pack & Go with two files of the same name in different folders, one copied (renamed) and the other set to
  Keep. A copy that used the KEPT one was pointed at the copy of the other one. Copied and kept files with the same
  name now compete together (saved path, own folder, closest folders); a kept file keeps its real path.

## 1.38.0
- Run in the background: no window, an icon by the clock (double-click to open, right-click to stop).
  Start SWhereUsed in background.bat; Setup can start it this way when you sign in (a shortcut straight to
  pythonw.exe, so not even a window for a moment). Stop SWhereUsed on the Setup page.
- Helpers always run with python.exe and without a window of their own; output without a window goes to console.log.

## 1.37.1
- The path above the column headers always starts with the main assembly (no more flicker between main and
  subassemblies), with a small thumbnail for each.

## 1.37.0
- Setup in tabs: Getting started, Appearance, Updates, All settings, Help & about.
- A welcome on the start page for a first start: three steps, each ticked when done.
- Search suggestions with thumbnails, the typed part marked, and All / Parts / Assemblies / Drawings.
- Column headers stay in view in long lists; in a deep structure the path of the subassembly you are in.
- Columns: choose which columns to show, per view, remembered.
- Thumbnails rounded with a fine edge, a little dimmed in dark themes.
- A thin progress bar under the index button while the index is updated.

## 1.36.2
- The kind mark on thumbnails: the icon fully visible, without a background behind it (only a thin light edge).

## 1.36.1
- Folder boxes: Backspace right after a \\ goes one whole folder back.
- Thumbnails show the kind of file (part, assembly, drawing) bottom right, see-through; also on the enlarged picture.

## 1.36.0
- The app's own dialogs instead of the browser's confirm / alert / prompt: a clear title, lists and notes apart, and
  buttons that say what they do (red when something is removed or overwritten). Enter confirms, Esc cancels.

## 1.35.2
- After Fix paths the structure shows the new paths right away (the index is updated at once, not only after it
  has read the assemblies again).

## 1.35.1
- Fix paths found nothing to fix: the Document Manager searched for the files and reported where it FOUND them, not
  the paths as saved. It now asks for the saved paths (no searching), and uses the saved paths from the index as a
  safety net. Test a file.bat shows both lists.

## 1.35.0
- **Fix paths** in the Structure: references whose saved path no longer exists ("found by name") get the path where
  the file really is (own folder first, else the only file with that name; never a guess). Backed up, checked, and
  undone from History.

## 1.34.2
- Pack & Go: files set to Keep are now referred to by their real path in the copies. When the original assembly
  named them by an old path (found only through its own folder), the copy in another folder could not find them,
  and SWhereUsed showed them as "not found" / "not indexed".

## 1.34.1
- Pack & Go and Rename files: while a new plan is worked out the last summary stays (dimmed, with a small turning
  mark) instead of a short text in its place, so the page no longer jumps on every click.

## 1.34.0
- An assembly opens at its Structure; tabs in the order Structure, Used directly in, Top-level assemblies, Rename or
  move. Parts and drawings open at Used directly in.
- Folder suggestions: Enter or Tab opens the folder (adds the \\) and shows its subfolders right away.
- Pack & Go: property rules can be saved as profiles and picked again.

## 1.33.1
- Fixed: with saved Pack & Go property rules in the browser, the page stopped loading (no Setup, no recent files,
  no theme choice, empty file pages). Since 1.29.2.
- A programming error in the page now shows a red bar at the top instead of leaving the page half working.

## 1.33.0
- **Check for updates** and **Install update** on the Setup page: downloads the new version, checks it, keeps the
  current one as a backup, puts the new files in place and restarts; the page reloads by itself. Not while an index
  run, rename or Pack & Go is busy; your data is never touched. RELEASING.md says how to publish a version.

## 1.32.0
- Actions that take longer than a second are noted (slow.log) and summed up in Copy diagnostics.
- The preview pictures are kept within 500 MB (setting `thumbnail_cache_mb`); the ones not looked at longest go first.
- The index is tidied up once a week (ANALYZE, VACUUM) when no index run or rename is busy and the app is idle.
- New screenshots in the README, including History.

## 1.31.0
- Folder boxes suggest folders: when empty, the recent and pinned folders; while typing, the folders that match
  (as in the address bar of Explorer). Arrow keys, Enter or Tab to use one, \\ to go into it.

## 1.30.1
- Clearer words: Find / Replace "in folder paths (moves the files)"; the summary, the button and the confirmation
  say how many files are renamed, moved, or renamed and moved. No folder is ever renamed.

## 1.30.0
- Rename files: Find / Replace works in names, in folders (the files then move) or in both, and Prefix / Suffix /
  Add as in Pack & Go. The new name and folder show in green while you type; Replace or Add applies them.

## 1.29.2
- Property rules: a kind of file in which the property does not occur is greyed out (Clear, Delete, Replace);
  Set to can still add it everywhere.
- Pack & Go always opens at Files.
- The properties are read in the background as soon as a structure has been opened (up to 1,500 files), so they
  are ready when Pack & Go opens.

## 1.29.1
- A property rule can be for any combination of parts, assemblies and drawings (three on/off buttons per rule),
  for example parts and assemblies but not drawings.

## 1.29.0
- Pack & Go property rules can be for one kind of file: all files, parts, assemblies or drawings. The list of
  properties shows in which kinds of files each one occurs.

## 1.28.5
- Pack & Go, Properties: all files of the structure and their drawings are read once; the list shows what belongs
  to the files copied at that moment, so Copy / Keep changes it at once, without reading again.

## 1.28.4
- Pack & Go, Properties: the list now includes the drawings that go along (the rules already applied to them).

## 1.28.3
- Pack & Go reads the properties of the files in the background as soon as it opens (and again when files are set
  to Copy or Keep), so the Properties tab is ready when you open it.

## 1.28.2
- Browse no longer shows drives and network paths this PC does not have (old locations still named in saved
  references); "Show N locations not on this PC" brings them back. A known drive that is offline stays.
- Fixed: a folder that could not be reached could be expanded in the Browse tree, but not collapsed again.

## 1.28.1
- PDM vault views from the registry: the folder is in the value ShellRoot (as SOLIDWORKS PDM writes it), the vault's
  name in DbName; Location is still tried as a spare.

## 1.28.0
- SOLIDWORKS PDM vault views are recognised from the Windows registry (for all users and per user), with the
  vault's desktop.ini as a safety net. Copy diagnostics lists the vaults found.

## 1.27.4
- The heart of the support link is always red.

## 1.27.3
- "Use at your own risk" in plain words, in the README and on the Setup page.

## 1.27.2
- README: licenses of the software used, the SOLIDWORKS trademark, and how SWhereUsed was made.

## 1.27.1
- A quiet "Support development" link (Ko-fi) in the footer and on the Setup page; a Sponsor button on GitHub.

## 1.27.0
- Pack & Go, Properties: a list of the custom properties in the files being copied (in how many files, which values),
  with Clear / Delete / Set / Replace to make a rule in one click; rule names are suggested while typing.
  Read in a separate process, kept per file version.
- The label "own copy" is now "own folder" (SOLIDWORKS uses "Copy of" for something else).

## 1.26.0
- **Undo** and a **History** page: renames and moves are undone by renaming back (references updated again);
  Pack & Go by removing its copies and putting back what it overwrote (it warns about copies changed since).
- **Right-click menu** on every file and folder in a list; **pinned** files and folders on the start page.
- Pack & Go in **tabs**: Files, Names, Folders, Properties.
- A **?** next to each list explains the labels. Clearer label words: found by name, different file,
  other drive/share, not found, excl. from BOM, ready.

## 1.25.0
- A folder picker next to every folder box (Pack & Go's target and each file's folder, Move to folder, Rename or
  move): browse folders on disk, Up, the drives at the top, New folder, Use this folder.

## 1.24.1
- Fixed: opening Statistics from a file's page left that page standing above the statistics.

## 1.24.0
- Fixed: a file renamed or moved by another tool, found in ONE assembly, was applied to ALL assemblies naming the
  same old path. With copies of a project sharing an old location, every copy got linked to the files of the copy
  read first. Now kept per assembly; a drawing (which cannot find out itself) only takes over what all assemblies
  agree on. Once, on the first index run: those old links are removed (your own renames stay) and the assemblies
  they touched are read again.

## 1.23.3
- Test a file.bat: double-click it and drag files into its window (Enter after each), as well as dropping files
  onto it in Explorer.

## 1.23.2
- Test a file.bat closed at once when a path held a ")" (for example a folder "SWhereUsed (1)" from a second
  download) or "&". Rewritten without ( ) blocks, and its window now always stays open, so any problem shows.

## 1.23.1
- **Test a file.bat**: drag SOLIDWORKS files onto it to see what SWhereUsed reads from them; uses the right
  (64-bit) Python and saves the output in test_output.txt.
- `--test` with a 32-bit Python stops with a clear message and the right command.

## 1.23.0
- Copies of a project in other folders: when an assembly's saved path is gone and its own folder has a file with
  that name and type, that is the file it uses (as in SOLIDWORKS). Marked "own copy": not counted for, and not
  updated when renaming, the file in another folder, unless chosen ("Also update ... own file").
- The structure follows the same order: already loaded, then the assembly's own folder.

## 1.22.2
- Thumbnails no longer flicker when the structure is redrawn: pictures already seen are shown straight away,
  a new one appears only when it has fully loaded.
- A thumbnail that is (nearly) all white or transparent is replaced by the normal icon.

## 1.22.1
- Structure tree: expanding or collapsing keeps the row you clicked exactly where it was on the screen (also with
  Expand all and Collapse); icons and thumbnails share a fixed box, so rows no longer grow when pictures arrive.

## 1.22.0
- Pack & Go: **Properties of the copies**: clear, delete, set or replace text in custom properties of every copy
  (file and/or configurations), with {name}, {oldname} and {date}; checked after saving, remembered in the browser.

## 1.21.0
- Pack & Go: **Select by** name, folder or both (with `*`), as in SOLIDWORKS Pack and Go, and Ctrl+click to select
  files yourself. The tools (Replace, Prefix, Suffix, Copy, Keep) then work on the selection.

## 1.20.0
- Pack & Go: **Overwrite existing files** (off by default). Overwritten files are backed up first and put back if
  anything fails; open, read-only and PDM files and the originals themselves are never overwritten.

## 1.19.2
- Replace, Prefix and Suffix (Pack & Go) and Find/Replace (Rename files) show what they would do while you type:
  the names that change get a green frame. A click on Replace or Add (or Enter) applies it and empties the fields.

## 1.19.1
- Thumbnails in "Recently looked up".
- The thumbnail of a file stands in place of the type icon next to its name (the type as a small mark in its
  corner); its enlargement opens to the right.

## 1.19.0
- Pack & Go marks conflicts in the list: red when two files would become the same copy (stops), orange when
  copies get the same name in different folders (allowed, but in one assembly SOLIDWORKS loads only one of them).
- **Make names unique** adds the folder name to the second and further files with the same name.
- The assembly itself is a row in the list, with its own Copy box, name and folder; it can be left out.
- A folder of its own per file (column "To folder"); its drawing goes along.

## 1.18.4
- Pack & Go picks the file a reference means the way SOLIDWORKS finds it: the saved path when it still exists,
  otherwise a file with that name in the assembly's own folder, otherwise the closest match over the folders.
  Fixes copies of supplier assemblies whose saved references pointed to another product's folder.

## 1.18.3
- Pack & Go: files with the same name in different folders (for example a supplier's set per product) each get
  their own copy in the right assembly. The references are matched over the folders from the end of the path,
  so an older location in the reference still finds the right file. When it is not certain, Pack & Go stops
  with a clear message instead of guessing.

## 1.18.2
- The assembly itself is the top row of its structure (tree and flat list), with its thumbnail.
- Enlarged thumbnails show at their real size: one picture pixel on one screen pixel, also with Windows scaling;
  never blown up, only made smaller when they do not fit.
- Fixed: thumbnails could be missing when the page was drawn before the server had answered.

## 1.18.1
- After Pack & Go, SWhereUsed showed the copies still using the original parts (their component lists name the
  originals until SOLIDWORKS saves them; SOLIDWORKS itself loads the copies). SWhereUsed now remembers per copy what
  Pack & Go changed, and shows the copies using the copies.

## 1.18.0
- SOLIDWORKS PDM: renaming and moving files in a vault view is blocked (do that in PDM). Pack & Go into a vault
  folder says to check the copies in.
- `file_timeout` is now a setting, for very large assemblies on a slow network.
- New screenshots, a license and this list.

## 1.17
- Renames done by other tools (SOLIDWORKS Explorer, PDM, Document Manager tools) are recognised through the
  external references of an assembly, in a separate helper process; "renamed" mark in the structure.
- Fixed: two index processes at the same time ("database is locked"): the whole process tree is stopped, and only
  one index process can run. Fixed: crashes (0xC0000374) when reading external references; slow storing of
  recognised renames on a big index; "Open now" counting files that were already read.
- Statistics open right away with the last numbers; new ones are worked out in the background.
- Pack & Go of a whole assembly: results kept alive in the job process; only direct references are replaced.
- Thumbnails enlarge next to the name column; index size shown; SOLIDWORKS libraries left out of
  "nothing uses" and "top-level".

## 1.16
- **Pack & Go**: copy an assembly, all of it or part, under new names (replace, prefix, suffix); the copies refer to
  each other, the originals stay as they are.

## 1.15
- Copy diagnostics; all settings in the app; update check (GitHub releases); broken references; custom
  properties as columns; drawings per assembly; thumbnails; move several files to another folder.

## 1.14
- Nine themes (Windows 11, macOS, Windows XP, Windows 98, terminal, high contrast, ...), with real previews.

## 1.10 - 1.13
- Fun facts, SOLIDWORKS version per file, time per index run, envelopes and "exclude from BOM" not counted;
  checks after renaming read the external references (what SOLIDWORKS loads).

## 1.4 - 1.9
- Structure of an assembly (tree and flat), renaming several files at once with a status per file, Browse,
  parts in the index (derived and mirrored parts), searching with sorting and pages over all results,
  statistics, start with Windows, loading indicators.

## 1.0 - 1.3
- Where used (direct and top-level, per configuration), rename or move with all references updated
  (all or nothing, with backups), search and results list.
