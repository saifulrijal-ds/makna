"""DAT-1 acceptance 1 and 5: byte-identical output per seed, and the speed of --scale 0.05."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .generator_support import ROOT, TABLE_NAMES

SEED = "20261111"


def run_generator(out: Path, seed: str, scale: str, hash_seed: str) -> float:
    """Run the documented command in a fresh process and return its wall time in seconds."""
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    started = time.perf_counter()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "data.generator",
            "--out",
            str(out),
            "--seed",
            seed,
            "--scale",
            scale,
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    return time.perf_counter() - started


def digests(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / f"{name}.parquet").read_bytes()).hexdigest()
        for name in TABLE_NAMES
    }


@pytest.fixture(scope="module")
def first_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    out = tmp_path_factory.mktemp("run_a")
    seconds = run_generator(out, SEED, "0.05", hash_seed="1")
    return out, seconds


def test_same_seed_gives_byte_identical_files(
    first_run: tuple[Path, float], tmp_path: Path, data_small: Path
) -> None:
    run_a, _ = first_run
    # A different PYTHONHASHSEED would expose any dependence on set or dict ordering of strings.
    run_generator(tmp_path, SEED, "0.05", hash_seed="2")
    assert digests(run_a) == digests(tmp_path)
    # The in-process API used by the other tests gives the same bytes as the command.
    assert digests(run_a) == digests(data_small)


def test_different_seed_gives_different_files(
    first_run: tuple[Path, float], tmp_path: Path
) -> None:
    run_a, _ = first_run
    run_generator(tmp_path, "7", "0.05", hash_seed="1")
    same, other = digests(run_a), digests(tmp_path)
    changed = {name for name in TABLE_NAMES if same[name] != other[name]}
    # `products` is fixed reference data (eight invented products); every other table is random.
    assert changed == set(TABLE_NAMES) - {"products"}


def test_scale_changes_the_data(first_run: tuple[Path, float], tmp_path: Path) -> None:
    run_a, _ = first_run
    run_generator(tmp_path, SEED, "0.03", hash_seed="1")
    assert digests(run_a)["customers"] != digests(tmp_path)["customers"]


def test_scale_0_05_finishes_in_under_20_seconds(first_run: tuple[Path, float]) -> None:
    _, seconds = first_run
    assert seconds < 20, f"--scale 0.05 took {seconds:.1f} s"


def test_files_carry_no_timestamps_or_paths(first_run: tuple[Path, float]) -> None:
    import pyarrow.parquet as pq

    run_a, _ = first_run
    meta = pq.ParquetFile(run_a / "contracts.parquet").metadata
    assert str(run_a) not in str(meta.to_dict())
    assert meta.num_row_groups == 1
