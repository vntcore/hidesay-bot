"""Files the bot sends: what `~hidesay_bot.Bot.send_media` takes.

An `~hidesay_bot.InputFile` is the bytes of a file and what the phones need to lay it out
before they download it: its kind, its name and type, the size of a picture, the length of a
clip. `~hidesay_bot.Bot.send_media` uploads it and describes it, and in an end-to-end encrypted
chat seals it first, so the server never sees what is inside. Paths and raw bytes work as well;
they become an ``InputFile`` with what can be told from the name.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _validate as v
from .errors import ValidationError
from .models import MediaIn
from .wire import envelope

__all__ = ["InputFile"]


@dataclass(frozen=True, slots=True, kw_only=True)
class InputFile:
    """A file to send, with what the phones need to show it before they download it.

    Only ``data`` is required. The kind and the MIME type are worked out from the name when
    they are left out: a ``.jpg`` is a picture, a ``.mp4`` a clip, anything else a file. A
    voice note is never guessed: say ``kind="audio"``. Width, height and length are not read
    from the bytes; give them when you know them, and the phones draw the right box at once.

    Attributes:
        data: The file's bytes.
        name: The file name people see, or ``None``.
        mime: The MIME type, or ``None`` to guess it from ``name``.
        kind: ``"image"``, ``"video"``, ``"audio"`` (a voice note) or ``"file"``, or ``None``
            to take it from the MIME type.
        width: The width of a picture or a clip, in pixels, or ``None``.
        height: The height, in pixels, or ``None``.
        duration_ms: The length of a clip or a voice note, in milliseconds, or ``None``.
        thumb: A tiny JPEG, base64, up to 12000 characters, shown blurred until the file is
            fetched, or ``None``.

    Example:
        >>> photo = InputFile(data=b"...", name="sunset.jpg", width=1280, height=720)
        >>> photo.content_kind, photo.content_type
        ('image', 'image/jpeg')
    """

    data: bytes = field(repr=False)
    name: str | None = None
    mime: str | None = None
    kind: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    thumb: str | None = None

    def __repr__(self) -> str:
        return f"InputFile(name={self.name!r}, kind={self.content_kind!r}, {len(self.data)} bytes)"

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        mime: str | None = None,
        kind: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        thumb: str | None = None,
    ) -> InputFile:
        """Reads a file from disk.

        Args:
            path: The file.
            name: The name people see. ``None`` takes the file's own name.
            mime: The MIME type. ``None`` guesses it from the name.
            kind: The kind. ``None`` takes it from the MIME type.
            width: The width of a picture or a clip, in pixels.
            height: The height, in pixels.
            duration_ms: The length of a clip or a voice note, in milliseconds.
            thumb: A tiny JPEG, base64.

        Returns:
            The file, read whole into memory.

        Raises:
            ValidationError: The file can't be read.
        """
        source = Path(path)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ValidationError(f"cannot read {source}: {exc.strerror}", field="files") from None
        return cls(
            data=data,
            name=name if name is not None else source.name,
            mime=mime,
            kind=kind,
            width=width,
            height=height,
            duration_ms=duration_ms,
            thumb=thumb,
        )

    @property
    def content_type(self) -> str:
        """The MIME type that goes with the file, from `mime`, a guess from `name`, or the kind's default."""
        if self.mime:
            return self.mime
        guessed = mimetypes.guess_type(self.name)[0] if self.name else None
        if guessed:
            return guessed
        return {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/ogg"}.get(
            self.kind or "", "application/octet-stream"
        )

    @property
    def content_kind(self) -> str:
        """The kind that goes with the file, from `kind`, or what its MIME type says."""
        if self.kind:
            return self.kind
        mime = self.content_type
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        return "file"

    @property
    def sha256(self) -> str:
        """The SHA-256 of `data` in lowercase hex, which the phones check the file against."""
        return hashlib.sha256(self.data).hexdigest()

    def check(self, *, limit: int = v.UPLOAD_MAX) -> InputFile:
        """Checks the file against the rules of the server, before anything is sent.

        Args:
            limit: The largest file accepted, in bytes.

        Returns:
            The file itself.

        Raises:
            ValidationError: The data is not bytes or is too large, or a field breaks a rule.
        """
        v.check_upload(self.data, limit=limit)
        v.check_media_kind(self.content_kind)
        v.check_mime(self.content_type)
        if self.name is not None:
            v.check_media_name(self.name)
        sizes: tuple[tuple[object, str, int], ...] = (
            (self.width, "width", v.MEDIA_SIDE_MAX),
            (self.height, "height", v.MEDIA_SIDE_MAX),
            (self.duration_ms, "duration_ms", v.MEDIA_DURATION_MAX),
        )
        for value, label, top in sizes:
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= top):
                raise ValidationError(f"{label} must be a positive integer up to {top}", field=label)
        if self.thumb is not None:
            v.check_thumb(self.thumb)
        return self

    def media_in(self, media_id: str) -> MediaIn:
        """Describes the file for a readable chat, once it is uploaded.

        Args:
            media_id: The ID the upload gave.

        Returns:
            The description, for the ``media`` of a send.
        """
        return MediaIn(
            id=media_id,
            kind=self.content_kind,
            mime=self.content_type,
            name=self.name,
            size=len(self.data) or None,
            width=self.width,
            height=self.height,
            duration_ms=self.duration_ms,
            thumb=self.thumb,
            sha256=self.sha256,
        )

    def envelope(self, media_id: str) -> dict[str, Any]:
        """Describes the file for an encrypted chat, once it is uploaded sealed.

        Args:
            media_id: The ID the upload of the sealed file gave.

        Returns:
            The envelope, in the phones' own field names.
        """
        return envelope(
            media_id,
            self.content_kind,
            mime=self.content_type,
            name=self.name,
            size=len(self.data),
            width=self.width,
            height=self.height,
            duration_ms=self.duration_ms,
            thumb=self.thumb,
            sha256=self.sha256,
        )


def input_files(files: Any, *, limit: int) -> list[InputFile]:
    """What `Bot.send_media` was given, as checked files: one, or an album of up to 10."""
    many = isinstance(files, (list, tuple))
    given = list(files) if many else [files]
    if not 1 <= len(given) <= v.MEDIA_PER_MESSAGE:
        raise ValidationError(f"a message holds 1 to {v.MEDIA_PER_MESSAGE} files", field="files")
    out: list[InputFile] = []
    for item in given:
        if isinstance(item, InputFile):
            out.append(item)
        elif isinstance(item, (bytes, bytearray, memoryview)):
            out.append(InputFile(data=bytes(item)))
        elif isinstance(item, (str, os.PathLike)):
            out.append(InputFile.from_path(item))
        else:
            raise ValidationError(f"a file is an InputFile, bytes or a path, got {type(item).__name__}", field="files")
    for item in out:
        item.check(limit=limit)
    if len(out) > 1 and any(item.content_kind not in ("image", "video") for item in out):
        raise ValidationError("an album holds pictures and videos only: send other files one by one", field="files")
    return out
