from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import traceback
import venv
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Iterator, TypeVar

from mypy_primer.git_utils import (
    RevisionLike,
    ensure_repo_at_revision,
    get_revision_for_revision_or_date,
    revision_or_recent_tag_fn,
)
from mypy_primer.globals import ctx, parse_options_and_set_ctx
from mypy_primer.model import MypyResult, Project
from mypy_primer.projects import get_projects
from mypy_primer.utils import (
    BIN_DIR,
    MYPY_EXE_NAME,
    Style,
    debug_print,
    line_count,
    run,
    strip_colour_code,
)

T = TypeVar("T")

# ==============================
# mypy utils
# ==============================


async def setup_mypy(mypy_dir: Path, revision_like: RevisionLike, editable: bool = False) -> Path:
    mypy_dir.mkdir(exist_ok=True)
    venv_dir = mypy_dir / "venv"
    venv.create(venv_dir, with_pip=True, clear=True)
    pip_exe = str(venv_dir / BIN_DIR / "pip")

    if ctx.get().mypyc_compile_level is not None:
        editable = True

    install_from_repo = True
    if (
        isinstance(revision_like, str)
        and not editable
        and ctx.get().repo == "https://github.com/python/mypy"
    ):
        # optimistically attempt to install the revision of mypy we want from pypi
        try:
            await run([pip_exe, "install", f"mypy=={revision_like}"])
            install_from_repo = False
        except subprocess.CalledProcessError:
            install_from_repo = True

    if install_from_repo:
        repo_dir = await ensure_repo_at_revision(ctx.get().repo, mypy_dir, revision_like)
        if ctx.get().mypyc_compile_level is not None:
            env = os.environ.copy()
            env["MYPYC_OPT_LEVEL"] = str(ctx.get().mypyc_compile_level)
            python_exe = str(venv_dir / BIN_DIR / "python")
            await run([pip_exe, "install", "typing_extensions", "mypy_extensions"])
            await run(
                [python_exe, "setup.py", "--use-mypyc", "build_ext", "--inplace"],
                cwd=repo_dir,
                env=env,
            )
        install_cmd = [pip_exe, "install"]
        if editable:
            install_cmd.append("--editable")
        install_cmd.append(str(repo_dir))
        install_cmd.append("tomli")
        await run(install_cmd)

    mypy_exe = venv_dir / BIN_DIR / MYPY_EXE_NAME
    if sys.platform == "darwin":
        # warm up mypy on macos to avoid the first run being slow
        await run([str(mypy_exe), "--version"])
    assert mypy_exe.exists()
    return mypy_exe


async def setup_new_and_old_mypy(
    new_mypy_revision: RevisionLike, old_mypy_revision: RevisionLike
) -> tuple[Path, Path]:
    new_mypy, old_mypy = await asyncio.gather(
        setup_mypy(ctx.get().base_dir / "new_mypy", new_mypy_revision),
        setup_mypy(ctx.get().base_dir / "old_mypy", old_mypy_revision),
    )

    if ctx.get().debug:
        (new_version, _), (old_version, _) = await asyncio.gather(
            run([str(new_mypy), "--version"], output=True),
            run([str(old_mypy), "--version"], output=True),
        )
        debug_print(f"{Style.BLUE}new mypy version: {new_version.stdout.strip()}{Style.RESET}")
        debug_print(f"{Style.BLUE}old mypy version: {old_version.stdout.strip()}{Style.RESET}")

    return new_mypy, old_mypy


async def setup_new_and_old_typeshed(
    new_typeshed_revision: RevisionLike, old_typeshed_revision: RevisionLike
) -> tuple[Path | None, Path | None]:
    typeshed_repo = ctx.get().custom_typeshed_repo

    new_typeshed_dir = None
    old_typeshed_dir = None
    if ctx.get().new_typeshed:
        parent_dir = ctx.get().base_dir / "new_typeshed"
        if parent_dir.exists():
            shutil.rmtree(parent_dir)
        parent_dir.mkdir(exist_ok=True)
        new_typeshed_dir = await ensure_repo_at_revision(
            typeshed_repo, ctx.get().base_dir / "new_typeshed", new_typeshed_revision
        )
    if ctx.get().old_typeshed:
        parent_dir = ctx.get().base_dir / "old_typeshed"
        if parent_dir.exists():
            shutil.rmtree(parent_dir)
        parent_dir.mkdir(exist_ok=True)
        old_typeshed_dir = await ensure_repo_at_revision(
            typeshed_repo, parent_dir, old_typeshed_revision
        )
    return new_typeshed_dir, old_typeshed_dir


# ==============================
# project utils
# ==============================


