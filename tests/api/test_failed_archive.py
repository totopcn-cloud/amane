from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from amane.api.support.task_batch import archive_failed_scrape_files
from amane.db.models import MediaFileStatus
from amane.db.repository import Repository


@pytest_asyncio.fixture
async def repo():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield Repository(engine)
    await engine.dispose()


@pytest.mark.asyncio(loop_scope="function")
async def test_manual_archive_exemption_keeps_failed_media_in_place(repo: Repository, tmp_path: Path) -> None:
    root = tmp_path / "library"
    source = root / "演员" / "ABC-001.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    library = await repo.create_library(name="test", path=str(root))
    assert library.id is not None
    media = await repo.create_media_file(
        library_id=library.id,
        path=str(source),
        status=MediaFileStatus.FAILED,
        archive_exempt=True,
    )
    assert media.id is not None

    result = await archive_failed_scrape_files(repo)

    assert result.archived == 0
    assert source.is_file()
    saved = await repo.get_media_file(media.id)
    assert saved is not None
    assert saved.path == str(source)
