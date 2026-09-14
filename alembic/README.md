# Migrations

Alembic, targeting `autotwin_core.db.base.Base.metadata`. The database URL comes from
`autotwin_core.config.get_settings().database_url_sync` — never from `alembic.ini` — so a
migration run and the application cannot be pointed at different databases by accident.

## Running

```bash
make db-upgrade                      # alembic upgrade head
make db-downgrade                    # alembic downgrade -1
make db-revision m="add fleet table" # autogenerate a new revision
uv run alembic upgrade head --sql    # emit SQL without touching a database
```

## Writing a revision

1. Change the models in `packages/core/src/autotwin_core/db/models.py`.
2. `make db-revision m="..."` — autogenerate against a database that is already at head.
3. **Read the generated file.** Autogenerate does not detect everything: it misses
   `CREATE EXTENSION`, enum value additions, data backfills, and most constraint renames.
4. Apply it, then `alembic downgrade -1` and `alembic upgrade head` again. A revision that
   cannot round-trip is not finished.

## Rules

- **A committed migration is never edited.** It has already run on someone's database. Fix it
  forward with a new revision.
- Revisions are numbered `0001`, `0002`, … Ordinals sort correctly in a directory listing;
  timestamp prefixes do not, for a human reading a pull request.
- Enum types are created explicitly with `checkfirst=True` before the tables that use them, and
  dropped in `downgrade()`. PostgreSQL does not drop an enum type when the last column using it
  is dropped.
- Spatial indexes are declared in the models and created explicitly with
  `postgresql_using="gist"`. GeoAlchemy2's `spatial_index=True` would create a second,
  differently-named index behind our back.
- PostGIS's own objects (`spatial_ref_sys`, `geometry_columns`, the `tiger`/`topology` schemas)
  are filtered out by `include_object` in `env.py`. Without that filter, autogenerate proposes
  dropping them and applying the result breaks PostGIS.
