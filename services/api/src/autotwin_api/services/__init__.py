"""Analytical helpers that are too big — or too shared — to live inside a router module.

A router's job is to validate query parameters, call one thing and render the result. When the
"one thing" is a page of corridor mathematics that two endpoints need and a dbt model already
implements, it belongs here instead: ``/api/v1/charging/coverage`` and
``/api/v1/charging/underserved`` are two presentations of a single analysis, and duplicating it
would let them drift into reporting different worst gaps for the same corridor.
"""

from __future__ import annotations

__all__: list[str] = []
