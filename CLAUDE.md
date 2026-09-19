# CLAUDE.md

Gymnasium environment for Dota 2 (1v1 mid), extracted from `../LastOrder-Dota2`. Read `README.md` for the
interface, `docs/PARAMETERS.md` for every launch flag / env parameter / timing constant (keep it in sync when
changing `launch_args`, `config_auto`, or `DotaMid1v1Env.__init__`), `docs/VERSION_DIFF.md` for what differs on current Dota clients,
and `docs/IPC_CHANNELS.md` for why the Python <-> Lua channel is files and not the bot VM's `CreateHTTPRequest`
(measured; re-run `scripts/probe_http.py` before reopening that question).
`AGENTS.md` is the coding style contract - read it before writing Python here, and run its
review checklist over your own diff.

## Commands

```bash
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q                                   # no Dota needed (tests/fake_session.py)
.venv/bin/python examples/scripted_agent.py --timescale 4        # real headless Dota, ~2 min per episode
.venv/bin/python scripts/probe_worldstate.py --seconds 120       # raw bridge compatibility probe
```

- Real-Dota runs need Steam running. Prefer headless (`render_mode=None`, `-dedicated`); only one instance at a time.
- Scripts that create the env must live in a file with an `if __name__ == '__main__':` guard: the worldstate
  listener is a `multiprocessing` child and macOS uses spawn (running from stdin / `python -c` fails).
- A crashed run can leave `<dota>/game/dota/scripts/vscripts/bots` symlinked and session folders
  (`$TMPDIR/dota2_env_*`) behind; `DotaGame` replaces a stale symlink on the next start.

## Layout rules

- `dota2_env/bridge/` talks to Dota and must not import gymnasium/numpy. Everything above it only goes through
  `DotaSession` (start / observe / act / lua_status / match_winner / close), which `tests/fake_session.py` mirrors —
  keep the two in sync when the session interface changes.
- Observation feature order is defined by the `*_FEATURES` tuples in `observation.py`; row `i` of the unit table is
  action `target = i` and text row `[i]`. Changing either changes the spaces, so update tests and README tables.
- Lua lives in `bridge/lua/`; `bot_controlled.lua.tpl` is copied to `bot_<hero>.lua` per configured hero. Lua reads
  `config_auto.lua` (`heroes`, `control` per team: agent / builtin / idle).
- Every action file needs a main action for the controlled player (NONE for no-op), otherwise Lua re-runs
  `extra_actions` every tick.
- Client quirks the env works around (hidden hero after respawn, feed stops at match end) are documented in
  `docs/VERSION_DIFF.md`; re-verify them with a real run before removing the workarounds.
