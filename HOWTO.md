# HOWTO

The basic commands for working in this repo. For *what the packages are and how they fit
together*, see [README.md](README.md). This file is just "which command do I type."

We use **uv** to manage everything (Python version, the virtualenv, and all the packages in
this monorepo). You almost never call `python` or `pip` directly — you use `uv`.

> **During the reorg (now):** the workspace is being assembled in phases. Until the move
> lands, the app still runs from the single `catalogue` package the old way, and `uv sync`
> is **not** ready yet. The commands below are how things work *once the reorg completes*.

---

## 1. One-time: install uv

macOS / Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Check it worked:

```
uv --version
```

(Full install options: <https://docs.astral.sh/uv/getting-started/installation/>.)

## 2. Set up the project

From the repo root, once after cloning (and any time dependencies change):

```
uv sync
```

This creates a virtualenv in `.venv/` and installs **every package in the repo**, editable,
from the locked versions in `uv.lock`. "Editable" means: when you edit code, the change is
live immediately — no reinstall.

## 3. Run things

Prefix any command with `uv run` and it runs inside the project's environment:

```
uv run pytest                 # run the whole test suite
bash scripts/library-serve.sh # start the web server + tunnel (the supported way — it sources auth)
```

You do **not** need to "activate" anything — `uv run` handles it. (If you prefer a classic
activated shell: `source .venv/bin/activate`.)

## 4. Run tests for just one package

```
uv run pytest catalogue/access-api/tests
```

Each package keeps its own tests next to it (see the package list in the README).

## 5. Add or remove a dependency

Always say *which* package the dependency belongs to with `--package`:

```
uv add flask --package catalogue-webui          # add a third-party dependency
uv add access-api --package catalogue-webui      # depend on one of OUR packages
uv remove flask --package catalogue-webui        # remove one
```

`uv` updates that package's `pyproject.toml` and re-locks. Which package owns which
dependency is listed in the README's "Package dependencies" section.

## 6. After someone else changes dependencies

Pulled new code and things don't import? Re-sync:

```
uv sync
```

## 7. Update locked versions

```
uv lock --upgrade        # bump everything to the newest allowed versions
uv sync                  # then install them
```

---

## Cheat sheet

| I want to… | Command |
|------------|---------|
| Set up / refresh the project | `uv sync` |
| Run the tests | `uv run pytest` |
| Test one package | `uv run pytest catalogue/<pkg>/tests` |
| Run any command in the env | `uv run <command>` |
| Add a dependency to a package | `uv add <dep> --package <pkg>` |
| Remove a dependency | `uv remove <dep> --package <pkg>` |
| Re-lock after upstream changes | `uv lock` then `uv sync` |

## When something is off

- **`uv: command not found`** → redo step 1, then open a new terminal.
- **Import errors after pulling** → `uv sync`.
- **Which package needs what / how packages depend on each other** → see
  [README.md](README.md) ("Package dependencies").
