# Ownership parser fixtures

`tdcc_synthetic.csv` is hand-authored **synthetic** numerical test data. It uses the verified TDCC six-column CSV shape and current17-code scheme, with15regular rows of100 units each, adjustment20 and source total1480. Adjustment holder_count=3 is deliberately not added to the total15. Reported regular percentages sum99.90 and are preserved, not scaled to100. This fixture is not an official observed report, is not used as a seed, and must never be imported as real production coverage.

Official O1 source / scheme / encoding evidence is described in `docs/specs/phase-2a-data-sources.md`. Current UTF8 bytes were separately audited in `/tmp`; no official full payload is committed here.
