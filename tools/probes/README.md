# Injection delivery matrix

This probe measures delivery of a fixed sentence to four browser targets: the
omnibox, textarea, contenteditable, and contenteditable textbox. It is release
evidence for Samsara's delivery paths; the gate calls the real
`samsara.clipboard.paste_with_preservation` implementation.

Run from the repository root:

```powershell
F:\envs\sami\python.exe tools\probes\inject_matrix.py --gate
```

The gate runs only the production clipboard path against all four targets and
prints one `INJECT_MATRIX_GATE PASS` or `FAIL` line. It normally takes about
60 seconds and grabs the screen while the throwaway browser is active. Do not
run it while Samsara's Ctrl+Shift hold hotkey is down. JSON evidence is written
under `tools/probes/results/`.

For diagnosis, `--strats`, `--targets`, `--reps`, and `--browser brave|chrome|edge`
select the matrix. `--browser auto` detects the first installed Brave, Chrome,
or Edge executable.
