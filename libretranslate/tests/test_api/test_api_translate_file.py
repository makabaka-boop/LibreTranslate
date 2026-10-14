import io
import json
import os
import re
from urllib.parse import unquote, urlsplit

import pytest

from libretranslate.app import get_upload_dir

UNSET = object()

UUID_BASENAME_RE = re.compile(r"^[0-9a-f-]{36}\.test_es\.txt$")
FILENAME_STAR_RE = re.compile(r"filename\*=UTF-8''([^;]+)")
FILENAME_RE = re.compile(r'filename="?([^";]+)"?')


def translate_file(client, download_name=UNSET, filename="test.txt",
                   content=b"Hello world!\n"):
    data = {
        "source": "en",
        "target": "es",
        "file": (io.BytesIO(content), filename),
    }
    if download_name is not UNSET:
        data["download_name"] = download_name

    return client.post(
        "/translate_file",
        data=data,
        content_type="multipart/form-data",
    )


def translate_and_download(client, download_name=UNSET, content=b"Hello world!\n"):
    response = translate_file(client, download_name=download_name, content=content)
    assert response.status_code == 200, response.data

    response_json = json.loads(response.data)
    assert set(response_json.keys()) == {"translatedFileUrl"}

    url = response_json["translatedFileUrl"]
    download_response = client.get(urlsplit(url).path)
    assert download_response.status_code == 200

    return response_json, download_response


def get_attachment_filename(response):
    # RFC 5987 filename* takes precedence over the ASCII filename fallback
    disposition = response.headers.get("Content-Disposition", "")
    star = FILENAME_STAR_RE.search(disposition)
    if star is not None:
        return unquote(star.group(1))

    match = FILENAME_RE.search(disposition)
    assert match is not None, disposition
    return match.group(1)


def test_translate_file_default_download_name(client):
    _, download = translate_and_download(client)

    disposition = download.headers["Content-Disposition"]
    assert "test_es.txt" in disposition


def test_translate_file_custom_ascii_download_name(client):
    _, download = translate_and_download(client, download_name="greeting.txt")

    assert 'filename=greeting.txt' in download.headers["Content-Disposition"]


def test_translate_file_unicode_download_name_with_spaces(client):
    name = "我的 报告.txt"
    _, download = translate_and_download(client, download_name=name)

    assert get_attachment_filename(download) == name


def test_translate_file_download_name_with_spaces(client):
    _, download = translate_and_download(client, download_name="my report.txt")

    assert get_attachment_filename(download) == "my report.txt"


def test_translate_file_download_name_extension_case_insensitive(client):
    _, download = translate_and_download(client, download_name="greeting.TXT")

    assert get_attachment_filename(download) == "greeting.TXT"


def test_translate_file_download_name_does_not_change_storage_or_content(client):
    content = b"Hello world!\n"

    default_response_json, default_download = translate_and_download(client, content=content)
    custom_response_json, custom_download = translate_and_download(
        client, download_name="我的 报告.txt", content=content)

    default_path = urlsplit(default_response_json["translatedFileUrl"]).path
    custom_path = urlsplit(custom_response_json["translatedFileUrl"]).path

    default_basename = os.path.basename(default_path)
    custom_basename = os.path.basename(custom_path)

    # Storage keeps the UUID prefixed naming and the original extension
    assert UUID_BASENAME_RE.match(default_basename)
    assert UUID_BASENAME_RE.match(custom_basename)
    assert custom_basename.isascii()
    assert "报告" not in custom_basename

    # Only the attachment name differs: content is identical
    assert default_download.data == custom_download.data

    # The custom name is stored in a sidecar, never as the file name;
    # requests without download_name leave no sidecar behind
    upload_dir = get_upload_dir()
    assert os.path.isfile(os.path.join(upload_dir, default_basename))
    assert not os.path.exists(os.path.join(upload_dir, default_basename + ".download_name"))
    assert os.path.isfile(os.path.join(upload_dir, custom_basename))
    assert os.path.isfile(os.path.join(upload_dir, custom_basename + ".download_name"))


def test_translate_file_same_download_name_does_not_overwrite(client):
    first_response = translate_file(client, download_name="same.txt")
    second_response = translate_file(client, download_name="same.txt")

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    first_url = json.loads(first_response.data)["translatedFileUrl"]
    second_url = json.loads(second_response.data)["translatedFileUrl"]
    assert first_url != second_url

    first_path = urlsplit(first_url).path
    second_path = urlsplit(second_url).path

    # Both translated files are available at the same time
    upload_dir = get_upload_dir()
    first_basename = os.path.basename(first_path)
    second_basename = os.path.basename(second_path)
    assert first_basename != second_basename
    assert os.path.isfile(os.path.join(upload_dir, first_basename))
    assert os.path.isfile(os.path.join(upload_dir, second_basename))

    first_download = client.get(first_path)
    second_download = client.get(second_path)
    assert first_download.status_code == 200
    assert second_download.status_code == 200
    assert 'filename=same.txt' in first_download.headers["Content-Disposition"]
    assert 'filename=same.txt' in second_download.headers["Content-Disposition"]


def test_download_file_metadata_sidecar_not_served(client):
    response_json, _ = translate_and_download(client, download_name="greeting.txt")
    path = urlsplit(response_json["translatedFileUrl"]).path

    metadata_response = client.get(path + ".download_name")
    assert metadata_response.status_code == 400


@pytest.mark.parametrize("download_name", [
    "",                          # empty
    "   ",                       # whitespace only
    "\t",                        # tab only
    "../greeting.txt",           # path traversal
    "sub/greeting.txt",          # unix path
    "sub\\greeting.txt",         # windows path
    "..",                        # parent reference
    "greeting\t.txt",            # tab
    "greeting\n.txt",            # newline (header injection)
    "greeting\r.txt",            # carriage return
    "greeting\x00.txt",          # NUL
    "greeting\x7f.txt",          # DEL
    "greeting",                  # missing extension
    ".txt",                      # missing name stem
    "greeting.pdf",              # wrong extension
    "greeting.docx",             # another wrong extension
])
def test_translate_file_invalid_download_name(client, download_name):
    upload_dir = get_upload_dir()
    before = set(os.listdir(upload_dir))

    response = translate_file(client, download_name=download_name)

    assert response.status_code == 400
    response_json = json.loads(response.data)
    assert "error" in response_json
    assert response_json["error"]

    # Validation happens before any file is written to disk
    after = set(os.listdir(upload_dir))
    assert after == before
