import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ninova_mcp.client import NinovaError
from ninova_mcp.remote_files import ManagedFiles, RemoteNinovaMcpApp, MIB
from ninova_mcp.server import NinovaMcpApp


# These contract tests cover every tool, so they serve the full tool profile.
os.environ.setdefault("ITU_MCP_TOOL_PROFILE", "full")


class RemoteFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "data"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {
            "NINOVA_REMOTE_FILE_ROOT": str(self.root),
            "NINOVA_REMOTE_REQUIRED_MOUNT": "", "NINOVA_STATE_DIR": str(Path(self.tmp.name) / "state"),
            "NINOVA_USERNAME": "dummy", "NINOVA_PASSWORD": "dummy",
            "NINOVA_REMOTE_MIN_FREE_MIB": "1",
            "NINOVA_ALLOW_UPLOADS": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = RemoteNinovaMcpApp()

    def response(self, data=b"test", name="test.txt", content_type="text/plain"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([data])
        response.url = "https://ninova.itu.edu.tr/Sinif/1.2/SinifDosyalari?g1"
        response.headers = {"Content-Type": content_type, "Content-Disposition": f'attachment; filename="{name}"'}
        self.app._client = MagicMock()
        self.app._client.base_url = "https://ninova.itu.edu.tr"
        self.app._client.get.return_value = response
        return response

    def test_download_streams_to_managed_disk_with_integrity(self):
        response = self.response()
        result = self.app.download_resource(response.url)
        self.assertEqual(Path(result["path"]).read_bytes(), b"test")
        self.assertTrue(Path(result["path"]).is_relative_to(self.root / "downloads"))
        self.assertEqual(result["sha256"], hashlib.sha256(b"test").hexdigest())
        response.__exit__.assert_called_once()

    def test_repeated_name_does_not_overwrite(self):
        a = self.app.files.save([b"a"], "same.txt")
        b = self.app.files.save([b"b"], "same.txt")
        self.assertNotEqual(a["path"], b["path"])
        self.assertEqual(Path(a["path"]).read_bytes(), b"a")

    def test_download_output_dir_forms_share_the_downloads_root(self):
        for supplied, suffix in (
            (None, ""), ("", ""), (".", ""),
            ("tool-smoke-test", "tool-smoke-test"),
            ("course/notes", "course/notes"),
            ("downloads/course/notes", "course/notes"),
            (str(self.root / "downloads" / "course" / "notes"), "course/notes"),
        ):
            with self.subTest(output_dir=supplied):
                response = self.response()
                result = self.app.download_resource(response.url, output_dir=supplied)
                self.assertEqual(Path(result["path"]).parent, self.root / "downloads" / suffix)
                self.assertEqual(Path(result["path"]).read_bytes(), b"test")

    def test_download_output_dir_rejects_other_areas_and_traversal_before_network(self):
        for supplied in ("../uploads", "course/../../uploads", "downloads/../uploads",
                         "uploads", "uploads/course", "snapshots/course",
                         str(self.root / "uploads" / "course")):
            with self.subTest(output_dir=supplied):
                response = self.response()
                with self.assertRaises(NinovaError):
                    self.app.download_resource(response.url, output_dir=supplied)
                self.app._client.get.assert_not_called()

    def test_download_output_dir_rejects_a_file_before_network(self):
        destination = self.root / "downloads" / "not-a-directory"
        destination.write_bytes(b"keep")
        response = self.response()
        with self.assertRaises(NinovaError):
            self.app.download_resource(response.url, output_dir=str(destination))
        self.app._client.get.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"keep")

