# SMB qualification rehearsal - NOT SMB EVIDENCE

The SMB qualification procedure (`python -m omr_scanner.tools.smb_qualification rehearse`)
run end to end on **one machine with local folders**, at commit `57449d6` (source build),
2026-10-07: two PowerShell scanner writers (the script the scanner PCs run), the
coordinator killed once with a sheet in a worker and restarted, one source taken
away by removing a folder link and returned, then the evaluator.

**Verdict: `NOT SMB QUALIFICATION` - as it must be.** It shows that the tooling
works. It is not SMB evidence of any kind: the sources are not UNC shares, the
writers ran on the OMRFlow machine, and the outage was a link removal. The real
run is described in [`docs/release/SMB_QUALIFICATION.md`](../../SMB_QUALIFICATION.md)
and has not been performed.

`genuine_smb_topology` and `scale_and_workload` are *not exercised* (never PASS
in a rehearsal); every other assertion passed. Timings here are local-disk numbers
with a fast stability policy and say nothing about a network share.
