"""Tests for ``autotwin_ml`` — the energy model, the charging optimiser and the insights.

A package rather than a bare directory because these four modules share a vocabulary of
hand-derived constants (the sedan profile's mass, its drag area, the 0.90 drivetrain
efficiency) and a reader moving between the files should see them as one suite.
"""
