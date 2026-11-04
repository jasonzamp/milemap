import pytest
from botocore.exceptions import ClientError

from milemap.storage.r2 import upload_dir


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict]] = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch, Metadata):  # noqa: N803
        assert IfNoneMatch == "*"
        if Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = (Body.read(), Metadata)

    def head_object(self, Bucket, Key):  # noqa: N803
        return {"Metadata": self.objects[Key][1]}


@pytest.fixture
def outbox(tmp_path):
    root = tmp_path / "outbox"
    (root / "source=x/endpoint=y").mkdir(parents=True)
    (root / "source=x/endpoint=y/a.parquet").write_bytes(b"data")
    (root / "_manifests/source=x").mkdir(parents=True)
    (root / "_manifests/source=x/a.json").write_text("{}")
    return root


def test_uploads_data_before_manifests_and_moves_to_mirror(outbox, tmp_path):
    s3 = FakeS3()
    res = upload_dir(outbox, client=s3, bucket="b", move_to=tmp_path / "raw")
    assert res.uploaded == 2 and not res.conflicts
    assert list(s3.objects) == ["source=x/endpoint=y/a.parquet", "_manifests/source=x/a.json"]
    assert (tmp_path / "raw/source=x/endpoint=y/a.parquet").read_bytes() == b"data"
    assert not any(p.is_file() for p in outbox.rglob("*"))


def test_identical_existing_object_counts_as_present(outbox, tmp_path):
    s3 = FakeS3()
    upload_dir(outbox, client=s3, bucket="b", move_to=None)
    res = upload_dir(outbox, client=s3, bucket="b", move_to=None)
    assert res.already_present == 2 and res.uploaded == 0


def test_different_existing_object_is_never_overwritten(outbox):
    s3 = FakeS3()
    s3.objects["source=x/endpoint=y/a.parquet"] = (b"OTHER", {"sha256": "nope"})
    res = upload_dir(outbox, client=s3, bucket="b", move_to=None)
    assert res.conflicts == ["source=x/endpoint=y/a.parquet"]
    assert s3.objects["source=x/endpoint=y/a.parquet"][0] == b"OTHER"
    assert (outbox / "source=x/endpoint=y/a.parquet").exists()  # kept in outbox
