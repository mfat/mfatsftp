"""Keep the documentation honest: every public name has a docstring and an
entry in docs/api.md, and the Python snippets in the docs at least compile."""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

import mfatsftp
from mfatsftp import protocol

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_DOC = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")

# Public members that are aliases of a documented member.
_ALIASES = {"SFTPClient.unlink"}


def _public_members():
    for cls in (
        mfatsftp.SFTPClient,
        mfatsftp.SFTPFile,
        mfatsftp.PipelinedWriter,
        mfatsftp.SFTPAttributes,
    ):
        for name, value in vars(cls).items():
            if name.startswith("_"):
                continue
            if callable(value) or isinstance(value, (property, classmethod, staticmethod)):
                yield f"{cls.__name__}.{name}", getattr(cls, name)


@pytest.mark.parametrize("qualname,member", list(_public_members()))
def test_public_member_has_docstring(qualname, member):
    if qualname in _ALIASES:
        pytest.skip("alias")
    assert inspect.getdoc(member), f"{qualname} has no docstring"


@pytest.mark.parametrize("qualname,member", list(_public_members()))
def test_public_member_is_in_api_reference(qualname, member):
    name = qualname.split(".")[1]
    assert re.search(rf"[`.]{re.escape(name)}\b", API_DOC), f"{qualname} missing from docs/api.md"


@pytest.mark.parametrize("name", [n for n in mfatsftp.__all__ if not n.startswith("__")])
def test_exported_name_is_documented(name):
    assert re.search(rf"\b{re.escape(name)}\b", API_DOC), f"{name} missing from docs/api.md"
    obj = getattr(mfatsftp, name)
    if inspect.isclass(obj) or inspect.isfunction(obj):
        assert inspect.getdoc(obj), f"{name} has no docstring"


def test_protocol_exceptions_are_exported():
    for name in dir(protocol):
        obj = getattr(protocol, name)
        if inspect.isclass(obj) and issubclass(obj, protocol.SFTPError):
            assert getattr(mfatsftp, name, None) is obj, f"{name} not exported"


def _python_snippets():
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        for index, match in enumerate(re.finditer(r"```python\n(.*?)```", text, re.S)):
            yield pytest.param(match.group(1), id=f"{path.name}-{index}")


@pytest.mark.parametrize("source", list(_python_snippets()))
def test_doc_snippets_compile(source):
    compile(source, "<doc snippet>", "exec")


def test_version_matches_changelog():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{mfatsftp.__version__}]" in changelog


def test_readme_links_work_on_pypi():
    # PyPI shows the README at pypi.org/project/mfatsftp/, where a relative
    # link such as docs/api.md resolves to a page that does not exist. Only
    # absolute URLs and in-page #anchors work there.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)", readme)
    relative = [t for t in targets if not t.startswith(("http://", "https://", "#"))]
    assert not relative, f"relative links break on PyPI: {relative}"
