"""Unit tests for engine-owned runtime paths.

Tests cover:
- Selection of the temp parent from the environment
- OS-native defaults, and the fact that resolving creates nothing
- Rejection of an explicitly selected path that cannot be used
- Owner-only, unique allocation of engine scratch
"""

import dataclasses
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from siteops.runtime import (
    POSIX_MODE_ENFORCED,
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    TEMP_DIR_ENV,
    RuntimePathError,
    RuntimePaths,
    bounded_runtime_path,
    create_private_directory,
    create_private_file,
    default_temp_root,
    describe_os_error,
    prepare_root,
)

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX file mode, Windows access is ACL-based",
)


class TestRuntimePathsModel:
    """The model is a frozen absolute temporary parent."""

    def test_the_model_is_frozen(self, tmp_path):
        paths = RuntimePaths(temp_root=tmp_path / "t")

        with pytest.raises(dataclasses.FrozenInstanceError):
            paths.temp_root = tmp_path / "other"

    def test_constructing_the_model_creates_nothing(self, tmp_path):
        paths = RuntimePaths(temp_root=tmp_path / "t")

        assert not paths.temp_root.exists()

    def test_a_relative_root_is_rejected(self):
        with pytest.raises(RuntimePathError, match="temp_root"):
            RuntimePaths(temp_root=Path("relative/path"))

    def test_a_string_root_is_rejected(self, tmp_path):
        """A string that looks like a path would silently defeat every Path
        operation the callers perform on these roots."""
        with pytest.raises(TypeError):
            RuntimePaths(temp_root=str(tmp_path))


class TestRootSelection:
    """An explicitly set Site Ops variable is honored or fails, never ignored."""

    def test_temp_root_is_selected_from_the_environment(self, tmp_path):
        environment = {
            TEMP_DIR_ENV: str(tmp_path / "chosen-temp"),
        }

        paths = RuntimePaths.resolve(environment)

        assert paths.temp_root == tmp_path / "chosen-temp"

    def test_selection_creates_nothing(self, tmp_path):
        environment = {
            TEMP_DIR_ENV: str(tmp_path / "chosen-temp"),
        }

        paths = RuntimePaths.resolve(environment)

        assert not paths.temp_root.exists()

    def test_absence_falls_back_to_the_defaults(self):
        paths = RuntimePaths.resolve({})

        assert paths.temp_root == default_temp_root()

    def test_the_process_environment_is_read_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path / "from-process-env"))

        assert RuntimePaths.resolve().temp_root == tmp_path / "from-process-env"

    def test_a_home_relative_selection_is_expanded(self):
        paths = RuntimePaths.resolve({TEMP_DIR_ENV: os.path.join("~", "siteops-temp")})

        assert paths.temp_root == Path.home() / "siteops-temp"

    @pytest.mark.parametrize("variable", [TEMP_DIR_ENV])
    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_an_empty_selection_is_rejected(self, variable, value):
        """An operator who exported the variable meant to redirect the engine.
        Falling back silently would put transient files somewhere they did not
        choose."""
        with pytest.raises(RuntimePathError) as error:
            RuntimePaths.resolve({variable: value})

        assert variable in str(error.value)

    @pytest.mark.parametrize("variable", [TEMP_DIR_ENV])
    @pytest.mark.parametrize(
        "value",
        ["relative/path", "./here", os.path.join("..", "up"), "siteops-temp"],
    )
    def test_a_relative_selection_is_rejected(self, variable, value):
        with pytest.raises(RuntimePathError) as error:
            RuntimePaths.resolve({variable: value})

        message = str(error.value)
        assert variable in message
        assert "absolute" in message

    @pytest.mark.skipif(os.name != "nt", reason="Windows drive-relative form")
    def test_a_drive_relative_selection_is_rejected(self):
        """`C:temp` resolves against the drive's working directory, which is
        process state, not a location."""
        with pytest.raises(RuntimePathError):
            RuntimePaths.resolve({TEMP_DIR_ENV: "C:temp"})

    def test_a_selected_root_is_trimmed(self, tmp_path):
        """A trailing newline from a shell export is not part of the path."""
        paths = RuntimePaths.resolve({TEMP_DIR_ENV: f"  {tmp_path}\n"})

        assert paths.temp_root == tmp_path


class TestPlatformDefaults:
    """Defaults are OS-native, and never the workspace or an AIO location."""

    def test_the_temp_default_is_the_system_temporary_directory(self):
        assert default_temp_root() == Path(tempfile.gettempdir())

    def test_no_default_is_derived_from_the_working_directory(self):
        """A workspace or an AIO checkout is never a default runtime root."""
        paths = RuntimePaths.resolve({})
        cwd = Path.cwd()

        assert paths.temp_root != cwd
        assert cwd not in paths.temp_root.parents


