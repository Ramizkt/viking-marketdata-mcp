from app.config import Settings
from app.export_store import ExportStore


def test_signed_export_round_trip(tmp_path):
    settings = Settings(
        export_dir=tmp_path,
        public_base_url="https://example.test",
        export_signing_key="test-signing-key",
    )
    store = ExportStore(settings)
    exported = store.save_csv(
        rows=[{"timestamp": 1, "buy": 10}],
        fields=["buy"],
        robot_id="1",
        portfolio="A",
    )
    query = exported.download_url.split("?", 1)[1]
    params = dict(part.split("=", 1) for part in query.split("&"))
    resolved = store.resolve_download(
        exported.filename,
        int(params["expires"]),
        params["sig"],
    )
    assert resolved == exported.path


def test_expired_link_is_rejected(tmp_path):
    settings = Settings(export_dir=tmp_path, export_signing_key="test-signing-key")
    store = ExportStore(settings)
    assert store.resolve_download("bad.csv", 0, "bad") is None
