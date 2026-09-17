"""artifacts — the numbers that live outside the code (DP phase 4, spec §9).

A figure in a deck, a cell in a workbook, a line in a report: its identity is
its data source (`metric:<name>`, `<dataset>.<column>`, `declared:<slug>`), and
the file is merely a place it turned up. `extract` locates text inside such a
file; `anchor` records that a person pinned one of those places to a node, and
says `anchor_lost` — never something else — when the place stops saying it.
"""