    def test_download_output_dir_rejects_relative_symlink_before_network(self):
        try:
            (self.root / "downloads" / "link").symlink_to(self.root / "uploads", target_is_directory=True)
        except OSError:
            self.skipTest("Host lacks symlink privilege")
        response = self.response()
        with self.assertRaises(NinovaError):
            self.app.download_resource(response.url, output_dir="link/subfolder")
        self.app._client.get.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX ownership/mode test")
    def test_private_service_umask_still_allows_storage_group_retrieval(self):
        import stat
        previous = os.umask(0o077)
        try:
            result = self.app.files.save([b"shared with owner"], "notes.txt",
                directory=str(self.root / "downloads" / "course" / "notes"))
        finally:
            os.umask(previous)
        saved = Path(result["path"])
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o770)
        self.assertEqual(stat.S_IMODE(saved.parent.parent.stat().st_mode), 0o770)
        self.assertEqual(saved.stat().st_gid, self.root.stat().st_gid)

    def test_nested_download_never_requests_setuid_or_setgid(self):
        original_mkdir, original_chmod = Path.mkdir, Path.chmod

        def guarded_mkdir(path, mode=0o777, parents=False, exist_ok=False):
            if mode & 0o6000:
                raise PermissionError("RestrictSUIDSGID: special bits prohibited")
            return original_mkdir(path, mode, parents, exist_ok)

        def guarded_chmod(path, mode, **kwargs):
            if mode & 0o6000:
                raise PermissionError("RestrictSUIDSGID: special bits prohibited")
            return original_chmod(path, mode, **kwargs)

        with patch.object(Path, "mkdir", autospec=True, side_effect=guarded_mkdir), \
             patch.object(Path, "chmod", autospec=True, side_effect=guarded_chmod):
            saved = self.app.files.save([b"sandbox compatible"], "test.txt", directory="course/notes")
        self.assertEqual(Path(saved["path"]).read_bytes(), b"sandbox compatible")

    @unittest.skipUnless(os.name == "posix", "POSIX group ownership test")
    def test_nested_download_uses_managed_group_without_setgid_inheritance(self):
        groups = [gid for gid in os.getgroups() if gid != os.getgid()]
        desired_group = groups[0] if groups else os.getgid()
        os.chown(self.root, -1, desired_group)
        saved = Path(self.app.files.save([b"owner retrievable"], "test.txt", directory="group/notes")["path"])
        for path in (saved, saved.parent, saved.parent.parent):
            self.assertEqual(path.stat().st_gid, desired_group)

    def test_output_escape_rejected_before_network(self):
        self.response()
        with self.assertRaises(NinovaError):
            self.app.download_resource("https://ninova.itu.edu.tr/", output_dir=self.tmp.name)
        self.app._client.get.assert_not_called()

    def test_traversal_rejected(self):
        for path in ("downloads/../../secret", "downloads/../uploads/test"):
            with self.subTest(path=path), self.assertRaises(NinovaError):
                self.app.files.checked_path(path, ("downloads",))

    def test_symlink_rejected(self):
        link = self.root / "downloads" / "link"
        try:
            link.symlink_to(self.root / "uploads", target_is_directory=True)
        except OSError:
            self.skipTest("Host lacks symlink privilege")
        with self.assertRaises(NinovaError):
            self.app.files.checked_path(str(link / "secret"), ("downloads", "uploads"))

    def test_missing_disk_does_not_create_root_on_underlay(self):
        missing = Path(self.tmp.name) / "missing" / "data"
        with self.assertRaises(NinovaError):
            ManagedFiles(missing)
        self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "posix", "Linux mountinfo test")
    def test_plain_directory_is_not_a_data_mount(self):
        with self.assertRaises(NinovaError):
            ManagedFiles(self.root, Path(self.tmp.name))

    def test_oversize_cleans_partial_file(self):
        self.app.files.max_bytes = 3
        with self.assertRaises(NinovaError):
            self.app.files.save([b"123", b"4"], "large.txt")
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_full_disk_cleans_partial_file(self):
        with patch("ninova_mcp.remote_files.shutil.disk_usage", return_value=MagicMock(free=1)):
            with self.assertRaises(NinovaError):
                self.app.files.save([b"a"], "full.txt")
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_network_error_cleans_partial_file(self):
        def chunks():
            yield b"partial"
            raise ConnectionError("network")
        with self.assertRaises(ConnectionError):
            self.app.files.save(chunks(), "broken.txt")
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_text_read_uses_real_credential_free_worker(self):
        saved = self.app.files.save([b"lecture notes"], "notes.txt")
        result = self.app.read_resource_text(path=saved["path"])
        self.assertEqual(result["text"], "lecture notes")
        self.assertTrue(result["ok"])

    def test_worker_does_not_receive_credentials(self):
        saved = self.app.files.save([b"test"], "notes.txt")
        completed = MagicMock(returncode=0, stdout=b'{"ok":true,"text":"test"}')
        with patch("ninova_mcp.remote_files.subprocess.run", return_value=completed) as run:
            self.app.read_resource_text(path=saved["path"])
        self.assertNotIn("NINOVA_PASSWORD", run.call_args.kwargs["env"])
        self.assertNotIn("NINOVA_REMOTE_API_KEY", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_pdf_signature_mismatch_rejected(self):
        saved = self.app.files.save([b"not a PDF"], "fake.pdf")
        with self.assertRaises(NinovaError):
            self.app.read_resource_text(path=saved["path"])

    def test_text_read_arbitrary_host_file_forbidden(self):
        secret = Path(self.tmp.name) / "state" / "secret.txt"
        secret.write_text("secret")
        with self.assertRaises(NinovaError):
            self.app.read_resource_text(path=str(secret))

    def test_submission_only_accepts_staged_uploads_and_preserves_confirmation(self):
        upload = self.root / "uploads" / "homework.pdf"
        upload.write_bytes(b"%PDF-test")
        with patch.object(NinovaMcpApp, "submit_assignment", return_value={"dry_run": True}) as submit:
            result = self.app.submit_assignment(str(upload), course="test")
        self.assertTrue(result["dry_run"])
        self.assertFalse(submit.call_args.kwargs["confirm"])
        self.assertFalse(submit.call_args.kwargs["allow_replace"])
        with patch.object(NinovaMcpApp, "submit_assignment") as submit:
            with self.assertRaises(NinovaError):
                self.app.submit_assignment(str(Path(self.tmp.name) / "secret"), confirm=True)
            submit.assert_not_called()

    def test_download_is_not_submission_staging_even_for_a_dry_run(self):
        download = self.app.files.save([b"%PDF-test"], "notes.pdf")
        for confirm in (False, True):
            with self.subTest(confirm=confirm), patch.object(NinovaMcpApp, "submit_assignment") as submit:
                with self.assertRaisesRegex(NinovaError, "deliberately staged.*uploads"):
                    self.app.submit_assignment(download["path"], confirm=confirm)
                submit.assert_not_called()

    def test_staged_upload_real_preview_logic_never_posts(self):
        upload = self.root / "uploads" / "homework.pdf"
        upload.write_bytes(b"%PDF-test")
        self.response()
        url = "https://ninova.itu.edu.tr/Sinif/32945.119022/Odev/245598/OdevGonder"
        html = (Path(__file__).parent / "fixtures" / "odev_gonder.html").read_text(encoding="utf-8")
        self.app._client.get_html.return_value = (html, MagicMock(url=url))
        with patch.object(self.app, "_resolve_assignment_upload_url", return_value={"upload_url": url}):
            for supplied in (str(upload), "uploads/homework.pdf"):
                result = self.app.submit_assignment(supplied, upload_url=url, slot_index=5, confirm=False)
                self.assertTrue(result["dry_run"])
                self.assertEqual(result["file"]["path"], str(upload))
                self.assertEqual(result["slot"]["index"], 5)
        self.app._client.post_multipart.assert_not_called()

    def test_snapshot_host_path_rejected_before_network(self):
        self.response()
        with self.assertRaises(NinovaError):
            self.app.diff_snapshot("https://ninova.itu.edu.tr/", snapshot_path=self.tmp.name)
        self.app._client.get.assert_not_called()

    def test_snapshot_round_trip_selects_newest_by_time_not_random_filename(self):
        from ninova_mcp.parsing import parse_html_page
        url = "https://ninova.itu.edu.tr/Sinif/1.2"
        self.response()
        first = parse_html_page(url, "<html><title>First</title>old</html>", self.app.client.base_url)
        second = parse_html_page(url, "<html><title>Second</title>new</html>", self.app.client.base_url)
        with patch.object(self.app, "_page", return_value=first):
            old = self.app.snapshot_page(url, label="test")
        with patch.object(self.app, "_page", return_value=second):
            new = self.app.snapshot_page(url, label="test")
        os.utime(old["snapshot_path"], (100, 100))
        os.utime(new["snapshot_path"], (200, 200))
        with patch.object(self.app, "_page", return_value=second):
            result = self.app.diff_snapshot(url, label="test")
        self.assertEqual(result["snapshot_path"], new["snapshot_path"])

    def test_full_remote_registry_retains_safe_annotations_and_schemas(self):
        import asyncio
        from ninova_mcp.remote import _build_fastmcp
        with patch.dict(os.environ, {
            "NINOVA_REMOTE_ENABLE_FILES": "1", "NINOVA_REMOTE_ENABLE_WRITES": "1",
            "NINOVA_REMOTE_ENABLE_MAIL": "1", "NINOVA_REMOTE_ALLOWED_TOOLS": "",
            "NINOVA_OBS_REGISTRATION_WRITES": "1", "NINOVA_COURSEHUB_WRITES": "1",
        }):
            server = _build_fastmcp(self.app, "/mcp")
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        from ninova_mcp.server import LOCAL_TOOL_NAMES
        self.assertEqual(set(tools), set(LOCAL_TOOL_NAMES))
        for name in ("submit_assignment",):
            self.assertIs(tools[name].annotations.readOnlyHint, False)
            self.assertIs(tools[name].inputSchema["properties"]["confirm"]["default"], False)
        self.assertIn("managed HDD", tools["download_resource"].description)

    def test_bounded_url_read_closes_response_on_error(self):
        response = self.response(b"x" * (25 * MIB + 1))
        with self.assertRaises(NinovaError):
            self.app.read_resource_text(url=response.url)
        response.__exit__.assert_called_once()

    def test_actual_pdf_worker(self):
        import io
        from pypdf import PdfWriter
        buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(buffer)
        saved = self.app.files.save([buffer.getvalue()], "blank.pdf")
        self.assertTrue(self.app.read_resource_text(path=saved["path"])["ok"])

    def test_docx_worker_rejects_expansion_bomb(self):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"x" * (33 * MIB))
        saved = self.app.files.save([buffer.getvalue()], "bomb.docx")
        with self.assertRaises(NinovaError):
            self.app.read_resource_text(path=saved["path"])

    def test_transcript_is_validated_and_saved_on_managed_disk(self):
        self.app._obs = MagicMock()
        self.app._obs.get_transcript_preview.return_value = {"belgeAsByteArray": base64.b64encode(b"%PDF-test").decode()}
        result = self.app.obs_download_transcript()
        self.assertTrue(Path(result["path"]).is_relative_to(self.root / "downloads"))
        self.app._obs.get_transcript_preview.return_value = {"belgeAsByteArray": base64.b64encode(b"login page").decode()}
        with self.assertRaises(NinovaError):
            self.app.obs_download_transcript()

    def test_transcript_accepts_the_same_relative_download_subfolders(self):
        self.app._obs = MagicMock()
        self.app._obs.get_transcript_preview.return_value = {"belgeAsByteArray": base64.b64encode(b"%PDF-test").decode()}
        for supplied in ("transcripts", "downloads/transcripts", str(self.root / "downloads" / "transcripts")):
            with self.subTest(output_dir=supplied):
                result = self.app.obs_download_transcript(output_dir=supplied)
                self.assertEqual(Path(result["path"]).parent, self.root / "downloads" / "transcripts")
        self.app._obs.reset_mock()
        with self.assertRaises(NinovaError):
            self.app.obs_download_transcript(output_dir="../uploads")
        self.app._obs.get_transcript_preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
