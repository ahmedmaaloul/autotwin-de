# AutoTwin DE — MCP server

Exposes the platform's analysis as **tools** for any Model Context Protocol client — Claude
Desktop, Cursor, Zed, an IDE agent. The client brings the model; this server brings grounded
numbers from the same code the API serves.

| Tool | What it returns |
|---|---|
| `list_routes` | The seeded corridors with length and driving time |
| `vehicle_profiles` | The five generic EV class profiles |
| `analyze_route` | Full journey analysis: energy, arrival SOC, weather/traffic penalties, per-segment intensity, the deterministic explanation |
| `plan_charging_stops` | Beam-search charging plan over corridor fast chargers, with the reason each stop won |
| `corridor_coverage` | PostGIS corridor coverage and the largest gap between chargers ≥ N kW, with its methodology sentence |
| `underserved_corridors` | Every corridor ranked by worst gap, flagged against a threshold — all parameters exposed |
| `search_charging_stations` | The Bundesnetzagentur register by text, state, power or proximity |
| `data_quality` | Per-source status, freshness, acceptance rate, mode, licence and attribution |

## Why tools, not a chat endpoint

The original brief sketched an in-app "copilot" with a model behind an HTTP endpoint. A tool
server is the better design for the same goal:

- **Nothing is invented.** A tool runs `analyse_route` or the corridor query and returns the
  result. The model can phrase it; it never computes a kWh figure itself.
- **Zero cost, no key in the repository.** The platform runs no model. Whoever connects pays
  for their own tokens or runs a local model.
- **Smaller and testable.** Tool schemas in, results out. The "why" a chat wrapper would have
  verbalised already exists deterministically in `autotwin_ml.insights`.

## Running

It needs the platform's database, like the API does:

```bash
make up && make db-upgrade && make seed      # once
uv run python -m autotwin_mcp                 # serves over stdio
```

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — adjust the path:

```json
{
  "mcpServers": {
    "autotwin-de": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/autotwin-de", "python", "-m", "autotwin_mcp"]
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the repository:

```json
{
  "mcpServers": {
    "autotwin-de": { "command": "uv", "args": ["run", "python", "-m", "autotwin_mcp"] }
  }
}
```

Then ask, in either language:

> Warum ist der Energieverbrauch auf Frankfurt → Stuttgart heute höher als der Normverbrauch?

> Which of the seeded corridors has the worst fast-charging gap at 150 kW, and how bad is it?

The client will call `analyze_route` / `underserved_corridors` and answer from the returned
figures. Geometry is omitted from tool results unless a client asks for it
(`include_geometry=true`) — a 41-segment route is ~200 KB of coordinates a language model has
no use for.

## Honesty

`instructions` handed to the client state that vehicle telemetry and the ML training labels
are simulated, and every tool's description repeats it where relevant. A client that ignores
that is a client problem; the server never presents simulated data as measured.