class TestPreparingARoot:
    """A root is created when missing and otherwise left exactly as it is."""

    def test_a_missing_root_is_created(self, tmp_path):
        root = tmp_path / "missing" / "nested"

        assert prepare_root(root) == root
        assert root.is_dir()

    def test_an_existing_root_keeps_its_permissions_and_content(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        operator_file = root / "operator-owned.txt"
        operator_file.write_text("keep me", encoding="utf-8")
        mode_before = root.stat().st_mode

        prepare_root(root)

        assert root.stat().st_mode == mode_before
        assert operator_file.read_text(encoding="utf-8") == "keep me"

    def test_a_root_that_cannot_be_created_fails_clearly(self, tmp_path):
        occupied = tmp_path / "a-file"
        occupied.write_text("not a directory", encoding="utf-8")

        with pytest.raises(RuntimePathError) as error:
            prepare_root(occupied / "child")

        assert "child" in str(error.value)


class TestPrivateAllocation:
    """Scratch is unique, owner-only from creation, and caller-owned."""

    def test_a_directory_is_created_under_the_given_parent(self, tmp_path):
        allocated = create_private_directory(tmp_path, prefix="siteops-run-")

        assert allocated.parent == tmp_path
        assert allocated.is_dir()
        assert allocated.name.startswith("siteops-run-")

    def test_a_missing_parent_is_created(self, tmp_path):
        parent = tmp_path / "not-yet"

        allocated = create_private_directory(parent, prefix="siteops-run-")

        assert allocated.parent == parent

    def test_the_parent_permissions_are_not_changed(self, tmp_path):
        parent = tmp_path / "root"
        parent.mkdir()
        mode_before = parent.stat().st_mode

        create_private_directory(parent, prefix="siteops-run-")

        assert parent.stat().st_mode == mode_before

    @posix_only
    def test_a_directory_is_owner_only_from_creation(self, tmp_path):
        allocated = create_private_directory(tmp_path, prefix="siteops-run-")

        assert stat.S_IMODE(allocated.stat().st_mode) == PRIVATE_DIR_MODE

    @posix_only
    def test_a_file_is_owner_only_from_creation(self, tmp_path):
        handle, path = create_private_file(tmp_path, prefix="params-", suffix=".json")
        os.close(handle)

        assert stat.S_IMODE(path.stat().st_mode) == PRIVATE_FILE_MODE

    def test_a_file_is_created_in_the_given_directory(self, tmp_path):
        handle, path = create_private_file(tmp_path, prefix="params-", suffix=".json")
        os.close(handle)

        assert path.parent == tmp_path
        assert path.name.startswith("params-")
        assert path.suffix == ".json"

    def test_the_returned_descriptor_writes_to_the_returned_path(self, tmp_path):
        handle, path = create_private_file(tmp_path, prefix="params-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("content")

        assert path.read_text(encoding="utf-8") == "content"

    def test_concurrent_allocations_are_unique(self, tmp_path):
        """Parallel sites, parallel runs, and parallel executors all allocate
        from the same root at once."""
        allocated: list[Path] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(12)

        def allocate():
            try:
                barrier.wait(timeout=10)
                allocated.append(create_private_directory(tmp_path, prefix="siteops-run-"))
            except BaseException as error:  # surfaced below
                errors.append(error)

        threads = [threading.Thread(target=allocate) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(allocated) == 12
        assert len(set(allocated)) == 12

    def test_an_unusable_parent_fails_clearly(self, tmp_path):
        occupied = tmp_path / "a-file"
        occupied.write_text("not a directory", encoding="utf-8")

        with pytest.raises(RuntimePathError):
            create_private_directory(occupied / "child", prefix="siteops-run-")


class TestDiagnostics:
    """A private path reaches a log, so it is reported bounded."""

    def test_a_path_is_reported_without_its_prefix(self, tmp_path):
        allocated = tmp_path / "temp-root" / "siteops-run-abc123"

        rendered = bounded_runtime_path(allocated)

        assert "siteops-run-abc123" in rendered
        assert "temp-root" in rendered
        assert str(tmp_path) not in rendered

    def test_a_bare_name_survives(self):
        assert bounded_runtime_path(Path("siteops-run-abc123")) == "siteops-run-abc123"

    def test_an_os_error_is_reported_without_its_filename(self, tmp_path):
        error = OSError(39, "Directory not empty", str(tmp_path / "secret-place"))

        rendered = describe_os_error(error)

        assert rendered == "Directory not empty"
        assert "secret-place" not in rendered

    def test_a_message_only_error_keeps_its_message(self):
        """An error raised with only a message carries no path to leak, and
        reducing it to a class name leaves an operator nothing to act on."""
        assert describe_os_error(OSError("compiler scratch is on a full disk")) == (
            "compiler scratch is on a full disk"
        )

    def test_a_filename_bearing_error_without_a_code_stays_bounded(self, tmp_path):
        error = OSError()
        error.filename = str(tmp_path / "secret-place")

        rendered = describe_os_error(error)

        assert "secret-place" not in rendered
        assert rendered == "OSError"

    def test_an_error_without_a_message_still_names_its_kind(self):
        assert describe_os_error(OSError()) == "OSError"


class TestPosixModeIsNotAWindowsGuarantee:
    """Windows access is ACL-based. The mode is advisory there, and no ACL is
    invented, so the limitation is reported rather than hidden."""

    def test_the_flag_matches_the_platform(self):
        assert POSIX_MODE_ENFORCED is (os.name != "nt")