def select_projects() -> list[Project]:
    ARGS = ctx.get()
    if ARGS.local_project:
        return [Project.from_location(ARGS.local_project)]

    project_iter: Iterator[Project] = iter(p for p in get_projects())
    if ARGS.project_selector:
        project_iter = iter(
            p for p in project_iter if re.search(ARGS.project_selector, p.location, flags=re.I)
        )
    if ARGS.expected_success:
        project_iter = (p for p in project_iter if p.expected_success)
    if ARGS.project_date:
        project_iter = (replace(p, revision=ARGS.project_date) for p in project_iter)

    projects = list(project_iter)
    if projects == []:
        raise ValueError("No projects selected!")

    if ARGS.num_shards:
        assert ARGS.shard_index is not None
        shard_costs = [0] * ARGS.num_shards
        shard_projects: list[list[Project]] = [[] for _ in range(ARGS.num_shards)]
        for p in sorted(projects, key=lambda p: (p.cost, p.location), reverse=True):
            min_shard = min(range(ARGS.num_shards), key=lambda i: shard_costs[i])
            shard_costs[min_shard] += p.cost
            shard_projects[min_shard].append(p)
        return shard_projects[ARGS.shard_index]
    return projects


# ==============================
# hidden entrypoint logic
# ==============================

RECENT_MYPYS = ["0.991", "0.982", "0.971", "0.961"]


async def validate_expected_success() -> None:
    """Check correctness of hardcoded Project.expected_success"""
    recent_mypy_exes = await asyncio.gather(
        *[
            setup_mypy(ctx.get().base_dir / ("mypy_" + recent_mypy), recent_mypy)
            for recent_mypy in RECENT_MYPYS
        ]
    )

    async def inner(project: Project) -> str | None:
        await project.setup()
        success = None
        for mypy_exe in recent_mypy_exes:
            mypy_result = await project.run_mypy(mypy_exe, typeshed_dir=None, mypy_path=[])
            if ctx.get().debug:
                debug_print(format(Style.BLUE))
                debug_print(mypy_result)
                debug_print(format(Style.RESET))
            if mypy_result.success:
                success = mypy_exe
                break
        if bool(success) and not project.expected_success:
            return (
                f"Project {project.location} succeeded with {success}, "
                "but is not marked as expecting success"
            )
        if not bool(success) and project.expected_success:
            return f"Project {project.location} did not succeed, but is marked as expecting success"
        return None

    results = await asyncio.gather(*[inner(project) for project in select_projects()])
    for result in results:
        if result:
            print(result)


async def measure_project_runtimes() -> None:
    """Check mypy's runtime over each project."""
    mypy_exe = await setup_mypy(
        ctx.get().base_dir / "timer_mypy", ctx.get().new_mypy or RECENT_MYPYS[0]
    )

    async def inner(project: Project) -> tuple[float, Project]:
        await project.setup()
        result = await project.run_mypy(mypy_exe, typeshed_dir=None, mypy_path=[])
        return (result.runtime, project)

    results = sorted(
        (await asyncio.gather(*[inner(project) for project in select_projects()])), reverse=True
    )
    for time_taken, project in results:
        print(f"{time_taken:6.2f}  {project.location}")


# ==============================
# entrypoint logic
# ==============================


# TODO: can't bisect over typeshed commits yet
async def bisect() -> None:
    ARGS = ctx.get()

    assert not ARGS.new_typeshed
    assert not ARGS.old_typeshed

    mypy_exe = await setup_mypy(
        ARGS.base_dir / "bisect_mypy",
        revision_or_recent_tag_fn(ARGS.old_mypy),
        editable=True,
    )
    repo_dir = ARGS.base_dir / "bisect_mypy" / "mypy"
    assert repo_dir.is_dir()

    projects = select_projects()
    await asyncio.wait([project.setup() for project in projects])

    async def run_wrapper(project: Project) -> tuple[str, MypyResult]:
        return project.name, (
            await project.run_mypy(str(mypy_exe), typeshed_dir=None, mypy_path=[])
        )

    results_fut = await asyncio.gather(*(run_wrapper(project) for project in projects))
    old_results: dict[str, MypyResult] = dict(results_fut)
    if ARGS.debug:
        debug_print("\n".join(str(result) for result in old_results.values()))
        debug_print(format(Style.RESET))

    # Note git bisect start will clean up old bisection state
    await run(["git", "bisect", "start"], cwd=repo_dir, output=True)
    await run(["git", "bisect", "good"], cwd=repo_dir, output=True)
    new_revision = await get_revision_for_revision_or_date(ARGS.new_mypy or "origin/HEAD", repo_dir)
    await run(["git", "bisect", "bad", new_revision], cwd=repo_dir, output=True)

    def are_results_good(results: dict[str, MypyResult]) -> bool:
        if ARGS.bisect_output:
            return not any(
                re.search(ARGS.bisect_output, strip_colour_code(results[project.name].output))
                for project in projects
            )
        return all(
            results[project.name].output == old_results[project.name].output for project in projects
        )

    assert are_results_good(old_results)

    while True:
        await run(["git", "submodule", "update", "--init"], cwd=repo_dir)
        results_fut = await asyncio.gather(*(run_wrapper(project) for project in projects))
        results: dict[str, MypyResult] = dict(results_fut)

        state = "good" if are_results_good(results) else "bad"
        proc, _ = await run(["git", "bisect", state], output=True, cwd=repo_dir)

        if "first bad commit" in proc.stdout:
            print(proc.stdout)
            return

        if ARGS.debug:
            debug_print("\n".join(str(result) for result in results.values()))
            debug_print(proc.stdout)
            debug_print(format(Style.RESET))


