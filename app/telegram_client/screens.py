"""Rich Message screen construction; never use Markdown parse mode."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import (InputRichBlockDetails, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichMessage, InputRichBlockPhoto,
    InputRichBlockDocument, InputMediaPhoto, InputMediaDocument, BufferedInputFile,
    RichTextCode, MessageEntity)


@dataclass(frozen=True)
class Screen:
    title: str
    lines: tuple[str, ...] = ()
    details_title: str | None = None
    details_lines: tuple[str, ...] = ()
    uri: str | None = None
    qr: bytes | None = None
    qr_title: str | None = None
    files: tuple[tuple[str, bytes], ...] = ()

    def rich(self) -> InputRichMessage:
        blocks = [InputRichBlockSectionHeading(text=self.title, size=2)]
        blocks.extend(InputRichBlockParagraph(text=line) for line in self.lines if line)
        if self.qr and self.qr_title:
            blocks.append(InputRichBlockDetails(summary=self.qr_title, is_open=False,
                blocks=[InputRichBlockPhoto(photo=InputMediaPhoto(
                    media=BufferedInputFile(self.qr, 'config.png')))]))
        if self.uri:
            blocks.append(InputRichBlockParagraph(text=RichTextCode(text=self.uri)))
        blocks.extend(InputRichBlockDocument(document=InputMediaDocument(
            media=BufferedInputFile(content, filename))) for filename, content in self.files)
        if self.details_title and self.details_lines:
            blocks.append(InputRichBlockDetails(summary=self.details_title,
                blocks=[InputRichBlockParagraph(text=line) for line in self.details_lines]))
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)

    def plain(self) -> str:
        lines = [self.title, *self.lines]
        if self.uri:
            lines.append(self.uri)
        if self.details_title and self.details_lines:
            lines.extend((self.details_title, *self.details_lines))
        return '\n\n'.join(line for line in lines if line)

    def plain_entities(self) -> list[MessageEntity] | None:
        if not self.uri:
            return None
        text = self.plain()
        offset = len(text[:text.index(self.uri)].encode('utf-16-le')) // 2
        return [MessageEntity(type='code', offset=offset,
            length=len(self.uri.encode('utf-16-le')) // 2)]
