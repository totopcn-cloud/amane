"""测试 NFO 附属文件生成"""

from typing import TYPE_CHECKING

import pytest

from amane.db.models import Metadata
from amane.media import update_nfo_classification, write_nfo
from amane.parsing import parse_file_info

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def metadata() -> Metadata:
    return Metadata(
        number="MIDV-123",
        title="Test Title",
        actors=["Actor A", "Actor B"],
        studio="Studio X",
        release="2026-01-15",
        runtime=120,
        tags=["Drama", "Romance"],
        series="Series Y",
        scores={"javdb": 85.0},
        directors=["Director Z"],
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_has_required_fields(tmp_path: Path, metadata: Metadata):
    nfo_path = tmp_path / "MIDV-123.nfo"
    ok = await write_nfo(metadata, nfo_path)
    assert ok is True
    assert nfo_path.exists()
    content = nfo_path.read_text(encoding="utf-8")
    assert "<title>MIDV-123 Test Title</title>" in content
    assert "<num>MIDV-123</num>" in content
    assert "<actor>" in content
    assert "<name>Actor A</name>" in content
    assert "<studio>Studio X</studio>" in content
    assert "<genre>Drama</genre>" in content
    assert "<director>Director Z</director>" in content
    assert "<rating>85.0</rating>" in content
    # 系列使用规范嵌套: <set><name>…</name></set>
    assert "<series>Series Y</series>" in content
    assert "<set>" in content
    assert "<name>Series Y</name>" in content


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_adds_uncensored_classification(tmp_path: Path, metadata: Metadata):
    nfo_path = tmp_path / "HEYZO-1234.nfo"

    ok = await write_nfo(metadata, nfo_path, file_info=parse_file_info("HEYZO-1234.mp4"))

    assert ok is True
    content = nfo_path.read_text(encoding="utf-8")
    assert "<tag>无码</tag>" in content
    assert "<genre>无码专区</genre>" in content


@pytest.mark.asyncio(loop_scope="function")
async def test_rebuild_classification_only_adds_missing_tags(tmp_path: Path):
    nfo_path = tmp_path / "SIRO-1234.nfo"
    nfo_path.write_text("<movie>\n  <title>keep me</title>\n</movie>\n", encoding="utf-8")

    ok = await update_nfo_classification(nfo_path, uncensored=True, amateur=True)
    assert ok is True
    assert await update_nfo_classification(nfo_path, uncensored=True, amateur=True) is True

    content = nfo_path.read_text(encoding="utf-8")
    assert "<title>keep me</title>" in content
    assert content.count("<tag>无码</tag>") == 1
    assert content.count("<tag>素人</tag>") == 1
    assert content.count("<genre>无码专区</genre>") == 1