async def coverage() -> None:
    mypy_exe = await setup_mypy(ctx.get().base_dir / "new_mypy", ctx.get().new_mypy)

    projects = select_projects()
    mypy_python = mypy_exe.parent / "python"
    assert mypy_python.exists()

    all_paths = await asyncio.gather(
        *[project.source_paths(str(mypy_python)) for project in projects]
    )

    project_to_paths: dict[str, int] = {}
    project_to_lines: dict[str, int] = {}
    for project, paths in zip(projects, all_paths):
        project_to_paths[project.location] = len(paths)
        project_to_lines[project.location] = sum(map(line_count, paths))

    for project in sorted(projects, key=lambda p: project_to_lines[p.location], reverse=True):
        p = project.location
        print(p, project_to_lines[p], project_to_paths[p])

    print(f"Checking {len(projects)} projects...")
    print(f"Containing {sum(project_to_paths.values())} files...")
    print(f"Totalling to {sum(project_to_lines.values())} lines...")


async def primer() -> int:
    projects = select_projects()
    new_mypy, old_mypy = await setup_new_and_old_mypy(
        new_mypy_revision=ctx.get().new_mypy,
        old_mypy_revision=revision_or_recent_tag_fn(ctx.get().old_mypy),
    )
    new_typeshed_dir, old_typeshed_dir = await setup_new_and_old_typeshed(
        ctx.get().new_typeshed, ctx.get().old_typeshed
    )

    results = [
        project.primer_result(
            new_mypy=str(new_mypy),
            old_mypy=str(old_mypy),
            new_typeshed=new_typeshed_dir,
            old_typeshed=old_typeshed_dir,
            new_mypypath=ctx.get().new_mypypath,
            old_mypypath=ctx.get().old_mypypath,
        )
        for project in projects
    ]
    retcode = 0
    for result_fut in asyncio.as_completed(results):
        result = await result_fut
        if ctx.get().old_success and not result.old_result.success:
            continue
        if ctx.get().output == "full":
            print(result.format_full())
        elif ctx.get().output == "diff":
            print(result.format_diff_only())
        elif ctx.get().output == "concise":
            # using ctx.get().output == "concise" also causes us to:
            # - always pass in --no-pretty and --no-error-summary
            concise = result.format_concise()
            if concise:
                print(concise)
                print()
        if not retcode and result.diff:
            retcode = 1
    return retcode


def main() -> None:
    if sys.platform == "win32":
        # Enables ANSI escape characters in terminal without resorting to ctypes or colorama
        os.system("")

    def inner() -> int | None:
        ARGS = parse_options_and_set_ctx(sys.argv[1:])

        if ARGS.base_dir.exists() and ARGS.clear:
            shutil.rmtree(ARGS.base_dir)
        ARGS.base_dir = ARGS.base_dir.absolute()
        ARGS.base_dir.mkdir(exist_ok=True)
        ARGS.projects_dir = ARGS.base_dir / "projects"
        ARGS.projects_dir.mkdir(exist_ok=True)

        coro: Awaitable[int | None]
        if ARGS.coverage:
            coro = coverage()
        elif ARGS.bisect or ARGS.bisect_output:
            coro = bisect()
        elif ARGS.validate_expected_success:
            coro = validate_expected_success()
        elif ARGS.measure_project_runtimes:
            coro = measure_project_runtimes()
        else:
            coro = primer()

        try:
            retcode = asyncio.run(coro)
        finally:
            if ARGS.base_dir.exists() and ARGS.clear:
                shutil.rmtree(ARGS.base_dir)
        return retcode

    try:
        retcode = inner()
    except Exception:
        traceback.print_exc()
        retcode = 70
    sys.exit(retcode)