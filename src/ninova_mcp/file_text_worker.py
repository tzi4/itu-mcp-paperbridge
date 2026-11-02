"""Bounded, credential-free subprocess for parsing untrusted remote documents."""
from __future__ import annotations
import io
import json
import sys
import zipfile


def main() -> None:
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    from .text_extract import extract_text_from_bytes
    extension, chars = sys.argv[1], int(sys.argv[2])
    data = sys.stdin.buffer.read(25 * 1024**2 + 1)
    if len(data) > 25 * 1024**2:
        raise ValueError("File too large")
    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("PDF signature mismatch")
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("Encrypted PDF or page limit exceeded")
    if extension in {".docx", ".pptx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items = archive.infolist()
            if len(items) > 2000 or sum(i.file_size for i in items) > 32 * 1024**2:
                raise ValueError("Office expansion limit exceeded")
            required = "word/document.xml" if extension == ".docx" else "ppt/presentation.xml"
            if required not in archive.namelist():
                raise ValueError("Office document signature mismatch")
    result = extract_text_from_bytes(data, extension=extension, max_chars=chars)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())


if __name__ == "__main__":
    main()
