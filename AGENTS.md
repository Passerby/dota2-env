# AGENTS.md — coding style for dota2-env

Read together with `CLAUDE.md` (what this project is, how to run it) and `docs/PARAMETERS.md`
(every flag and timing constant). This file is the style contract: agents paste it into their
instructions, humans use the last section as a review checklist. The ruff config in
`pyproject.toml` enforces the mechanical half; the rest is review.

**North star: simplicity.** Fewer files, fewer functions, fewer lines. Speculative generality is
noise. Match the surrounding code where it already has a convention; where it doesn't, follow this.

## 1. Comments

- Explain **why**, never **what**. If a comment restates the code, delete it. Most comments an LLM
  writes are of the "what" kind — delete them.
- Sign every comment that carries judgement: `# Note (ruidu): ...`. A comment with no author and no
  reason is a comment nobody can challenge later.
- Self-contained. Do not narrate the change you just made ("switched this to a dict", "now handles
  the empty case"). That belongs in the commit message.
- **No backticks in Python comments or docstrings.** Write `target = 3`, not the backticked form.
  (Markdown files are unaffected — backticks are fine in `README.md` and `docs/`.)
- No banners (`# ==== SECTION ====`), no process markers (`★`, `# P1`, `# [FIX]`), no bare `# TODO`
  without an issue link.
- No essay at the top of a file and no long comment block after a `class` line. One short module
  docstring, one short class docstring.
- Docstrings: 1-3 lines, Google style, args/returns only when non-obvious.
- A usage example in a module docstring is indented four spaces, the way `examples/` and `scripts/`
  already do it. The formatter strips that indent when *every* body line is indented, so keep one
  flush-left line of prose above the example (a `>>>` prompt works for Python snippets).
- Client quirks (hidden hero after respawn, feed stopping at match end) get one line in code naming
  the constraint plus a pointer to `docs/VERSION_DIFF.md`, not a ten-line explanation inline.

Bad:

```python
# Build the observation dict. Loops over units, filters to the nearest MAX_UNITS,
# and writes each row into the table. Called once per step from step().
def build_observation(world_state, team_id):
```

Good:

```python
# Note (ruidu): rows are sorted by distance because action target = row index, so a
# reordering between two steps would silently retarget the agent's action.
def build_observation(world_state, team_id):
```

## 2. Structure and simplicity

- A helper called **once** is not a helper. Inline it. `_hero_vector` and `_clock` are the current
  offenders; `player_stat` (3 call sites) legitimately stays.
- No abstraction before the second concrete implementation: no base class, registry, factory or
  plugin layer with one subclass. Two cases first, then abstract.
- A constant imported into exactly one other module should be defined in that module instead. A
  constant genuinely shared (`DEFAULT_HERO`, `CONTROL_AGENT`, `TEAM_RADIANT`) stays in
  `bridge/game.py` / `bridge/constants.py`.
- File over ~400 lines: split. A 30-line file holding one helper: merge it back.
- Layering is a hard rule: `dota2_env/bridge/` talks to Dota and must not import gymnasium or numpy.
  Everything above it goes through `DotaSession` (start / observe / act / lua_status / match_winner /
  close). `tests/fake_session.py` mirrors that interface — change both together.
- Observation feature order lives in the `*_FEATURES` tuples in `observation.py` (the map part in
  `map_features.py`). Row `i` of the unit table is action `target = i` and text row `[i]`. Touching
  either changes the spaces: update the tests and the README tables in the same change.
- Every action file needs a main action for the controlled player (`NONE` for a no-op), otherwise Lua
  re-runs `extra_actions` every tick.

## 3. Naming

- `PascalCase` classes, `snake_case` functions and variables, `UPPER_SNAKE` constants.
- **Leading `_` only on functions nested inside another function.** Module-level functions, classes,
  methods and attributes get plain names: `session_factory`, not `self._session_factory`;
  `hero_vector`, not `_hero_vector`. A leading underscore is a scope marker, not decoration.
- Names carry the physical meaning, not the shape:

  | Avoid | Use | Meaning |
  |---|---|---|
  | `t` | `dota_time` | game clock in seconds |
  | `ws` | `world_state` | one `CMsgBotWorldState` |
  | `n` | `ticks_per_observation` | game ticks per env step (30 ticks = 1 game second) |
  | `u` | `unit` / `hero` | a unit row |
  | `d` | `delivery_delay` | game seconds between observation and Lua executing the action |

- Single letters only for loop indices (`i`, `j`) or genuine math (`x`, `y`).
- No import aliases that rename a module to a letter: `from dota2_env import actions`, never
  `from dota2_env import actions as A`.

## 4. Typing

- New functions get full annotations, modern syntax: `X | Y`, `list[str]`, `dict[str, int]`,
  `tuple[float, float]`, `X | None`. Annotate the return type too.
- Annotate what you touch. 0 of 78 functions in `dota2_env/` are annotated today, so this arrives
  file by file — a function you edit comes back annotated.
- **No `Any`** to get past a checker, and no bare `dict` / `list` / `tuple`. Write the real type:
  `world_state: CMsgBotWorldState`, `session: DotaSession`.
- No `getattr` / `hasattr` with a default to paper over a field that must exist. `getattr(obj, name)`
  with a genuinely dynamic `name` and no default (as in `rewards.py`) is fine; `getattr(obj, "x", None)`
  is not.
- Closed value sets become `Literal` or `Enum`, not bare strings compared in an `if`: `control` is
  `Literal['agent', 'builtin', 'idle']`, `render_mode` is `Literal['human', 'ansi'] | None`.
- No mutable defaults. Use `None` and fill in, or `field(default_factory=...)` in a dataclass.
  Mutable class attributes get `ClassVar`.
- Never silence a checker with a fake parameter use. `def act(self, request_id: str)` followed by
  `del request_id` is worse than not taking the parameter. If an external API forces the signature
  (`reset(self, seed=None, options=None)`), keep the name and leave it alone.
- Internal value objects (config, state) are `@dataclass`, preferably `kw_only=True`.

## 5. Control flow and defensiveness

- Do the cheap checks first and return early; then one complete `if / elif / else`. An `if` that
  changes a value must have the `else` that handles the other case, rather than relying on a value
  set three lines above.
- Keep nesting shallow: two levels of `if` inside a loop is the limit before you restructure.
- **Do not defend against what cannot happen.** No broad `try / except Exception`, no
  `if x is not None and isinstance(x, ...)` chains protecting an invariant you control, no fallback
  path for a branch that never runs. Ask of every `try` and every defensive `if`: what breaks if I
  delete this? "Nothing, it was for debugging" means delete it.
- The real failure surface here is narrow and known — socket, subprocess, protobuf, queue timeouts.
  The existing excepts are the model: `queue.Empty` while waiting for a world state,
  `(ConnectionError, OSError)` on the worldstate socket, `ValueError` parsing a console line. Catch
  that specific exception, at that specific call, and nowhere else.
- `assert` for internal invariants (shapes, feature counts, team ids you constructed).
  `raise ValueError` / `RuntimeError` / `FileNotFoundError` for bad user input or a broken Dota
  install. No bare `except:`, no `except Exception: pass`.
- A long-term workaround gets one line naming the constraint and a pointer to `docs/VERSION_DIFF.md`.

## 6. Logging and output

- One logger per module: `logger = logging.getLogger('dota2_env')`, configured by the caller.
- **f-strings everywhere**, including logging: `logger.info(f'bots folder: {self.dota_bot_path}')`.
  Ruff's `G004` is deliberately off so that logging follows the same rule as everything else.
  The one exception is a wide table row with many fields, where positional `str.format` stays more
  readable than a line of inlined expressions: the unit and hero rows in `text.py` and the step line
  in `examples/llm_agent.py`. Nothing else uses `str.format`.
- Terse English. No manual level prefixes (`[Info]`, `[step 2]`) — the level field already says it.
- `print` only in `examples/` and `scripts/`, where a human reads the output. Library code logs.

## 7. Imports and entry points

- Three groups — stdlib, third-party, local — blank-line separated, alphabetical. Ruff's isort
  enforces this; do not hand-sort.
- No `sys.path` insertions, no function-local imports (except a documented circular-import break),
  no `from x import *`.
- Import by full path from the package root: `from dota2_env.bridge.session import DotaSession`.
- `__init__.py` holds no imports, with one deliberate exception: `dota2_env/__init__.py` re-exports
  the public surface and calls `register()` so `gym.make('dota2_env/Mid1v1-v0')` works. Keep it
  minimal and declare `__all__`. Every other `__init__.py` stays empty.
- Any file that constructs the env needs `if __name__ == '__main__':`. The worldstate listener is a
  `multiprocessing` child and macOS spawns, so running from stdin or `python -c` fails.

## 8. Tests

- `tests/` runs without Dota, against `tests/fake_session.py`. Keep the fake in sync with
  `DotaSession` whenever the session interface changes.
- New logic gets a test. Cover the main path and the failure modes that actually occur
  (missing world state, action lost before Lua read it, match ending mid-step).
- **Do not write tests for defensive branches that should not exist.** A test asserting that a broad
  `except` swallowed an impossible error is a reason to delete both.
- Tests stay short and assert on behaviour, not on internal call order.

## 9. Tooling

```bash
uv pip install -e ".[dev]"
uvx ruff format .        # or .venv/bin/ruff after the dev install
uvx ruff check --fix .
.venv/bin/python -m pytest -q
```

`pre-commit install` wires both ruff hooks to every commit. Generated protobuf modules (`*_pb2.py`)
are excluded from lint and format — never hand-edit them. A type checker (pyright or mypy) becomes
the gate once section 4 has landed across the package; until then ruff and the tests are it.

## 10. Review checklist

Run this over your own diff before calling it done.

- [ ] Comment that restates the code, or narrates the edit? Delete it.
- [ ] Comment carrying a judgement without `# Note (name):`? Sign it.
- [ ] Backtick inside a Python comment or docstring? Remove it.
- [ ] Helper called once? Inline it. New file under 30 lines? Merge it.
- [ ] New base class / registry with one implementation? Delete it.
- [ ] Leading `_` on anything that is not a nested function? Rename it.
- [ ] Constant defined in one module and imported by exactly one other? Move it to the user.
- [ ] `Any`, bare `dict`/`list`, missing return type, or a `del arg` line? Fix the type.
- [ ] `getattr`/`hasattr` with a default? Read the attribute directly.
- [ ] `try/except` or defensive `if` that protects no real failure? Delete it — and its test.
- [ ] `if` that sets a value with no `else`? Complete the branch.
- [ ] `%`-style logging, a manual `[Info]` prefix, or a `print` in `dota2_env/`? Convert it.
- [ ] New import in an `__init__.py` other than `dota2_env/__init__.py`? Move it.
- [ ] Changed `*_FEATURES`, the action space, `launch_args` or `config_auto`? Update the tests,
      `README.md` and `docs/PARAMETERS.md` in the same change.
- [ ] Changed the `DotaSession` interface? Update `tests/fake_session.py`.

## Adoption status

The mechanical half has landed: `ruff check` and `ruff format` are clean over the whole tree, and
the 13 findings that needed a hand (`ClassVar` on mutable class attributes, `actions as A`, a
`dict()` call, a nested `if`, unused unpacked variables, an unowned file handle, two over-long
diagnostic strings) are fixed. `str.format` survives only in the three wide table rows named in
section 6; everything else is an f-string.

What is left, largest first:

1. **No type annotations** — 0 of 78 functions in `dota2_env/`. Annotate on touch (section 4).
2. **24 `_`-prefixed module-level functions and methods**, plus the matching `self._x` attributes,
   need renaming (section 3). This one changes call sites across the package, so do it as its own
   commit rather than inside a feature change.
3. **~50 backticks** inside Python docstrings and comments need stripping (section 1).
4. **`_hero_vector` and `_clock` are called once each** — inline them when their module is next
   touched (section 2).
